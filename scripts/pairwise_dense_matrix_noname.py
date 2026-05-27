"""Pairwise dense similarity matrix on the NO-NAME test collections.

Every Supabase alias × every DynamoDB canonical (~190K rows), scored via
Qdrant cosine with NO filter applied. Form and veg fields included on both
sides so downstream threshold analysis can slice by form-family / veg.

Mirrors scripts.pairwise_dense_matrix but reads from the _noname collections
and emits richer columns.

Output: diagnostics/pairwise_dense_matrix_noname.csv
        Columns: alias, alias_form, alias_veg,
                 canonical, canonical_form, canonical_veg,
                 dense_score

Usage:
    python -m scripts.pairwise_dense_matrix_noname
"""

from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from core.connections import close_connections, get_qdrant_client

ALIAS_COLLECTION = "searchpoc_aliases_noname"
CANONICAL_COLLECTION = "searchpoc_canonicals_noname"
OUT_CSV = Path("diagnostics/pairwise_dense_matrix_noname.csv")
MAX_WORKERS = 16
TOP_K = 999


def _scroll_with_vectors(qdrant, collection: str):
    out = []
    nxt = None
    while True:
        points, nxt = qdrant.scroll(
            collection_name=collection, offset=nxt, limit=200,
            with_payload=True, with_vectors=True,
        )
        for p in points:
            n = (p.payload.get("name") if p.payload else None) or ""
            if not n:
                continue
            out.append({
                "name": n,
                "veg": (p.payload.get("veg_type") or "").upper(),
                "form": (p.payload.get("form") or "").lower(),
                "vector": np.asarray(p.vector, dtype=np.float32),
            })
        if nxt is None:
            break
    return out


def _scroll_payload(qdrant, collection: str):
    out = []
    nxt = None
    while True:
        points, nxt = qdrant.scroll(
            collection_name=collection, offset=nxt, limit=200,
            with_payload=True, with_vectors=False,
        )
        for p in points:
            n = (p.payload.get("name") if p.payload else None) or ""
            if not n:
                continue
            out.append({
                "name": n,
                "veg": (p.payload.get("veg_type") or "").upper(),
                "form": (p.payload.get("form") or "").lower(),
            })
        if nxt is None:
            break
    return out


def main() -> None:
    qdrant = get_qdrant_client()
    print("Fetching alias vectors…")
    aliases = _scroll_with_vectors(qdrant, ALIAS_COLLECTION)
    print(f"  {len(aliases)} aliases")

    print("Fetching canonical payloads…")
    canons = _scroll_payload(qdrant, CANONICAL_COLLECTION)
    print(f"  {len(canons)} canonicals")
    canon_meta = {c["name"]: c for c in canons}
    canonical_names = [c["name"] for c in canons]

    print(f"Querying Qdrant pairwise scores ({MAX_WORKERS} workers)…")

    def _score_one(idx: int):
        a = aliases[idx]
        hits = qdrant.query_points(
            collection_name=CANONICAL_COLLECTION,
            query=a["vector"].tolist(),
            limit=TOP_K,
            score_threshold=0.0,
            with_payload=True,
        ).points
        return a["name"], {h.payload.get("name") or "": float(h.score) for h in hits}

    all_scores: dict[str, dict[str, float]] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = [pool.submit(_score_one, i) for i in range(len(aliases))]
        for fut in as_completed(futs):
            name, scores = fut.result()
            all_scores[name] = scores
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(aliases)}")

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with OUT_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["alias", "alias_form", "alias_veg",
                    "canonical", "canonical_form", "canonical_veg",
                    "dense_score"])
        for a in aliases:
            scores = all_scores.get(a["name"], {})
            for cname in canonical_names:
                c = canon_meta[cname]
                w.writerow([
                    a["name"], a["form"], a["veg"],
                    cname, c["form"], c["veg"],
                    f"{scores.get(cname, 0.0):.4f}",
                ])
                n += 1

    print(f"\nWrote {n} rows to {OUT_CSV}")
    print(f"  ({len(aliases)} aliases × {len(canonical_names)} canonicals)")
    close_connections()


if __name__ == "__main__":
    main()
