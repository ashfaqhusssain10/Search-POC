"""Apply the recommended per-form thresholds to the no-name top-1 results
and report verdicts vs production for every alias.

Reads:
  diagnostics/pairwise_dense_matrix_noname.csv
  diagnostics/form_threshold_recommendations_noname.csv (new floors)
  diagnostics/noname_vs_current.csv (production top-1 per alias)

For each alias:
  - Filter canonicals to same veg-family + same form-family (v4-style)
  - Take top-1
  - Apply NEW per-form floor → pass / no-match
  - Compare to production current top-1 + current floor

Verdict buckets:
  WIN          : noname passes new floor AND current did not
  HOLD         : both pass, same top-1 canonical
  FLIP_OK      : both pass, different top-1 — surface for inspection
  REGRESSION   : current passed, noname does not
  BOTH_MISS    : neither passes

Outputs:
  diagnostics/test_top1_noname.csv — per-alias verdict
  Console: summary, per-form breakdown, sample of FLIP_OK and REGRESSION
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path

from scripts.search_v4 import _expand_form

MATRIX = Path("diagnostics/pairwise_dense_matrix_noname.csv")
RECS = Path("diagnostics/form_threshold_recommendations_noname.csv")
CUR = Path("diagnostics/noname_vs_current.csv")
OUT = Path("diagnostics/test_top1_noname.csv")

CURRENT_GLOBAL_FLOOR = 0.50  # V4_CANDIDATE_FLOOR — what the production path uses as gate

# Manual overrides on top of the analyzer's recommendation.
# Forms where max(in_p25, out_p99) was over-aggressive — relaxed based on
# inspection of the REGRESSION bucket in the previous test_top1_noname run.
FLOOR_OVERRIDES = {
    "kebab": 0.85,            # was 0.89 — Tandoori Chicken at 0.86 was correct
    "egg dish": 0.65,         # was 0.72 — Omelette / Scrambled Eggs → Boiled Egg
    "salad": 0.65,            # was 0.75 — Russian / Onion Salad → Green Salad
    "soup": 0.66,             # was 0.75 — Tomato Soup → Tomato Rasam at 0.67
    "mouth freshener": 0.85,  # was 0.94 — only 4 items, over-tuned
    "flatbread": 0.78,        # was 0.82 — rescues Millet Poori → Pulka
    # rice dish stays at 0.78: relaxing re-admits cross-protein matches
    #   (Prawn → Chicken Fried Rice, Egg → Chicken Fried Rice)
}


def _veg_match(a: str, c: str) -> bool:
    if not a or not c:
        return True
    if a == "VEG":
        return c == "VEG"
    if a in ("NONVEG", "EGG"):
        return c in ("NONVEG", "EGG")
    return a == c


def _form_match(a: str, c: str) -> bool:
    if not a:
        return True
    return c in set(_expand_form(a))


def main() -> None:
    print("Loading recommended thresholds…")
    new_floor: dict[str, float] = {}
    cur_floor: dict[str, float] = {}
    with RECS.open() as f:
        for r in csv.DictReader(f):
            new_floor[r["form"]] = float(r["recommended_floor"])
            cur_floor[r["form"]] = float(r["current_floor"])
    # Apply manual overrides
    for form, override in FLOOR_OVERRIDES.items():
        if form in new_floor:
            print(f"  override {form:<18} {new_floor[form]:.2f} → {override:.2f}")
            new_floor[form] = override

    print("Loading production top-1 per alias…")
    prod_top: dict[str, dict] = {}
    with CUR.open() as f:
        for r in csv.DictReader(f):
            prod_top[r["alias"]] = {
                "top1": r["current_top1"],
                "score": float(r["current_score"]),
                "form": r["form"],
                "veg": r["veg"],
            }

    print("Scanning pairwise matrix for filtered top-1…")
    best: dict[str, tuple[float, str]] = {}
    alias_meta: dict[str, dict] = {}
    with MATRIX.open() as f:
        for row in csv.DictReader(f):
            a = row["alias"]
            alias_meta.setdefault(a, {"form": row["alias_form"], "veg": row["alias_veg"]})
            if not _veg_match(row["alias_veg"], row["canonical_veg"]):
                continue
            if not _form_match(row["alias_form"], row["canonical_form"]):
                continue
            s = float(row["dense_score"])
            cur = best.get(a)
            if cur is None or s > cur[0]:
                best[a] = (s, row["canonical"])

    rows = []
    for a, meta in alias_meta.items():
        form = meta["form"]
        nf = new_floor.get(form, 0.0)
        new_s, new_top = best.get(a, (0.0, ""))
        new_pass = new_s >= nf
        prod = prod_top.get(a, {})
        cur_s = prod.get("score", 0.0)
        cur_top = prod.get("top1", "")
        # production uses per-form floor too — read from cur_floor dict
        cf = cur_floor.get(form, 0.0)
        # A 0.00 score means no candidate at all — don't treat it as "passing"
        # even when the floor is also 0.00 (which happens for forms with no
        # in-family canonicals like pizza).
        cur_pass = cur_s >= cf and cur_s > 0.0

        if new_pass and not cur_pass:
            verdict = "WIN"
        elif new_pass and cur_pass and new_top == cur_top:
            verdict = "HOLD"
        elif new_pass and cur_pass and new_top != cur_top:
            verdict = "FLIP_OK"
        elif cur_pass and not new_pass:
            verdict = "REGRESSION"
        else:
            verdict = "BOTH_MISS"

        rows.append({
            "alias": a, "form": form, "veg": meta["veg"],
            "current_top1": cur_top, "current_score": cur_s, "current_floor": cf, "current_pass": cur_pass,
            "noname_top1": new_top, "noname_score": new_s, "new_floor": nf, "new_pass": new_pass,
            "verdict": verdict,
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    priority = {"WIN": 0, "FLIP_OK": 1, "REGRESSION": 2, "HOLD": 3, "BOTH_MISS": 4}
    rows.sort(key=lambda r: (priority[r["verdict"]], -r["noname_score"]))
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            r2 = dict(r)
            r2["current_score"] = f"{r['current_score']:.4f}"
            r2["noname_score"] = f"{r['noname_score']:.4f}"
            w.writerow(r2)
    print(f"\nWrote {len(rows)} rows to {OUT}\n")

    # Summary
    verdicts = Counter(r["verdict"] for r in rows)
    n = len(rows)
    print("=" * 72)
    print(f"VERDICT SUMMARY  (n={n})")
    print("=" * 72)
    for v in ("WIN", "FLIP_OK", "HOLD", "REGRESSION", "BOTH_MISS"):
        c = verdicts.get(v, 0)
        print(f"  {v:<11} {c:>4}  ({c/n*100:5.1f}%)")

    # Per-form
    print()
    print("=" * 80)
    print("PER-FORM VERDICT BREAKDOWN")
    print("=" * 80)
    per: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        per[r["form"] or "(none)"][r["verdict"]] += 1
    print(f"{'form':<22}{'n':>5}  {'WIN':>5}{'FLIP_OK':>9}{'HOLD':>6}{'REGR':>6}{'MISS':>6}{'cur':>7}{'new':>7}")
    for form in sorted(per):
        c = per[form]
        total = sum(c.values())
        nf = new_floor.get(form, 0.0)
        cf = cur_floor.get(form, 0.0)
        print(f"  {form:<20}{total:>5}  {c['WIN']:>5}{c['FLIP_OK']:>9}{c['HOLD']:>6}{c['REGRESSION']:>6}{c['BOTH_MISS']:>6}"
              f"{cf:>7.2f}{nf:>7.2f}")

    def sample(label: str, k: int = 20):
        sub = [r for r in rows if r["verdict"] == label][:k]
        if not sub:
            return
        print()
        print(f"── {label} sample (top {len(sub)}) ──")
        for r in sub:
            print(f"  [{r['form']:<14}] {r['alias'][:32]:<34}  "
                  f"cur:{r['current_score']:.2f}→{(r['current_top1'] or '—')[:22]:<24}  "
                  f"new:{r['noname_score']:.2f}→{(r['noname_top1'] or '—')[:22]}")

    sample("WIN", 20)
    sample("FLIP_OK", 20)
    sample("REGRESSION", 25)


if __name__ == "__main__":
    main()
