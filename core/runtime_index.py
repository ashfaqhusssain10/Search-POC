"""Loads the S3-published runtime artifact bundle once at process start.

For Slice 3a we only need the small JSON metadata files — name → item_id maps
and per-name (form, veg, item_id). Vector arrays (.npy) stay in S3 unused
until we add tier-C numpy substitute fallback.

Layout in S3 (built by scripts.build_runtime_artifacts):
    s3://search-item-item-poc/current.json     → {"version": "v1"}
    s3://search-item-item-poc/versions/v1/
        manifest.json
        alias_names.json, alias_meta.json
        canonical_names.json, canonical_meta.json
        alias_vectors.npy, canonical_vectors.npy   (not loaded here)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)

REGION = os.getenv("AWS_REGION", "ap-south-1")
S3_BUCKET = os.getenv("RUNTIME_ARTIFACT_BUCKET", "search-item-item-poc")


@dataclass
class RuntimeIndex:
    version: str
    alias_meta: dict[str, dict[str, Any]]      # name → {item_id, form, veg}
    canonical_meta: dict[str, dict[str, Any]]  # name → {item_id, form, veg}
    alias_name_to_id: dict[str, str] = field(default_factory=dict)
    canonical_name_to_id: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.alias_name_to_id:
            self.alias_name_to_id = {
                n: m["item_id"] for n, m in self.alias_meta.items() if m.get("item_id")
            }
        if not self.canonical_name_to_id:
            self.canonical_name_to_id = {
                n: m["item_id"] for n, m in self.canonical_meta.items() if m.get("item_id")
            }


_index: RuntimeIndex | None = None
_lock = threading.Lock()


def _get_current_version(s3) -> str:
    try:
        resp = s3.get_object(Bucket=S3_BUCKET, Key="current.json")
    except ClientError as e:
        raise RuntimeError(
            f"Cannot read s3://{S3_BUCKET}/current.json — has the precompute "
            f"pipeline run yet? underlying error: {e}"
        ) from e
    current = json.loads(resp["Body"].read())
    version = current.get("version")
    if not version:
        raise RuntimeError(f"current.json missing 'version' field: {current!r}")
    return version


def _get_json(s3, key: str) -> Any:
    resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return json.loads(resp["Body"].read())


def load() -> RuntimeIndex:
    """Load the live runtime index from S3. Thread-safe; cached after first call."""
    global _index
    if _index is not None:
        return _index
    with _lock:
        if _index is not None:
            return _index
        s3 = boto3.client("s3", region_name=REGION)
        version = _get_current_version(s3)
        prefix = f"versions/{version}"
        log.info("Loading runtime index from s3://%s/%s/", S3_BUCKET, prefix)
        alias_meta = _get_json(s3, f"{prefix}/alias_meta.json")
        canonical_meta = _get_json(s3, f"{prefix}/canonical_meta.json")
        _index = RuntimeIndex(
            version=version,
            alias_meta=alias_meta,
            canonical_meta=canonical_meta,
        )
        log.info("  loaded %s: %d aliases, %d canonicals",
                 version, len(alias_meta), len(canonical_meta))
        return _index
