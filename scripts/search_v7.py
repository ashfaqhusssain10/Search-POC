"""v7: Platter search with precomputed alias-resolution from MySQL RDS.

Runtime path is entirely zero-LLM, zero-Qdrant:
  1. Map each user-picked alias name → alias_item_id (in-memory).
  2. BatchGetItem the resolution records from DynamoDB.
  3. Collect every candidate canonical (best + top_k) across all user dishes.
  4. Pull every platter that contains any of those canonicals.
  5. For each platter × dish: pick the best match by tier
       Tier A — best_canonical is present in this platter (direct hit)
       Tier B — any top_k canonical is present (alternate, marked substitute)
  6. Compute coverage / quality / specificity, rank, return top-N.

Scoring matches v5 (fixed weights, same constants) so results are comparable
during cutover.

CLI:
    python -m scripts.search_v7 "Paneer Butter Masala, Garlic Naan"
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core import ddb_resolution, runtime_index
from core.platter_cache import get_cache as get_platter_cache

_WEIGHTS_PATH = Path(__file__).resolve().parents[1] / "core" / "category_weights.json"
_CATEGORY_WEIGHTS: dict[str, float] = json.loads(_WEIGHTS_PATH.read_text())
_DEFAULT_WEIGHT = 0.5

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Scoring weights — locked to match v5 so v7 vs v5 comparisons are apples-to-apples.
COVERAGE_WEIGHT = 0.55
QUALITY_WEIGHT = 0.30
SPECIFICITY_WEIGHT = 0.15

DEFAULT_TOP_N = 10

SERVICE_TYPE_LABELS: dict[str, str] = {
    "Delivery Box": "DELIVERYBOX",
    "Meal Box": "MEALBOX",
    "Snack Box": "SNACKBOX",
}


@dataclass
class DishMatchV7:
    query_item: str               # alias name user typed
    matched_canonical: str | None
    score: float                  # cosine score recorded at precompute time
    confidence: float             # LLM confidence on the resolution
    is_substitute: bool = False   # True if matched via top_k tier-B fallback
    decision_source: str | None = None  # llm / single_candidate / no_candidates / unknown_alias


@dataclass
class SkeletonSlot:
    family: str
    slot_count: int
    order: int


@dataclass
class PlatterResultV7:
    platter_id: str
    name: str
    platter_type: str | None
    meal_type: str | None
    veg: bool | None
    min_price: float | None
    coverage: float
    quality: float
    specificity: float
    intended_slot_count: int
    final_score: float
    matched_count: int
    total_query_dishes: int
    dish_matches: list[DishMatchV7]
    weighted_match_pct: float = 0.0   # category-weighted match % (0–100)
    skeleton: list[SkeletonSlot] = field(default_factory=list)
    all_items: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def _build_skeleton(skeleton_raw: list[dict[str, Any]] | None) -> list[SkeletonSlot]:
    family_totals: dict[str, tuple[int, int]] = {}
    for entry in skeleton_raw or []:
        family = entry.get("family") or "Other"
        slot_count = int(entry.get("slot_count") or 0)
        order = int(entry.get("order") or 999)
        existing_count, existing_order = family_totals.get(family, (0, 999))
        family_totals[family] = (existing_count + slot_count, min(existing_order, order))
    return [
        SkeletonSlot(family=fam, slot_count=count, order=order)
        for fam, (count, order) in sorted(family_totals.items(), key=lambda kv: kv[1][1])
        if count > 0
    ]


def search_platters_v7(
    user_dishes: list[str],
    top_n: int = DEFAULT_TOP_N,
    service_types: list[str] | None = None,
) -> list[PlatterResultV7]:
    """Find best platters for the user's dish selection. See module docstring."""
    user_dishes = [d.strip() for d in user_dishes if d and d.strip()]
    if not user_dishes:
        return []

    # ── 1. Resolve user alias names → alias_item_id via the in-memory index
    index = runtime_index.load()
    dish_to_id: dict[str, str | None] = {
        name: index.alias_name_to_id.get(name) for name in user_dishes
    }
    unknown = [n for n, i in dish_to_id.items() if not i]
    if unknown:
        log.warning("Unknown aliases (not in runtime index): %s", unknown)

    # ── 2. Batch-fetch resolution records from RDS
    known_ids = [i for i in dish_to_id.values() if i]
    resolutions_by_id = ddb_resolution.get_many(known_ids)
    log.info("Resolved %d/%d aliases from DynamoDB", len(resolutions_by_id), len(known_ids))

    # Per-dish resolution record (or None if missing/unknown)
    dish_to_resolution: dict[str, dict[str, Any] | None] = {
        name: resolutions_by_id.get(item_id) if item_id else None
        for name, item_id in dish_to_id.items()
    }

    # ── 3. Collect every canonical that could match anything (best + top_k union)
    candidate_canonicals: set[str] = set()
    for res in dish_to_resolution.values():
        if not res:
            continue
        if res.get("best_canonical"):
            candidate_canonicals.add(res["best_canonical"])
        for tk in res.get("top_k") or []:
            n = tk.get("name")
            if n:
                candidate_canonicals.add(n)

    if not candidate_canonicals:
        log.info("No candidate canonicals after resolution — returning empty.")
        return []

    # ── 4. Fetch every platter containing any candidate canonical
    platters = get_platter_cache().fetch_for_canonicals(
        list(candidate_canonicals),
        service_types=service_types if service_types else None,
    )
    log.info("Found %d candidate platters", len(platters))

    # ── 5. Pre-compute category weight per dish (constant across platters)
    cache = get_platter_cache()
    dish_weights: dict[str, float] = {}
    for dish in user_dishes:
        res = dish_to_resolution.get(dish)
        item_id = res.get("best_canonical_item_id") if res else None
        cat_name = cache.get_item_category_name(item_id) if item_id else None
        dish_weights[dish] = _CATEGORY_WEIGHTS.get(cat_name, _DEFAULT_WEIGHT) if cat_name else _DEFAULT_WEIGHT
    total_weight = sum(dish_weights.values()) or 1.0
    log.info("Dish weights: %s", {d: dish_weights[d] for d in user_dishes})

    # ── 6. Score each platter
    n_dishes = len(user_dishes)
    results: list[PlatterResultV7] = []

    for row in platters:
        platter_items_set = set(row["all_items"])
        dish_matches: list[DishMatchV7] = []
        match_scores: list[float] = []

        for dish in user_dishes:
            res = dish_to_resolution.get(dish)
            if not res:
                dish_matches.append(DishMatchV7(
                    query_item=dish,
                    matched_canonical=None,
                    score=0.0,
                    confidence=0.0,
                    decision_source="unknown_alias" if not dish_to_id[dish] else "no_resolution",
                ))
                continue

            best_canon = res.get("best_canonical")
            decision_source = res.get("decision_source")
            confidence = float(res.get("confidence") or 0.0)

            # Tier A — direct hit
            if best_canon and best_canon in platter_items_set:
                score = float(res.get("best_canonical_score") or 0.0)
                dish_matches.append(DishMatchV7(
                    query_item=dish,
                    matched_canonical=best_canon,
                    score=score,
                    confidence=confidence,
                    is_substitute=False,
                    decision_source=decision_source,
                ))
                match_scores.append(score)
                continue

            # Tier B — best top_k canonical that the platter has
            best_alt: str | None = None
            best_alt_score = 0.0
            for tk in res.get("top_k") or []:
                name = tk.get("name")
                if name and name != best_canon and name in platter_items_set:
                    s = float(tk.get("score") or 0.0)
                    if s > best_alt_score:
                        best_alt = name
                        best_alt_score = s
            if best_alt:
                dish_matches.append(DishMatchV7(
                    query_item=dish,
                    matched_canonical=best_alt,
                    score=best_alt_score,
                    confidence=confidence,
                    is_substitute=True,
                    decision_source=decision_source,
                ))
                match_scores.append(best_alt_score)
                continue

            # No match in this platter for this dish
            dish_matches.append(DishMatchV7(
                query_item=dish,
                matched_canonical=None,
                score=0.0,
                confidence=confidence,
                decision_source=decision_source,
            ))

        matched_count = len(match_scores)
        coverage = matched_count / n_dishes
        quality = sum(match_scores) / matched_count if match_scores else 0.0

        # Weighted match % — each dish contributes proportionally to its category weight
        weighted_sum = 0.0
        for m in dish_matches:
            w = dish_weights.get(m.query_item, _DEFAULT_WEIGHT)
            if m.matched_canonical and not m.is_substitute:
                quality_score = 1.0
            elif m.matched_canonical and m.is_substitute:
                quality_score = 0.5
            else:
                quality_score = 0.0
            weighted_sum += quality_score * w
        weighted_match_pct = (weighted_sum / total_weight) * 100

        skeleton = _build_skeleton(row.get("skeleton_raw"))
        intended_slot_count = sum(s.slot_count for s in skeleton)
        denominator = intended_slot_count or (len(row["all_items"]) or 1)
        specificity = min(matched_count / denominator, 1.0)
        final_score = (
            COVERAGE_WEIGHT * coverage
            + QUALITY_WEIGHT * quality
            + SPECIFICITY_WEIGHT * specificity
        )

        results.append(PlatterResultV7(
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
            weighted_match_pct=round(weighted_match_pct, 1),
            skeleton=skeleton,
            all_items=row["all_items"],
        ))

    results.sort(
        key=lambda r: (r.final_score, r.matched_count, r.quality, r.specificity),
        reverse=True,
    )
    return results[:top_n]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dishes", nargs="?",
                        default="Paneer Butter Masala, Garlic Naan, Bagara Rice, Gulab Jamun",
                        help="Comma-separated user-selected dishes")
    parser.add_argument("--top-n", type=int, default=10)
    args = parser.parse_args()

    selected = [d.strip() for d in args.dishes.split(",") if d.strip()]
    log.info("Query dishes: %s", selected)

    results = search_platters_v7(selected, top_n=args.top_n)
    if not results:
        log.info("No platters matched.")
        raise SystemExit(0)

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
                tag = "(sub)" if m.is_substitute else ""
                same = m.matched_canonical.lower() == m.query_item.lower()
                arrow = (
                    f"({m.score:.3f}, conf={m.confidence:.2f})"
                    if same
                    else f"= {m.matched_canonical} ({m.score:.3f}, conf={m.confidence:.2f}) {tag}"
                )
                log.info("    ✓ %-30s %s", m.query_item, arrow)
            else:
                log.info("    ✗ %-30s (not in this platter)", m.query_item)
