"""Audit item-to-item search quality across every Supabase alias.

For each alias item, fetch its stored vector and find its top-1 canonical hit
restricted to (same veg, same form-family). The resulting top-1 score is a
proxy for "how well does this item find its peer in the catalog?"

Outputs:
  - diagnostics/embedding_quality_audit.csv — every alias with top-1 score,
    top-1 name, form, veg, and llm_description length (proxy for metadata
    richness)
  - Console:
      * Score distribution histogram across the catalog
      * Bottom 30 items (the tail — likely culprits for "no results" queries)
      * Correlation between metadata length and top-1 score (sanity check)

Use the bottom-30 list to decide:
  - If items have sparse llm_description → re-enrich them (cheap fix)
  - If metadata is fine but score is still low → catalog gap / need hybrid search

Usage:
    python -m scripts.audit_embedding_quality
"""

from __future__ import annotations

import csv
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import mean, median

import numpy as np
from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from core.connections import close_connections, get_qdrant_client, neo4j_session

MAX_WORKERS = 16  # Qdrant cloud handles parallel reads well; bump if rate-limit OK

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ALIAS_COLLECTION = "searchpoc_aliases"
CANONICAL_COLLECTION = "searchpoc_canonicals"
OUT_CSV = Path("diagnostics/embedding_quality_audit.csv")
TAIL_SIZE = 30

# Mirrors v4/v5 FORM_FAMILIES.
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


FETCH_DESC_QUERY = """
MATCH (i:Item {source: 'supabase'})
WHERE i.name IN $names
RETURN i.name AS name, i.llm_description AS llm_description
"""


def _fetch_aliases(qdrant) -> list[tuple[str, str | None, str | None, np.ndarray]]:
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


def _histogram(scores: list[float]) -> None:
    bands = [(0.0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
    print(f"\n  Score distribution across {len(scores)} items:")
    for lo, hi in bands:
        n = sum(1 for s in scores if lo <= s < hi)
        bar = "█" * int(n / 2)
        print(f"    {lo:.2f}–{hi:.2f}  {n:>4}  {bar}")


def main() -> None:
    qdrant = get_qdrant_client()
    print("Fetching all Supabase aliases…")
    aliases = _fetch_aliases(qdrant)
    print(f"  {len(aliases)} aliases loaded")

    # Pull llm_description lengths in one shot to correlate with score.
    print("Fetching llm_description lengths from Neo4j…")
    names = [a[0] for a in aliases]
    desc_lengths: dict[str, int] = {}
    with neo4j_session() as session:
        for r in session.run(FETCH_DESC_QUERY, names=names):
            desc = r["llm_description"] or ""
            desc_lengths[r["name"]] = len(desc) if isinstance(desc, str) else len(str(desc))

    print(f"Running same-form, same-veg top-1 search for {len(aliases)} aliases "
          f"(parallel, {MAX_WORKERS} workers)…")

    def _score_one(item: tuple[str, str | None, str | None, np.ndarray]) -> tuple[str, str, str, float, str, int]:
        name, veg, form, vec = item
        flt = _build_filter(veg, form)
        hits = qdrant.query_points(
            collection_name=CANONICAL_COLLECTION,
            query=vec.tolist(),
            limit=2,
            score_threshold=0.0,
            with_payload=True,
            query_filter=flt,
        ).points
        filtered = [h for h in hits if (h.payload.get("name") or "").lower() != name.lower()]
        if not filtered:
            return (name, veg or "", form or "", 0.0, "", desc_lengths.get(name, 0))
        top = filtered[0]
        return (
            name, veg or "", form or "",
            float(top.score),
            top.payload.get("name") or "",
            desc_lengths.get(name, 0),
        )

    rows: list[tuple[str, str, str, float, str, int]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_score_one, a) for a in aliases]
        for fut in as_completed(futures):
            rows.append(fut.result())
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(aliases)}")

    # CSV
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["alias_name", "veg", "form", "top1_score", "top1_canonical", "desc_length"])
        for r in rows:
            w.writerow([r[0], r[1], r[2], f"{r[3]:.4f}", r[4], r[5]])
    print(f"\nWrote {len(rows)} rows to {OUT_CSV}")

    # Histogram
    scores = [r[3] for r in rows]
    _histogram(scores)

    # Tail
    tail = sorted(rows, key=lambda r: r[3])[:TAIL_SIZE]
    print(f"\n{'='*100}")
    print(f"BOTTOM {TAIL_SIZE} — these are the dishes that fail to find a good peer")
    print(f"{'='*100}")
    print(f"{'score':>7}  {'form':<22}{'veg':<8}{'desc_len':>9}  {'alias':<36} → top-1")
    print("-" * 100)
    for name, veg, form, score, top1, dlen in tail:
        print(f"{score:>7.3f}  {form:<22}{veg:<8}{dlen:>9}  {name:<36} → {top1}")

    # Metadata richness correlation
    print(f"\n{'='*100}")
    print("Correlation: does shorter llm_description correlate with lower top-1 score?")
    print(f"{'='*100}")
    by_len_band = defaultdict(list)
    for _, _, _, score, _, dlen in rows:
        if dlen == 0:
            band = "0 (empty)"
        elif dlen < 200:
            band = "1-199"
        elif dlen < 400:
            band = "200-399"
        elif dlen < 600:
            band = "400-599"
        else:
            band = "600+"
        by_len_band[band].append(score)
    for band in ["0 (empty)", "1-199", "200-399", "400-599", "600+"]:
        ss = by_len_band.get(band, [])
        if not ss:
            continue
        print(f"  desc_length {band:<12}  n={len(ss):>4}  mean_top1={mean(ss):.3f}  median={median(ss):.3f}")

    print()
    print("Next steps based on tail:")
    print("  - If tail items have desc_length=0 or very low → re-enrich those items.")
    print("  - If tail items have rich descriptions but low scores → embedding/catalog gap.")
    print("    Hybrid search (BM25) is the production fix.")

    close_connections()


if __name__ == "__main__":
    main()
