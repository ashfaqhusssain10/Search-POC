"""Upload diagnostics/alias_resolution.json to DynamoDB.

Reads the precompute output and writes one record per alias to the
`Item-Item-Similarity-Search` table. Idempotent — re-running overwrites
existing items with the same primary key.

CLI:
    python -m scripts.upload_resolution_to_ddb
    python -m scripts.upload_resolution_to_ddb --input diagnostics/alias_resolution.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

REGION = os.getenv("AWS_REGION", "ap-south-1")
DDB_TABLE = "Item-Item-Similarity-Search"
DEFAULT_INPUT = Path("diagnostics/alias_resolution.json")
BATCH_SIZE = 25  # DDB BatchWriteItem hard limit


def _to_ddb(value: Any) -> Any:
    """Convert Python types to DynamoDB-compatible types. Floats → Decimal."""
    if isinstance(value, float):
        # DDB rejects float; Decimal handles serialization cleanly.
        # str() avoids float-repr precision noise.
        return Decimal(str(value))
    if isinstance(value, list):
        return [_to_ddb(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_ddb(v) for k, v in value.items() if v is not None}
    return value


def _record_to_item(record: dict[str, Any]) -> dict[str, Any] | None:
    """Shape one precompute record into a DDB item. Primary key is
    `alias_item_id` (stable Supabase id). Records missing the id are skipped —
    they can't be keyed and would silently overwrite each other.
    None-valued fields are dropped (DDB rejects null on PutItem attributes).
    """
    if not record.get("alias_item_id"):
        return None
    item = {k: v for k, v in record.items() if v is not None}
    return _to_ddb(item)


def upload(input_path: Path) -> None:
    data = json.loads(input_path.read_text())
    records = data["records"]
    log.info("Loaded %d records from %s", len(records), input_path)

    ddb = boto3.resource("dynamodb", region_name=REGION)
    table = ddb.Table(DDB_TABLE)

    written = 0
    skipped = 0
    with table.batch_writer() as batch:
        for r in records:
            item = _record_to_item(r)
            if item is None:
                skipped += 1
                continue
            batch.put_item(Item=item)
            written += 1
            if written % 100 == 0:
                log.info("  wrote %d/%d", written, len(records))

    log.info("Done. Wrote %d items, skipped %d (missing alias_item_id).",
             written, skipped)


def verify_sample(input_path: Path, sample_size: int = 3) -> None:
    """Sanity check: read back a few items and confirm they match the source."""
    data = json.loads(input_path.read_text())
    records = data["records"]
    sample = [r for r in records if r.get("alias_item_id")][:sample_size]

    ddb = boto3.resource("dynamodb", region_name=REGION)
    table = ddb.Table(DDB_TABLE)

    log.info("Verifying %d sample records…", len(sample))
    for r in sample:
        key = r["alias_item_id"]
        label = f"{r.get('alias', '?')} [{key}]"
        try:
            resp = table.get_item(Key={"alias_item_id": key})
        except ClientError as e:
            log.error("  ✗ %s: DDB error %s", label, e)
            continue
        item = resp.get("Item")
        if not item:
            log.error("  ✗ %s: missing in DDB", label)
            continue
        if item.get("best_canonical") == r.get("best_canonical"):
            log.info("  ✓ %s → %s", label, item.get("best_canonical"))
        else:
            log.error("  ✗ %s: expected %r, got %r",
                      label, r.get("best_canonical"), item.get("best_canonical"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT,
                        help=f"Path to alias_resolution.json (default: {DEFAULT_INPUT})")
    parser.add_argument("--skip-verify", action="store_true",
                        help="Skip the post-upload sample verification")
    args = parser.parse_args()

    if not args.input.exists():
        log.error("Input file not found: %s", args.input)
        raise SystemExit(1)

    upload(args.input)
    if not args.skip_verify:
        verify_sample(args.input)
