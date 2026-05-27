"""Compare the no-name test collections against the current production
collections on the exact same query path search_v4 uses.

For every alias point in `searchpoc_aliases_noname`, query the matching
canonicals collection with the same veg + form-family filter v4 applies,
take top-1, and pair it against the production top-1 for the same alias.

Outputs:
  diagnostics/noname_vs_current.csv — one row per alias
  Console: summary stats, gainers, losers, floor crossings

This is the production-realistic A/B (filter + score), not raw cosine.
"""

from __future__ import annotations

import csv
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from core.connections import close_connections, get_qdrant_client
from scripts.search_v4 import _expand_form

CUR_ALIASES = "searchpoc_aliases"
CUR_CANONS = "searchpoc_canonicals"
NEW_ALIASES = "searchpoc_aliases_noname"
NEW_CANONS = "searchpoc_canonicals_noname"

OUT_CSV = Path("diagnostics/noname_vs_current.csv")
MAX_WORKERS = 16
CANDIDATE_FLOOR = 0.50  # match V4_CANDIDATE_FLOOR


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
        rel = _expand_form(form)
        if len(rel) == 1:
            must.append(FieldCondition(key="form", match=MatchValue(value=rel[0])))
        else:
            must.append(FieldCondition(key="form", match=MatchAny(any=rel)))
    return Filter(must=must) if must else None


def _scroll_all(qdrant, collection: str):
    out = []
    nxt = None
    while True:
        points, nxt = qdrant.scroll(
            collection_name=collection, offset=nxt, limit=200,
            with_payload=True, with_vectors=True,
        )
        for p in points:
            n = (p.payload.get("name") if p.payload else None) or ""
            if not n:
                continue
            out.append({
                "name": n,
                "veg": (p.payload.get("veg_type") or "").upper() or None,
                "form": (p.payload.get("form") or "").lower() or None,
                "vector": p.vector,
            })
        if nxt is None:
            break
    return out


def _top1(qdrant, collection: str, vec, veg, form):
    hits = qdrant.query_points(
        collection_name=collection,
        query=list(vec),
        limit=1,
        score_threshold=CANDIDATE_FLOOR,
        with_payload=True,
        query_filter=_build_filter(veg, form),
    ).points
    if not hits:
        return 0.0, ""
    return float(hits[0].score), (hits[0].payload.get("name") or "")


def _row(qdrant, cur_pt: dict, new_pt: dict) -> dict:
    cur_score, cur_top = _top1(qdrant, CUR_CANONS, cur_pt["vector"], cur_pt["veg"], cur_pt["form"])
    new_score, new_top = _top1(qdrant, NEW_CANONS, new_pt["vector"], new_pt["veg"], new_pt["form"])
    return {
        "alias": cur_pt["name"],
        "form": cur_pt["form"] or "",
        "veg": cur_pt["veg"] or "",
        "current_score": cur_score, "current_top1": cur_top,
        "noname_score": new_score, "noname_top1": new_top,
        "delta": new_score - cur_score,
        "same_match": cur_top == new_top,
    }


def main() -> None:
    qdrant = get_qdrant_client()

    print("Loading current alias vectors…")
    cur = {p["name"]: p for p in _scroll_all(qdrant, CUR_ALIASES)}
    print(f"  {len(cur)} aliases")

    print("Loading no-name alias vectors…")
    new = {p["name"]: p for p in _scroll_all(qdrant, NEW_ALIASES)}
    print(f"  {len(new)} aliases")

    common = sorted(set(cur) & set(new))
    print(f"Common aliases: {len(common)}\n")

    print(f"Running top-1 lookups against {CUR_CANONS} and {NEW_CANONS} ({MAX_WORKERS} workers)…")
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = [pool.submit(_row, qdrant, cur[n], new[n]) for n in common]
        done = 0
        for fut in as_completed(futs):
            rows.append(fut.result())
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(common)}")

    rows.sort(key=lambda r: -r["delta"])

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            r2 = dict(r)
            r2["current_score"] = f"{r['current_score']:.4f}"
            r2["noname_score"] = f"{r['noname_score']:.4f}"
            r2["delta"] = f"{r['delta']:+.4f}"
            w.writerow(r2)
    print(f"\nWrote {len(rows)} rows to {OUT_CSV}")

    # ── Summary ────────────────────────────────────────────────────────────
    n = len(rows)
    cur_s = [r["current_score"] for r in rows]
    new_s = [r["noname_score"] for r in rows]
    deltas = [r["delta"] for r in rows]
    same = sum(1 for r in rows if r["same_match"])

    def avg(xs): return sum(xs) / len(xs) if xs else 0.0
    def med(xs): return sorted(xs)[len(xs)//2] if xs else 0.0

    print()
    print("=" * 72)
    print(f"SUMMARY  (n={n})")
    print("=" * 72)
    print(f"  mean cur score : {avg(cur_s):.4f}")
    print(f"  mean new score : {avg(new_s):.4f}   (Δ = {avg(new_s)-avg(cur_s):+.4f})")
    print(f"  median Δ       : {med(deltas):+.4f}")
    big_up = sum(1 for d in deltas if d >= 0.05)
    big_dn = sum(1 for d in deltas if d <= -0.05)
    print(f"  Δ ≥ +0.05      : {big_up}  ({big_up/n*100:.1f}%)")
    print(f"  Δ ≤ -0.05      : {big_dn}  ({big_dn/n*100:.1f}%)")
    print(f"  same top-1     : {same}/{n}  ({same/n*100:.1f}%)")
    print(f"  top-1 changed  : {n-same}")

    print()
    print("Floor crossings (how many aliases pass a score bar):")
    for floor in (0.70, 0.75, 0.80, 0.85, 0.90):
        cp = sum(1 for s in cur_s if s >= floor)
        np_ = sum(1 for s in new_s if s >= floor)
        print(f"  ≥ {floor}: current {cp:>4}  →  noname {np_:>4}   (Δ = {np_-cp:+d})")

    # Per-form deltas
    print()
    print("Per-form mean Δ:")
    per_form: dict[str, list[float]] = {}
    for r in rows:
        per_form.setdefault(r["form"] or "(none)", []).append(r["delta"])
    for form, ds in sorted(per_form.items(), key=lambda kv: -avg(kv[1])):
        print(f"  {form:<22}  n={len(ds):>4}  meanΔ={avg(ds):+.4f}  medΔ={med(ds):+.4f}")

    print()
    print("Top 15 GAINERS:")
    for r in rows[:15]:
        print(f"  {r['delta']:+.3f}  [{r['form']:<14}] {r['alias'][:30]:<32}"
              f"  cur:{r['current_score']:.2f}→{(r['current_top1'] or '—')[:22]:<24}"
              f"  new:{r['noname_score']:.2f}→{(r['noname_top1'] or '—')[:22]}")

    print()
    print("Top 15 LOSERS:")
    for r in rows[-15:][::-1]:
        print(f"  {r['delta']:+.3f}  [{r['form']:<14}] {r['alias'][:30]:<32}"
              f"  cur:{r['current_score']:.2f}→{(r['current_top1'] or '—')[:22]:<24}"
              f"  new:{r['noname_score']:.2f}→{(r['noname_top1'] or '—')[:22]}")

    # Match changes — what flipped?
    changed = [r for r in rows if not r["same_match"]]
    print()
    print(f"Sample of match flips (cur → new), up to 20:")
    for r in changed[:20]:
        print(f"  [{r['form']:<14}] {r['alias'][:30]:<32}"
              f"  cur:{r['current_score']:.2f}→{(r['current_top1'] or '—')[:22]:<24}"
              f"  new:{r['noname_score']:.2f}→{(r['noname_top1'] or '—')[:22]}")

    close_connections()


if __name__ == "__main__":
    main()
