"""v5: Platter-level search built on v4's item-to-item matching.

For each user-selected dish:
  1. Run search_items_v4 → top-K canonical hits with similarity scores
  2. Pool the canonical hits across all queries
  3. For every Platter that CONTAINS at least one pooled canonical, compute:
       - coverage   = (# of user dishes matched by any of this platter's items)
                       / (total user dishes)
       - quality    = average similarity score of the matches that landed
       - score      = 0.7 * coverage + 0.3 * quality
  4. Return top-N platters ranked by score, including the per-dish "matched
     as <canonical>" mapping so the user can see what filled each slot.

This reuses the cleaned, closed-vocab item embeddings — no community
detection, no LLM at query time, just one Qdrant call per dish + one
Cypher aggregation.

Programmatic entry point:
    from scripts.search_v5 import search_platters_v5
    results = search_platters_v5(["Paneer Butter Masala", "Butter Naan"], top_n=10)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.connections import close_connections, get_qdrant_client, neo4j_session
from scripts.search_v4 import ItemHit, search_items_v4

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

DEFAULT_TOP_K_PER_ITEM = 5    # canonical hits to consider per user dish
DEFAULT_TOP_N_PLATTERS = 10

# Map of UI-friendly service type labels → the raw Platter.type values stored
# in Neo4j. Used by the multiselect filter in app.py.
SERVICE_TYPE_LABELS: dict[str, str] = {
    "Delivery Box": "DELIVERYBOX",
    "Meal Box": "MEALBOX",
    "Snack Box": "SNACKBOX",
}

# Scoring weights. Coverage is the dominant signal — we want platters that
# cover all the user's dishes. Quality (avg similarity of matches) discriminates
# between candidates with equal coverage. Specificity (= what fraction of the
# platter's items the user actually asked for) tiebreaks among large
# 100%-coverage platters so a focused 6-item platter ranks above a 30-item
# everything-bag.
COVERAGE_WEIGHT = 0.55
QUALITY_WEIGHT = 0.30
SPECIFICITY_WEIGHT = 0.15


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class DishMatch:
    query_item: str
    matched_canonical: str | None  # None if no item in this platter matched the dish
    score: float                   # similarity score from v4 (0 if unmatched)


@dataclass
class SkeletonSlot:
    family: str
    slot_count: int
    order: int


@dataclass
class PlatterResultV5:
    platter_id: str
    name: str
    platter_type: str | None
    meal_type: str | None
    veg: bool | None
    min_price: float | None
    coverage: float           # 0.0 - 1.0
    quality: float            # 0.0 - 1.0  (avg sim score of matched dishes)
    specificity: float        # matched_items / intended_slot_count
    intended_slot_count: int  # sum of PlatterCategory.items_limit
    final_score: float        # weighted blend
    matched_count: int
    total_query_dishes: int
    dish_matches: list[DishMatch]
    skeleton: list[SkeletonSlot] = field(default_factory=list)
    all_items: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Neo4j: pull platter membership for the candidate canonicals
# ---------------------------------------------------------------------------

FETCH_PLATTERS_QUERY = """
MATCH (p:Platter)-[:CONTAINS]->(i:Item)
WHERE i.name IN $canonical_names
  AND ($service_types IS NULL OR p.type IN $service_types)
WITH p, collect(DISTINCT i.name) AS matched_items
MATCH (p)-[:CONTAINS]->(all_item:Item)
WITH p, matched_items, collect(DISTINCT all_item.name) AS all_items
OPTIONAL MATCH (p)-[:HAS_CATEGORY]->(pc:PlatterCategory)
WITH p, matched_items, all_items,
     collect(DISTINCT {
       family: coalesce(pc.category_family, pc.category_name_raw, 'Other'),
       slot_count: coalesce(pc.items_limit, 0),
       order: coalesce(pc.category_order, 999)
     }) AS skeleton_raw
RETURN p.id AS id,
       p.name AS name,
       p.type AS platter_type,
       p.mealType AS meal_type,
       p.veg AS veg,
       p.minPrice AS min_price,
       matched_items,
       all_items,
       skeleton_raw
"""


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def search_platters_v5(
    items: list[str],
    top_k_per_item: int = DEFAULT_TOP_K_PER_ITEM,
    top_n: int = DEFAULT_TOP_N_PLATTERS,
    service_types: list[str] | None = None,
) -> list[PlatterResultV5]:
    """Find platters that best cover the user's dish selection.

    Returns up to `top_n` platters sorted by a (coverage, quality) blend.
    """
    items = [i.strip() for i in items if i and i.strip()]
    if not items:
        return []

    # ── 1. Item-level matching via v4 ─────────────────────────────────────
    item_results = search_items_v4(items, top_k=top_k_per_item)

    # canonical_name → {query_dish: best_score_for_this_pair}
    # Tracking best-score-per-pair lets us pick the single best dish→canonical
    # mapping when one canonical appears in the hits of multiple queries.
    canonical_to_dish_scores: dict[str, dict[str, float]] = {}
    for r in item_results:
        for h in r.hits:
            slot = canonical_to_dish_scores.setdefault(h.name, {})
            prev = slot.get(r.query_item, 0.0)
            if h.score > prev:
                slot[r.query_item] = h.score

    candidate_canonicals = list(canonical_to_dish_scores.keys())
    if not candidate_canonicals:
        log.info("No canonical candidates from v4 — no platters can be ranked.")
        return []

    log.info("v4 produced %d candidate canonical items across %d user dishes",
             len(candidate_canonicals), len(items))

    # ── 2. Pull every platter that contains any candidate canonical ───────
    # `service_types` is passed as None when the user wants no filter — the
    # Cypher uses a null-aware predicate so a single query handles both cases.
    with neo4j_session() as session:
        rows = list(session.run(
            FETCH_PLATTERS_QUERY,
            canonical_names=candidate_canonicals,
            service_types=service_types if service_types else None,
        ))
    log.info("Found %d candidate platters", len(rows))

    # ── 3. Score each platter ─────────────────────────────────────────────
    n_dishes = len(items)
    results: list[PlatterResultV5] = []

    for row in rows:
        platter_items: list[str] = row["matched_items"]  # canonicals in this platter that matched something
        # For each user dish, find the best canonical in this platter that mapped to it.
        dish_matches: list[DishMatch] = []
        match_scores: list[float] = []
        for query_dish in items:
            best_canonical: str | None = None
            best_score = 0.0
            for canonical in platter_items:
                score = canonical_to_dish_scores.get(canonical, {}).get(query_dish, 0.0)
                if score > best_score:
                    best_score = score
                    best_canonical = canonical
            dish_matches.append(DishMatch(query_dish, best_canonical, best_score))
            if best_canonical is not None:
                match_scores.append(best_score)

        matched_count = len(match_scores)
        coverage = matched_count / n_dishes
        quality = sum(match_scores) / matched_count if match_scores else 0.0

        # Build the platter's category skeleton from PlatterCategory edges.
        # Aggregate slot counts per family (the same family can appear under
        # multiple PlatterCategory nodes — e.g. premium + standard slots).
        family_totals: dict[str, tuple[int, int]] = {}  # family → (slot_count, min_order)
        for entry in row["skeleton_raw"] or []:
            family = entry.get("family") or "Other"
            slot_count = int(entry.get("slot_count") or 0)
            order = int(entry.get("order") or 999)
            existing_count, existing_order = family_totals.get(family, (0, 999))
            family_totals[family] = (existing_count + slot_count, min(existing_order, order))
        skeleton = [
            SkeletonSlot(family=fam, slot_count=count, order=order)
            for fam, (count, order) in sorted(family_totals.items(), key=lambda kv: kv[1][1])
            if count > 0
        ]

        intended_slot_count = sum(s.slot_count for s in skeleton)
        # If a platter somehow has no skeleton data, fall back to all_items count
        # so we don't divide by zero — covers stragglers in legacy data.
        denominator = intended_slot_count or (len(row["all_items"]) or 1)
        specificity = min(matched_count / denominator, 1.0)
        final_score = (
            COVERAGE_WEIGHT * coverage
            + QUALITY_WEIGHT * quality
            + SPECIFICITY_WEIGHT * specificity
        )

        results.append(PlatterResultV5(
            platter_id=row["id"],
            name=row["name"],
            platter_type=row["platter_type"],
            meal_type=row["meal_type"],
            veg=row["veg"],
            min_price=row["min_price"],
            coverage=coverage,
            quality=quality,
            specificity=specificity,
            intended_slot_count=intended_slot_count,
            final_score=final_score,
            matched_count=matched_count,
            total_query_dishes=n_dishes,
            dish_matches=dish_matches,
            skeleton=skeleton,
            all_items=row["all_items"],
        ))

    # ── 4. Rank and trim ──────────────────────────────────────────────────
    results.sort(
        key=lambda r: (r.final_score, r.matched_count, r.quality, r.specificity),
        reverse=True,
    )
    return results[:top_n]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    raw = " ".join(sys.argv[1:]) or (
        "Paneer Butter Masala Curry, Garlic Naan, Bagara Rice, Gulab Jamun"
    )
    selected = [i.strip() for i in raw.split(",") if i.strip()]
    log.info("Query dishes: %s", selected)

    results = search_platters_v5(selected, top_k_per_item=5, top_n=10)
    if not results:
        log.info("No platters matched.")
        sys.exit(0)

    for i, r in enumerate(results, 1):
        price = f"  ₹{int(r.min_price)}" if r.min_price else ""
        meal = ", ".join(r.meal_type) if isinstance(r.meal_type, list) else (r.meal_type or "")
        type_bits = " · ".join(b for b in (r.platter_type or "", meal) if b)
        type_suffix = f"  ({type_bits})" if type_bits else ""
        log.info("")
        log.info("#%d  %s%s%s", i, r.name, type_suffix, price)
        log.info("    coverage=%.0f%% (%d/%d)  quality=%.3f  specificity=%.0f%% (%d/%d slots)  score=%.3f",
                 100 * r.coverage, r.matched_count, r.total_query_dishes,
                 r.quality, 100 * r.specificity, r.matched_count, r.intended_slot_count,
                 r.final_score)
        if r.skeleton:
            log.info("    skeleton: %s",
                     " · ".join(f"{s.slot_count} {s.family}" for s in r.skeleton))
        for m in r.dish_matches:
            if m.matched_canonical:
                arrow = (
                    f"= {m.matched_canonical} ({m.score:.3f})"
                    if m.matched_canonical.lower() != m.query_item.lower()
                    else f"({m.score:.3f})"
                )
                log.info("    ✓ %-30s %s", m.query_item, arrow)
            else:
                log.info("    ✗ %-30s (not in this platter)", m.query_item)

    close_connections()
