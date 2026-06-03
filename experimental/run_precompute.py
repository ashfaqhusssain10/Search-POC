"""Orchestrator: end-to-end precompute pipeline.

Runs, in order:
  1. precompute_alias_resolution → diagnostics/alias_resolution.json
  2. upload_resolution_to_ddb     → DynamoDB Item-Item-Similarity-Search
  3. build_runtime_artifacts      → S3 search-item-item-poc/versions/v{N}/
                                  → atomic flip of current.json

Any step failing aborts the pipeline before the S3 pointer flip,
keeping the previous live version intact.

CLI:
    python -m scripts.run_precompute
    python -m scripts.run_precompute --workers 8
    python -m scripts.run_precompute --skip-resolution    # re-upload existing JSON
"""

from __future__ import annotations

import argparse
import logging
import sys

from scripts.build_runtime_artifacts import build_and_upload
from scripts.precompute_alias_resolution import run as run_precompute
from scripts.upload_resolution_to_ddb import (
    DEFAULT_INPUT,
    upload as upload_to_ddb,
    verify_sample,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


def main(workers: int, skip_resolution: bool) -> None:
    if skip_resolution:
        log.info("=== Step 1/3: precompute_alias_resolution — SKIPPED ===")
    else:
        log.info("=== Step 1/3: precompute_alias_resolution ===")
        run_precompute(workers=workers)

    log.info("=== Step 2/3: upload_resolution_to_ddb ===")
    upload_to_ddb(DEFAULT_INPUT)
    verify_sample(DEFAULT_INPUT)

    log.info("=== Step 3/3: build_runtime_artifacts ===")
    version = build_and_upload()
    log.info("Pipeline complete. Live artifact version: %s", version)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=6,
                        help="Bedrock concurrency for precompute step")
    parser.add_argument("--skip-resolution", action="store_true",
                        help="Skip step 1 (re-use existing diagnostics/alias_resolution.json)")
    args = parser.parse_args()
    try:
        main(workers=args.workers, skip_resolution=args.skip_resolution)
    except Exception as e:
        log.error("Pipeline failed: %s", e)
        sys.exit(1)
