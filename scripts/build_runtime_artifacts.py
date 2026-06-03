"""Build the runtime artifact bundle and upload to S3.

Scrolls Qdrant for both collections, packs vectors + metadata into a
versioned folder in S3, then atomically flips the `current.json` pointer.

Layout produced:
    s3://search-item-item-poc/
        versions/v{N}/
            canonical_vectors.npy
            canonical_names.json
            canonical_meta.json     (name → {form, veg, item_id})
            alias_vectors.npy
            alias_names.json
            alias_meta.json
            manifest.json
        current.json                ({"version": "v{N}", "promoted_at": "..."})

Atomic-swap protocol: all version files uploaded + verified before
current.json is overwritten. Old versions are kept for rollback.

CLI:
    python -m scripts.build_runtime_artifacts
    python -m scripts.build_runtime_artifacts --version v3   # explicit version
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
from io import BytesIO
from typing import Any

import boto3
import numpy as np
from botocore.exceptions import ClientError

from core.connections import close_connections, get_qdrant_client
from scripts.search_v4 import ALIAS_COLLECTION, CANONICAL_COLLECTION

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

REGION = os.getenv("AWS_REGION", "ap-south-1")
S3_BUCKET = "search-item-item-poc"
SCROLL_BATCH = 256


def _scroll(qdrant, collection: str) -> tuple[np.ndarray, list[str], dict[str, dict[str, Any]]]:
    """Returns (vectors_matrix, ordered_names, name_to_meta)."""
    vectors: list[list[float]] = []
    names: list[str] = []
    meta: dict[str, dict[str, Any]] = {}
    next_offset = None
    while True:
        points, next_offset = qdrant.scroll(
            collection_name=collection,
            offset=next_offset,
            limit=SCROLL_BATCH,
            with_payload=True,
            with_vectors=True,
        )
        for p in points:
            payload = p.payload or {}
            name = payload.get("name")
            if not name or name in meta:
                continue
            vectors.append(p.vector)
            names.append(name)
            meta[name] = {
                "item_id": payload.get("item_id"),
                "form": payload.get("form"),
                "veg": payload.get("veg_type"),
            }
        if next_offset is None:
            break
    matrix = np.asarray(vectors, dtype=np.float32)
    return matrix, names, meta


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _npy_bytes(arr: np.ndarray) -> bytes:
    buf = BytesIO()
    np.save(buf, arr, allow_pickle=False)
    return buf.getvalue()


def _next_version(s3, bucket: str) -> str:
    """Read existing current.json (if any) and bump to v{N+1}. Default v1."""
    try:
        resp = s3.get_object(Bucket=bucket, Key="current.json")
        current = json.loads(resp["Body"].read())
        existing = current.get("version", "")
        if existing.startswith("v"):
            n = int(existing[1:]) + 1
            return f"v{n}"
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchKey":
            raise
    return "v1"


def _put_object(s3, bucket: str, key: str, body: bytes, content_type: str) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)


def _verify_uploaded(s3, bucket: str, prefix: str, expected_sha256: dict[str, str]) -> None:
    """Re-download each artifact and confirm SHA256 matches. Fails loudly."""
    for filename, expected in expected_sha256.items():
        resp = s3.get_object(Bucket=bucket, Key=f"{prefix}/{filename}")
        actual = _sha256_bytes(resp["Body"].read())
        if actual != expected:
            raise RuntimeError(
                f"Verification failed for {filename}: "
                f"expected {expected[:12]}…, got {actual[:12]}…"
            )
    log.info("  ✓ all %d artifacts verified", len(expected_sha256))


def build_and_upload(version: str | None = None) -> str:
    qdrant = get_qdrant_client()

    log.info("Scrolling Qdrant for canonical collection…")
    canon_matrix, canon_names, canon_meta = _scroll(qdrant, CANONICAL_COLLECTION)
    log.info("  %d canonicals, shape=%s", len(canon_names), canon_matrix.shape)

    log.info("Scrolling Qdrant for alias collection…")
    alias_matrix, alias_names, alias_meta = _scroll(qdrant, ALIAS_COLLECTION)
    log.info("  %d aliases, shape=%s", len(alias_names), alias_matrix.shape)

    s3 = boto3.client("s3", region_name=REGION)
    if version is None:
        version = _next_version(s3, S3_BUCKET)
    prefix = f"versions/{version}"
    log.info("Uploading to s3://%s/%s/", S3_BUCKET, prefix)

    # Serialize everything to bytes in memory so we can hash before upload.
    artifacts: dict[str, tuple[bytes, str]] = {
        "canonical_vectors.npy": (_npy_bytes(canon_matrix), "application/octet-stream"),
        "canonical_names.json":  (json.dumps(canon_names).encode(), "application/json"),
        "canonical_meta.json":   (json.dumps(canon_meta).encode(), "application/json"),
        "alias_vectors.npy":     (_npy_bytes(alias_matrix), "application/octet-stream"),
        "alias_names.json":      (json.dumps(alias_names).encode(), "application/json"),
        "alias_meta.json":       (json.dumps(alias_meta).encode(), "application/json"),
    }

    sha256_map = {fn: _sha256_bytes(body) for fn, (body, _) in artifacts.items()}

    manifest = {
        "version": version,
        "computed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_canonicals": len(canon_names),
        "n_aliases": len(alias_names),
        "vector_dim": int(canon_matrix.shape[1]) if canon_matrix.size else 0,
        "sha256": sha256_map,
        "qdrant_collections": {
            "canonical": CANONICAL_COLLECTION,
            "alias": ALIAS_COLLECTION,
        },
    }
    manifest_body = json.dumps(manifest, indent=2).encode()
    artifacts["manifest.json"] = (manifest_body, "application/json")
    # manifest sha is itself written into manifest? No — chicken-and-egg. Skip.

    # Upload all artifacts.
    for filename, (body, ct) in artifacts.items():
        _put_object(s3, S3_BUCKET, f"{prefix}/{filename}", body, ct)
        log.info("  uploaded %s (%d bytes)", filename, len(body))

    # Verify by reading back and re-hashing.
    log.info("Verifying upload…")
    _verify_uploaded(s3, S3_BUCKET, prefix, sha256_map)

    # Atomic flip: ONLY now write current.json.
    current_body = json.dumps({
        "version": version,
        "promoted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2).encode()
    _put_object(s3, S3_BUCKET, "current.json", current_body, "application/json")
    log.info("Promoted current.json → %s", version)

    return version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", type=str, default=None,
                        help="Explicit version label (default: auto-increment from current.json)")
    args = parser.parse_args()
    try:
        v = build_and_upload(version=args.version)
        log.info("Done. Live version: %s", v)
    finally:
        close_connections()
