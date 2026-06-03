"""PM-spec platter search: fetch pre-stored alias vector, search canonicals.

Difference from search_v2:
    v2 re-embeds the user-selected item at query time using its Neo4j metadata.
    v3 fetches the *already-stored* alias vector from `searchpoc_aliases`
       (which was built from the same `build_item_embedding_text` schema)
       and uses it directly to search `searchpoc_canonicals`.

Same query result expected; difference is operational:
    - v3: 0 OpenAI calls per query (vector pulled from Qdrant)
    - v2: 1 OpenAI call per query (re-embed query items)

Validates the PM's recommended architecture:
    user pick (master list) → fetch stored embedding → similarity-search
    against catalog (actual items) with dietary filter.

Programmatic entry point:
    from scripts.search_v3 import search_platters_v3
    results = search_platters_v3("Paneer Butter Masala, Butter Naan")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from core.connections import close_connections, get_qdrant_client, neo4j_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ALIAS_COLLECTION = "searchpoc_aliases"
CANONICAL_COLLECTION = "searchpoc_canonicals"
TOP_K_CANONICALS = 5
ITEM_SCORE_THRESHOLD = 0.45
TOP_N_PLATTERS = 3

COVERAGE_WEIGHT = 0.7
QUALITY_WEIGHT = 0.3


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class CanonicalMatch:
    canonical_name: str
    score: float


@dataclass
class ItemMatch:
    query_item: str
    canonical: str | None
    platter_item_name: str | None
    score: float | None


@dataclass
class PlatterResultV3:
    id: str
    name: str
    platter_type: str
    veg: bool
    min_price: float | None
    max_price: float | None
    item_names: list[str]
    matches: list[ItemMatch] = field(default_factory=list)
    matched_count: int = 0
    coverage: float = 0.0
    quality: float = 0.0
    final_score: float = 0.0


# ---------------------------------------------------------------------------
# Neo4j queries
# ---------------------------------------------------------------------------

FETCH_ITEM_TYPES_QUERY = """
MATCH (i:Item {source: 'supabase'})
WHERE i.name IN $names
RETURN i.name AS name, i.itemType AS item_type
"""

FETCH_PLATTERS_WITH_ITEMS_QUERY = """
MATCH (p:Platter)
OPTIONAL MATCH (p)-[:CONTAINS]->(pi:Item)
WITH p, collect(DISTINCT pi.name) AS item_names,
     collect(DISTINCT {name: pi.name, item_type: pi.itemType}) AS item_data
RETURN p.id        AS id,
       p.name      AS name,
       p.type      AS platter_type,
       p.veg       AS veg,
       p.minPrice  AS min_price,
       p.maxPrice  AS max_price,
       item_names,
       item_data
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _veg_filter(item_type: str | None) -> Filter | None:
    """Dietary as a hard filter — never let similarity override dietary preference."""
    if item_type and item_type.upper() == "VEG":
        return Filter(must=[FieldCondition(key="veg_type", match=MatchValue(value="VEG"))])
    if item_type and item_type.upper() == "EGG":
        return Filter(must=[FieldCondition(key="veg_type", match=MatchAny(any=["EGG", "NONVEG"]))])
    return None


def _fetch_alias_vectors(qdrant, names: list[str]) -> dict[str, list[float]]:
    """Look up the stored alias vector for each user-selected name.

    Scrolls the alias collection and matches by payload `name`. Names without a
    matching alias vector are silently skipped (caller will see them missing
    from the returned dict and can decide how to handle).
    """
    name_set = set(names)
    found: dict[str, list[float]] = {}
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
            if n in name_set and n not in found:
                found[n] = p.vector
        if next_offset is None or len(found) == len(name_set):
            break
    return found


# ---------------------------------------------------------------------------
# Stage 1: For each query item, fetch stored vector → search canonicals
# ---------------------------------------------------------------------------

def _find_canonical_matches(
    qdrant,
    items: list[str],
    item_to_item_type: dict[str, str | None],
    name_to_vector: dict[str, list[float]],
) -> dict[str, list[CanonicalMatch]]:
    per_item: dict[str, list[CanonicalMatch]] = {}
    for item in items:
        vec = name_to_vector.get(item)
        if vec is None:
            log.warning("  %r → no alias vector found, skipping", item)
            per_item[item] = []
            continue
        hits = qdrant.query_points(
            collection_name=CANONICAL_COLLECTION,
            query=vec,
            limit=TOP_K_CANONICALS,
            score_threshold=ITEM_SCORE_THRESHOLD,
            with_payload=True,
            query_filter=_veg_filter(item_to_item_type.get(item)),
        ).points
        matches = [
            CanonicalMatch(canonical_name=h.payload.get("name", ""), score=float(h.score))
            for h in hits if h.payload.get("name")
        ]
        per_item[item] = matches
        if matches:
            top = matches[0]
            log.info("  %r → top: %r (score %.3f), %d candidates",
                     item, top.canonical_name, top.score, len(matches))
        else:
            log.info("  %r → no matches above threshold %.2f", item, ITEM_SCORE_THRESHOLD)
    return per_item


# ---------------------------------------------------------------------------
# Stage 2: Score platters
# ---------------------------------------------------------------------------

def _score_platters(
    session,
    items: list[str],
    per_item_matches: dict[str, list[CanonicalMatch]],
    item_to_item_type: dict[str, str | None],
) -> list[PlatterResultV3]:
    rows = session.run(FETCH_PLATTERS_WITH_ITEMS_QUERY)
    platters: list[PlatterResultV3] = []

    for rec in rows:
        item_names_in_platter = [n for n in (rec["item_names"] or []) if n]
        item_name_set = set(item_names_in_platter)
        item_type_lookup = {
            row["name"]: row["item_type"]
            for row in (rec["item_data"] or []) if row.get("name")
        }

        match_list: list[ItemMatch] = []
        match_scores: list[float] = []

        for item in items:
            query_item_type = item_to_item_type.get(item)
            best: ItemMatch | None = None
            best_score = -1.0
            for cand in per_item_matches.get(item, []):
                if cand.canonical_name not in item_name_set:
                    continue
                # Option B: dietary preference is enforced platter-side too
                pi_type = item_type_lookup.get(cand.canonical_name)
                if query_item_type == "VEG" and pi_type != "VEG":
                    continue
                if query_item_type == "EGG" and pi_type not in ("EGG", "NONVEG"):
                    continue
                if cand.score > best_score:
                    best_score = cand.score
                    best = ItemMatch(
                        query_item=item,
                        canonical=cand.canonical_name,
                        platter_item_name=cand.canonical_name,
                        score=cand.score,
                    )
            if best:
                match_list.append(best)
                match_scores.append(best.score)
            else:
                match_list.append(ItemMatch(query_item=item, canonical=None, platter_item_name=None, score=None))

        matched_count = sum(1 for m in match_list if m.canonical is not None)
        coverage = matched_count / len(items) if items else 0.0
        quality = (sum(match_scores) / len(match_scores)) if match_scores else 0.0
        final_score = COVERAGE_WEIGHT * coverage + QUALITY_WEIGHT * quality

        platters.append(PlatterResultV3(
            id=rec["id"],
            name=rec["name"] or "",
            platter_type=rec["platter_type"] or "",
            veg=bool(rec["veg"]),
            min_price=rec["min_price"],
            max_price=rec["max_price"],
            item_names=sorted(item_names_in_platter),
            matches=match_list,
            matched_count=matched_count,
            coverage=coverage,
            quality=quality,
            final_score=final_score,
        ))

    platters.sort(key=lambda p: (-p.final_score, -p.matched_count, -p.quality, p.name.lower()))
    return platters


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_platters_v3(query: str) -> list[PlatterResultV3]:
    """PM-spec search. Returns top-3 ranked PlatterResultV3."""
    items = [i.strip() for i in query.split(",") if i.strip()]
    if not items:
        return []

    qdrant = get_qdrant_client()

    # Fetch all pre-stored alias vectors in one scroll
    log.info("Fetching pre-stored vectors for %d items from '%s'", len(items), ALIAS_COLLECTION)
    name_to_vector = _fetch_alias_vectors(qdrant, items)
    missing = [i for i in items if i not in name_to_vector]
    if missing:
        log.warning("  Missing alias vectors for: %s", missing)

    with neo4j_session() as session:
        rows = session.run(FETCH_ITEM_TYPES_QUERY, names=items)
        item_to_item_type: dict[str, str | None] = {r["name"]: r["item_type"] for r in rows}

        per_item_matches = _find_canonical_matches(
            qdrant, items, item_to_item_type, name_to_vector,
        )

        platters = _score_platters(session, items, per_item_matches, item_to_item_type)

    return platters[:TOP_N_PLATTERS]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "Paneer Butter Masala, Veg Biryani, Butter Naan"
    log.info("Query: %r", q)
    results = search_platters_v3(q)
    for i, r in enumerate(results, 1):
        veg = "VEG" if r.veg else "NON-VEG"
        log.info(
            "#%d %s [%s] | matched %d/%d | score=%.3f (coverage=%.2f, quality=%.3f)",
            i, r.name, veg, r.matched_count, len(r.matches), r.final_score, r.coverage, r.quality,
        )
        for m in r.matches:
            if m.canonical:
                log.info("    ✅ %s → %s (score=%.3f)", m.query_item, m.platter_item_name, m.score)
            else:
                log.info("    ❌ %s → no match in this platter", m.query_item)
    close_connections()
