"""Step 1+2: Load canonical DynamoDB items and Supabase alias items into Neo4j.

Usage:
    python -m scripts.load_items
"""

import csv
import json
import logging
import sys
from pathlib import Path
from typing import Any

from core.categories import normalize_category
from core.connections import close_connections, neo4j_session
from core.settings import DYNAMODB_CSV, SUPABASE_CSV

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

def parse_meal_types(raw: str) -> list[str]:
    """Parse DynamoDB JSON-wrapped mealType list.

    Input examples:
        '[{"S":"LUNCH"}]'
        '[{"S":"LUNCH"},{"S":"DINNER"}]'
        'null' / '' / None
    """
    if not raw or raw.strip() in ("", "null"):
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [item["S"] for item in parsed if isinstance(item, dict) and "S" in item]
    except (json.JSONDecodeError, KeyError):
        pass
    # Fallback: treat as plain string
    return [raw.strip()]


# ---------------------------------------------------------------------------
# DynamoDB loader
# ---------------------------------------------------------------------------

def load_dynamodb_items(csv_path: Path) -> list[dict[str, Any]]:
    """Read CSV, dedup by itemName (keep first UUID), return list of item dicts."""
    seen: dict[str, dict[str, Any]] = {}  # name → item dict

    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            name = row["itemName"].strip()
            if not name:
                continue

            if name in seen:
                # Merge unique mealTypes from duplicate
                existing = seen[name]
                extra_meals = parse_meal_types(row["mealType"])
                for m in extra_meals:
                    if m not in existing["mealType"]:
                        existing["mealType"].append(m)
                continue

            seen[name] = {
                "id": row["itemId"].strip(),
                "name": name,
                "itemType": row["itemType"].strip(),
                "itemCategory": normalize_category(row["itemCategory"]),
                "mealType": parse_meal_types(row["mealType"]),
                "description": row.get("itemDescription", "").strip(),
                "basePrice": _safe_float(row.get("itemBasePrice")),
                "is_veg": row["itemType"].strip().upper() == "VEG",
                "llm_description": row.get("llm_description", "").strip(),
                "source": "dynamodb",
            }

    log.info("DynamoDB: %d raw rows → %d unique items", _count_rows(csv_path), len(seen))
    return list(seen.values())


def _count_rows(csv_path: Path) -> int:
    with open(csv_path, newline="", encoding="utf-8") as fh:
        return sum(1 for _ in csv.DictReader(fh))


def _safe_float(val: str | None) -> float | None:
    try:
        return float(val) if val else None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Supabase loader
# ---------------------------------------------------------------------------

def load_supabase_items(csv_path: Path) -> list[dict[str, Any]]:
    """Read Supabase CSV and return list of alias item dicts."""
    items: list[dict[str, Any]] = []

    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            name = row["item_name"].strip()
            if not name:
                continue
            items.append({
                "id": f"sub_{row['item_id'].strip()}",
                "name": name,
                "itemType": row.get("item_type", "").strip().upper(),
                "category_name": row.get("category_name", "").strip(),
                "typecode_name": row.get("typecode_name", "").strip(),
                "basePrice": _safe_float(row.get("item_base_price")),
                "llm_description": row.get("llm_description", "").strip(),
                "source": "supabase",
            })

    log.info("Supabase: %d items loaded", len(items))
    return items


# ---------------------------------------------------------------------------
# Neo4j writes
# ---------------------------------------------------------------------------

SETUP_CONSTRAINTS = [
    "CREATE CONSTRAINT item_id_unique IF NOT EXISTS FOR (i:Item) REQUIRE i.id IS UNIQUE",
]

UPSERT_DYNAMODB_ITEM = """
MERGE (i:Item {id: $id})
SET i.name        = $name,
    i.itemType    = $itemType,
    i.itemCategory = $itemCategory,
    i.mealType    = $mealType,
    i.description = $description,
    i.basePrice   = $basePrice,
    i.is_veg      = $is_veg,
    i.source      = 'dynamodb'
"""

UPSERT_SUPABASE_ITEM = """
MERGE (i:Item {id: $id})
SET i.name          = $name,
    i.itemType      = $itemType,
    i.category_name = $category_name,
    i.typecode_name = $typecode_name,
    i.basePrice     = $basePrice,
    i.source        = 'supabase'
"""

BATCH_SIZE = 200


def _batch_upsert(session, query: str, rows: list[dict[str, Any]]) -> None:
    for i in range(0, len(rows), BATCH_SIZE):
        chunk = rows[i : i + BATCH_SIZE]
        session.run(
            "UNWIND $rows AS row " + query.replace("$", "row.").replace("MERGE (i:Item {id: row.id})", "MERGE (i:Item {id: row.id})"),
            rows=chunk,
        )


def write_items_to_neo4j(
    dynamodb_items: list[dict[str, Any]],
    supabase_items: list[dict[str, Any]],
) -> None:
    """Write all Item nodes to Neo4j in batches."""
    with neo4j_session() as session:
        # Constraints
        for stmt in SETUP_CONSTRAINTS:
            session.run(stmt)
        log.info("Constraints ensured.")

        # DynamoDB items
        for i in range(0, len(dynamodb_items), BATCH_SIZE):
            chunk = dynamodb_items[i : i + BATCH_SIZE]
            session.run(
                """
                UNWIND $rows AS row
                MERGE (i:Item {id: row.id})
                SET i.name         = row.name,
                    i.itemType     = row.itemType,
                    i.itemCategory = row.itemCategory,
                    i.mealType     = row.mealType,
                    i.description  = row.description,
                    i.basePrice        = row.basePrice,
                    i.is_veg           = row.is_veg,
                    i.llm_description  = row.llm_description,
                    i.source           = 'dynamodb'
                """,
                rows=chunk,
            )
        log.info("Wrote %d DynamoDB Item nodes.", len(dynamodb_items))

        # Supabase items
        for i in range(0, len(supabase_items), BATCH_SIZE):
            chunk = supabase_items[i : i + BATCH_SIZE]
            session.run(
                """
                UNWIND $rows AS row
                MERGE (i:Item {id: row.id})
                SET i.name          = row.name,
                    i.itemType      = row.itemType,
                    i.category_name = row.category_name,
                    i.typecode_name = row.typecode_name,
                    i.basePrice        = row.basePrice,
                    i.llm_description  = row.llm_description,
                    i.source           = 'supabase'
                """,
                rows=chunk,
            )
        log.info("Wrote %d Supabase Item nodes.", len(supabase_items))


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify(session) -> None:
    result = session.run(
        "MATCH (i:Item) RETURN i.source AS source, count(*) AS cnt"
    )
    for record in result:
        log.info("  source=%-10s  count=%d", record["source"], record["cnt"])


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    root = Path(__file__).parent.parent
    dynamodb_csv = root / DYNAMODB_CSV
    supabase_csv = root / SUPABASE_CSV

    if not dynamodb_csv.exists():
        log.error("DynamoDB CSV not found: %s", dynamodb_csv)
        sys.exit(1)
    if not supabase_csv.exists():
        log.error("Supabase CSV not found: %s", supabase_csv)
        sys.exit(1)

    dynamodb_items = load_dynamodb_items(dynamodb_csv)
    supabase_items = load_supabase_items(supabase_csv)

    write_items_to_neo4j(dynamodb_items, supabase_items)

    with neo4j_session() as session:
        log.info("Verification — Item node counts by source:")
        verify(session)

    close_connections()
    log.info("Done.")


if __name__ == "__main__":
    main()
