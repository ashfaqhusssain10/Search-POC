"""Full analysis of the pairwise dense matrix (no filtering).

Re-loads the matrix and joins alias + canonical form/veg info from Qdrant
payloads so each row carries context. Form/veg are NOT used to filter; they
are purely there so we can see what kinds of pairs are landing at what
similarity bands.

Writes:
  - diagnostics/pairwise_dense_matrix_enriched.csv
       columns: alias, alias_form, alias_veg, canonical, canonical_form,
                canonical_veg, dense_score, same_form, same_veg
  - Console:
       * Overall score distribution (190K pairs)
       * Per-alias top-1 stats (best peer for each query)
       * Same-form vs cross-form score distributions
       * Same-veg vs cross-veg score distributions
       * Cross-form pairs scoring > 0.80 (vector ignoring our form taxonomy)
       * Same-form pairs scoring < 0.40 (form labels disagree with embedding)
"""
from __future__ import annotations

import csv
import logging
from collections import defaultdict
from pathlib import Path
from statistics import mean, median

from core.connections import close_connections, get_qdrant_client

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ALIAS_COLLECTION = "searchpoc_aliases"
CANONICAL_COLLECTION = "searchpoc_canonicals"
SRC_CSV = Path("diagnostics/pairwise_dense_matrix.csv")
OUT_CSV = Path("diagnostics/pairwise_dense_matrix_enriched.csv")


def _scroll_payload(qdrant, collection: str) -> dict[str, tuple[str, str]]:
    """Return name → (form, veg) for every item in the collection."""
    out: dict[str, tuple[str, str]] = {}
    next_offset = None
    while True:
        points, next_offset = qdrant.scroll(
            collection_name=collection, offset=next_offset, limit=200,
            with_payload=True, with_vectors=False,
        )
        for p in points:
            n = (p.payload.get("name") if p.payload else None) or ""
            if not n:
                continue
            out[n] = (
                (p.payload.get("form") or "").strip().lower(),
                (p.payload.get("veg_type") or "").strip().upper(),
            )
        if next_offset is None:
            break
    return out


def _histogram(scores: list[float], title: str) -> None:
    bands = [(0.0, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.6),
             (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
    n = len(scores)
    print(f"\n  {title}  (n={n:,})")
    for lo, hi in bands:
        c = sum(1 for s in scores if lo <= s < hi)
        pct = c / n * 100 if n else 0
        bar = "█" * int(pct / 1.2)
        print(f"    {lo:.2f}–{hi:.2f}  {c:>7,}  {pct:>5.1f}%  {bar}")


def main() -> None:
    qdrant = get_qdrant_client()
    print("Fetching alias payloads…")
    alias_meta = _scroll_payload(qdrant, ALIAS_COLLECTION)
    print(f"  {len(alias_meta)} aliases")
    print("Fetching canonical payloads…")
    canon_meta = _scroll_payload(qdrant, CANONICAL_COLLECTION)
    print(f"  {len(canon_meta)} canonicals")

    print(f"\nReading {SRC_CSV}…")
    rows = list(csv.DictReader(SRC_CSV.open()))
    print(f"  {len(rows):,} rows loaded")

    # Enrich + write
    print(f"Writing enriched CSV to {OUT_CSV}…")
    same_form_scores: list[float] = []
    cross_form_scores: list[float] = []
    same_veg_scores: list[float] = []
    cross_veg_scores: list[float] = []
    all_scores: list[float] = []
    per_alias_best: dict[str, tuple[float, str]] = {}  # alias → (best_score, canonical)
    per_alias_form_best: dict[tuple[str, bool], list[float]] = defaultdict(list)  # (alias, same_form) → scores

    cross_form_high: list[tuple[str, str, str, str, float]] = []   # cross-form but high score
    same_form_low: list[tuple[str, str, str, str, float]] = []     # same-form but low score
    cross_veg_high: list[tuple[str, str, str, str, float]] = []    # cross-veg but high score

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "alias", "alias_form", "alias_veg",
            "canonical", "canonical_form", "canonical_veg",
            "dense_score", "same_form", "same_veg",
        ])
        for r in rows:
            alias = r["alias"]
            canon = r["canonical"]
            score = float(r["dense_score"])
            af, av = alias_meta.get(alias, ("", ""))
            cf, cv = canon_meta.get(canon, ("", ""))
            same_form = (af == cf) and bool(af)
            same_veg = (av == cv) and bool(av)
            w.writerow([alias, af, av, canon, cf, cv, f"{score:.4f}",
                        "Y" if same_form else "N", "Y" if same_veg else "N"])

            all_scores.append(score)
            (same_form_scores if same_form else cross_form_scores).append(score)
            (same_veg_scores if same_veg else cross_veg_scores).append(score)

            best = per_alias_best.get(alias)
            if best is None or score > best[0]:
                per_alias_best[alias] = (score, canon)

            per_alias_form_best[(alias, same_form)].append(score)

            if not same_form and score >= 0.80:
                cross_form_high.append((alias, af, canon, cf, score))
            if same_form and 0.0 < score < 0.40:
                same_form_low.append((alias, af, canon, cf, score))
            if not same_veg and score >= 0.80:
                cross_veg_high.append((alias, av, canon, cv, score))

    print(f"  wrote {len(rows):,} rows")

    # ── Distributions ──────────────────────────────────────────────────────
    print("\n" + "=" * 90)
    print("DISTRIBUTIONS")
    print("=" * 90)
    _histogram(all_scores, "All 190K pairs")
    _histogram(same_form_scores, "Same-form pairs")
    _histogram(cross_form_scores, "Cross-form pairs")
    _histogram(same_veg_scores, "Same-veg pairs")
    _histogram(cross_veg_scores, "Cross-veg pairs")

    # ── Per-alias best peer ────────────────────────────────────────────────
    best_scores = [b[0] for b in per_alias_best.values()]
    print("\n" + "=" * 90)
    print("PER-ALIAS BEST PEER (no filter — what's the absolute closest canonical for each alias?)")
    print("=" * 90)
    print(f"  n={len(best_scores)}  mean={mean(best_scores):.3f}  median={median(best_scores):.3f}")
    print(f"  min={min(best_scores):.3f}  max={max(best_scores):.3f}")
    bands = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
    for lo, hi in bands:
        c = sum(1 for s in best_scores if lo <= s < hi)
        print(f"  {lo:.2f}–{hi:.2f}  {c:>4}  ({c/len(best_scores)*100:.1f}%)")

    # ── Aliases whose best peer is in a different form ─────────────────────
    print("\n" + "=" * 90)
    print("ALIASES WHERE BEST PEER IS IN A DIFFERENT FORM (signals form-label disagreement)")
    print("=" * 90)
    cross_form_top1: list[tuple[str, str, str, str, float]] = []
    for alias, (score, canon) in per_alias_best.items():
        af, _ = alias_meta.get(alias, ("", ""))
        cf, _ = canon_meta.get(canon, ("", ""))
        if af and cf and af != cf:
            cross_form_top1.append((alias, af, canon, cf, score))
    cross_form_top1.sort(key=lambda x: -x[4])
    print(f"  {len(cross_form_top1)} aliases ({len(cross_form_top1)/len(per_alias_best)*100:.1f}%)")
    print(f"  Top 25 by score:")
    print(f"  {'score':>6}  {'alias':<32}{'alias_form':<18}{'canonical':<32}canon_form")
    for alias, af, canon, cf, score in cross_form_top1[:25]:
        print(f"  {score:>6.3f}  {alias[:30]:<32}{af[:16]:<18}{canon[:30]:<32}{cf}")

    # ── Same-form but low score ────────────────────────────────────────────
    print("\n" + "=" * 90)
    print(f"SAME-FORM BUT LOW SCORE (<0.40)  — embedding disagrees with form label")
    print(f"  ({len(same_form_low):,} pairs — showing 15 random)")
    print("=" * 90)
    if same_form_low:
        import random
        random.seed(0)
        sample = random.sample(same_form_low, min(15, len(same_form_low)))
        for alias, af, canon, cf, score in sorted(sample, key=lambda x: x[4]):
            print(f"  {score:>6.3f}  {alias[:30]:<32}{af[:16]:<18}→ {canon[:30]:<32}{cf}")

    # ── Cross-form but high score ──────────────────────────────────────────
    print("\n" + "=" * 90)
    print(f"CROSS-FORM BUT HIGH SCORE (≥0.80)  — vector ignores form taxonomy")
    print(f"  ({len(cross_form_high):,} pairs — showing top 20)")
    print("=" * 90)
    for alias, af, canon, cf, score in sorted(cross_form_high, key=lambda x: -x[4])[:20]:
        print(f"  {score:>6.3f}  {alias[:30]:<32}{af[:16]:<18}→ {canon[:30]:<32}{cf}")

    # ── Cross-veg but high score ───────────────────────────────────────────
    print("\n" + "=" * 90)
    print(f"CROSS-VEG BUT HIGH SCORE (≥0.80)  — veg taxonomy disagrees with embedding")
    print(f"  ({len(cross_veg_high):,} pairs — showing top 20)")
    print("=" * 90)
    for alias, av, canon, cv, score in sorted(cross_veg_high, key=lambda x: -x[4])[:20]:
        print(f"  {score:>6.3f}  {alias[:30]:<32}{av:<8}→ {canon[:30]:<32}{cv}")

    close_connections()


if __name__ == "__main__":
    main()
