"""Load DynamoDB canonical items directly from MenuItemsTable.

Why this exists (and not just `load_items.py`):
  The legacy loader reads from a stale CSV snapshot (246 items). The live
  DynamoDB platter-items table references ~1,004 distinct item IDs — 793 of
  which don't exist in the CSV. Result: `load_platters.py` silently drops
  79% of platter→item edges because its MATCH on (:Item {source:'dynamodb'})
  finds nothing for those IDs.

  This loader pulls from `MenuItemsTable` (1,362 rows) so every itemId the
  platter table references actually resolves.

Active-flag policy:
  We load every item referenced by any platter, regardless of its own
  `itemActive` flag. Reasoning: the platter is the source of truth for
  "this dish is currently served as part of some menu." Many platter-
  referenced items are flagged INACTIVE individually but still appear on
  active platters. We mark inactive items with `item_active=False` so a
  downstream consumer can filter if needed; we do NOT drop them.

  Items not referenced by any platter are skipped — they'd just be dead
  weight in the search index.

Idempotent: MERGE on item id. Re-running upserts.

Usage:
    python -m scripts.load_items_from_dynamodb
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from core.categories import normalize_category
from core.connections import close_connections, neo4j_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
MENU_ITEMS_TABLE = "MenuItemsTable"
PLATTER_ITEMS_TABLE = "DefaultPlatterItemsTable"
BATCH_SIZE = 200


# ---------------------------------------------------------------------------
# Scan helpers
# ---------------------------------------------------------------------------

def _scan(table_name: str) -> list[dict[str, Any]]:
    table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(table_name)
    out: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {}
    while True:
        resp = table.scan(**kwargs)
        out.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    log.info("Scanned %d rows from %s", len(out), table_name)
    return out


def _str(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _float_or_none(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_meal_types(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, set)):
        return sorted({_str(x).upper() for x in v if x})
    raw = _str(v).upper()
    return [raw] if raw else []


def _parse_item(record: dict[str, Any]) -> dict[str, Any] | None:
    item_id = _str(record.get("itemId"))
    name = _str(record.get("itemName"))
    if not item_id or not name:
        return None
    return {
        "id": item_id,
        "name": name,
        "itemType": _str(record.get("itemType")).upper(),
        "itemCategory": normalize_category(_str(record.get("itemCategory"))),
        "mealType": _parse_meal_types(record.get("mealType")),
        "description": _str(record.get("itemDescription")),
        "basePrice": _float_or_none(record.get("itemBasePrice")),
        "is_veg": _str(record.get("itemType")).upper() == "VEG",
        "item_active": _str(record.get("itemActive")).upper() == "ACTIVE",
    }


# ---------------------------------------------------------------------------
# Neo4j write
# ---------------------------------------------------------------------------

UPSERT_DYNAMODB_ITEM = """
UNWIND $rows AS row
MERGE (i:Item {id: row.id})
SET i.name         = row.name,
    i.itemType     = row.itemType,
    i.itemCategory = row.itemCategory,
    i.mealType     = row.mealType,
    i.description  = row.description,
    i.basePrice    = row.basePrice,
    i.is_veg       = row.is_veg,
    i.item_active  = row.item_active,
    i.source       = 'dynamodb'
"""


def write_items(session, items: list[dict[str, Any]]) -> None:
    session.run(
        "CREATE CONSTRAINT item_id_unique IF NOT EXISTS "
        "FOR (i:Item) REQUIRE i.id IS UNIQUE"
    )
    for i in range(0, len(items), BATCH_SIZE):
        chunk = items[i : i + BATCH_SIZE]
        session.run(UPSERT_DYNAMODB_ITEM, rows=chunk)
    log.info("Wrote %d DynamoDB Item nodes.", len(items))


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify(session, referenced_ids: set[str]) -> None:
    r = session.run(
        "MATCH (i:Item {source: 'dynamodb'}) RETURN count(i) AS cnt"
    ).single()
    log.info("Total dynamodb Item nodes after load: %d", r["cnt"])

    found = session.run(
        "MATCH (i:Item {source: 'dynamodb'}) WHERE i.id IN $ids "
        "RETURN count(i) AS cnt",
        ids=list(referenced_ids),
    ).single()
    log.info(
        "Coverage of platter-referenced itemIds: %d / %d (%.1f%%)",
        found["cnt"], len(referenced_ids),
        100 * found["cnt"] / max(1, len(referenced_ids)),
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        platter_items = _scan(PLATTER_ITEMS_TABLE)
        referenced_ids = {
            _str(p.get("itemId")) for p in platter_items if p.get("itemId")
        }
        log.info("Platters reference %d unique itemIds", len(referenced_ids))

        menu_items = _scan(MENU_ITEMS_TABLE)
        parsed = [
            p for r in menu_items if (p := _parse_item(r))
        ]
        # Only keep items actually referenced by platters — anything else is
        # noise that would just bloat the search index.
        relevant = [p for p in parsed if p["id"] in referenced_ids]
        skipped = len(parsed) - len(relevant)
        log.info(
            "MenuItemsTable parsed=%d, kept (platter-referenced)=%d, skipped=%d",
            len(parsed), len(relevant), skipped,
        )

        # Counts for visibility
        inactive_kept = sum(1 for p in relevant if not p["item_active"])
        log.info(
            "  of kept: %d active, %d inactive (kept because referenced by platters)",
            len(relevant) - inactive_kept, inactive_kept,
        )

        with neo4j_session() as session:
            write_items(session, relevant)
            verify(session, referenced_ids)

    except (BotoCoreError, ClientError) as exc:
        log.error("DynamoDB error: %s", exc)
        sys.exit(1)
    finally:
        close_connections()


if __name__ == "__main__":
    main()
