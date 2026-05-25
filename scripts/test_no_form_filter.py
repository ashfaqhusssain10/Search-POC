"""Test: compare v4 WITH form filter (current production) vs v4 WITHOUT form
filter, across realistic queries.

For each test alias we run two queries against searchpoc_canonicals:
  A. WITH form filter   — current v4 behavior (form + veg hard filter)
  B. WITHOUT form filter — veg filter only

For each query we record top-1 and top-3 hits + scores. The hypothesis is
that B will rescue ~15% of aliases whose best peer lives in a different
form (the cross-form-best-peer set we surfaced from the pairwise matrix).

Test set:
  - All 28 scenarios from test_platter_scenarios.py / shadow_fallback_test.py
  - All 117 aliases whose pairwise top-1 was cross-form (from the matrix)

Writes diagnostics/test_no_form_filter.csv with both modes side by side.
Prints a console summary: wins (B finds a better peer), losses (B finds
something worse), draws, and a sample of each.
"""

from __future__ import annotations

import csv
import logging
from collections import defaultdict
from pathlib import Path

import numpy as np
from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from core.connections import close_connections, get_qdrant_client
from scripts.shadow_fallback_test import SCENARIOS

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ALIAS_COLLECTION = "searchpoc_aliases"
CANONICAL_COLLECTION = "searchpoc_canonicals"
PAIRWISE_CSV = Path("diagnostics/pairwise_dense_matrix_enriched.csv")
OUT_CSV = Path("diagnostics/test_no_form_filter.csv")

# Mirror of v4 FORM_FAMILIES so the WITH-filter mode matches production.
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


def _veg_clause(veg: str | None) -> FieldCondition | None:
    if not veg:
        return None
    v = veg.upper()
    if v == "VEG":
        return FieldCondition(key="veg_type", match=MatchValue(value="VEG"))
    if v == "NONVEG":
        return FieldCondition(key="veg_type", match=MatchValue(value="NONVEG"))
    if v == "EGG":
        return FieldCondition(key="veg_type", match=MatchAny(any=["EGG", "NONVEG"]))
    return None


def _form_clause(form: str | None) -> FieldCondition | None:
    if not form:
        return None
    related = _expand_form(form)
    if len(related) == 1:
        return FieldCondition(key="form", match=MatchValue(value=related[0]))
    return FieldCondition(key="form", match=MatchAny(any=related))


def _filter(veg: str | None, form: str | None, *, include_form: bool) -> Filter | None:
    must: list[FieldCondition] = []
    v = _veg_clause(veg)
    if v:
        must.append(v)
    if include_form:
        f = _form_clause(form)
        if f:
            must.append(f)
    return Filter(must=must) if must else None


def _fetch_alias(qdrant, name: str) -> tuple[np.ndarray, str | None, str | None] | None:
    """Find one alias by name in the alias collection — returns (vec, veg, form)."""
    next_offset = None
    while True:
        points, next_offset = qdrant.scroll(
            collection_name=ALIAS_COLLECTION, offset=next_offset, limit=200,
            with_payload=True, with_vectors=True,
        )
        for p in points:
            n = (p.payload.get("name") if p.payload else None) or ""
            if n.lower() == name.lower():
                return (
                    np.asarray(p.vector, dtype=np.float32),
                    p.payload.get("veg_type"),
                    p.payload.get("form"),
                )
        if next_offset is None:
            return None


def _scroll_aliases(qdrant) -> dict[str, tuple[np.ndarray, str | None, str | None]]:
    """One scroll, all aliases. Used so we don't paginate per query."""
    out: dict[str, tuple[np.ndarray, str | None, str | None]] = {}
    next_offset = None
    while True:
        points, next_offset = qdrant.scroll(
            collection_name=ALIAS_COLLECTION, offset=next_offset, limit=200,
            with_payload=True, with_vectors=True,
        )
        for p in points:
            n = (p.payload.get("name") if p.payload else None) or ""
            if n:
                out[n] = (
                    np.asarray(p.vector, dtype=np.float32),
                    p.payload.get("veg_type"),
                    p.payload.get("form"),
                )
        if next_offset is None:
            return out


def _top1(qdrant, vec: np.ndarray, veg: str | None, form: str | None,
          *, include_form: bool) -> tuple[float, str, str]:
    hits = qdrant.query_points(
        collection_name=CANONICAL_COLLECTION,
        query=vec.tolist(),
        limit=3,
        score_threshold=0.0,
        with_payload=True,
        query_filter=_filter(veg, form, include_form=include_form),
    ).points
    if not hits:
        return 0.0, "", ""
    h = hits[0]
    return float(h.score), h.payload.get("name") or "", (h.payload.get("form") or "")


def _load_cross_form_aliases() -> list[str]:
    """117 aliases whose best peer is in a different form (from the pairwise matrix)."""
    # Group by alias, find best, check if same/diff form.
    best: dict[str, tuple[float, str, str, str]] = {}  # alias → (score, canon, aform, cform)
    for r in csv.DictReader(PAIRWISE_CSV.open()):
        a = r["alias"]
        s = float(r["dense_score"])
        prev = best.get(a)
        if prev is None or s > prev[0]:
            best[a] = (s, r["canonical"], r["alias_form"], r["canonical_form"])
    return [a for a, (_, _, af, cf) in best.items() if af and cf and af != cf]


def main() -> None:
    qdrant = get_qdrant_client()
    print("Loading alias vectors…")
    alias_data = _scroll_aliases(qdrant)
    print(f"  {len(alias_data)} aliases loaded")

    # Test set: dishes from the 28 PM scenarios + the 117 cross-form aliases
    test_set: list[str] = []
    seen: set[str] = set()
    for _, dishes in SCENARIOS:
        for d in dishes:
            if d not in seen:
                test_set.append(d)
                seen.add(d)
    cross_form = _load_cross_form_aliases()
    print(f"  {len(cross_form)} cross-form aliases from pairwise matrix")
    for a in cross_form:
        if a not in seen:
            test_set.append(a)
            seen.add(a)
    print(f"  → {len(test_set)} unique test queries")

    # Run both modes
    print("\nRunning A=with-form-filter and B=without-form-filter…")
    rows: list[dict] = []
    missing: list[str] = []
    for q in test_set:
        meta = alias_data.get(q)
        if meta is None:
            missing.append(q)
            continue
        vec, veg, form = meta
        a_score, a_name, a_form = _top1(qdrant, vec, veg, form, include_form=True)
        b_score, b_name, b_form = _top1(qdrant, vec, veg, form, include_form=False)
        rows.append({
            "alias": q, "alias_veg": veg or "", "alias_form": form or "",
            "with_form_score": a_score, "with_form_top1": a_name, "with_form_top1_form": a_form,
            "no_form_score": b_score, "no_form_top1": b_name, "no_form_top1_form": b_form,
        })
    if missing:
        print(f"  WARN: no alias vector for {len(missing)} items: {missing[:10]}…")

    # Categorize
    rescued: list[dict] = []        # A returned nothing or low; B found something materially better
    same: list[dict] = []           # both returned same canonical
    diff_better_B: list[dict] = []  # different canonicals, B scored higher
    diff_better_A: list[dict] = []  # different canonicals, A scored higher
    for r in rows:
        if r["with_form_top1"] == r["no_form_top1"]:
            same.append(r)
        elif r["with_form_top1"] == "" or r["with_form_score"] < 0.55:
            if r["no_form_score"] >= 0.65:
                rescued.append(r)
            elif r["no_form_score"] > r["with_form_score"]:
                diff_better_B.append(r)
            else:
                diff_better_A.append(r)
        elif r["no_form_score"] > r["with_form_score"] + 0.02:
            diff_better_B.append(r)
        elif r["with_form_score"] > r["no_form_score"] + 0.02:
            diff_better_A.append(r)
        else:
            diff_better_B.append(r) if r["no_form_score"] >= r["with_form_score"] else diff_better_A.append(r)

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["alias", "alias_veg", "alias_form",
                    "with_form_score", "with_form_top1", "with_form_top1_form",
                    "no_form_score", "no_form_top1", "no_form_top1_form",
                    "verdict"])
        verdict_map: dict[int, str] = {}
        for r in rescued: verdict_map[id(r)] = "RESCUED"
        for r in same: verdict_map[id(r)] = "SAME"
        for r in diff_better_B: verdict_map[id(r)] = "B_BETTER"
        for r in diff_better_A: verdict_map[id(r)] = "A_BETTER"
        for r in rows:
            w.writerow([
                r["alias"], r["alias_veg"], r["alias_form"],
                f"{r['with_form_score']:.4f}", r["with_form_top1"], r["with_form_top1_form"],
                f"{r['no_form_score']:.4f}", r["no_form_top1"], r["no_form_top1_form"],
                verdict_map.get(id(r), "?"),
            ])
    print(f"\nWrote {len(rows)} rows to {OUT_CSV}")

    # Console summary
    print()
    print("=" * 95)
    print(f"VERDICTS over {len(rows)} test queries")
    print("=" * 95)
    print(f"  SAME      {len(same):>4}  ({len(same)/len(rows)*100:5.1f}%)  both modes agree")
    print(f"  RESCUED   {len(rescued):>4}  ({len(rescued)/len(rows)*100:5.1f}%)  A failed (<0.55 or empty), B found a good peer (≥0.65)")
    print(f"  B_BETTER  {len(diff_better_B):>4}  ({len(diff_better_B)/len(rows)*100:5.1f}%)  different top-1, B scored higher")
    print(f"  A_BETTER  {len(diff_better_A):>4}  ({len(diff_better_A)/len(rows)*100:5.1f}%)  different top-1, A scored higher (potential regression)")

    def _show(title: str, lst: list[dict], n: int = 25) -> None:
        if not lst:
            return
        print()
        print("=" * 95)
        print(f"{title}  (showing up to {n})")
        print("=" * 95)
        print(f"  {'A_score':>8}{'B_score':>8}  {'alias':<32}{'A→':<32}{'  B→'}")
        sample = sorted(lst, key=lambda r: -(r["no_form_score"] - r["with_form_score"]))[:n]
        for r in sample:
            atop = r["with_form_top1"] or "—"
            btop = r["no_form_top1"] or "—"
            print(f"  {r['with_form_score']:>8.3f}{r['no_form_score']:>8.3f}  "
                  f"{r['alias'][:30]:<32}{atop[:30]:<32}  {btop[:30]}")

    _show("RESCUED — A returned nothing usable, B rescued", rescued)
    _show("B_BETTER — B picked a meaningfully closer peer", diff_better_B, n=30)
    _show("A_BETTER — A picked a closer peer (possible regression if we drop form filter)",
          diff_better_A, n=15)

    close_connections()


if __name__ == "__main__":
    main()
