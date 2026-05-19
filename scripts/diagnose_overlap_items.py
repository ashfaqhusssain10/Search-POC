"""Diagnose search results for Supabase items that have an exact-name twin
in DynamoDB (case-insensitive name match).

For these 120-or-so items the "right answer" is unambiguous — the canonical
with the same name. We expect top-1 to be that canonical at very high score.
Anything else here is a real ranking issue (filter mis-fire, form mismatch,
or embedding noise pulling a different dish above the obvious match).

Writes diagnostics/overlap_quality.csv plus a summary.

Usage:
    python -m scripts.diagnose_overlap_items
"""

from __future__ import annotations

import csv
import logging
from collections import Counter
from pathlib import Path

from core.connections import close_connections, neo4j_session
from scripts.search_v4 import search_items_v4

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

OUT_DIR = Path("diagnostics")
OUT_PATH = OUT_DIR / "overlap_quality.csv"
BATCH = 50


def fetch_overlap_names() -> list[tuple[str, str]]:
    """Return (supabase_name, dynamodb_name) pairs where lowercased names match.

    Supabase name is what the user picks in the UI; DynamoDB name is the
    canonical we expect top-1 to be.
    """
    with neo4j_session() as session:
        dyn = {r["name"].strip().lower(): r["name"]
               for r in session.run("MATCH (i:Item {source:'dynamodb'}) RETURN i.name AS name")
               if r["name"]}
        sup = {r["name"].strip().lower(): r["name"]
               for r in session.run("MATCH (i:Item {source:'supabase'}) RETURN i.name AS name")
               if r["name"]}
    close_connections()
    overlap_keys = sorted(dyn.keys() & sup.keys())
    return [(sup[k], dyn[k]) for k in overlap_keys]


def main() -> None:
    pairs = fetch_overlap_names()
    print(f"Diagnosing {len(pairs)} overlap items (exact-name twins).\n")

    sup_names = [s for s, _ in pairs]
    expected_by_sup = {s.lower().strip(): d for s, d in pairs}

    OUT_DIR.mkdir(exist_ok=True)
    counter: Counter[str] = Counter()
    misses: list[tuple[str, str, str, float]] = []

    with OUT_PATH.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "supabase_name", "expected_canonical", "status", "top1_name",
            "top1_score", "top1_form", "top1_category",
            "top2_name", "top2_score", "top3_name", "top3_score",
        ])

        for start in range(0, len(sup_names), BATCH):
            batch = sup_names[start : start + BATCH]
            results = search_items_v4(batch, top_k=3)
            for r in results:
                expected = expected_by_sup[r.query_item.lower().strip()]

                if not r.hits:
                    status = "EMPTY"
                    counter[status] += 1
                    w.writerow([r.query_item, expected, status, "", "", "", "", "", "", "", ""])
                    misses.append((r.query_item, expected, "(no hits)", 0.0))
                    continue

                top = r.hits[0]
                if top.name.strip().lower() == expected.strip().lower():
                    status = "TOP1_OK"
                elif any(h.name.strip().lower() == expected.strip().lower() for h in r.hits):
                    status = "TOP3_OK"   # expected canonical present but not at #1
                else:
                    status = "MISS"
                    misses.append((r.query_item, expected, top.name, top.score))
                counter[status] += 1

                t2 = r.hits[1] if len(r.hits) > 1 else None
                t3 = r.hits[2] if len(r.hits) > 2 else None
                w.writerow([
                    r.query_item, expected, status, top.name, f"{top.score:.4f}",
                    top.form or "", top.category or "",
                    t2.name if t2 else "", f"{t2.score:.4f}" if t2 else "",
                    t3.name if t3 else "", f"{t3.score:.4f}" if t3 else "",
                ])
            print(f"  Processed {start + len(batch):>3} / {len(sup_names)}")

    total = sum(counter.values())
    print()
    print(f"=== Overlap diagnostic ({total} items) ===")
    order = ["TOP1_OK", "TOP3_OK", "MISS", "EMPTY"]
    for k in order:
        n = counter.get(k, 0)
        pct = 100 * n / total if total else 0
        bar = "█" * int(pct / 2)
        print(f"  {k:<8} {n:>4}  ({pct:5.1f}%)  {bar}")

    if misses:
        print()
        print(f"=== {len(misses)} misses (expected canonical NOT in top-3) ===")
        for sup_name, expected, top_name, top_score in misses[:30]:
            print(f"  '{sup_name}'  →  got '{top_name}' ({top_score:.3f})  "
                  f"expected '{expected}'")
        if len(misses) > 30:
            print(f"  ... and {len(misses) - 30} more (see CSV)")

    print()
    print(f"CSV: {OUT_PATH}")


if __name__ == "__main__":
    main()
