"""Diagnose how many platters have empty slots — categories that exist via
HAS_CATEGORY (with items_limit > 0) but have no items wired via CONTAINS_ITEM.

For each platter we report:
  - total declared slots (sum of items_limit across HAS_CATEGORY)
  - filled slots (# of distinct items wired via CONTAINS_ITEM)
  - empty categories (PlatterCategory nodes with items_limit > 0 but 0 items)

Console summary:
  - Histogram of fill rate (filled / declared)
  - Top-25 most-empty platters
  - Per-category breakdown: which categories are most often empty?

Writes diagnostics/platter_slot_gaps.csv with one row per platter.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from core.connections import close_connections, neo4j_session

OUT_CSV = Path("diagnostics/platter_slot_gaps.csv")

QUERY = """
MATCH (p:Platter)
OPTIONAL MATCH (p)-[:HAS_CATEGORY]->(pc:PlatterCategory)
WITH p, pc
OPTIONAL MATCH (pc)-[:CONTAINS_ITEM]->(i:Item)
WITH p, pc,
     count(DISTINCT i) AS items_in_category
RETURN p.id AS platter_id,
       p.name AS platter_name,
       p.type AS platter_type,
       collect(DISTINCT {
         category_family: coalesce(pc.category_family, 'Other'),
         category_name: pc.category_name_raw,
         declared_slots: coalesce(pc.items_limit, 0),
         filled_items: items_in_category
       }) AS categories
"""


def main() -> None:
    rows: list[dict] = []
    empty_by_family: dict[str, int] = defaultdict(int)
    total_by_family: dict[str, int] = defaultdict(int)

    with neo4j_session() as s:
        for r in s.run(QUERY):
            platter_id = r["platter_id"]
            platter_name = r["platter_name"]
            platter_type = r["platter_type"]
            cats = [c for c in (r["categories"] or []) if c.get("declared_slots", 0) > 0]
            declared = sum(c["declared_slots"] for c in cats)
            filled_items = sum(min(c["filled_items"], c["declared_slots"]) for c in cats)
            empty_categories = [c for c in cats if c["filled_items"] == 0]

            for c in cats:
                fam = c["category_family"] or "Other"
                total_by_family[fam] += 1
                if c["filled_items"] == 0:
                    empty_by_family[fam] += 1

            rows.append({
                "platter_id": platter_id,
                "platter_name": platter_name,
                "platter_type": platter_type or "",
                "n_categories": len(cats),
                "declared_slots": declared,
                "filled_slots_estimate": filled_items,
                "empty_categories": len(empty_categories),
                "fill_rate": (filled_items / declared) if declared else 0.0,
                "empty_category_families": ",".join(
                    sorted({c["category_family"] or "Other" for c in empty_categories})
                ),
            })

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["platter_id", "platter_name", "platter_type",
                    "n_categories", "declared_slots", "filled_slots_estimate",
                    "empty_categories", "fill_rate", "empty_category_families"])
        for r in rows:
            w.writerow([
                r["platter_id"], r["platter_name"], r["platter_type"],
                r["n_categories"], r["declared_slots"], r["filled_slots_estimate"],
                r["empty_categories"], f"{r['fill_rate']:.2f}",
                r["empty_category_families"],
            ])
    print(f"Wrote {len(rows)} rows to {OUT_CSV}\n")

    # ── Summary ─────────────────────────────────────────────────────────────
    print("=" * 95)
    print(f"PLATTER FILL-RATE HISTOGRAM   (n={len(rows)} platters)")
    print("=" * 95)
    bands = [(0.0, 0.01), (0.01, 0.25), (0.25, 0.5), (0.5, 0.75),
             (0.75, 0.99), (0.99, 1.01)]
    labels = ["completely empty (0%)", "1–25% filled", "25–50% filled",
              "50–75% filled", "75–99% filled", "100% filled"]
    for (lo, hi), lab in zip(bands, labels):
        n = sum(1 for r in rows if lo <= r["fill_rate"] < hi)
        bar = "█" * int(n / max(1, len(rows) // 30))
        print(f"  {lab:<26} {n:>4}  {bar}")

    total_platters = len(rows)
    fully_filled = sum(1 for r in rows if r["fill_rate"] >= 0.99)
    partially_filled = sum(1 for r in rows if 0 < r["fill_rate"] < 0.99)
    completely_empty = sum(1 for r in rows if r["fill_rate"] == 0)
    print()
    print(f"  Fully filled (≥99%):     {fully_filled}/{total_platters} ({fully_filled/total_platters*100:.1f}%)")
    print(f"  Partially filled:        {partially_filled}/{total_platters} ({partially_filled/total_platters*100:.1f}%)")
    print(f"  Completely empty (0%):   {completely_empty}/{total_platters} ({completely_empty/total_platters*100:.1f}%)")

    # Top-25 emptiest (only those with declared slots)
    nonempty_decl = [r for r in rows if r["declared_slots"] > 0]
    nonempty_decl.sort(key=lambda r: (r["fill_rate"], -r["declared_slots"]))
    print()
    print("=" * 95)
    print("BOTTOM 25 — platters with the lowest fill rate")
    print("=" * 95)
    print(f"  {'fill':>5}  {'filled/declared':>16}  {'empty cats':>11}  platter")
    for r in nonempty_decl[:25]:
        empty_cats = r["empty_category_families"]
        print(f"  {r['fill_rate']*100:>4.0f}%  "
              f"{r['filled_slots_estimate']}/{r['declared_slots']:<14}  "
              f"{r['empty_categories']:>11}  {r['platter_name']:<45} ({empty_cats})")

    # Category-level: which slot types are most often empty across the catalog?
    print()
    print("=" * 95)
    print("EMPTY-CATEGORY FREQUENCY by family (across all platter-category instances)")
    print("=" * 95)
    print(f"  {'family':<22}{'empty':>8}{'total':>8}{'%':>8}")
    cats_sorted = sorted(total_by_family.items(), key=lambda kv: -empty_by_family.get(kv[0], 0))
    for fam, total in cats_sorted:
        empty = empty_by_family.get(fam, 0)
        if total == 0:
            continue
        print(f"  {fam:<22}{empty:>8}{total:>8}{empty/total*100:>7.1f}%")

    close_connections()


if __name__ == "__main__":
    main()
