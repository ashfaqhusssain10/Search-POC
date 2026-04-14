"""Step 8: Embed community summaries and upsert to Qdrant collection.

Embedding text format (designed to catch both canonical and alias-style queries):
  Community: <name>. Members: <canonical names>. Also known as: <variant names>.
  Hub items: <hub_items>. <LLM narrative>

Creates the Qdrant collection `item_search_communities` if it doesn't exist.

Usage:
    python -m scripts.index_communities
"""

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
from core.settings import (
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    OPENAI_API_KEY,
    QDRANT_COLLECTION,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

EMBED_BATCH_SIZE = 50
UPSERT_BATCH_SIZE = 25
MAX_RETRIES = 3
RETRY_DELAY = 2.0

# ---------------------------------------------------------------------------
# Fetch communities from Neo4j
# ---------------------------------------------------------------------------

FETCH_COMMUNITIES = """
MATCH (c:Community)
WHERE c.summary_json IS NOT NULL
RETURN c.id AS community_id,
       c.name AS name,
       c.summary_json AS summary_json,
       c.member_count AS member_count
"""


def fetch_communities(session) -> list[dict[str, Any]]:
    result = session.run(FETCH_COMMUNITIES)
    communities = []
    for rec in result:
        raw = rec["summary_json"]
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            log.warning("Invalid summary_json for %s — skipping", rec["community_id"])
            continue
        communities.append({
            "community_id": rec["community_id"],
            "name": rec["name"] or rec["community_id"],
            "member_count": rec["member_count"] or 0,
            "members": payload.get("members", []),
            "variant_names": payload.get("variant_names", []),
            "hub_items": payload.get("hub_items", []),
            "narrative": payload.get("narrative", ""),
        })
    return communities


# ---------------------------------------------------------------------------
# Build embedding text
# ---------------------------------------------------------------------------

def build_embedding_text(community: dict[str, Any]) -> str:
    """Construct the text to embed — includes all alias names for broad matching."""
    parts = [
        f"Community: {community['name']}.",
        f"Members: {', '.join(community['members'])}." if community["members"] else "",
        f"Also known as: {', '.join(community['variant_names'])}." if community["variant_names"] else "",
        f"Hub items: {', '.join(community['hub_items'])}." if community["hub_items"] else "",
        community["narrative"],
    ]
    return " ".join(p for p in parts if p).strip()


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def embed_texts(client: OpenAI, texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts. Returns list of vectors."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=texts,
            )
            return [item.embedding for item in response.data]
        except Exception as exc:
            log.warning("Embedding attempt %d/%d failed: %s", attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"Embedding failed after {MAX_RETRIES} attempts")


# ---------------------------------------------------------------------------
# Qdrant collection setup + upsert
# ---------------------------------------------------------------------------

def ensure_collection(qdrant) -> None:
    """Create Qdrant collection if it doesn't exist."""
    existing = {c.name for c in qdrant.get_collections().collections}
    if QDRANT_COLLECTION in existing:
        log.info("Collection '%s' already exists.", QDRANT_COLLECTION)
        return

    qdrant.create_collection(
        collection_name=QDRANT_COLLECTION,
        vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
    )
    qdrant.create_payload_index(
        collection_name=QDRANT_COLLECTION,
        field_name="community_id",
        field_schema=PayloadSchemaType.KEYWORD,
    )
    log.info("Created Qdrant collection '%s' with community_id index.", QDRANT_COLLECTION)


def community_to_point(community: dict[str, Any], vector: list[float]) -> PointStruct:
    """Build a Qdrant PointStruct from community data."""
    # Use a stable integer ID derived from community index (e.g., comm_7 → 7)
    raw_id = community["community_id"].replace("comm_", "")
    try:
        point_id = int(raw_id)
    except ValueError:
        # Fallback: hash to int
        point_id = abs(hash(community["community_id"])) % (10**9)

    return PointStruct(
        id=point_id,
        vector=vector,
        payload={
            "community_id": community["community_id"],
            "name": community["name"],
            "members": community["members"],
            "variant_names": community["variant_names"],
            "hub_items": community["hub_items"],
            "member_count": community["member_count"],
        },
    )


def upsert_to_qdrant(qdrant, points: list[PointStruct]) -> None:
    for i in range(0, len(points), UPSERT_BATCH_SIZE):
        batch = points[i : i + UPSERT_BATCH_SIZE]
        qdrant.upsert(collection_name=QDRANT_COLLECTION, points=batch)
        log.info("  Upserted points %d–%d", i, i + len(batch) - 1)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
    qdrant = get_qdrant_client()

    with neo4j_session() as session:
        communities = fetch_communities(session)

    log.info("Fetched %d communities with summaries.", len(communities))

    ensure_collection(qdrant)

    # Build embedding texts
    texts = [build_embedding_text(c) for c in communities]

    # Embed in batches
    all_vectors: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch_texts = texts[i : i + EMBED_BATCH_SIZE]
        batch_vectors = embed_texts(openai_client, batch_texts)
        all_vectors.extend(batch_vectors)
        log.info("  Embedded %d / %d", min(i + EMBED_BATCH_SIZE, len(texts)), len(texts))

    # Build Qdrant points
    points = [
        community_to_point(comm, vec)
        for comm, vec in zip(communities, all_vectors)
    ]

    # Upsert
    upsert_to_qdrant(qdrant, points)

    log.info("Done. %d communities indexed in Qdrant collection '%s'.", len(points), QDRANT_COLLECTION)
    close_connections()


if __name__ == "__main__":
    main()
