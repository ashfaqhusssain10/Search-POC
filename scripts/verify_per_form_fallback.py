"""End-to-end check: run all 28 scenarios through the production fallback
(per-form thresholds) and tally rescues + any obviously bad pairs.

Reads the same SCENARIOS list as shadow_fallback_test.py for parity.
"""
from __future__ import annotations

from collections import defaultdict

from scripts.shadow_fallback_test import SCENARIOS
from scripts.search_v5 import search_platters_v5

TOP_N = 3


def main() -> None:
    print(f"Running {len(SCENARIOS)} scenarios × 2 rankers (production path, per-form thresholds)")
    rescues_by_pair: dict[tuple[str, str], list[float]] = defaultdict(list)
    summary = {"current": [0, 0, 0], "coverage_dominant": [0, 0, 0]}  # uncovered, rescued, direct

    for label, dishes in SCENARIOS:
        for ranker in ("current", "coverage_dominant"):
            platters = search_platters_v5(
                dishes, top_k_per_item=5, top_n=TOP_N,
                ranker=ranker, enable_fallback=True,
            )
            for p in platters:
                for m in p.dish_matches:
                    if m.is_substitute:
                        summary[ranker][1] += 1
                        rescues_by_pair[(m.query_item, m.matched_canonical or "")].append(m.score)
                    elif m.matched_canonical:
                        summary[ranker][2] += 1
                    else:
                        summary[ranker][0] += 1

    print()
    print("=" * 80)
    print(f"{'ranker':<22}{'direct':>10}{'rescues':>10}{'uncovered':>12}")
    print("-" * 80)
    for r, (unc, res, dir_) in summary.items():
        print(f"{r:<22}{dir_:>10}{res:>10}{unc:>12}")

    print()
    print("=" * 80)
    print("Unique rescues observed (query → substitute, score)")
    print("=" * 80)
    for (q, sub), scores in sorted(rescues_by_pair.items()):
        avg = sum(scores) / len(scores)
        print(f"  {q:<35} → {sub:<35} {avg:.3f}  (n={len(scores)})")


if __name__ == "__main__":
    main()
