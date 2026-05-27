"""Recalibrate per-form similarity thresholds from the no-name pairwise matrix.

For each alias form, we split candidate canonicals into two buckets:
  IN-FAMILY  : same form-family (v4 _expand_form) AND same veg-family
  OUT-FAMILY : everything else
and compare the score distributions. A well-calibrated threshold sits
between the OUT bucket's high tail and the IN bucket's true-match peak.

Outputs:
  diagnostics/form_threshold_recommendations_noname.csv
    form, n_aliases, n_in_pairs, n_out_pairs,
    in_p50, in_p75, in_p90,
    out_p90, out_p95, out_p99,
    current_floor, recommended_floor

Recommendation rule:
  recommended = max(in_p25, out_p99)
  - Below in_p25 we'd accept too many borderline noisy in-family pairs
  - At/above out_p99 we exclude >99% of cross-form noise
  - The max() makes sure we don't drop below the natural in-family floor

Console prints a table + sample of items affected by each threshold.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from scripts.search_v4 import _expand_form
from scripts.search_v5 import FORM_THRESHOLDS

MATRIX = Path("diagnostics/pairwise_dense_matrix_noname.csv")
OUT = Path("diagnostics/form_threshold_recommendations_noname.csv")


def _veg_match(a: str, c: str) -> bool:
    if not a or not c:
        return True
    if a == "VEG":
        return c == "VEG"
    if a == "NONVEG":
        return c in ("NONVEG", "EGG")  # nonveg query may pick up egg
    if a == "EGG":
        return c in ("EGG", "NONVEG")
    return a == c


def _form_match(a: str, c: str) -> bool:
    if not a:
        return True
    return c in set(_expand_form(a))


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1)))))
    return xs[k]


def main() -> None:
    print(f"Loading {MATRIX}…")
    in_scores: dict[str, list[float]] = defaultdict(list)
    out_scores: dict[str, list[float]] = defaultdict(list)
    aliases_per_form: dict[str, set] = defaultdict(set)

    with MATRIX.open() as f:
        r = csv.DictReader(f)
        for row in r:
            af = row["alias_form"]
            if not af:
                continue
            aliases_per_form[af].add(row["alias"])
            score = float(row["dense_score"])
            if _form_match(af, row["canonical_form"]) and _veg_match(row["alias_veg"], row["canonical_veg"]):
                in_scores[af].append(score)
            else:
                out_scores[af].append(score)

    print(f"  loaded {sum(len(v) for v in in_scores.values())} in-family pairs, "
          f"{sum(len(v) for v in out_scores.values())} out-family pairs\n")

    rows = []
    forms = sorted(set(in_scores) | set(out_scores))
    for form in forms:
        ins = in_scores.get(form, [])
        outs = out_scores.get(form, [])
        in_p25 = _pct(ins, 25)
        in_p50 = _pct(ins, 50)
        in_p75 = _pct(ins, 75)
        in_p90 = _pct(ins, 90)
        out_p90 = _pct(outs, 90)
        out_p95 = _pct(outs, 95)
        out_p99 = _pct(outs, 99)
        cur = FORM_THRESHOLDS.get(form, 0.0)
        rec = round(max(in_p25, out_p99), 2)
        rows.append({
            "form": form,
            "n_aliases": len(aliases_per_form[form]),
            "n_in_pairs": len(ins),
            "n_out_pairs": len(outs),
            "in_p25": round(in_p25, 4),
            "in_p50": round(in_p50, 4),
            "in_p75": round(in_p75, 4),
            "in_p90": round(in_p90, 4),
            "out_p90": round(out_p90, 4),
            "out_p95": round(out_p95, 4),
            "out_p99": round(out_p99, 4),
            "current_floor": cur,
            "recommended_floor": rec,
            "delta": round(rec - cur, 2),
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {OUT}\n")

    # Console table
    print("=" * 110)
    print(f"{'form':<18}{'aliases':>8}{'in_p25':>9}{'in_p50':>9}{'in_p75':>9}"
          f"{'out_p95':>9}{'out_p99':>9}{'cur':>7}{'rec':>7}{'Δ':>7}")
    print("=" * 110)
    for r in rows:
        print(f"{r['form']:<18}{r['n_aliases']:>8}"
              f"{r['in_p25']:>9.3f}{r['in_p50']:>9.3f}{r['in_p75']:>9.3f}"
              f"{r['out_p95']:>9.3f}{r['out_p99']:>9.3f}"
              f"{r['current_floor']:>7.2f}{r['recommended_floor']:>7.2f}{r['delta']:>+7.2f}")


if __name__ == "__main__":
    main()
