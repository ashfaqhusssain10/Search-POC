"""Per-form similarity threshold calibration.

Goal: pick a fallback similarity threshold *per form* (bread, gravy, rice-dish,
etc.) instead of one global 0.70 floor. Same-form pairs have very different
"natural" similarity distributions — breads cluster loosely (0.50-0.65 even
between obvious substitutes), curries cluster tightly (0.75-0.85), and using a
single threshold either misses valid bread subs or admits curry noise.

What this script does:
  1. For each Supabase alias item, fetch its stored vector
  2. Query the canonical collection restricted to (same form-family, same veg)
  3. Record the top-K canonical hits' scores
  4. Output:
       - diagnostics/form_threshold_calibration.csv (one row per query-candidate)
       - console: per-form histogram + suggested threshold (5th–25th percentile
         of top-1 scores, depending on form sample size)

Then a human eyeballs the borderline band per form and locks in
FORM_THRESHOLDS in search_v5.py.

Usage:
    python -m scripts.calibrate_form_thresholds
"""

from __future__ import annotations

import csv
import logging
from collections import defaultdict
from pathlib import Path
from statistics import quantiles, mean

import numpy as np
from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from core.connections import close_connections, get_qdrant_client

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ALIAS_COLLECTION = "searchpoc_aliases"
CANONICAL_COLLECTION = "searchpoc_canonicals"
TOP_K = 3                       # canonical hits per query item
OUT_CSV = Path("diagnostics/form_threshold_calibration.csv")
MIN_SAMPLE_FOR_PER_FORM = 12    # if fewer items in a form, suggest global only

# Mirrors v4/v5 FORM_FAMILIES so we don't over-restrict.
FORM_FAMILIES: list[set[str]] = [
    {"gravy", "stew"},
    {"dry-fry", "snack"},
    {"soup", "stew"},
]


def _expand_form(form: str) -> list[str]:
    f = form.strip().lower()
    out = {f}
    for fam in FORM_FAMILIES:
        if f in fam:
            out |= fam
    return sorted(out)


def _build_filter(veg: str | None, form: str | None) -> Filter | None:
    must: list[FieldCondition] = []
    if veg:
        v = veg.upper()
        if v == "VEG":
            must.append(FieldCondition(key="veg_type", match=MatchValue(value="VEG")))
        elif v == "NONVEG":
            must.append(FieldCondition(key="veg_type", match=MatchValue(value="NONVEG")))
        elif v == "EGG":
            must.append(FieldCondition(key="veg_type", match=MatchAny(any=["EGG", "NONVEG"])))
    if form:
        related = _expand_form(form)
        if len(related) == 1:
            must.append(FieldCondition(key="form", match=MatchValue(value=related[0])))
        else:
            must.append(FieldCondition(key="form", match=MatchAny(any=related)))
    return Filter(must=must) if must else None


def _fetch_all_aliases(qdrant) -> list[tuple[str, str | None, str | None, np.ndarray]]:
    """Pull every alias item with its (name, veg, form, vector)."""
    out: list[tuple[str, str | None, str | None, np.ndarray]] = []
    next_offset = None
    while True:
        points, next_offset = qdrant.scroll(
            collection_name=ALIAS_COLLECTION,
            offset=next_offset,
            limit=200,
            with_payload=True,
            with_vectors=True,
        )
        for p in points:
            n = p.payload.get("name") if p.payload else None
            if not n:
                continue
            out.append((
                n,
                p.payload.get("veg_type"),
                p.payload.get("form"),
                np.asarray(p.vector, dtype=np.float32),
            ))
        if next_offset is None:
            break
    return out


def main() -> None:
    qdrant = get_qdrant_client()
    print("Fetching all Supabase alias items…")
    aliases = _fetch_all_aliases(qdrant)
    print(f"  {len(aliases)} aliases loaded")

    # per-form bucket of (query_name, top1_score, top1_name, all_scores)
    by_form: dict[str, list[tuple[str, float, str | None, list[float]]]] = defaultdict(list)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    rows_out: list[tuple[str, str, str | None, int, str, float]] = []

    print(f"Running same-form, same-veg search for each alias (top-{TOP_K})…")
    for i, (name, veg, form, vec) in enumerate(aliases, 1):
        if i % 100 == 0:
            print(f"  {i}/{len(aliases)}")
        if not form:
            continue
        flt = _build_filter(veg, form)
        hits = qdrant.query_points(
            collection_name=CANONICAL_COLLECTION,
            query=vec.tolist(),
            limit=TOP_K + 1,  # +1 so we can skip exact self-name if present
            score_threshold=0.0,
            with_payload=True,
            query_filter=flt,
        ).points

        # Skip the exact-name self-match — that doesn't tell us anything about
        # substitute similarity (it's just the item finding itself).
        filtered = [h for h in hits if (h.payload.get("name") or "").lower() != name.lower()]
        filtered = filtered[:TOP_K]
        if not filtered:
            continue

        scores = [float(h.score) for h in filtered]
        top1_score = scores[0]
        top1_name = filtered[0].payload.get("name")
        by_form[form].append((name, top1_score, top1_name, scores))

        for rank, h in enumerate(filtered, 1):
            rows_out.append((
                form, name, veg, rank,
                h.payload.get("name") or "",
                float(h.score),
            ))

    # Write CSV
    with OUT_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["form", "query_item", "query_veg", "rank", "candidate", "score"])
        for r in rows_out:
            w.writerow([r[0], r[1], r[2] or "", r[3], r[4], f"{r[5]:.4f}"])
    print(f"\nWrote {len(rows_out)} rows to {OUT_CSV}\n")

    # Per-form summary
    print("=" * 100)
    print(f"{'form':<22}{'n':>5}  {'min':>6}  {'p10':>6}  {'p25':>6}  {'p50':>6}  {'p75':>6}  "
          f"{'p90':>6}  {'max':>6}   {'mean':>6}   suggested")
    print("-" * 100)
    suggestions: dict[str, float] = {}
    for form in sorted(by_form.keys()):
        entries = by_form[form]
        top1s = sorted(e[1] for e in entries)
        n = len(top1s)
        if n == 0:
            continue
        if n >= 5:
            q = quantiles(top1s, n=10)  # deciles
            p10, p25, p50, p75, p90 = q[0], q[1], q[4], q[6], q[8]
        else:
            p10 = p25 = p50 = p75 = p90 = top1s[n // 2]
        avg = mean(top1s)

        if n < MIN_SAMPLE_FOR_PER_FORM:
            suggestion = "(too few — use global)"
        else:
            # Suggested floor = p25 of top-1 same-form scores. This is the
            # "borderline but legit" band — items whose true closest peer in
            # the catalog sits at this score. Anything lower is likely noise
            # even within the same form.
            suggestion = f"{p25:.2f}"
            suggestions[form] = round(p25, 2)

        print(f"{form:<22}{n:>5}  {top1s[0]:>6.3f}  {p10:>6.3f}  {p25:>6.3f}  "
              f"{p50:>6.3f}  {p75:>6.3f}  {p90:>6.3f}  {top1s[-1]:>6.3f}   "
              f"{avg:>6.3f}   {suggestion}")

    print()
    print("=" * 100)
    print("Suggested FORM_THRESHOLDS dict (review against CSV before locking in):")
    print("=" * 100)
    print("FORM_THRESHOLDS: dict[str, float] = {")
    for form in sorted(suggestions):
        print(f"    {form!r:<22}: {suggestions[form]},")
    print("}")
    print()
    print(f"Forms with < {MIN_SAMPLE_FOR_PER_FORM} samples fall back to global FALLBACK_THRESHOLD.")
    print(f"\nNext: eyeball {OUT_CSV} around each form's p25 to confirm the suggested floor.")

    close_connections()


if __name__ == "__main__":
    main()
