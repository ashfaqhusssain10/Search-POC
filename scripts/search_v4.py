"""Pure item-to-item search. No platter ranking, no graph traversal.

For each user-selected item from the Supabase master list:
    1. Fetch its pre-stored vector from `searchpoc_aliases`
    2. Query `searchpoc_canonicals` with dietary filter
    3. Return top-K most-similar canonical items with scores

Use this to evaluate the raw matching quality of the embedding pipeline,
independent of any platter coverage / scoring logic.

Programmatic entry point:
    from scripts.search_v4 import search_items_v4
    results = search_items_v4(["Paneer Butter Masala", "Butter Naan"], top_k=5)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from core.connections import close_connections, get_qdrant_client, neo4j_session
from core.embedding_text import build_item_embedding_text

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ALIAS_COLLECTION = "searchpoc_aliases"
CANONICAL_COLLECTION = "searchpoc_canonicals"
DEFAULT_TOP_K = 5

# Display floor: hits below this score are never surfaced to the user. Grounded
# in diagnostics/search_quality.csv analysis — below 0.80 matches start losing
# dish identity and feel like generic category fallbacks ("any curry", "any
# biscuit"). The pipeline-level QDRANT_SCORE_THRESHOLD (~0.35) is still
# appropriate for ETL retrieval; this is the UI-display floor.
ITEM_SCORE_THRESHOLD = 0.80

# Two-tier quality labels for UI rendering. Hits below the Good lower bound are
# already filtered out by ITEM_SCORE_THRESHOLD.
QUALITY_EXCELLENT_MIN = 0.90
QUALITY_GOOD_MIN = 0.80


def quality_tier(score: float) -> str:
    """Return the quality tier label for a similarity score."""
    if score >= QUALITY_EXCELLENT_MIN:
        return "Excellent"
    if score >= QUALITY_GOOD_MIN:
        return "Good"
    return "Hidden"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ItemHit:
    name: str
    score: float
    veg_type: str | None
    form: str | None
    category: str | None
    embedding_text: str | None = None
    tier: str = "Hidden"


@dataclass
class ItemQueryResult:
    query_item: str
    veg_type: str | None
    hits: list[ItemHit]
    query_embedding_text: str | None = None
    # Form of the queried alias, surfaced so callers (v5) can apply per-form
    # display thresholds without re-querying the alias collection.
    query_form: str | None = None


# ---------------------------------------------------------------------------
# Neo4j: dietary lookup (only thing we need from the graph)
# ---------------------------------------------------------------------------

FETCH_ITEM_TYPES_QUERY = """
MATCH (i:Item {source: 'supabase'})
WHERE i.name IN $names
RETURN i.name AS name, i.itemType AS item_type, i.llm_description AS llm_description
"""

FETCH_CANONICAL_DESCRIPTIONS_QUERY = """
MATCH (i:Item {source: 'dynamodb'})
WHERE i.name IN $names
RETURN i.name AS name, i.llm_description AS llm_description
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Form families — semantically close forms that should match each other.
# Prevents misses when the alias and canonical LLM enrichments disagree on
# borderline categories (e.g. "stew" vs "gravy", "snack" vs "dry-fry").
# ---------------------------------------------------------------------------

FORM_FAMILIES: list[set[str]] = [
    {"gravy", "stew"},          # Dal Makhani, Dal Tadka, Pappu Charu-type items
    {"dry-fry", "snack"},       # Manchurian, Chicken 65, Tikka-type starters
    {"soup", "stew"},           # Broths / lentil soups
]


def _expand_form(form: str) -> list[str]:
    """Return the form plus any family relatives.

    >>> _expand_form("gravy")
    ['gravy', 'stew']
    """
    form_lower = form.strip().lower()
    expanded = {form_lower}
    for family in FORM_FAMILIES:
        if form_lower in family:
            expanded |= family
    return sorted(expanded)


def _build_filter(item_type: str | None, form: str | None) -> Filter | None:
    """Combined dietary + form filter.

    Dietary (hard match):
        VEG    → VEG only
        NONVEG → NONVEG only
        EGG    → EGG or NONVEG

    Form (family match, when known):
        Uses FORM_FAMILIES to expand the query form to include related forms.
        e.g. form="gravy" also matches "stew"; form="dry-fry" also matches "snack".
        Constrains the result to the same meal slot — a bread query won't
        surface a curry or starter just because of shared cuisine tokens.

    None / unknown values are skipped (defensive).
    """
    must: list[FieldCondition] = []
    if item_type:
        it = item_type.upper()
        if it == "VEG":
            must.append(FieldCondition(key="veg_type", match=MatchValue(value="VEG")))
        elif it == "NONVEG":
            must.append(FieldCondition(key="veg_type", match=MatchValue(value="NONVEG")))
        elif it == "EGG":
            must.append(FieldCondition(key="veg_type", match=MatchAny(any=["EGG", "NONVEG"])))
    if form:
        related = _expand_form(form)
        if len(related) == 1:
            must.append(FieldCondition(key="form", match=MatchValue(value=related[0])))
        else:
            must.append(FieldCondition(key="form", match=MatchAny(any=related)))
    return Filter(must=must) if must else None


def _fetch_alias_vectors(
    qdrant, names: list[str]
) -> tuple[dict[str, list[float]], dict[str, str | None]]:
    """Pull stored alias vectors and `form` by payload name.

    Returns (name → vector, name → form). The form is used as a hard filter
    on the canonical search so a bread query only returns breads, etc.
    """
    name_set = set(names)
    vectors: dict[str, list[float]] = {}
    forms: dict[str, str | None] = {}
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
            if n in name_set and n not in vectors:
                vectors[n] = p.vector
                forms[n] = p.payload.get("form") if p.payload else None
        if next_offset is None or len(vectors) == len(name_set):
            break
    return vectors, forms


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def search_items_v4(items: list[str], top_k: int = DEFAULT_TOP_K) -> list[ItemQueryResult]:
    """Pure item-to-item search. Returns per-query top-K canonical hits."""
    items = [i.strip() for i in items if i and i.strip()]
    if not items:
        return []

    qdrant = get_qdrant_client()

    log.info("Fetching pre-stored vectors for %d items", len(items))
    name_to_vector, name_to_form = _fetch_alias_vectors(qdrant, items)
    missing = [i for i in items if i not in name_to_vector]
    if missing:
        log.warning("  Missing alias vectors for: %s", missing)

    with neo4j_session() as session:
        rows = list(session.run(FETCH_ITEM_TYPES_QUERY, names=items))
        item_to_item_type: dict[str, str | None] = {r["name"]: r["item_type"] for r in rows}
        item_to_alias_desc: dict[str, str | None] = {r["name"]: r["llm_description"] for r in rows}

    results: list[ItemQueryResult] = []
    hit_names_collected: set[str] = set()
    pending: list[tuple[str, list, str | None, str | None]] = []  # (query_item, hits_raw, veg, form)

    for item in items:
        vec = name_to_vector.get(item)
        if vec is None:
            results.append(ItemQueryResult(
                query_item=item,
                veg_type=item_to_item_type.get(item),
                hits=[],
                query_form=name_to_form.get(item),
            ))
            continue
        veg = item_to_item_type.get(item)
        form = name_to_form.get(item)
        hits_raw = qdrant.query_points(
            collection_name=CANONICAL_COLLECTION,
            query=vec,
            limit=top_k,
            score_threshold=ITEM_SCORE_THRESHOLD,
            with_payload=True,
            query_filter=_build_filter(veg, form),
        ).points
        pending.append((item, hits_raw, veg, form))
        for h in hits_raw:
            n = h.payload.get("name") if h.payload else None
            if n:
                hit_names_collected.add(n)

    # Single Neo4j round-trip to fetch llm_description for every canonical hit.
    canonical_descs: dict[str, str | None] = {}
    if hit_names_collected:
        with neo4j_session() as session:
            for r in session.run(FETCH_CANONICAL_DESCRIPTIONS_QUERY, names=list(hit_names_collected)):
                canonical_descs[r["name"]] = r["llm_description"]

    for item, hits_raw, veg, form in pending:
        hits = [
            ItemHit(
                name=h.payload.get("name", ""),
                score=float(h.score),
                veg_type=h.payload.get("veg_type"),
                form=h.payload.get("form"),
                category=h.payload.get("category"),
                embedding_text=build_item_embedding_text(
                    name=h.payload.get("name", ""),
                    llm_description=canonical_descs.get(h.payload.get("name", "")),
                ),
                tier=quality_tier(float(h.score)),
            )
            for h in hits_raw if h.payload.get("name")
        ]
        results.append(
            ItemQueryResult(
                query_item=item,
                veg_type=veg,
                hits=hits,
                query_embedding_text=build_item_embedding_text(
                    name=item, llm_description=item_to_alias_desc.get(item)
                ),
                query_form=form,
            )
        )

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    raw = " ".join(sys.argv[1:]) or "Paneer Butter Masala, Garlic Naan, Gobi 65, Bagara Rice, Achari Chicken Curry"
    items = [i.strip() for i in raw.split(",") if i.strip()]
    log.info("Query items: %s", items)
    results = search_items_v4(items, top_k=5)
    for r in results:
        log.info("")
        log.info("─── %r (veg_type=%s) ───", r.query_item, r.veg_type)
        if not r.hits:
            log.info("    (no hits)")
            continue
        for rank, h in enumerate(r.hits, 1):
            log.info("    #%d  %.4f  %-35s  [%s, %s, %s]",
                     rank, h.score, h.name, h.veg_type, h.form, h.category)
    close_connections()
