"""Targeted CSV inspection — surface the slices that matter for deciding next steps.

Reads diagnostics/embedding_quality_audit.csv and prints:
  1. Zero-score items grouped by form (catalog gaps)
  2. Per-form mean / median / pct-below-0.65 (where each form's tail lives)
  3. Items 0.40-0.60 sorted ascending (the "real embedding failures" tail)
  4. Items 0.60-0.70 sorted ascending (the mid-pack at-risk band)
  5. Cuisine-mismatch suspects (sweet, snack, main dish with continental names)
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from statistics import mean, median

CSV_PATH = Path("diagnostics/embedding_quality_audit.csv")


def main() -> None:
    rows = list(csv.DictReader(CSV_PATH.open()))
    for r in rows:
        r["top1_score"] = float(r["top1_score"])

    # 1. Zero-score grouped by form
    zeros = [r for r in rows if r["top1_score"] == 0.0]
    print("=" * 100)
    print(f"1. ZERO-SCORE ITEMS ({len(zeros)}) — catalog gaps (no canonical in this form-veg bucket)")
    print("=" * 100)
    by_form: dict[str, list[str]] = defaultdict(list)
    for r in zeros:
        by_form[f"{r['form']} ({r['veg']})"].append(r["alias_name"])
    for key in sorted(by_form):
        print(f"  {key}  ({len(by_form[key])}):  {by_form[key]}")

    # 2. Per-form distribution
    print("\n" + "=" * 100)
    print("2. PER-FORM HEALTH — where does each form's tail live?")
    print("=" * 100)
    print(f"{'form':<22}{'n':>5}{'mean':>8}{'median':>8}  {'%<0.65':>8}  {'%<0.55':>8}")
    print("-" * 100)
    by_form_scores: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_form_scores[r["form"] or "(none)"].append(r["top1_score"])
    for form in sorted(by_form_scores):
        ss = by_form_scores[form]
        below65 = sum(1 for s in ss if s < 0.65)
        below55 = sum(1 for s in ss if s < 0.55)
        print(f"{form:<22}{len(ss):>5}{mean(ss):>8.3f}{median(ss):>8.3f}  "
              f"{below65/len(ss)*100:>7.1f}%  {below55/len(ss)*100:>7.1f}%")

    # 3. 0.40 - 0.60 band — real embedding/cuisine failures
    print("\n" + "=" * 100)
    print("3. ITEMS 0.40–0.60 (real failures — would hybrid/BM25 help?)")
    print("=" * 100)
    band = sorted([r for r in rows if 0.40 <= r["top1_score"] < 0.60], key=lambda r: r["top1_score"])
    print(f"{'score':>7}  {'form':<22}{'veg':<7}  {'alias':<38} → top-1")
    for r in band:
        print(f"{r['top1_score']:>7.3f}  {r['form']:<22}{r['veg']:<7}  {r['alias_name']:<38} → {r['top1_canonical']}")

    # 4. 0.60 - 0.70 band — at-risk mid-pack
    print("\n" + "=" * 100)
    print(f"4. ITEMS 0.60–0.70 — currently blocked by 0.65 / 0.70 floors ({len([r for r in rows if 0.60 <= r['top1_score'] < 0.70])} items)")
    print("    Showing bottom 30 of this band (closest to failure):")
    print("=" * 100)
    band2 = sorted([r for r in rows if 0.60 <= r["top1_score"] < 0.70], key=lambda r: r["top1_score"])[:30]
    print(f"{'score':>7}  {'form':<22}{'veg':<7}  {'alias':<38} → top-1")
    for r in band2:
        print(f"{r['top1_score']:>7.3f}  {r['form']:<22}{r['veg']:<7}  {r['alias_name']:<38} → {r['top1_canonical']}")


if __name__ == "__main__":
    main()
