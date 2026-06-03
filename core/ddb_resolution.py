"""DynamoDB wrapper for the alias-resolution table.

Single hot-path operation: batch-fetch resolution records by `alias_item_id`.
Decimal values are converted back to floats for the caller.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Any

import boto3

log = logging.getLogger(__name__)

REGION = os.getenv("AWS_REGION", "ap-south-1")
DDB_TABLE = os.getenv("RESOLUTION_TABLE", "Item-Item-Similarity-Search")
BATCH_GET_LIMIT = 100  # DDB BatchGetItem hard limit

_table = None


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb", region_name=REGION).Table(DDB_TABLE)
    return _table


def _from_ddb(value: Any) -> Any:
    """Convert DDB types back to plain Python (Decimal → float, recurse)."""
    if isinstance(value, Decimal):
        # Preserve int-ness so item counts etc. stay as ints
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_from_ddb(v) for v in value]
    if isinstance(value, dict):
        return {k: _from_ddb(v) for k, v in value.items()}
    return value


def get_many(alias_item_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Batch fetch resolution records. Returns {alias_item_id: record}.
    Missing ids are silently absent from the result."""
    ids = [i for i in alias_item_ids if i]
    if not ids:
        return {}

    ddb = boto3.client("dynamodb", region_name=REGION)
    out: dict[str, dict[str, Any]] = {}

    for start in range(0, len(ids), BATCH_GET_LIMIT):
        chunk = ids[start:start + BATCH_GET_LIMIT]
        request = {
            DDB_TABLE: {
                "Keys": [{"alias_item_id": {"S": i}} for i in chunk],
            }
        }
        # Loop in case DDB returns UnprocessedKeys (it batches under load)
        while request:
            resp = ddb.batch_get_item(RequestItems=request)
            for raw in resp.get("Responses", {}).get(DDB_TABLE, []):
                item = _deserialize(raw)
                if item.get("alias_item_id"):
                    out[item["alias_item_id"]] = item
            request = resp.get("UnprocessedKeys") or {}

    return out


def _deserialize(raw: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Unwrap DDB low-level format ({'S': 'foo'}) into plain Python."""
    from boto3.dynamodb.types import TypeDeserializer
    deserializer = TypeDeserializer()
    return _from_ddb({k: deserializer.deserialize(v) for k, v in raw.items()})
