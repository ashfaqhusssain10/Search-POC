"""Per-alias coverage report: where does each Supabase alias land against
the DynamoDB canonical catalog, and what's the recommended action?

For each of the 774 aliases this writes one row to
`diagnostics/alias_coverage_report.csv`:

  alias_name, alias_form, alias_veg,
  has_name_twin,           — is there a canonical with the same name?
  best_peer_score,         — top-1 cosine across the whole canonical catalog
  best_peer_canonical,
  best_peer_form,
  same_form,               — Y if the best peer shares the alias's form
  bucket                   — EXACT_TWIN / GOOD_PEER / WEAK_PEER / CATALOG_GAP
  recommendation           — OK / RE_ENRICH / FINE_TUNE / ADD_CANONICAL

Bucket rules:
  - EXACT_TWIN     : a canonical with the same name exists. Always OK.
  - GOOD_PEER      : no name twin, but best peer ≥ 0.80 (treat as same dish).
  - WEAK_PEER      : no name twin, best peer in [0.60, 0.80) (looks like a
                     defensible substitute but may need attention).
  - CATALOG_GAP    : best peer < 0.60. The catalog has no real equivalent.

Recommendation rules:
  - EXACT_TWIN                                                    → OK
  - GOOD_PEER + same_form                                         → OK
  - GOOD_PEER + cross_form                                        → RE_ENRICH
                  (same dish surfaced but the form labels disagree —
                   likely Gemini enrichment drift, cheap to fix)
  - WEAK_PEER  + same_form                                        → FINE_TUNE
                  (peer exists, embedding just doesn't rank it high enough —
                   the training-set fodder for contrastive fine-tuning)
  - WEAK_PEER  + cross_form                                       → RE_ENRICH
                  (form mismatch is the bigger lever; re-enrich first)
  - CATALOG_GAP                                                   → ADD_CANONICAL
                  (no algorithmic fix; ops needs to backfill)
"""

from __future__ import annotations

import csv
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from core.connections import close_connections, get_qdrant_client

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ALIAS_COLLECTION = "searchpoc_aliases"
CANONICAL_COLLECTION = "searchpoc_canonicals"
OUT_CSV = Path("diagnostics/alias_coverage_report.csv")
MAX_WORKERS = 16

# Thresholds for the bucketing — calibrated against the score distributions
# we saw earlier: exact-name twins cluster at 0.95+, real-substitute pairs
# at 0.70-0.85, weak-but-defensible at 0.60-0.70, garbage below 0.60.
GOOD_PEER_THRESHOLD = 0.80
WEAK_PEER_THRESHOLD = 0.60


def _scroll(qdrant, collection: str) -> list[tuple[str, str, str, np.ndarray]]:
    """Return [(name, veg, form, vector), …] for every point in a collection."""
    out: list[tuple[str, str, str, np.ndarray]] = []
    next_offset = None
    while True:
        points, next_offset = qdrant.scroll(
            collection_name=collection, offset=next_offset, limit=200,
            with_payload=True, with_vectors=True,
        )
        for p in points:
            n = (p.payload.get("name") if p.payload else None) or ""
            if not n:
                continue
            out.append((
                n,
                (p.payload.get("veg_type") or "").strip().upper(),
                (p.payload.get("form") or "").strip().lower(),
                np.asarray(p.vector, dtype=np.float32),
            ))
        if next_offset is None:
            break
    return out


def _best_peer(
    qdrant, name: str, veg: str, vec: np.ndarray,
) -> tuple[float, str, str]:
    """Top-1 across the entire canonical catalog with no form filter.
    veg filter is kept because cross-veg matches are never substitutes."""
    from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue
    flt = None
    if veg:
        if veg == "VEG":
            flt = Filter(must=[FieldCondition(key="veg_type", match=MatchValue(value="VEG"))])
        elif veg == "NONVEG":
            flt = Filter(must=[FieldCondition(key="veg_type", match=MatchValue(value="NONVEG"))])
        elif veg == "EGG":
            flt = Filter(must=[FieldCondition(key="veg_type", match=MatchAny(any=["EGG", "NONVEG"]))])

    hits = qdrant.query_points(
        collection_name=CANONICAL_COLLECTION,
        query=vec.tolist(),
        limit=5,
        score_threshold=0.0,
        with_payload=True,
        query_filter=flt,
    ).points
    if not hits:
        return 0.0, "", ""
    # Prefer the highest-scoring hit that isn't a self-name match — but if the
    # only hits ARE self-name, fall through and report the exact twin (mean
    # 0.97). The caller will see it as EXACT_TWIN via the has_name_twin flag.
    non_self = [h for h in hits if (h.payload.get("name") or "").lower() != name.lower()]
    pick = non_self[0] if non_self else hits[0]
    return (
        float(pick.score),
        pick.payload.get("name") or "",
        (pick.payload.get("form") or "").strip().lower(),
    )


def _classify(
    has_twin: bool, best_score: float, alias_form: str, peer_form: str,
) -> tuple[str, str]:
    """Return (bucket, recommendation)."""
    if has_twin:
        return "EXACT_TWIN", "OK"
    same_form = bool(alias_form) and bool(peer_form) and (alias_form == peer_form)
    if best_score >= GOOD_PEER_THRESHOLD:
        if same_form:
            return "GOOD_PEER", "OK"
        return "GOOD_PEER", "RE_ENRICH"
    if best_score >= WEAK_PEER_THRESHOLD:
        if same_form:
            return "WEAK_PEER", "FINE_TUNE"
        return "WEAK_PEER", "RE_ENRICH"
    return "CATALOG_GAP", "ADD_CANONICAL"


def main() -> None:
    qdrant = get_qdrant_client()
    print("Loading alias and canonical vectors…")
    aliases = _scroll(qdrant, ALIAS_COLLECTION)
    canonicals = _scroll(qdrant, CANONICAL_COLLECTION)
    canonical_names_lc = {c[0].lower() for c in canonicals}
    print(f"  aliases: {len(aliases)}   canonicals: {len(canonicals)}")

    print(f"Scoring best peer per alias ({MAX_WORKERS} workers)…")

    def _score_one(idx: int) -> dict:
        name, veg, form, vec = aliases[idx]
        score, peer_name, peer_form = _best_peer(qdrant, name, veg, vec)
        has_twin = name.lower() in canonical_names_lc
        bucket, rec = _classify(has_twin, score, form, peer_form)
        return {
            "alias_name": name,
            "alias_form": form,
            "alias_veg": veg,
            "has_name_twin": "Y" if has_twin else "N",
            "best_peer_score": score,
            "best_peer_canonical": peer_name,
            "best_peer_form": peer_form,
            "same_form": "Y" if form and peer_form and form == peer_form else "N",
            "bucket": bucket,
            "recommendation": rec,
        }

    rows: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_score_one, i) for i in range(len(aliases))]
        for fut in as_completed(futures):
            rows.append(fut.result())
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(aliases)}")

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "alias_name", "alias_form", "alias_veg",
            "has_name_twin", "best_peer_score", "best_peer_canonical",
            "best_peer_form", "same_form", "bucket", "recommendation",
        ])
        # Sort: most actionable first (CATALOG_GAPs at top so ops sees them)
        priority = {"ADD_CANONICAL": 0, "RE_ENRICH": 1, "FINE_TUNE": 2, "OK": 3}
        rows.sort(key=lambda r: (priority.get(r["recommendation"], 9), r["best_peer_score"]))
        for r in rows:
            w.writerow([
                r["alias_name"], r["alias_form"], r["alias_veg"],
                r["has_name_twin"], f"{r['best_peer_score']:.4f}",
                r["best_peer_canonical"], r["best_peer_form"],
                r["same_form"], r["bucket"], r["recommendation"],
            ])
    print(f"\nWrote {len(rows)} rows to {OUT_CSV}\n")

    # ── Summary ────────────────────────────────────────────────────────────
    by_bucket: dict[str, int] = defaultdict(int)
    by_rec: dict[str, int] = defaultdict(int)
    for r in rows:
        by_bucket[r["bucket"]] += 1
        by_rec[r["recommendation"]] += 1
    n = len(rows)

    print("=" * 80)
    print("BUCKET BREAKDOWN")
    print("=" * 80)
    for b in ("EXACT_TWIN", "GOOD_PEER", "WEAK_PEER", "CATALOG_GAP"):
        c = by_bucket.get(b, 0)
        print(f"  {b:<16} {c:>4}  ({c/n*100:5.1f}%)")

    print()
    print("=" * 80)
    print("RECOMMENDATION BREAKDOWN — what to do next")
    print("=" * 80)
    rec_order = ["ADD_CANONICAL", "RE_ENRICH", "FINE_TUNE", "OK"]
    desc = {
        "ADD_CANONICAL": "ops backfills the catalog — no algorithmic fix",
        "RE_ENRICH": "same dish exists but form-label drift — cheap to fix",
        "FINE_TUNE": "peer exists but ranked low — training data for embedding fine-tune",
        "OK": "system is working correctly for this alias",
    }
    for rec in rec_order:
        c = by_rec.get(rec, 0)
        print(f"  {rec:<16} {c:>4}  ({c/n*100:5.1f}%)   {desc[rec]}")

    # Show a sample from each non-OK bucket
    print()
    print("=" * 80)
    print("SAMPLE: 12 from each actionable bucket")
    print("=" * 80)
    for rec in ("ADD_CANONICAL", "RE_ENRICH", "FINE_TUNE"):
        sample = [r for r in rows if r["recommendation"] == rec][:12]
        if not sample:
            continue
        print(f"\n  --- {rec} ---")
        print(f"  {'score':>6}  {'alias':<36}{'alias_form':<18}{'best peer':<36}peer_form")
        for r in sample:
            print(f"  {r['best_peer_score']:>6.3f}  "
                  f"{r['alias_name'][:34]:<36}"
                  f"{(r['alias_form'] or '—')[:16]:<18}"
                  f"{(r['best_peer_canonical'] or '—')[:34]:<36}"
                  f"{r['best_peer_form'] or '—'}")

    close_connections()


if __name__ == "__main__":
    main()
