"""Pairwise dense similarity matrix — every Supabase alias × every DynamoDB
canonical, scored via Qdrant (cosine, no filter).

Output: diagnostics/pairwise_dense_matrix.csv
        Columns: alias, canonical, dense_score
        Every pair included, including 0.0 scores. ~774 × 246 = ~190K rows.

Use Qdrant for the scoring so the numbers are exactly what production sees —
no local approximation of cosine, no normalization drift.

Strategy: one query per alias with limit=999 (no filter). Each response
contains scores for all 246 canonicals. Total ~774 queries, ~2 min.
Parallelized so it finishes faster.

Usage:
    python -m scripts.pairwise_dense_matrix
"""

from __future__ import annotations

import csv
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from core.connections import close_connections, get_qdrant_client

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ALIAS_COLLECTION = "searchpoc_aliases"
CANONICAL_COLLECTION = "searchpoc_canonicals"
OUT_CSV = Path("diagnostics/pairwise_dense_matrix.csv")
MAX_WORKERS = 16
TOP_K = 999  # >> #canonicals (246) so we get every pair back


def _scroll_aliases(qdrant) -> list[tuple[str, np.ndarray]]:
    """Pull every alias name + its dense vector."""
    out: list[tuple[str, np.ndarray]] = []
    next_offset = None
    while True:
        points, next_offset = qdrant.scroll(
            collection_name=ALIAS_COLLECTION,
            offset=next_offset,
            limit=200,
            with_payload=True,
            with_vectors=True,
        )
        for p in points:
            n = p.payload.get("name") if p.payload else None
            if not n:
                continue
            out.append((n, np.asarray(p.vector, dtype=np.float32)))
        if next_offset is None:
            break
    return out


def _canonical_names(qdrant) -> list[str]:
    """Pull every canonical name so we can pad any missing pairs with 0.0."""
    out: list[str] = []
    next_offset = None
    while True:
        points, next_offset = qdrant.scroll(
            collection_name=CANONICAL_COLLECTION,
            offset=next_offset,
            limit=200,
            with_payload=True,
            with_vectors=False,
        )
        for p in points:
            n = p.payload.get("name") if p.payload else None
            if n:
                out.append(n)
        if next_offset is None:
            break
    return out


def main() -> None:
    qdrant = get_qdrant_client()
    print("Fetching alias vectors…")
    aliases = _scroll_aliases(qdrant)
    print(f"  {len(aliases)} aliases")

    print("Fetching canonical names…")
    canonicals = _canonical_names(qdrant)
    print(f"  {len(canonicals)} canonicals")
    canonical_set = set(canonicals)

    print(f"Querying Qdrant pairwise scores ({MAX_WORKERS} workers)…")

    def _score_one(alias_idx: int) -> tuple[str, dict[str, float]]:
        name, vec = aliases[alias_idx]
        hits = qdrant.query_points(
            collection_name=CANONICAL_COLLECTION,
            query=vec.tolist(),
            limit=TOP_K,
            score_threshold=0.0,  # do NOT clip; we want zeros and negatives
            with_payload=True,
        ).points
        return name, {h.payload.get("name") or "": float(h.score) for h in hits}

    all_scores: dict[str, dict[str, float]] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_score_one, i) for i in range(len(aliases))]
        for fut in as_completed(futures):
            alias_name, scores = fut.result()
            all_scores[alias_name] = scores
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(aliases)}")

    # Write the full matrix — every (alias × canonical) pair, including 0.0
    # for pairs that didn't appear in the top-K (shouldn't happen with
    # TOP_K=999 and 246 canonicals, but safe anyway).
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    n_rows = 0
    with OUT_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["alias", "canonical", "dense_score"])
        for alias_name, _ in aliases:
            alias_scores = all_scores.get(alias_name, {})
            for c in canonicals:
                score = alias_scores.get(c, 0.0)
                w.writerow([alias_name, c, f"{score:.4f}"])
                n_rows += 1

    print(f"\nWrote {n_rows} rows to {OUT_CSV}")
    print(f"  ({len(aliases)} aliases × {len(canonicals)} canonicals)")
    close_connections()


if __name__ == "__main__":
    main()
