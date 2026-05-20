"""Diagnostic-only: run realistic PM-style platter queries through search_v5
and print what comes back, with NO changes to the search logic.

Goal: see exactly which behaviors feel right and which don't before we
touch the algorithm. Reports per scenario:
  - the v4 per-item matches (so we know what canonicals are even available)
  - the top-3 platters returned by v5
  - flags surfaced behavior: cross-veg queries, hidden-but-good substitutes,
    coverage shortfalls, and specificity-vs-coverage tension.

Usage:
    python -m scripts.test_platter_scenarios
"""

from __future__ import annotations

import logging

import scripts.search_v4 as v4
from scripts.search_v5 import search_platters_v5

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")


SCENARIOS: list[tuple[str, list[str], str]] = [
    # (label, dishes, expected behavior the PM would describe)
    ("Mixed veg + non-veg (the bug we just saw)",
     ["Achari Chicken Curry", "Mutton Dum Biryani", "Rumali Roti"],
     "Expect: a platter with all 3 (or close subs). User wants a menu containing chicken curry + biryani + roti."),

    ("Strongly-named regional curry",
     ["Achari Chicken Curry"],
     "Achari Chicken Curry top-1 sits at 0.788 — just under the 0.80 floor. Expect platters covering this."),

    ("Clean veg lunch",
     ["Paneer Butter Masala Curry", "Garlic Naan", "Bagara Rice", "Gulab Jamun"],
     "Expect: small focused meal box with all 4."),

    ("All-veg, simple",
     ["Dal Tadka", "Jeera Rice", "Phulka"],
     "Common North-Indian thali order."),

    ("Pure non-veg",
     ["Mutton Biryani", "Raita", "Gulab Jamun"],
     "Hyderabadi non-veg meal."),

    ("Snacks only",
     ["Samosa", "Chicken 65"],
     "Expect: snack box, NOT a meal box."),

    ("South Indian breakfast",
     ["Idly", "Vada", "Sambar"],
     "Expect: snack/breakfast platters not generic lunch boxes."),

    ("Premium party spread",
     ["Hariyali Paneer Tikka", "Mutton Biryani", "Butter Naan", "Gulab Jamun"],
     "Expect: party platter with all 4."),
]


def _v4_summary(dishes: list[str]) -> dict[str, tuple[str | None, float, str | None]]:
    """For each dish, return (top1_name, top1_score, top1_veg_type). Bypasses
    the display floor so we see what was *available* even if hidden."""
    original_floor = v4.ITEM_SCORE_THRESHOLD
    v4.ITEM_SCORE_THRESHOLD = 0.0
    try:
        results = v4.search_items_v4(dishes, top_k=1)
    finally:
        v4.ITEM_SCORE_THRESHOLD = original_floor

    out: dict[str, tuple[str | None, float, str | None]] = {}
    for r in results:
        if r.hits:
            h = r.hits[0]
            out[r.query_item] = (h.name, h.score, h.veg_type)
        else:
            out[r.query_item] = (None, 0.0, None)
    return out


def _flag(condition: bool, msg: str) -> str:
    return f"  ⚠ {msg}" if condition else ""


def run_scenario(label: str, dishes: list[str], note: str) -> None:
    print("=" * 100)
    print(f"## {label}")
    print(f"   Query: {dishes}")
    print(f"   PM intent: {note}")
    print("-" * 100)

    # v4 summary (what canonicals could potentially be used, regardless of floor)
    summary = _v4_summary(dishes)
    veg_types: set[str] = set()
    below_floor: list[tuple[str, str, float]] = []
    no_hit: list[str] = []

    print("v4 per-dish top-1 (showing everything, ignoring 0.80 floor):")
    for dish, (top_name, score, veg) in summary.items():
        if top_name is None:
            print(f"  ✗ {dish:<30} → NO HIT (filter wiped everything)")
            no_hit.append(dish)
            continue
        marker = "✓" if score >= 0.80 else "~"
        veg_str = veg or "—"
        print(f"  {marker} {dish:<30} → {top_name:<35} ({score:.3f}, {veg_str})")
        if veg:
            veg_types.add(veg)
        if 0.0 < score < 0.80:
            below_floor.append((dish, top_name, score))

    # Run both rankers side-by-side
    for ranker in ("current", "coverage_dominant"):
        print()
        print(f"v5 top platters [ranker={ranker}]:")
        platters = search_platters_v5(dishes, top_k_per_item=5, top_n=3, ranker=ranker)
        if not platters:
            print("  (no platters returned)")
            continue
        for i, p in enumerate(platters, 1):
            veg_label = "VEG" if p.veg is True else "NONVEG" if p.veg is False else "?"
            sk = " · ".join(f"{s.slot_count} {s.family}" for s in p.skeleton) or "—"
            print(f"  #{i} {p.name:<35} [{veg_label}, {p.platter_type}]  "
                  f"coverage={p.matched_count}/{p.total_query_dishes}  "
                  f"quality={p.quality:.2f}  spec={p.specificity:.0%}  score={p.final_score:.2f}")
            print(f"      skeleton: {sk}")
            for m in p.dish_matches:
                if m.matched_canonical:
                    print(f"      ✓ {m.query_item:<28} → {m.matched_canonical} ({m.score:.2f})")
                else:
                    print(f"      ✗ {m.query_item:<28} (not covered)")

    # Heuristic flags
    print()
    print("Issues flagged automatically:")
    flags = [
        _flag(len(veg_types) > 1, f"Cross-veg query: {sorted(veg_types)} — single-veg platters can't cover all dishes"),
        _flag(bool(below_floor), f"{len(below_floor)} dish(es) had a top-1 between 0.0 and 0.80, hidden by display floor"),
        _flag(bool(no_hit), f"{len(no_hit)} dish(es) returned NO hit at all (form/veg filter starved them)"),
        _flag(bool(platters) and max(p.matched_count for p in platters) < len(dishes),
              f"No platter covers all {len(dishes)} dishes — best coverage was "
              f"{max(p.matched_count for p in platters) if platters else 0}/{len(dishes)}"),
        _flag(bool(platters) and platters[0].matched_count < 0.5 * len(dishes) and len(dishes) >= 3,
              "Top platter covers less than half of the user's selection — coverage may be too weak to be useful"),
    ]
    for f in flags:
        if f:
            print(f)
    if not any(flags):
        print("  (none — this scenario behaves as expected)")
    print()


def main() -> None:
    for label, dishes, note in SCENARIOS:
        run_scenario(label, dishes, note)
    print("=" * 100)
    print("Done.")


if __name__ == "__main__":
    main()
