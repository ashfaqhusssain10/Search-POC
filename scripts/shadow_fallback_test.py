"""Shadow test for the in-platter substitute fallback.

Goal: measure whether a per-platter vector-similarity fallback can rescue
dishes that current v5 marks as ✗ (not covered), WITHOUT touching v5 itself.

For each scenario:
  1. Run search_platters_v5 with both rankers ("current", "coverage_dominant")
  2. For each top-3 platter, look at every uncovered query dish
  3. Compute cosine similarity between the dish's stored alias vector and
     the alias vectors of every canonical item in that platter's CONTAINS
  4. At thresholds T ∈ {0.60, 0.70, 0.80}, record whether the fallback would
     have rescued the dish, what item it picked, and the score

Outputs:
  - diagnostics/fallback_shadow_results.csv  (one row per dish×platter×T)
  - Console summary table: rescue rate per ranker × threshold

Usage:
    python -m scripts.shadow_fallback_test
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from core.connections import close_connections, get_qdrant_client, neo4j_session
from scripts.search_v5 import search_platters_v5, PlatterResultV5

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ALIAS_COLLECTION = "searchpoc_aliases"
CANONICAL_COLLECTION = "searchpoc_canonicals"
THRESHOLDS = (0.60, 0.70, 0.80)
TOP_N_PLATTERS_TO_INSPECT = 3
OUT_CSV = Path("diagnostics/fallback_shadow_results.csv")


SCENARIOS: list[tuple[str, list[str]]] = [
    # Originals from test_platter_scenarios.py
    ("orig: Mixed veg + non-veg",
     ["Achari Chicken Curry", "Mutton Dum Biryani", "Rumali Roti"]),
    ("orig: Strongly-named regional curry",
     ["Achari Chicken Curry"]),
    ("orig: Clean veg lunch",
     ["Paneer Butter Masala Curry", "Garlic Naan", "Bagara Rice", "Gulab Jamun"]),
    ("orig: All-veg simple",
     ["Dal Tadka", "Jeera Rice", "Phulka"]),
    ("orig: Pure non-veg",
     ["Mutton Biryani", "Raita", "Gulab Jamun"]),
    ("orig: Snacks only",
     ["Samosa", "Chicken 65"]),
    ("orig: South Indian breakfast",
     ["Idly", "Vada", "Sambar"]),
    ("orig: Premium party spread",
     ["Hariyali Paneer Tikka", "Mutton Biryani", "Butter Naan", "Gulab Jamun"]),

    # 20 new — all names verified in llm_cache/table2_supabase_aliases.csv
    ("supa-01: Strong-named regional curry alone",
     ["Gongura Mutton Curry"]),
    ("supa-02: Cross-veg meal",
     ["Hariyali Chicken Curry", "Chicken Dum Pulav", "Roti"]),
    ("supa-03: Clean veg lunch",
     ["Dal Makhani", "Plain Naan", "Jeera Matar Pulav", "Brownie"]),
    ("supa-04: North-Indian thali",
     ["Palak Paneer", "Aloo Paratha", "Boondi Raita"]),
    ("supa-05: Hyderabadi non-veg",
     ["Telangana Mutton Curry", "Boondi Raita", "Shahi Ka Tukda"]),
    ("supa-06: Snack-only",
     ["Chicken Pakora", "Veg Puff"]),
    ("supa-07: Veg-EGG mix",
     ["Egg Curry", "Jeera Matar Pulav", "Roti"]),
    ("supa-08: Party spread (large)",
     ["Marinated Chicken", "Prawn Biryani", "Plain Naan", "Shahi Ka Tukda", "Boondi Raita"]),
    ("supa-09: Andhra meal",
     ["Pappu Charu", "Gongura Mutton Curry", "Miriyala Rasam"]),
    ("supa-10: Indo-Chinese",
     ["Chilli Chicken Lollipop", "Chicken Fried Rice", "Gobi Manchurian"]),
    ("supa-11: Biryani-centric NONVEG",
     ["Prawn Biryani", "Mutton Marag Soup", "Shahi Ka Tukda"]),
    ("supa-12: All-dessert",
     ["Brownie", "Shahi Ka Tukda", "Kadhu Ka Kheer"]),
    ("supa-13: Premium veg",
     ["Kaju Paneer Butter Masala", "Sheermal Roti", "Corn Kaju Pulav", "Sticky Toffee Pudding"]),
    ("supa-14: Single niche dish",
     ["Mutton Nihari"]),
    ("supa-15: Tandoori-style starters",
     ["Marinated Chicken", "Thai Roasted Paneer", "Mango Pickle"]),
    ("supa-16: Wedding spread",
     ["Telangana Mutton Curry", "Chicken Pakora", "Paneer Majestic", "Shahi Ka Tukda", "Boondi Raita"]),
    ("supa-17: Andhra dal meal",
     ["Thotakura Pappu", "Telangana Chicken Dry", "Roti"]),
    ("supa-18: Breakfast-ish",
     ["Aloo Paratha", "Boondi Raita", "Tea"]),
    ("supa-19: Rice + curry minimal",
     ["Veg Pulav", "Dal Makhani"]),
    ("supa-20: Soup-led NONVEG",
     ["Chicken Hot and Sour Soup", "Chicken Fried Rice", "Chilli Garlic Fish"]),
]


# ---------------------------------------------------------------------------
# Vector helpers
# ---------------------------------------------------------------------------

def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two unit-or-non-unit vectors."""
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _scroll_vectors(qdrant, collection: str, names: set[str]) -> dict[str, tuple[np.ndarray, str | None, str | None]]:
    """Return name → (vector, veg_type, form) for every requested name found."""
    if not names:
        return {}
    out: dict[str, tuple[np.ndarray, str | None, str | None]] = {}
    next_offset = None
    while True:
        points, next_offset = qdrant.scroll(
            collection_name=collection,
            offset=next_offset,
            limit=200,
            with_payload=True,
            with_vectors=True,
        )
        for p in points:
            n = p.payload.get("name") if p.payload else None
            if n in names and n not in out:
                out[n] = (
                    np.asarray(p.vector, dtype=np.float32),
                    p.payload.get("veg_type"),
                    p.payload.get("form"),
                )
        if next_offset is None or len(out) == len(names):
            break
    return out


# Mirror v4's FORM_FAMILIES so fallback uses the same form-equivalence rules.
FORM_FAMILIES: list[set[str]] = [
    {"gravy", "stew"},
    {"dry-fry", "snack"},
    {"soup", "stew"},
]


def _forms_compatible(a: str | None, b: str | None) -> bool:
    """True if forms match exactly or live in the same family."""
    if not a or not b:
        return True  # unknown form → don't block
    a, b = a.strip().lower(), b.strip().lower()
    if a == b:
        return True
    for fam in FORM_FAMILIES:
        if a in fam and b in fam:
            return True
    return False


def _veg_compatible(query_veg: str | None, cand_veg: str | None) -> bool:
    """VEG↔VEG, NONVEG↔NONVEG, EGG↔{EGG,NONVEG}. Unknown = pass."""
    if not query_veg or not cand_veg:
        return True
    q, c = query_veg.upper(), cand_veg.upper()
    if q == "VEG":
        return c == "VEG"
    if q == "NONVEG":
        return c == "NONVEG"
    if q == "EGG":
        return c in ("EGG", "NONVEG")
    return True


# ---------------------------------------------------------------------------
# Main shadow
# ---------------------------------------------------------------------------

@dataclass
class ShadowRow:
    scenario: str
    ranker: str
    platter_rank: int
    platter_name: str
    query_dish: str
    matched_in_v5: bool
    matched_canonical: str | None
    v5_score: float
    fallback_substitute: str | None
    fallback_score: float
    threshold: float
    rescued: bool


def run_shadow() -> list[ShadowRow]:
    qdrant = get_qdrant_client()
    rows: list[ShadowRow] = []

    for label, dishes in SCENARIOS:
        print(f"  ▸ {label}")
        # Vectors for the user's query dishes (from alias collection)
        query_vectors = _scroll_vectors(qdrant, ALIAS_COLLECTION, set(dishes))
        missing_q = [d for d in dishes if d not in query_vectors]
        if missing_q:
            print(f"    ⚠ no alias vector for: {missing_q}")

        for ranker in ("current", "coverage_dominant"):
            platters = search_platters_v5(
                dishes, top_k_per_item=5, top_n=TOP_N_PLATTERS_TO_INSPECT, ranker=ranker
            )

            # Collect every canonical name appearing in any inspected platter,
            # so we fetch their vectors in one shot.
            all_canon_names: set[str] = set()
            for p in platters:
                all_canon_names |= set(p.all_items)
            canon_meta = _scroll_vectors(qdrant, CANONICAL_COLLECTION, all_canon_names)

            for rank, platter in enumerate(platters, 1):
                # Restrict fallback candidates to items in this platter that we have
                # vectors for.
                platter_items = {n: canon_meta[n] for n in platter.all_items if n in canon_meta}
                for m in platter.dish_matches:
                    matched_in_v5 = m.matched_canonical is not None
                    qmeta = query_vectors.get(m.query_item)

                    # Compute best fallback once; record per-threshold rescue flag.
                    best_sub: str | None = None
                    best_score = 0.0
                    if not matched_in_v5 and qmeta is not None:
                        qv, q_veg, q_form = qmeta
                        for cname, (cv, c_veg, c_form) in platter_items.items():
                            if not _veg_compatible(q_veg, c_veg):
                                continue
                            if not _forms_compatible(q_form, c_form):
                                continue
                            s = _cosine(qv, cv)
                            if s > best_score:
                                best_score = s
                                best_sub = cname

                    for T in THRESHOLDS:
                        rescued = (not matched_in_v5) and (best_score >= T)
                        rows.append(ShadowRow(
                            scenario=label,
                            ranker=ranker,
                            platter_rank=rank,
                            platter_name=platter.name,
                            query_dish=m.query_item,
                            matched_in_v5=matched_in_v5,
                            matched_canonical=m.matched_canonical,
                            v5_score=m.score,
                            fallback_substitute=best_sub,
                            fallback_score=best_score,
                            threshold=T,
                            rescued=rescued,
                        ))
    return rows


def write_csv(rows: list[ShadowRow]) -> None:
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "scenario", "ranker", "platter_rank", "platter_name",
            "query_dish", "matched_in_v5", "matched_canonical", "v5_score",
            "fallback_substitute", "fallback_score", "threshold", "rescued",
        ])
        for r in rows:
            w.writerow([
                r.scenario, r.ranker, r.platter_rank, r.platter_name,
                r.query_dish, r.matched_in_v5,
                r.matched_canonical or "",
                f"{r.v5_score:.4f}",
                r.fallback_substitute or "",
                f"{r.fallback_score:.4f}",
                f"{r.threshold:.2f}",
                r.rescued,
            ])


def summarize(rows: list[ShadowRow]) -> None:
    """Print rescue rates per ranker×threshold and a sample of top rescues."""
    print()
    print("=" * 90)
    print("Rescue rates (uncovered dishes that the fallback would have filled)")
    print("=" * 90)
    print(f"{'ranker':<22}{'threshold':<12}{'uncovered':<14}{'rescued':<12}{'rate':<8}")
    print("-" * 90)

    by_key: dict[tuple[str, float], tuple[int, int]] = {}
    for r in rows:
        if r.matched_in_v5:
            continue
        key = (r.ranker, r.threshold)
        unc, res = by_key.get(key, (0, 0))
        by_key[key] = (unc + 1, res + (1 if r.rescued else 0))

    for (ranker, T), (unc, res) in sorted(by_key.items()):
        rate = (res / unc * 100) if unc else 0.0
        print(f"{ranker:<22}{T:<12.2f}{unc:<14}{res:<12}{rate:.1f}%")

    print()
    print("=" * 90)
    print("Sample rescues at T=0.70 (sanity check — do these look reasonable?)")
    print("=" * 90)
    seen_pairs: set[tuple[str, str]] = set()
    shown = 0
    for r in rows:
        if r.threshold != 0.70 or not r.rescued:
            continue
        key = (r.query_dish, r.fallback_substitute or "")
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        print(f"  {r.query_dish:<32} → {r.fallback_substitute:<32} ({r.fallback_score:.3f})  "
              f"[{r.ranker} · {r.scenario}]")
        shown += 1
        if shown >= 25:
            break
    if shown == 0:
        print("  (no rescues at T=0.70)")


def main() -> None:
    print(f"Running shadow over {len(SCENARIOS)} scenarios × 2 rankers × top-{TOP_N_PLATTERS_TO_INSPECT} platters")
    rows = run_shadow()
    write_csv(rows)
    print(f"\nWrote {len(rows)} rows to {OUT_CSV}")
    summarize(rows)
    close_connections()


if __name__ == "__main__":
    main()
