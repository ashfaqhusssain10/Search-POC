"""Step 2b: Embed canonical and alias items into Qdrant for variant matching.

Creates two collections:
  - searchpoc_canonicals  : DynamoDB canonical items with veg_type/form/ingredients payload
  - searchpoc_aliases     : Supabase alias items with veg_type/form payload for filtering

Both collections use text-embedding-3-small (1536-dim, cosine) and are consumed
exclusively by generate_variants.py (tiered semantic retrieval) — not by the
query-time search path (that uses item_search_communities).

Idempotent: re-running upserts over existing points using stable integer IDs.

Usage:
    python -m scripts.embed_items
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from openai import OpenAI
from qdrant_client.http.models import (
    Distance,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from core.connections import close_connections, get_qdrant_client, neo4j_session
from core.embedding_text import build_item_embedding_text
from core.settings import EMBEDDING_DIM, EMBEDDING_MODEL, OPENAI_API_KEY

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COLLECTION_CANONICALS = "searchpoc_canonicals"
COLLECTION_ALIASES = "searchpoc_aliases"

EMBED_BATCH_SIZE = 100
UPSERT_BATCH_SIZE = 50
MAX_RETRIES = 3
RETRY_DELAY = 2.0

# ---------------------------------------------------------------------------
# Neo4j fetchers
# ---------------------------------------------------------------------------

FETCH_CANONICALS = """
MATCH (i:Item {source: 'dynamodb'})
RETURN i.id AS id,
       i.name AS name,
       i.itemType AS item_type,
       i.itemCategory AS category,
       i.typecode_name AS typecode,
       i.llm_description AS llm_description,
       i.llm_description_prose AS prose
ORDER BY i.name
"""

FETCH_ALIASES = """
MATCH (i:Item {source: 'supabase'})
RETURN i.id AS id,
       i.name AS name,
       i.itemType AS item_type,
       i.category_name AS category,
       i.typecode_name AS typecode,
       i.llm_description AS llm_description,
       i.llm_description_prose AS prose
ORDER BY i.name
"""


def fetch_canonicals(session) -> list[dict[str, Any]]:
    return [dict(r) for r in session.run(FETCH_CANONICALS)]


def fetch_aliases(session) -> list[dict[str, Any]]:
    return [dict(r) for r in session.run(FETCH_ALIASES)]


# ---------------------------------------------------------------------------
# llm_description parsing
# ---------------------------------------------------------------------------

def _parse_desc(llm_description: str | None) -> dict[str, Any]:
    if not llm_description:
        return {}
    try:
        return json.loads(llm_description)
    except (json.JSONDecodeError, ValueError):
        return {}


def _veg_type(item: dict[str, Any]) -> str:
    """Prefer llm_description.veg_type; fall back to item_type field."""
    desc = _parse_desc(item.get("llm_description"))
    return (desc.get("veg_type") or item.get("item_type") or "").strip().upper()


def _form(item: dict[str, Any]) -> str:
    desc = _parse_desc(item.get("llm_description"))
    return (desc.get("form") or "").strip().lower()


def _ingredients(item: dict[str, Any]) -> list[str]:
    desc = _parse_desc(item.get("llm_description"))
    return desc.get("ingredients", [])


def _also_known_as(item: dict[str, Any]) -> list[str]:
    desc = _parse_desc(item.get("llm_description"))
    return [s for s in desc.get("also_known_as", []) if s]


def _regional_tags(item: dict[str, Any]) -> list[str]:
    desc = _parse_desc(item.get("llm_description"))
    return [s for s in desc.get("regional_tags", []) if s]


def _cooking_method(item: dict[str, Any]) -> str:
    desc = _parse_desc(item.get("llm_description"))
    return (desc.get("cooking_method(recipe)") or "").strip()


# ---------------------------------------------------------------------------
# Embedding text — single shared builder so canonicals, aliases, and query-time
# enrichment all produce text in the same format. This is what makes the
# vector space symmetric: master-list items and actual-catalog items embed
# under the same schema, so similarity scores are meaningful across the two.
# ---------------------------------------------------------------------------

def item_embedding_text(item: dict[str, Any]) -> str:
    """Build embedding text via the shared core/embedding_text helper.

    Includes the LLM-generated prose paragraph (`llm_description_prose`) when
    present — adds flavor, texture, and usage signals that the structured
    fields don't capture.
    """
    return build_item_embedding_text(
        name=item.get("name", ""),
        item_type=_veg_type(item) or item.get("item_type"),
        typecode=item.get("typecode"),
        category=item.get("category"),
        llm_description=item.get("llm_description"),
        prose=item.get("prose"),
    )


# ---------------------------------------------------------------------------
# Stable integer IDs for Qdrant
# ---------------------------------------------------------------------------

def _point_id(item_id: str, prefix: str = "") -> int:
    """
    Derive a stable non-negative integer Qdrant point ID from an item ID.

    DynamoDB IDs are UUIDs → SHA1-hashed for determinism. (Python's built-in
    hash() is randomized per process via PYTHONHASHSEED, which caused
    duplicate points on re-runs; SHA1 is deterministic across runs.)
    Supabase IDs are "sub_<int>" → extract the integer (offset to avoid
    collision with canonical IDs).
    """
    import hashlib
    clean = item_id.removeprefix("sub_")
    try:
        base = int(clean)
        return base + 10_000_000
    except ValueError:
        digest = hashlib.sha1((prefix + item_id).encode("utf-8")).hexdigest()
        return int(digest[:12], 16)  # 48 bits, fits comfortably in int64


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def embed_texts(client: OpenAI, texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts with retry logic."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
            return [item.embedding for item in response.data]
        except Exception as exc:
            log.warning("Embedding attempt %d/%d failed: %s", attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
    raise RuntimeError(f"Embedding failed after {MAX_RETRIES} attempts")


def embed_all(client: OpenAI, texts: list[str]) -> list[list[float]]:
    """Embed a large list in batches."""
    all_vectors: list[list[float]] = []
    total = len(texts)
    for i in range(0, total, EMBED_BATCH_SIZE):
        batch = texts[i : i + EMBED_BATCH_SIZE]
        vectors = embed_texts(client, batch)
        all_vectors.extend(vectors)
        log.info("  Embedded %d / %d", min(i + EMBED_BATCH_SIZE, total), total)
    return all_vectors


# ---------------------------------------------------------------------------
# Qdrant collection setup
# ---------------------------------------------------------------------------

def ensure_collection(qdrant, name: str, payload_index_fields: list[str]) -> None:
    """Create collection + keyword indexes if not already present."""
    existing = {c.name for c in qdrant.get_collections().collections}
    if name not in existing:
        qdrant.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )
        log.info("Created Qdrant collection '%s'.", name)
    else:
        log.info("Collection '%s' already exists — will upsert.", name)

    for field in payload_index_fields:
        qdrant.create_payload_index(
            collection_name=name,
            field_name=field,
            field_schema=PayloadSchemaType.KEYWORD,
        )


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------

def upsert_points(qdrant, collection: str, points: list[PointStruct]) -> None:
    for i in range(0, len(points), UPSERT_BATCH_SIZE):
        batch = points[i : i + UPSERT_BATCH_SIZE]
        qdrant.upsert(collection_name=collection, points=batch)
        log.info("  Upserted %d–%d into '%s'", i, i + len(batch) - 1, collection)


# ---------------------------------------------------------------------------
# Build points
# ---------------------------------------------------------------------------

def build_canonical_points(
    items: list[dict[str, Any]],
    vectors: list[list[float]],
) -> list[PointStruct]:
    points = []
    for item, vector in zip(items, vectors):
        points.append(
            PointStruct(
                id=_point_id(item["id"], prefix="can_"),
                vector=vector,
                payload={
                    "item_id": item["id"],
                    "name": item["name"],
                    "category": item.get("category") or "",
                    "veg_type": _veg_type(item),
                    "form": _form(item),
                    "ingredients": _ingredients(item),
                },
            )
        )
    return points


def build_alias_points(
    items: list[dict[str, Any]],
    vectors: list[list[float]],
) -> list[PointStruct]:
    points = []
    for item, vector in zip(items, vectors):
        points.append(
            PointStruct(
                id=_point_id(item["id"]),
                vector=vector,
                payload={
                    "item_id": item["id"],
                    "name": item["name"],
                    "veg_type": _veg_type(item),
                    "form": _form(item),
                },
            )
        )
    return points


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_collection(qdrant, name: str) -> None:
    info = qdrant.get_collection(name)
    log.info(
        "Collection '%s': %d vectors, dim=%d",
        name,
        info.points_count,
        info.config.params.vectors.size,
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    client = OpenAI(api_key=OPENAI_API_KEY)
    qdrant = get_qdrant_client()

    with neo4j_session() as session:
        canonicals = fetch_canonicals(session)
        aliases = fetch_aliases(session)

    log.info("Fetched %d canonical items, %d alias items from Neo4j.", len(canonicals), len(aliases))

    # ── Canonicals ──────────────────────────────────────────────────────────
    log.info("Embedding %d canonicals...", len(canonicals))
    canonical_texts = [item_embedding_text(i) for i in canonicals]
    canonical_vectors = embed_all(client, canonical_texts)

    ensure_collection(
        qdrant,
        COLLECTION_CANONICALS,
        payload_index_fields=["item_id", "veg_type", "form"],
    )
    canonical_points = build_canonical_points(canonicals, canonical_vectors)
    upsert_points(qdrant, COLLECTION_CANONICALS, canonical_points)
    verify_collection(qdrant, COLLECTION_CANONICALS)

    # ── Aliases ─────────────────────────────────────────────────────────────
    log.info("Embedding %d aliases...", len(aliases))
    alias_texts = [item_embedding_text(i) for i in aliases]
    alias_vectors = embed_all(client, alias_texts)

    ensure_collection(
        qdrant,
        COLLECTION_ALIASES,
        payload_index_fields=["item_id", "veg_type", "form"],
    )
    alias_points = build_alias_points(aliases, alias_vectors)
    upsert_points(qdrant, COLLECTION_ALIASES, alias_points)
    verify_collection(qdrant, COLLECTION_ALIASES)

    log.info(
        "Done. %d canonical vectors → '%s', %d alias vectors → '%s'.",
        len(canonical_points),
        COLLECTION_CANONICALS,
        len(alias_points),
        COLLECTION_ALIASES,
    )
    close_connections()


if __name__ == "__main__":
    main()
