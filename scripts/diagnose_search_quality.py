"""Diagnose top-1 match quality across the entire Supabase master list.

For every Supabase alias item, run an item-to-item search against the
DynamoDB canonical collection and classify the outcome:

  EXACT      : top-1 canonical has the same name as the alias (lowercased)
  NEAR_EXACT : alias name and top-1 share a discriminating token (e.g. dish word)
  SUBSTITUTE : top-1 is in same form/category but a different dish
  WEAK       : top-1 score < 0.5 — low confidence even after filters
  EMPTY      : filters returned no hits (likely catalogue gap)

Writes a CSV (`diagnostics/search_quality.csv`) with one row per alias plus
prints a summary distribution.

Usage:
    python -m scripts.diagnose_search_quality
    python -m scripts.diagnose_search_quality --limit 50    # quick sample
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
from collections import Counter
from pathlib import Path

from scripts.search_v4 import search_items_v4

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

OUT_DIR = Path("diagnostics")
OUT_PATH = OUT_DIR / "search_quality.csv"
WEAK_THRESHOLD = 0.5
NEAR_EXACT_THRESHOLD = 0.85  # if names don't match but score is huge, treat as near-exact


# Stopwords that don't discriminate — ignored when computing shared tokens.
STOPWORDS = {
    "veg", "non", "nonveg", "non-veg", "the", "and", "with", "of", "in",
    "style", "masala", "curry", "fry", "dry", "gravy", "rice", "dish",
    "indian", "south", "north", "special", "mini", "stuffed", "homemade",
    "plain", "tadka", "chatpat",
}


def _tokens(name: str) -> set[str]:
    """Tokenize a dish name into discriminating words."""
    raw = re.findall(r"[a-zA-Z]+", name.lower())
    return {t for t in raw if len(t) > 2 and t not in STOPWORDS}


def classify(alias_name: str, top1_name: str | None, top1_score: float | None) -> str:
    if top1_name is None:
        return "EMPTY"
    if top1_score is not None and top1_score < WEAK_THRESHOLD:
        return "WEAK"
    if alias_name.lower().strip() == top1_name.lower().strip():
        return "EXACT"
    shared = _tokens(alias_name) & _tokens(top1_name)
    if shared:
        return "NEAR_EXACT"
    if top1_score is not None and top1_score >= NEAR_EXACT_THRESHOLD:
        return "NEAR_EXACT"
    return "SUBSTITUTE"


def fetch_alias_names(limit: int | None) -> list[str]:
    from core.connections import close_connections, neo4j_session

    with neo4j_session() as session:
        rows = session.run(
            "MATCH (i:Item {source:'supabase'}) RETURN i.name AS name ORDER BY i.name"
        )
        names = [r["name"] for r in rows if r["name"]]
    close_connections()
    if limit is not None:
        names = names[:limit]
    return names


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="Sample N aliases instead of all (alphabetical).")
    parser.add_argument("--batch", type=int, default=50,
                        help="Items per search batch (default 50).")
    args = parser.parse_args()

    names = fetch_alias_names(args.limit)
    print(f"Running diagnostic on {len(names)} Supabase aliases...\n")

    OUT_DIR.mkdir(exist_ok=True)
    counter: Counter[str] = Counter()

    with OUT_PATH.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "alias", "classification", "top1_name", "top1_score",
            "top1_form", "top1_category", "top1_veg_type",
            "top2_name", "top2_score", "top3_name", "top3_score",
        ])

        for start in range(0, len(names), args.batch):
            batch = names[start : start + args.batch]
            results = search_items_v4(batch, top_k=3)
            for r in results:
                if not r.hits:
                    cls = classify(r.query_item, None, None)
                    counter[cls] += 1
                    writer.writerow([r.query_item, cls, "", "", "", "", "", "", "", "", ""])
                    continue
                top = r.hits[0]
                cls = classify(r.query_item, top.name, top.score)
                counter[cls] += 1
                t2 = r.hits[1] if len(r.hits) > 1 else None
                t3 = r.hits[2] if len(r.hits) > 2 else None
                writer.writerow([
                    r.query_item, cls, top.name, f"{top.score:.4f}",
                    top.form or "", top.category or "", top.veg_type or "",
                    t2.name if t2 else "", f"{t2.score:.4f}" if t2 else "",
                    t3.name if t3 else "", f"{t3.score:.4f}" if t3 else "",
                ])
            print(f"  Processed {start + len(batch):>4} / {len(names)}")

    total = sum(counter.values())
    print()
    print(f"=== Distribution ({total} items) ===")
    order = ["EXACT", "NEAR_EXACT", "SUBSTITUTE", "WEAK", "EMPTY"]
    for k in order:
        n = counter.get(k, 0)
        pct = 100 * n / total if total else 0
        bar = "█" * int(pct / 2)
        print(f"  {k:<11} {n:>4}  ({pct:5.1f}%)  {bar}")
    print()
    print(f"CSV written: {OUT_PATH}")
    print()
    print("Next: inspect SUBSTITUTE/WEAK/EMPTY rows for catalogue gaps vs. ranking issues.")


if __name__ == "__main__":
    main()
