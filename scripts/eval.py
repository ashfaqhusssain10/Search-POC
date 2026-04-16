"""Eval harness: run the 20 frozen eval queries through search_platters and report.

Usage:
    python -m scripts.eval                  # run all, print table
    python -m scripts.eval --json out.json  # also dump full results to JSON
    python -m scripts.eval --diff prev.json # diff current run vs a prior JSON dump

Output columns per query:
    ID  | label | matched/query | top-3 platters (coverage%)

Queries are frozen in EVAL_QUERIES below. To edit, update eval_queries.md first,
then mirror the change here.
"""

import argparse
import json
import logging
from pathlib import Path

from scripts.search import search_platters
from core.connections import close_connections

logging.getLogger().setLevel(logging.WARNING)  # silence search.py INFO noise

# ---------------------------------------------------------------------------
# Frozen eval set (mirrors .planning/search_eval/eval_queries.md)
# ---------------------------------------------------------------------------

EVAL_QUERIES: list[dict[str, str]] = [
    {"id": "Q1",  "label": "North Indian veg lunch",           "query": "Paneer Butter Masala, Veg Biryani, Pulka, Raitha"},
    {"id": "Q2",  "label": "North Indian non-veg lunch",       "query": "Butter Chicken Masala, Chicken Pulao, Pulka, Raitha"},
    {"id": "Q3",  "label": "South Indian breakfast",           "query": "Idly, Vada, Sambar, Peanut Chutney, Pongal"},
    {"id": "Q4",  "label": "Telugu traditional lunch",         "query": "Pulihora, Pappu Charu Annam, Bhendi Peanut Fry, Palak Dal, Plain Curd"},
    {"id": "Q5",  "label": "Biryani-centric non-veg",          "query": "Chicken Biryani, Raitha, Green Salad"},
    {"id": "Q6",  "label": "Mixed veg + non-veg feast",        "query": "Paneer Butter Masala, Butter Chicken Masala, Chicken Pulao, Veg Biryani"},
    {"id": "Q7",  "label": "Starters only",                    "query": "Chilli Chicken, VEG Manchuria, Cut Mirchi Bajji"},
    {"id": "Q8",  "label": "Alias: paneer variations",         "query": "Paneer Makhani, Kaju Paneer Butter Masala, Pulka, Veg Biryani"},
    {"id": "Q9",  "label": "Alias: biryani variations",        "query": "Chicken Dum Biryani, Chicken Fry Piece Biryani, Raitha"},
    {"id": "Q10", "label": "Alias: butter chicken variations", "query": "Chicken Tikka Masala, Butter Chicken Curry, Pulka, Chicken Pulao"},
    {"id": "Q11", "label": "Casual phrasing",                  "query": "paneer curry, chicken curry, rice, curd"},
    {"id": "Q12", "label": "Kids party",                       "query": "Chicken Pulao, Green Salad, Tomato Ketchup, Cookie - 2pc"},
    {"id": "Q13", "label": "Full veg feast (8 items)",         "query": "Paneer Butter Masala, Veg Biryani, VEG Pulao, VEG Manchuria, Sambar, Pulka, Raitha, Plain Curd"},
    {"id": "Q14", "label": "Full non-veg feast (8 items)",     "query": "Paneer Butter Masala, Butter Chicken Masala, Chilli Chicken, Chicken Pulao, Veg Biryani, Pulka, Raitha, Green Salad"},
    {"id": "Q15", "label": "Minimal rice + curry",             "query": "Jeera rice, Paneer Butter Masala, Butter Chicken Masala"},
    {"id": "Q16", "label": "Spec example (catalog gap)",       "query": "Chicken Fried Drumsticks, Dal Makhani, Garlic Naan"},
    {"id": "Q17", "label": "Sweets + breakfast",               "query": "Gulab Jamun, Kesaribath, Badam Milk, Idly, Vada"},
    {"id": "Q18", "label": "Regional Telugu festive",          "query": "Pulihora, Nethi Bobatlu, Cut Mirchi Bajji, Palak Dal, Raitha"},
    {"id": "Q19", "label": "Negative / out-of-catalog",        "query": "Sushi, Ramen, Pizza"},
    {"id": "Q20", "label": "Single item",                      "query": "Chicken Biryani"},
]


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run_query(q: dict[str, str]) -> dict:
    """Execute one query and capture a compact result record."""
    results = search_platters(q["query"])
    top3 = [
        {
            "rank": i + 1,
            "name": r.name,
            "coverage_ratio": r.coverage_ratio,
            "skeleton_coverage_score": r.skeleton_coverage_score,
            "final_score": r.final_score,
            "matched_communities": r.matched_communities,
            "query_community_count": r.query_community_count,
            "total_communities": r.total_communities,
            "matched_community_names": r.matched_community_names,
            "item_to_community": r.item_to_community,
            "item_to_category": r.item_to_category,
            "query_category_counts": r.query_category_counts,
            "platter_category_counts": r.platter_category_counts,
            "matched_query_categories": r.matched_query_categories,
            "missing_query_categories": r.missing_query_categories,
            "suggested_alternatives": r.suggested_alternatives,
        }
        for i, r in enumerate(results[:3])
    ]
    return {
        "id": q["id"],
        "label": q["label"],
        "query": q["query"],
        "returned_count": len(results),
        "top3": top3,
    }


def format_row(rec: dict) -> str:
    """One-line summary per query for the table view."""
    if not rec["top3"]:
        top_str = "(no results)"
    else:
        parts = [
            f"{t['name'][:24]} (cov {t['coverage_ratio']:.0%}, fit {t['skeleton_coverage_score']:.0%})"
            for t in rec["top3"]
        ]
        top_str = " | ".join(parts)
    return f"{rec['id']:4s}  {rec['label'][:36]:36s}  →  {top_str}"


def format_detail(rec: dict) -> str:
    """Expanded per-query block with item→community mapping."""
    lines = [f"\n{'='*100}", f"{rec['id']}  {rec['label']}", f"  query: {rec['query']}"]
    if not rec["top3"]:
        lines.append("  (no platters returned)")
        return "\n".join(lines)
    for t in rec["top3"]:
        lines.append(
            f"  #{t['rank']}  {t['name']}  "
            f"[{t['matched_communities']}/{t['query_community_count']} covered, "
            f"{t['coverage_ratio']:.0%} coverage, "
            f"{t['skeleton_coverage_score']:.0%} menu fit, "
            f"final={t['final_score']:.2f}]"
        )
        lines.append(
            f"      query skeleton={t['query_category_counts']}  "
            f"platter skeleton={t['platter_category_counts']}"
        )
        lines.append(
            f"      matched categories={t['matched_query_categories']}  "
            f"missing categories={t['missing_query_categories']}"
        )
        for item, comm in t["item_to_community"].items():
            marker = "✓" if comm and comm in t["matched_community_names"] else ("~" if comm else "✗")
            alt = t["suggested_alternatives"].get(item)
            category = t["item_to_category"].get(item)
            category_suffix = f" [{category}]" if category else ""
            alt_str = f"  (closest in platter: {alt})" if alt else ""
            lines.append(f"      {marker} {item}{category_suffix} → {comm}{alt_str}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def diff_runs(current: list[dict], prior_path: Path) -> None:
    prior = json.loads(prior_path.read_text())
    prior_by_id = {r["id"]: r for r in prior}

    changed = 0
    print(f"\n{'='*100}\nDIFF vs {prior_path.name}\n{'='*100}")
    for curr in current:
        p = prior_by_id.get(curr["id"])
        if not p:
            continue
        curr_names = [t["name"] for t in curr["top3"]]
        prior_names = [t["name"] for t in p["top3"]]
        if curr_names != prior_names:
            changed += 1
            print(f"\n{curr['id']} {curr['label']}")
            print(f"  prior:   {prior_names or '(none)'}")
            print(f"  current: {curr_names or '(none)'}")
    print(f"\n{changed}/{len(current)} queries changed top-3 ordering.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, help="Write full results to this JSON path")
    ap.add_argument("--diff", type=Path, help="Compare current run vs this prior JSON dump")
    ap.add_argument("--detail", action="store_true", help="Print per-query item→community breakdown")
    args = ap.parse_args()

    print(f"Running {len(EVAL_QUERIES)} eval queries...\n")
    records: list[dict] = []
    for q in EVAL_QUERIES:
        try:
            rec = run_query(q)
        except Exception as e:
            rec = {"id": q["id"], "label": q["label"], "query": q["query"],
                   "returned_count": 0, "top3": [], "error": str(e)}
        records.append(rec)
        print(format_row(rec))

    nonempty = sum(1 for r in records if r["top3"])
    print(f"\n{nonempty}/{len(records)} queries returned ≥1 platter.")

    if args.detail:
        for rec in records:
            print(format_detail(rec))

    if args.json:
        args.json.write_text(json.dumps(records, indent=2))
        print(f"\nWrote full results → {args.json}")

    if args.diff:
        diff_runs(records, args.diff)

    close_connections()


if __name__ == "__main__":
    main()
