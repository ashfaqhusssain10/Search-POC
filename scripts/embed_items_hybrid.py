"""Hybrid ETL: re-create both Qdrant collections with named vectors —
dense (OpenAI text-embedding-3-small) + sparse (Qdrant BM25 via FastEmbed).

Why hybrid:
  Pure-dense retrieval fails on Continental dishes (Tres Leches → Gulab Jamun
  @ 0.44) and mid-tail items (Aloo Baingan Curry @ 0.63) because cosine
  similarity over PM-spec text under-weights distinctive name tokens. BM25
  catches these via lexical overlap — "Tres Leches" lexically matches "Biscoff
  Tres Leches Cake" with high IDF weight regardless of cuisine drift.

What this writes:
  - searchpoc_canonicals_v2  (named vectors: "dense", "sparse")
  - searchpoc_aliases_v2     (named vectors: "dense", "sparse")

We keep the original v1 collections untouched so v4/v5 keep working and we
can A/B compare. After eval confirms wins, the consumers can switch over.

Idempotent: re-running upserts over existing points using stable integer IDs.

Usage:
    python -m scripts.embed_items_hybrid
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from fastembed import SparseTextEmbedding
from openai import OpenAI
from qdrant_client.http.models import (
    Distance,
    PayloadSchemaType,
    PointStruct,
    SparseIndexParams,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from core.connections import close_connections, get_qdrant_client, neo4j_session
from core.embedding_text import build_item_embedding_text
from core.settings import EMBEDDING_DIM, EMBEDDING_MODEL, OPENAI_API_KEY

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — _v2 collections so v1 stays as a safety net
# ---------------------------------------------------------------------------

COLLECTION_CANONICALS = "searchpoc_canonicals_v2"
COLLECTION_ALIASES = "searchpoc_aliases_v2"

# Named-vector keys. v4/v5 will reference these literals; keeping them as
# constants lets us rename in one place if we ever switch sparse model.
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"

SPARSE_MODEL = "Qdrant/bm25"  # CPU, no external service, ~1ms/doc

EMBED_BATCH_SIZE = 100
UPSERT_BATCH_SIZE = 50
MAX_RETRIES = 3
RETRY_DELAY = 2.0

# ---------------------------------------------------------------------------
# Neo4j fetchers (same as embed_items.py)
# ---------------------------------------------------------------------------

FETCH_CANONICALS = """
MATCH (i:Item {source: 'dynamodb'})
RETURN i.id AS id, i.name AS name, i.itemType AS item_type,
       i.itemCategory AS category, i.llm_description AS llm_description
ORDER BY i.name
"""

FETCH_ALIASES = """
MATCH (i:Item {source: 'supabase'})
RETURN i.id AS id, i.name AS name, i.itemType AS item_type,
       i.category_name AS category, i.llm_description AS llm_description
ORDER BY i.name
"""


def _parse_desc(llm_description: str | None) -> dict[str, Any]:
    if not llm_description:
        return {}
    try:
        return json.loads(llm_description)
    except (json.JSONDecodeError, ValueError):
        return {}


def _veg_type(item: dict[str, Any]) -> str:
    desc = _parse_desc(item.get("llm_description"))
    return (desc.get("veg_type") or item.get("item_type") or "").strip().upper()


def _form(item: dict[str, Any]) -> str:
    desc = _parse_desc(item.get("llm_description"))
    return (desc.get("sub_category") or "").strip().lower()


def _ingredients(item: dict[str, Any]) -> list[str]:
    desc = _parse_desc(item.get("llm_description"))
    return desc.get("primary_ingredients", []) or []


def item_embedding_text(item: dict[str, Any]) -> str:
    """Same PM-spec blob used for dense embeddings. BM25 will tokenize it too —
    means rare tokens like dish names get high IDF weight automatically."""
    return build_item_embedding_text(
        name=item.get("name", ""),
        llm_description=item.get("llm_description"),
    )


def _point_id(item_id: str, prefix: str = "") -> int:
    """Mirror of embed_items._point_id so points overlap by ID across v1/v2."""
    clean = item_id.removeprefix("sub_")
    try:
        base = int(clean)
        return base + 10_000_000
    except ValueError:
        digest = hashlib.sha1((prefix + item_id).encode("utf-8")).hexdigest()
        return int(digest[:12], 16)


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------

def embed_dense_batch(client: OpenAI, texts: list[str]) -> list[list[float]]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
            return [item.embedding for item in response.data]
        except Exception as exc:
            log.warning("Dense embed attempt %d/%d failed: %s", attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
    raise RuntimeError(f"Dense embedding failed after {MAX_RETRIES} attempts")


def embed_all_dense(client: OpenAI, texts: list[str]) -> list[list[float]]:
    all_vectors: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i : i + EMBED_BATCH_SIZE]
        all_vectors.extend(embed_dense_batch(client, batch))
        log.info("  Dense embedded %d / %d", min(i + EMBED_BATCH_SIZE, len(texts)), len(texts))
    return all_vectors


def embed_all_sparse(sparse_model: SparseTextEmbedding, texts: list[str]) -> list[SparseVector]:
    """BM25 is local — just call .embed and convert to Qdrant SparseVector."""
    sparse_vectors: list[SparseVector] = []
    for emb in sparse_model.embed(texts):
        sparse_vectors.append(SparseVector(
            indices=emb.indices.tolist(),
            values=emb.values.tolist(),
        ))
    log.info("  Sparse embedded %d", len(sparse_vectors))
    return sparse_vectors


# ---------------------------------------------------------------------------
# Collection setup — named vectors (dense + sparse)
# ---------------------------------------------------------------------------

def ensure_hybrid_collection(qdrant, name: str, payload_index_fields: list[str]) -> None:
    """Create the collection with named-vector config if not already present.
    We do NOT recreate-in-place: if v2 already exists, we upsert into it."""
    existing = {c.name for c in qdrant.get_collections().collections}
    if name not in existing:
        qdrant.create_collection(
            collection_name=name,
            vectors_config={
                DENSE_VECTOR_NAME: VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
            },
            sparse_vectors_config={
                SPARSE_VECTOR_NAME: SparseVectorParams(index=SparseIndexParams()),
            },
        )
        log.info("Created hybrid collection '%s'.", name)
    else:
        log.info("Collection '%s' already exists — will upsert.", name)

    for field in payload_index_fields:
        qdrant.create_payload_index(
            collection_name=name,
            field_name=field,
            field_schema=PayloadSchemaType.KEYWORD,
        )


# ---------------------------------------------------------------------------
# Build points with both vectors
# ---------------------------------------------------------------------------

def build_hybrid_points(
    items: list[dict[str, Any]],
    dense_vectors: list[list[float]],
    sparse_vectors: list[SparseVector],
    *,
    is_canonical: bool,
) -> list[PointStruct]:
    points: list[PointStruct] = []
    for item, dv, sv in zip(items, dense_vectors, sparse_vectors):
        payload: dict[str, Any] = {
            "item_id": item["id"],
            "name": item["name"],
            "veg_type": _veg_type(item),
            "form": _form(item),
        }
        if is_canonical:
            payload["category"] = item.get("category") or ""
            payload["ingredients"] = _ingredients(item)
        prefix = "can_" if is_canonical else ""
        points.append(
            PointStruct(
                id=_point_id(item["id"], prefix=prefix),
                vector={
                    DENSE_VECTOR_NAME: dv,
                    SPARSE_VECTOR_NAME: sv,
                },
                payload=payload,
            )
        )
    return points


def upsert_points(qdrant, collection: str, points: list[PointStruct]) -> None:
    for i in range(0, len(points), UPSERT_BATCH_SIZE):
        batch = points[i : i + UPSERT_BATCH_SIZE]
        qdrant.upsert(collection_name=collection, points=batch)
        log.info("  Upserted %d–%d into '%s'", i, i + len(batch) - 1, collection)


def verify_collection(qdrant, name: str) -> None:
    info = qdrant.get_collection(name)
    log.info("Collection '%s': %d points", name, info.points_count)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    client = OpenAI(api_key=OPENAI_API_KEY)
    qdrant = get_qdrant_client()

    log.info("Loading sparse BM25 model '%s'…", SPARSE_MODEL)
    sparse_model = SparseTextEmbedding(model_name=SPARSE_MODEL)

    with neo4j_session() as session:
        canonicals = [dict(r) for r in session.run(FETCH_CANONICALS)]
        aliases = [dict(r) for r in session.run(FETCH_ALIASES)]
    log.info("Fetched %d canonicals, %d aliases.", len(canonicals), len(aliases))

    # ── Canonicals ───────────────────────────────────────────────────────────
    log.info("Embedding %d canonicals (dense + sparse)…", len(canonicals))
    can_texts = [item_embedding_text(i) for i in canonicals]
    can_dense = embed_all_dense(client, can_texts)
    can_sparse = embed_all_sparse(sparse_model, can_texts)

    ensure_hybrid_collection(
        qdrant, COLLECTION_CANONICALS,
        payload_index_fields=["item_id", "veg_type", "form"],
    )
    upsert_points(
        qdrant, COLLECTION_CANONICALS,
        build_hybrid_points(canonicals, can_dense, can_sparse, is_canonical=True),
    )
    verify_collection(qdrant, COLLECTION_CANONICALS)

    # ── Aliases ──────────────────────────────────────────────────────────────
    log.info("Embedding %d aliases (dense + sparse)…", len(aliases))
    ali_texts = [item_embedding_text(i) for i in aliases]
    ali_dense = embed_all_dense(client, ali_texts)
    ali_sparse = embed_all_sparse(sparse_model, ali_texts)

    ensure_hybrid_collection(
        qdrant, COLLECTION_ALIASES,
        payload_index_fields=["item_id", "veg_type", "form"],
    )
    upsert_points(
        qdrant, COLLECTION_ALIASES,
        build_hybrid_points(aliases, ali_dense, ali_sparse, is_canonical=False),
    )
    verify_collection(qdrant, COLLECTION_ALIASES)

    log.info("Done. Hybrid collections ready: %s, %s",
             COLLECTION_CANONICALS, COLLECTION_ALIASES)
    close_connections()


if __name__ == "__main__":
    main()
