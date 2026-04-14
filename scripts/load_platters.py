"""Step 4: Scan DynamoDB platter tables and write Platter nodes + CONTAINS edges to Neo4j.

Tables used:
  - DefaultPlattersTable      → Platter nodes
  - DefaultPlatterItemsTable  → (Platter)-[:CONTAINS]->(Item) edges

DefaultPlattersCategoriesTable is not used — category limits are not needed for this POC.

Usage:
    python -m scripts.load_platters

Env vars:
    AWS_REGION  (default: ap-south-1)
"""

import logging
import os
import sys
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from core.connections import close_connections, neo4j_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

AWS_REGION: str = os.getenv("AWS_REGION", "ap-south-1")
PLATTERS_TABLE: str = "DefaultPlattersTable"
PLATTER_ITEMS_TABLE: str = "DefaultPlatterItemsTable"
BATCH_SIZE: int = 100


# ---------------------------------------------------------------------------
# DynamoDB scan
# ---------------------------------------------------------------------------

def scan_table(table_name: str) -> list[dict[str, Any]]:
    """Full paginated scan of a DynamoDB table."""
    table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(table_name)
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {}

    while True:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key

    log.info("Scanned %d records from %s", len(items), table_name)
    return items


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _str(val: Any) -> str:
    return str(val).strip() if val is not None else ""


def _float_or_none(val: Any) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _parse_meal_times(val: Any) -> list[str]:
    """Parse mealTimes from DynamoDB into a list of strings.

    DynamoDB may store this as a string, a list, or a set.
    Returns a sorted deduplicated list of meal type strings.
    """
    if val is None:
        return []
    if isinstance(val, (list, set)):
        return sorted({str(v).strip().upper() for v in val if v})
    raw = str(val).strip()
    if not raw:
        return []
    return [raw.upper()]


def parse_platter(record: dict[str, Any]) -> dict[str, Any] | None:
    pid = _str(record.get("platterId"))
    if not pid:
        return None
    return {
        "id": pid,
        "name": _str(record.get("platterName")),
        "type": _str(record.get("platterType")),
        "subType": _str(record.get("platterSubType")),
        "mealType": _parse_meal_times(record.get("mealTimes")),
        "veg": _str(record.get("menuType")).upper() == "VEG",
        "minPrice": _float_or_none(record.get("minPrice")),
        "maxPrice": _float_or_none(record.get("maxPrice")),
        "active": _str(record.get("platterActive")).upper() == "ACTIVE",
    }


def parse_platter_item(record: dict[str, Any]) -> dict[str, str] | None:
    pid = _str(record.get("platterId"))
    iid = _str(record.get("itemId"))
    if not pid or not iid:
        return None
    return {"platter_id": pid, "item_id": iid}


# ---------------------------------------------------------------------------
# Neo4j writes
# ---------------------------------------------------------------------------

SETUP_CONSTRAINT = (
    "CREATE CONSTRAINT platter_id_unique IF NOT EXISTS FOR (p:Platter) REQUIRE p.id IS UNIQUE"
)

UPSERT_PLATTERS = """
UNWIND $rows AS row
MERGE (p:Platter {id: row.id})
SET p.name     = row.name,
    p.type     = row.type,
    p.subType  = row.subType,
    p.mealType = row.mealType,
    p.veg      = row.veg,
    p.minPrice = row.minPrice,
    p.maxPrice = row.maxPrice,
    p.active   = row.active
"""

UPSERT_CONTAINS = """
UNWIND $pairs AS pair
MATCH (p:Platter {id: pair.platter_id})
MATCH (i:Item {id: pair.item_id, source: 'dynamodb'})
MERGE (p)-[:CONTAINS]->(i)
"""


def write_platters(session, platters: list[dict[str, Any]]) -> None:
    session.run(SETUP_CONSTRAINT)
    for i in range(0, len(platters), BATCH_SIZE):
        session.run(UPSERT_PLATTERS, rows=platters[i : i + BATCH_SIZE])
    log.info("Wrote %d Platter nodes.", len(platters))


def write_contains_edges(session, pairs: list[dict[str, str]]) -> None:
    for i in range(0, len(pairs), BATCH_SIZE):
        session.run(UPSERT_CONTAINS, pairs=pairs[i : i + BATCH_SIZE])
    log.info("Wrote %d CONTAINS edges.", len(pairs))


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify(session) -> None:
    r1 = session.run("MATCH (p:Platter) RETURN count(p) AS cnt").single()
    r2 = session.run("MATCH (:Platter)-[:CONTAINS]->() RETURN count(*) AS cnt").single()
    log.info("Platter nodes: %d | CONTAINS edges: %d", r1["cnt"], r2["cnt"])


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        platter_records = scan_table(PLATTERS_TABLE)
        platters = [p for r in platter_records if (p := parse_platter(r))]
        log.info("Parsed %d valid Platter records", len(platters))

        item_records = scan_table(PLATTER_ITEMS_TABLE)
        pairs = [p for r in item_records if (p := parse_platter_item(r))]
        log.info("Parsed %d Platter→Item pairs", len(pairs))

        with neo4j_session() as session:
            write_platters(session, platters)
            write_contains_edges(session, pairs)
            verify(session)

    except (BotoCoreError, ClientError) as exc:
        log.error("DynamoDB error: %s", exc)
        sys.exit(1)
    finally:
        close_connections()


if __name__ == "__main__":
    main()
