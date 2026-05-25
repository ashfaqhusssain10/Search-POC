"""Compare dense-only retrieval (v1 collections) vs hybrid dense+BM25 (v2).

For every Supabase alias, run the same query against both:
  - Dense-only:  query searchpoc_canonicals using stored alias dense vector
  - Hybrid:      query searchpoc_canonicals_v2 with Prefetch[dense, sparse]
                 fused via Reciprocal Rank Fusion (RRF)

Both queries use the same (veg_type, form) filter so the comparison is apples-
to-apples.

Outputs:
  - diagnostics/hybrid_vs_dense.csv — one row per alias with both modes' top-1
  - Console:
      * Distribution shift (how many items moved from low-score bands → higher)
      * Recovery cases (dense score < 0.55 → hybrid surfaces a sensible peer)
      * Regressions (dense was right, hybrid swapped to something worse)

Usage:
    python -m scripts.eval_hybrid_vs_dense
"""

from __future__ import annotations

import csv
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import mean

import numpy as np
from fastembed import SparseTextEmbedding
from qdrant_client.models import (
    FieldCondition,
    Filter,
    FusionQuery,
    MatchAny,
    MatchValue,
    Prefetch,
    SparseVector,
    Fusion,
)

from core.connections import close_connections, get_qdrant_client
from scripts.embed_items_hybrid import (
    COLLECTION_ALIASES as COLLECTION_ALIASES_V2,
    COLLECTION_CANONICALS as COLLECTION_CANONICALS_V2,
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    SPARSE_MODEL,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ALIAS_V1 = "searchpoc_aliases"
CANONICAL_V1 = "searchpoc_canonicals"
OUT_CSV = Path("diagnostics/hybrid_vs_dense.csv")
MAX_WORKERS = 12
RRF_LIMIT = 3  # take top-3 fused; report rank-1

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


def _fetch_aliases_v1(qdrant) -> list[tuple[str, str | None, str | None, np.ndarray]]:
    """v1 alias scroll — we need the dense vector + the name/form/veg payload."""
    out: list[tuple[str, str | None, str | None, np.ndarray]] = []
    next_offset = None
    while True:
        points, next_offset = qdrant.scroll(
            collection_name=ALIAS_V1, offset=next_offset, limit=200,
            with_payload=True, with_vectors=True,
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


def _skip_self(name: str, hits) -> list:
    return [h for h in hits if (h.payload.get("name") or "").lower() != name.lower()]


def dense_top1(qdrant, name: str, vec: np.ndarray, veg: str | None, form: str | None) -> tuple[float, str]:
    """v1 collection — same as the audit script."""
    hits = qdrant.query_points(
        collection_name=CANONICAL_V1,
        query=vec.tolist(),
        limit=2,
        score_threshold=0.0,
        with_payload=True,
        query_filter=_build_filter(veg, form),
    ).points
    filtered = _skip_self(name, hits)
    if not filtered:
        return 0.0, ""
    return float(filtered[0].score), filtered[0].payload.get("name") or ""


def hybrid_top1(
    qdrant, name: str, dense_vec: np.ndarray, sparse_vec: SparseVector,
    veg: str | None, form: str | None,
) -> tuple[float, str]:
    """v2 collection — RRF over (dense, sparse) prefetches with the same filter.
    Fused score is rank-based (Qdrant RRF returns a small fused score, not
    cosine). We compare ranks, not raw scores, for the recovery analysis."""
    flt = _build_filter(veg, form)
    res = qdrant.query_points(
        collection_name=COLLECTION_CANONICALS_V2,
        prefetch=[
            Prefetch(query=dense_vec.tolist(), using=DENSE_VECTOR_NAME, filter=flt, limit=20),
            Prefetch(query=sparse_vec, using=SPARSE_VECTOR_NAME, filter=flt, limit=20),
        ],
        query=FusionQuery(fusion=Fusion.RRF),
        limit=RRF_LIMIT + 1,
        with_payload=True,
    )
    filtered = _skip_self(name, res.points)
    if not filtered:
        return 0.0, ""
    return float(filtered[0].score), filtered[0].payload.get("name") or ""


def main() -> None:
    qdrant = get_qdrant_client()
    print("Loading sparse model…")
    sparse_model = SparseTextEmbedding(model_name=SPARSE_MODEL)

    print("Fetching v1 aliases (dense vectors + payload)…")
    aliases = _fetch_aliases_v1(qdrant)
    print(f"  {len(aliases)} aliases loaded")

    # For hybrid, we need a sparse query vector per alias. Compute them in one
    # batch from the dish name + (optionally) form. Using name alone is fine
    # here because BM25 weights rare tokens highly — dish names carry the
    # signal we need.
    print("Embedding sparse query vectors locally…")
    sparse_texts = [a[0] for a in aliases]
    sparse_vecs: list[SparseVector] = []
    for emb in sparse_model.embed(sparse_texts):
        sparse_vecs.append(SparseVector(indices=emb.indices.tolist(), values=emb.values.tolist()))
    print(f"  {len(sparse_vecs)} sparse vectors ready")

    print(f"Querying both modes in parallel ({MAX_WORKERS} workers)…")

    def _eval_one(idx: int) -> dict:
        name, veg, form, dense_vec = aliases[idx]
        sparse_vec = sparse_vecs[idx]
        d_score, d_top1 = dense_top1(qdrant, name, dense_vec, veg, form)
        h_score, h_top1 = hybrid_top1(qdrant, name, dense_vec, sparse_vec, veg, form)
        return {
            "alias": name, "veg": veg or "", "form": form or "",
            "dense_score": d_score, "dense_top1": d_top1,
            "hybrid_score": h_score, "hybrid_top1": h_top1,
        }

    results: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_eval_one, i) for i in range(len(aliases))]
        for fut in as_completed(futures):
            results.append(fut.result())
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(aliases)}")

    # ── CSV ─────────────────────────────────────────────────────────────────
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["alias", "veg", "form", "dense_score", "dense_top1",
                    "hybrid_top1", "agreement"])
        for r in results:
            agreement = "SAME" if r["dense_top1"].lower() == r["hybrid_top1"].lower() else "DIFF"
            if r["dense_top1"] == "" and r["hybrid_top1"] != "":
                agreement = "HYBRID_RESCUED"
            elif r["dense_top1"] != "" and r["hybrid_top1"] == "":
                agreement = "HYBRID_LOST"
            w.writerow([r["alias"], r["veg"], r["form"],
                        f"{r['dense_score']:.4f}", r["dense_top1"],
                        r["hybrid_top1"], agreement])
    print(f"\nWrote {len(results)} rows to {OUT_CSV}")

    # ── Summary ─────────────────────────────────────────────────────────────
    same = sum(1 for r in results if r["dense_top1"].lower() == r["hybrid_top1"].lower())
    diff = len(results) - same
    rescued = sum(1 for r in results if r["dense_top1"] == "" and r["hybrid_top1"] != "")
    lost = sum(1 for r in results if r["dense_top1"] != "" and r["hybrid_top1"] == "")
    print()
    print("=" * 90)
    print(f"AGREEMENT: same top-1: {same}/{len(results)} ({same/len(results)*100:.1f}%)")
    print(f"           different : {diff}/{len(results)} ({diff/len(results)*100:.1f}%)")
    print(f"           hybrid rescued (dense was empty, hybrid found one): {rescued}")
    print(f"           hybrid lost (dense found one, hybrid empty):         {lost}")

    # Recovery cases — dense was weak (<0.55), hybrid swapped to something different
    recoveries = [r for r in results
                  if r["dense_score"] < 0.55 and r["dense_top1"].lower() != r["hybrid_top1"].lower()
                  and r["hybrid_top1"]]
    print()
    print("=" * 90)
    print(f"RECOVERY CANDIDATES — dense score < 0.55 AND hybrid found a different top-1")
    print(f"  ({len(recoveries)} items — these are the production wins to eyeball)")
    print("=" * 90)
    print(f"{'dense':>7}  {'form':<18}{'alias':<36}{'dense top-1':<32}→ hybrid top-1")
    for r in sorted(recoveries, key=lambda x: x["dense_score"])[:40]:
        print(f"{r['dense_score']:>7.3f}  {r['form']:<18}{r['alias']:<36}"
              f"{r['dense_top1'][:30]:<32}→ {r['hybrid_top1']}")

    # Disagreements where dense was strong — possible regressions
    disagreements_strong = [r for r in results
                            if r["dense_score"] >= 0.80
                            and r["dense_top1"].lower() != r["hybrid_top1"].lower()
                            and r["hybrid_top1"]]
    print()
    print("=" * 90)
    print(f"POSSIBLE REGRESSIONS — dense ≥ 0.80 but hybrid picked a different top-1")
    print(f"  ({len(disagreements_strong)} items — eyeball to make sure hybrid isn't worse here)")
    print("=" * 90)
    print(f"{'dense':>7}  {'form':<18}{'alias':<36}{'dense top-1':<32}→ hybrid top-1")
    for r in sorted(disagreements_strong, key=lambda x: -x["dense_score"])[:25]:
        print(f"{r['dense_score']:>7.3f}  {r['form']:<18}{r['alias']:<36}"
              f"{r['dense_top1'][:30]:<32}→ {r['hybrid_top1']}")

    close_connections()


if __name__ == "__main__":
    main()
