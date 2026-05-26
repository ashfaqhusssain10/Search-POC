"""Compare the OLD search logic (single global 0.80 floor) against the NEW
logic (per-form FORM_THRESHOLDS) across every Supabase alias.

We don't run the full v5 platter pipeline here — that would couple the
comparison to platter membership and obscure the threshold effect. Instead
we measure the item-level decision the production code makes:

  - Fetch each alias's stored vector + form + veg
  - Run a Qdrant top-3 against canonicals (veg + form-family filter, matching
    v4 behavior) at the PERMISSIVE V4_CANDIDATE_FLOOR
  - Apply OLD verdict: top-1 score >= 0.80?
  - Apply NEW verdict: top-1 score >= FORM_THRESHOLDS[form]?
  - Record both verdicts + the score + the top-1 canonical name

Outputs:
  - diagnostics/old_vs_new_logic.csv  — one row per alias, both verdicts
  - Console summary: GAINED / LOST / UNCHANGED counts, per-form breakdown,
    sample of items that gained a match under the new logic

Use this to answer "did the logic change actually help, and where?"
"""

from __future__ import annotations

import csv
import logging
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from core.connections import close_connections, get_qdrant_client
from scripts.search_v4 import FORM_FAMILIES, _expand_form
from scripts.search_v5 import FORM_THRESHOLDS, _threshold_for_form

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ALIAS_COLLECTION = "searchpoc_aliases"
CANONICAL_COLLECTION = "searchpoc_canonicals"
OUT_CSV = Path("diagnostics/old_vs_new_logic.csv")
MAX_WORKERS = 16

OLD_GLOBAL_FLOOR = 0.80  # the old "current" ranker's display floor


def _build_filter(veg: str | None, form: str | None) -> Filter | None:
    """Mirror v4's filter so we compare like-for-like."""
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


def _scroll_aliases(qdrant) -> list[tuple[str, str | None, str | None, np.ndarray]]:
    out: list[tuple[str, str | None, str | None, np.ndarray]] = []
    next_offset = None
    while True:
        points, next_offset = qdrant.scroll(
            collection_name=ALIAS_COLLECTION, offset=next_offset, limit=200,
            with_payload=True, with_vectors=True,
        )
        for p in points:
            n = (p.payload.get("name") if p.payload else None) or ""
            if not n:
                continue
            out.append((
                n,
                (p.payload.get("veg_type") or "").strip().upper() or None,
                (p.payload.get("form") or "").strip().lower() or None,
                np.asarray(p.vector, dtype=np.float32),
            ))
        if next_offset is None:
            break
    return out


def _score_one(qdrant, name: str, veg: str | None, form: str | None, vec: np.ndarray) -> dict:
    """Replicate v4 behavior for one alias and apply both verdicts."""
    hits = qdrant.query_points(
        collection_name=CANONICAL_COLLECTION,
        query=vec.tolist(),
        limit=3,
        score_threshold=0.50,  # match V4_CANDIDATE_FLOOR
        with_payload=True,
        query_filter=_build_filter(veg, form),
    ).points

    if not hits:
        top1_score = 0.0
        top1_name = ""
    else:
        top1_score = float(hits[0].score)
        top1_name = hits[0].payload.get("name") or ""

    new_floor = _threshold_for_form(form)
    old_passes = top1_score >= OLD_GLOBAL_FLOOR
    new_passes = top1_score >= new_floor

    if old_passes and new_passes:
        verdict = "BOTH_MATCH"
    elif new_passes and not old_passes:
        verdict = "GAINED"
    elif old_passes and not new_passes:
        verdict = "LOST"
    else:
        verdict = "BOTH_MISS"

    return {
        "alias": name, "alias_form": form or "", "alias_veg": veg or "",
        "top1_score": top1_score, "top1_canonical": top1_name,
        "old_floor": OLD_GLOBAL_FLOOR, "new_floor": new_floor,
        "old_passes": old_passes, "new_passes": new_passes,
        "verdict": verdict,
    }


def main() -> None:
    qdrant = get_qdrant_client()
    print("Loading alias vectors…")
    aliases = _scroll_aliases(qdrant)
    print(f"  {len(aliases)} aliases loaded\n")

    print(f"Running OLD vs NEW logic comparison ({MAX_WORKERS} workers)…")
    results: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [
            pool.submit(_score_one, qdrant, name, veg, form, vec)
            for name, veg, form, vec in aliases
        ]
        for fut in as_completed(futures):
            results.append(fut.result())
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(aliases)}")

    # ── CSV ────────────────────────────────────────────────────────────────
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "alias", "alias_form", "alias_veg",
            "top1_score", "top1_canonical",
            "old_floor", "new_floor",
            "old_passes", "new_passes", "verdict",
        ])
        # Sort: GAINED first so they're easy to inspect
        priority = {"GAINED": 0, "LOST": 1, "BOTH_MATCH": 2, "BOTH_MISS": 3}
        results.sort(key=lambda r: (priority[r["verdict"]], -r["top1_score"]))
        for r in results:
            w.writerow([
                r["alias"], r["alias_form"], r["alias_veg"],
                f"{r['top1_score']:.4f}", r["top1_canonical"],
                f"{r['old_floor']:.2f}", f"{r['new_floor']:.2f}",
                r["old_passes"], r["new_passes"], r["verdict"],
            ])
    print(f"\nWrote {len(results)} rows to {OUT_CSV}")

    # ── Summary ────────────────────────────────────────────────────────────
    verdicts = Counter(r["verdict"] for r in results)
    n = len(results)
    print()
    print("=" * 80)
    print(f"VERDICT SUMMARY  (n={n})")
    print("=" * 80)
    print(f"  BOTH_MATCH  {verdicts['BOTH_MATCH']:>4}  ({verdicts['BOTH_MATCH']/n*100:5.1f}%)  matched under old AND new logic")
    print(f"  GAINED      {verdicts['GAINED']:>4}  ({verdicts['GAINED']/n*100:5.1f}%)  no match under old, matches under new ← THE WIN")
    print(f"  LOST        {verdicts['LOST']:>4}  ({verdicts['LOST']/n*100:5.1f}%)  matched under old, NOT under new (regression — should be 0)")
    print(f"  BOTH_MISS   {verdicts['BOTH_MISS']:>4}  ({verdicts['BOTH_MISS']/n*100:5.1f}%)  no match under either")

    # Per-form breakdown
    print()
    print("=" * 80)
    print("PER-FORM BREAKDOWN — where do the gains live?")
    print("=" * 80)
    per_form: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        per_form[r["alias_form"] or "(none)"][r["verdict"]] += 1
    print(f"{'form':<22}{'n':>5}  {'BOTH_MATCH':>11}{'GAINED':>9}{'LOST':>7}{'BOTH_MISS':>11}{'floor':>8}")
    for form in sorted(per_form):
        c = per_form[form]
        total = sum(c.values())
        floor = _threshold_for_form(form if form != "(none)" else None)
        print(f"  {form:<20}{total:>5}  {c['BOTH_MATCH']:>11}{c['GAINED']:>9}{c['LOST']:>7}{c['BOTH_MISS']:>11}{floor:>8.2f}")

    # Sample of GAINED items
    gained = [r for r in results if r["verdict"] == "GAINED"]
    if gained:
        print()
        print("=" * 80)
        print(f"SAMPLE of GAINED — items that the new logic rescues (top 25 by score)")
        print("=" * 80)
        gained.sort(key=lambda r: -r["top1_score"])
        print(f"  {'score':>6}  {'form_floor':>10}  {'alias':<36}{'top-1 canonical':<32}")
        for r in gained[:25]:
            print(f"  {r['top1_score']:>6.3f}  {r['new_floor']:>10.2f}  "
                  f"{r['alias'][:34]:<36}{(r['top1_canonical'] or '—')[:30]:<32}")

    # Any LOSSES — should be empty since new floors are <= old
    lost = [r for r in results if r["verdict"] == "LOST"]
    if lost:
        print()
        print("=" * 80)
        print(f"⚠ LOSSES — items the new logic dropped (review carefully)")
        print("=" * 80)
        for r in lost[:20]:
            print(f"  {r['top1_score']:>6.3f}  {r['alias']:<36} (old<{r['old_floor']:.2f} ✓, new<{r['new_floor']:.2f} ✗)")

    close_connections()


if __name__ == "__main__":
    main()
