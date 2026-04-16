"""Step 4: Scan DynamoDB platter tables and write platter structure to Neo4j.

Tables used:
  - DefaultPlattersTable            → Platter nodes
  - DefaultPlattersCategoriesTable  → PlatterCategory nodes + HAS_CATEGORY edges
  - DefaultPlatterItemsTable        → CONTAINS + CONTAINS_ITEM edges

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

from core.categories import category_family, normalize_category
from core.connections import close_connections, neo4j_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

AWS_REGION: str = os.getenv("AWS_REGION", "ap-south-1")
PLATTERS_TABLE: str = "DefaultPlattersTable"
PLATTER_CATEGORIES_TABLE: str = "DefaultPlattersCategoriesTable"
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


def _int_or_none(val: Any) -> int | None:
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _bool_or_none(val: Any) -> bool | None:
    raw = _str(val).upper()
    if raw in {"TRUE", "T", "Y", "YES", "1"}:
        return True
    if raw in {"FALSE", "F", "N", "NO", "0"}:
        return False
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


def parse_platter_category(record: dict[str, Any]) -> dict[str, Any] | None:
    platter_id = _str(record.get("platterId"))
    category_id = _str(record.get("categoryId"))
    if not platter_id or not category_id:
        return None

    raw_name = _str(record.get("categoryName"))
    normalized_name = normalize_category(raw_name)
    family_name = category_family(raw_name)
    if raw_name and not family_name:
        log.warning(
            "Unmapped platter category %r on platter %s; excluding it from family scoring.",
            raw_name,
            platter_id,
        )

    return {
        "id": f"{platter_id}::{category_id}",
        "platter_id": platter_id,
        "category_id": category_id,
        "category_name_raw": raw_name,
        "category_name_normalized": normalized_name,
        "category_family": family_name,
        "category_order": _int_or_none(record.get("category_order")),
        "items_limit": _int_or_none(record.get("categoryItemsLimit")),
        "premium_items_limit": _int_or_none(record.get("premiumItemsLimit")),
        "is_combo": _bool_or_none(record.get("isCombo")),
        "active": _str(record.get("categoryPlatterActive")).upper() == "ACTIVE",
    }


def parse_platter_item(record: dict[str, Any]) -> dict[str, Any] | None:
    pid = _str(record.get("platterId"))
    iid = _str(record.get("itemId"))
    cid = _str(record.get("categoryId"))
    if not pid or not iid or not cid:
        return None
    return {
        "platter_id": pid,
        "item_id": iid,
        "category_id": cid,
        "item_quantity": _float_or_none(record.get("itemQuantity")),
        "item_quantity_unit": _str(record.get("itemQuantityUnit")),
        "premium": _str(record.get("premium")).upper() == "Y",
        "is_base_item": _bool_or_none(record.get("isBaseItem")),
        "item_base_price": _float_or_none(record.get("itemBasePrice")),
        "item_min_price": _float_or_none(record.get("itemMinPrice")),
    }


# ---------------------------------------------------------------------------
# Neo4j writes
# ---------------------------------------------------------------------------

SETUP_CONSTRAINT = (
    "CREATE CONSTRAINT platter_id_unique IF NOT EXISTS FOR (p:Platter) REQUIRE p.id IS UNIQUE"
)
SETUP_PLATTER_CATEGORY_CONSTRAINT = """
CREATE CONSTRAINT platter_category_id_unique IF NOT EXISTS
FOR (pc:PlatterCategory) REQUIRE pc.id IS UNIQUE
"""

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

UPSERT_PLATTER_CATEGORIES = """
UNWIND $rows AS row
MERGE (pc:PlatterCategory {id: row.id})
SET pc.platter_id              = row.platter_id,
    pc.category_id             = row.category_id,
    pc.category_name_raw       = row.category_name_raw,
    pc.category_name_normalized = row.category_name_normalized,
    pc.category_family         = row.category_family,
    pc.category_order          = row.category_order,
    pc.items_limit             = row.items_limit,
    pc.premium_items_limit     = row.premium_items_limit,
    pc.is_combo                = row.is_combo,
    pc.active                  = row.active
"""

UPSERT_HAS_CATEGORY = """
UNWIND $rows AS row
MATCH (p:Platter {id: row.platter_id})
MATCH (pc:PlatterCategory {id: row.id})
MERGE (p)-[:HAS_CATEGORY]->(pc)
"""

UPSERT_CONTAINS_ITEM = """
UNWIND $rows AS row
MATCH (pc:PlatterCategory {id: row.platter_id + '::' + row.category_id})
MATCH (i:Item {id: row.item_id, source: 'dynamodb'})
MERGE (pc)-[r:CONTAINS_ITEM]->(i)
SET r.item_quantity      = row.item_quantity,
    r.item_quantity_unit = row.item_quantity_unit,
    r.premium            = row.premium,
    r.is_base_item       = row.is_base_item,
    r.item_base_price    = row.item_base_price,
    r.item_min_price     = row.item_min_price
"""


def write_platters(session, platters: list[dict[str, Any]]) -> None:
    session.run(SETUP_CONSTRAINT)
    for i in range(0, len(platters), BATCH_SIZE):
        session.run(UPSERT_PLATTERS, rows=platters[i : i + BATCH_SIZE])
    log.info("Wrote %d Platter nodes.", len(platters))


def write_platter_categories(session, categories: list[dict[str, Any]]) -> None:
    session.run(SETUP_PLATTER_CATEGORY_CONSTRAINT)
    for i in range(0, len(categories), BATCH_SIZE):
        chunk = categories[i : i + BATCH_SIZE]
        session.run(UPSERT_PLATTER_CATEGORIES, rows=chunk)
        session.run(UPSERT_HAS_CATEGORY, rows=chunk)
    log.info("Wrote %d PlatterCategory nodes.", len(categories))


def write_contains_edges(session, pairs: list[dict[str, str]]) -> None:
    for i in range(0, len(pairs), BATCH_SIZE):
        session.run(UPSERT_CONTAINS, pairs=pairs[i : i + BATCH_SIZE])
    log.info("Wrote %d CONTAINS edges.", len(pairs))


def write_contains_item_edges(session, rows: list[dict[str, Any]]) -> None:
    for i in range(0, len(rows), BATCH_SIZE):
        session.run(UPSERT_CONTAINS_ITEM, rows=rows[i : i + BATCH_SIZE])
    log.info("Wrote %d CONTAINS_ITEM edges.", len(rows))


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify(session) -> None:
    r1 = session.run("MATCH (p:Platter) RETURN count(p) AS cnt").single()
    r2 = session.run("MATCH (:Platter)-[:CONTAINS]->() RETURN count(*) AS cnt").single()
    r3 = session.run("MATCH (pc:PlatterCategory) RETURN count(pc) AS cnt").single()
    r4 = session.run("MATCH (:Platter)-[:HAS_CATEGORY]->() RETURN count(*) AS cnt").single()
    r5 = session.run("MATCH (:PlatterCategory)-[:CONTAINS_ITEM]->() RETURN count(*) AS cnt").single()
    log.info(
        "Platter nodes: %d | CONTAINS edges: %d | PlatterCategory nodes: %d | HAS_CATEGORY edges: %d | CONTAINS_ITEM edges: %d",
        r1["cnt"],
        r2["cnt"],
        r3["cnt"],
        r4["cnt"],
        r5["cnt"],
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        platter_records = scan_table(PLATTERS_TABLE)
        platters = [p for r in platter_records if (p := parse_platter(r))]
        log.info("Parsed %d valid Platter records", len(platters))

        platter_category_records = scan_table(PLATTER_CATEGORIES_TABLE)
        platter_categories = [
            pc for r in platter_category_records if (pc := parse_platter_category(r))
        ]
        log.info("Parsed %d PlatterCategory rows", len(platter_categories))

        item_records = scan_table(PLATTER_ITEMS_TABLE)
        pairs = [p for r in item_records if (p := parse_platter_item(r))]
        log.info("Parsed %d Platter→Item pairs", len(pairs))

        with neo4j_session() as session:
            write_platters(session, platters)
            write_platter_categories(session, platter_categories)
            write_contains_edges(session, pairs)
            write_contains_item_edges(session, pairs)
            verify(session)

    except (BotoCoreError, ClientError) as exc:
        log.error("DynamoDB error: %s", exc)
        sys.exit(1)
    finally:
        close_connections()


if __name__ == "__main__":
    main()
