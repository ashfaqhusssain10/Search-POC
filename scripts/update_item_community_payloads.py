"""Step 5b: Stamp community_id onto searchpoc_canonicals Qdrant payloads.

Must run AFTER detect_communities.py (step 5) which creates MEMBER_OF edges,
and BEFORE search.py which uses community_id to filter per-platter lookups.

Reads (Item)-[:MEMBER_OF]->(Community) from Neo4j for all DynamoDB canonical
items and calls qdrant.set_payload() to add community_id to each existing point.
No vectors are changed — this is a metadata-only update.

Idempotent: re-running overwrites with the same values.

Usage:
    python -m scripts.update_item_community_payloads
"""

from __future__ import annotations

import logging
from typing import Any

from qdrant_client.http.models import PayloadSchemaType
from qdrant_client.models import FieldCondition, Filter, MatchValue

from core.connections import close_connections, get_qdrant_client, neo4j_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COLLECTION_CANONICALS = "searchpoc_canonicals"
UPDATE_BATCH_SIZE = 100

# ---------------------------------------------------------------------------
# Neo4j fetcher
# ---------------------------------------------------------------------------

FETCH_ITEM_COMMUNITIES = """
MATCH (i:Item {source: 'dynamodb'})-[:MEMBER_OF]->(c:Community)
RETURN i.id AS item_id, c.id AS community_id
ORDER BY i.id
"""


def fetch_item_community_map(session) -> list[dict[str, Any]]:
    """Return list of {item_id, community_id} for all DynamoDB canonical items."""
    return [dict(r) for r in session.run(FETCH_ITEM_COMMUNITIES)]


# ---------------------------------------------------------------------------
# Payload update
# ---------------------------------------------------------------------------

def update_payloads(qdrant, rows: list[dict[str, Any]]) -> int:
    """Stamp community_id onto each canonical point in searchpoc_canonicals.

    Uses payload filter on item_id (indexed keyword field) to identify points —
    avoids recomputing Qdrant integer IDs which are not stable across processes.
    Processes one item at a time. Returns the number of points updated.
    """
    updated = 0
    for i, row in enumerate(rows):
        qdrant.set_payload(
            collection_name=COLLECTION_CANONICALS,
            payload={"community_id": row["community_id"]},
            points=Filter(
                must=[FieldCondition(key="item_id", match=MatchValue(value=row["item_id"]))]
            ),
        )
        updated += 1
        if updated % UPDATE_BATCH_SIZE == 0 or updated == len(rows):
            log.info("  Updated %d / %d points", updated, len(rows))
    return updated


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    qdrant = get_qdrant_client()

    with neo4j_session() as session:
        rows = fetch_item_community_map(session)

    log.info("Fetched %d item→community mappings from Neo4j.", len(rows))

    if not rows:
        log.warning("No mappings found — has detect_communities.py been run?")
        return

    # Ensure community_id is indexed so filtered queries work at search time.
    qdrant.create_payload_index(
        collection_name=COLLECTION_CANONICALS,
        field_name="community_id",
        field_schema=PayloadSchemaType.KEYWORD,
    )
    log.info("Payload index on 'community_id' ensured for '%s'.", COLLECTION_CANONICALS)

    updated = update_payloads(qdrant, rows)
    log.info("Done. Stamped community_id onto %d points in '%s'.", updated, COLLECTION_CANONICALS)
    close_connections()


if __name__ == "__main__":
    try:
        main()
    finally:
        close_connections()
