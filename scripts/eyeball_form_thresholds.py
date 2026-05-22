"""Pretty-print the borderline same-form pairs around each form's suggested
threshold so a human can decide whether the suggested floor is sensible.

For each form, shows up to 6 pairs in each band:
   [p25 - 0.05, p25 + 0.05]  (the borderline zone around the suggested floor)

Reads diagnostics/form_threshold_calibration.csv (only top-1 per query).
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from statistics import quantiles

CSV_PATH = Path("diagnostics/form_threshold_calibration.csv")
BAND = 0.05  # ±0.05 around the suggested p25
SAMPLES_PER_BAND = 8

# Mirror of suggestions from calibrate_form_thresholds.py output
SUGGESTED: dict[str, float] = {
    "baked good": 0.64, "beverage": 0.63, "condiment": 0.59, "dal": 0.75,
    "dry dish": 0.70, "egg dish": 0.61, "flatbread": 0.63, "gravy dish": 0.67,
    "kebab": 0.78, "main dish": 0.45, "pasta & noodles": 0.67,
    "rice dish": 0.74, "sandwich & wrap": 0.75, "snack": 0.64,
    "soup": 0.54, "sweet dish": 0.55,
}


def main() -> None:
    rows = list(csv.DictReader(CSV_PATH.open()))
    # Keep only rank=1 (top-1 per query) — that's what we calibrated on.
    top1_by_form: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    for r in rows:
        if r["rank"] != "1":
            continue
        top1_by_form[r["form"]].append((r["query_item"], r["candidate"], float(r["score"])))

    for form in sorted(top1_by_form):
        pairs = top1_by_form[form]
        if form not in SUGGESTED:
            print(f"\n── {form} (n={len(pairs)}) — TOO FEW, use global 0.70 ──")
            continue
        T = SUGGESTED[form]
        lo, hi = T - BAND, T + BAND
        in_band = sorted([p for p in pairs if lo <= p[2] <= hi], key=lambda x: x[2])
        below = sorted([p for p in pairs if p[2] < lo], key=lambda x: -x[2])[:3]
        above = sorted([p for p in pairs if p[2] > hi], key=lambda x: x[2])[:3]

        print(f"\n{'='*100}")
        print(f"  {form.upper()}   suggested floor = {T:.2f}   (band shown: {lo:.2f} – {hi:.2f},  n={len(pairs)})")
        print(f"{'='*100}")

        def show(label: str, rows: list[tuple[str, str, float]]) -> None:
            if not rows:
                return
            print(f"  ── {label} ──")
            for q, c, s in rows[:SAMPLES_PER_BAND]:
                mark = "  GOOD?" if s >= T else "  REJECT?"
                print(f"    {s:.3f}  {q:<35} → {c:<35} {mark}")

        show(f"just BELOW {T:.2f} (would be rejected — are these legit pairs that we'd miss?)", below)
        show(f"borderline {lo:.2f}–{hi:.2f} (this is where the threshold matters)", in_band)
        show(f"just ABOVE {T:.2f} (would pass — do these look right?)", above)


if __name__ == "__main__":
    main()
