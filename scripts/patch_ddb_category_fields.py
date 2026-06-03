"""One-shot patch: add alias_category_name + alias_typecode_name to every
existing record in DynamoDB Item-Item-Similarity-Search.

Fetches the two fields from Neo4j in one batch query, then batch-updates DDB
using update_item. Does NOT re-run the precompute or call Bedrock.

CLI:
    python -m scripts.patch_ddb_category_fields
"""

from __future__ import annotations

import logging
import os

import boto3

from core.connections import close_connections, neo4j_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

REGION = os.getenv("AWS_REGION", "ap-south-1")
DDB_TABLE = "Item-Item-Similarity-Search"
SCAN_BATCH = 100

FETCH_QUERY = """
MATCH (i:Item {source: 'supabase'})
RETURN i.name AS name,
       i.category_name AS category_name,
       i.typecode_name AS typecode_name
"""


def _fetch_from_neo4j() -> dict[str, dict[str, str]]:
    """Returns {alias_name: {category_name, typecode_name}}."""
    out: dict[str, dict[str, str]] = {}
    with neo4j_session() as session:
        for row in session.run(FETCH_QUERY):
            out[row["name"]] = {
                "category_name": row["category_name"] or "",
                "typecode_name": row["typecode_name"] or "",
            }
    log.info("Fetched category/typecode for %d aliases from Neo4j", len(out))
    return out


def _scan_all(table) -> list[dict]:
    """Scan DDB table returning alias_item_id + alias name for every record."""
    rows = []
    kwargs = {
        "ProjectionExpression": "alias_item_id, #a",
        "ExpressionAttributeNames": {"#a": "alias"},
    }
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            if item.get("alias_item_id") and item.get("alias"):
                rows.append({"pk": item["alias_item_id"], "alias": item["alias"]})
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return rows


def main() -> None:
    neo4j_data = _fetch_from_neo4j()

    ddb = boto3.resource("dynamodb", region_name=REGION)
    table = ddb.Table(DDB_TABLE)

    log.info("Scanning DDB table for existing records…")
    rows = _scan_all(table)
    log.info("Found %d records to patch", len(rows))

    updated = 0
    skipped = 0
    for row in rows:
        data = neo4j_data.get(row["alias"])
        if not data:
            skipped += 1
            continue
        table.update_item(
            Key={"alias_item_id": row["pk"]},
            UpdateExpression="SET alias_category_name = :cat, alias_typecode_name = :tc",
            ExpressionAttributeValues={
                ":cat": data["category_name"],
                ":tc": data["typecode_name"],
            },
        )
        updated += 1
        if updated % 100 == 0:
            log.info("  patched %d/%d", updated, len(rows))

    log.info("Done. patched=%d skipped=%d (not in Neo4j)", updated, skipped)


if __name__ == "__main__":
    try:
        main()
    finally:
        close_connections()
