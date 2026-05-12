"""Query-time search: dish names → ranked platters.

Zero LLM calls at query time. Flow:
  1. Embed each item in the query separately (batch API call)
  2. Per-item Qdrant top-1 search → one community per item
  3. Neo4j: MATCH platters by HAS_COMMUNITY, rank by how many query communities they cover
  4. Return top-N platters with per-item match metadata

Usage (interactive):
    python -m scripts.search

Usage (programmatic):
    from scripts.search import search_platters
    results = search_platters("Chicken Fried Pieces, Dal Makhani, Garlic Naan")
"""

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from openai import OpenAI
from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from core.categories import build_category_family_counts, category_family, typecode_family
from core.connections import close_connections, get_qdrant_client, neo4j_session
from core.embedding_text import build_item_embedding_text
from core.settings import (
    EMBEDDING_MODEL,
    OPENAI_API_KEY,
    QDRANT_COLLECTION,
    QDRANT_SCORE_THRESHOLD,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

TOP_N_RESULTS = 3
CANDIDATE_POOL_SIZE = 15
SUGGESTION_THRESHOLD = 0.60  # Minimum similarity to suggest a culinary alternative
COMMUNITY_WEIGHT = 0.7
SKELETON_WEIGHT = 0.3

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PlatterResult:
    """A platter match with its community coverage score."""

    id: str
    name: str
    platter_type: str
    meal_type: list[str]
    veg: bool
    min_price: float | None
    max_price: float | None
    matched_communities: int
    total_communities: int
    coverage_ratio: float                        # matched / query_community_count
    matched_community_names: list[str]
    query_community_count: int                   # denominator: unique communities from item lookup
    item_to_community: dict[str, str | None] = field(default_factory=dict)  # item → community name
    item_to_community_id: dict[str, str | None] = field(default_factory=dict)  # item → community id
    item_to_category: dict[str, str | None] = field(default_factory=dict)  # item → category family
    query_category_counts: dict[str, int] = field(default_factory=dict)  # query family skeleton counts
    platter_category_counts: dict[str, int] = field(default_factory=dict)  # platter family skeleton counts
    platter_category_labels: list[str] = field(default_factory=list)  # platter raw category labels
    skeleton_coverage_score: float = 0.0
    final_score: float = 0.0
    matched_query_categories: list[str] = field(default_factory=list)
    missing_query_categories: list[str] = field(default_factory=list)
    missing_family_count: int = 0
    items: list[str] = field(default_factory=list)  # all items in this platter
    all_community_ids: list[str] = field(default_factory=list)  # every community in this platter
    item_community_map: dict[str, str] = field(default_factory=dict)  # community_id → platter item name
    suggested_alternatives: dict[str, str | None] = field(default_factory=dict)  # item → closest item name
    family_item_candidates: dict[str, list[dict[str, str | None]]] = field(default_factory=dict)
    community_summaries: dict[str, dict[str, Any]] = field(default_factory=dict)  # community_id → metadata
    community_item_types: dict[str, set[str]] = field(default_factory=dict)  # community_id → itemTypes in this platter


# ---------------------------------------------------------------------------
# Step 1: Embed items (batch)
# ---------------------------------------------------------------------------

def embed_items(client: OpenAI, items: list[str]) -> list[list[float]]:
    """Embed a list of item strings in a single API call. Returns one vector per item."""
    log.info("Embedding %d items: %s", len(items), items)
    response = client.embeddings.create(model=EMBEDDING_MODEL, input=items)
    return [d.embedding for d in response.data]


# ---------------------------------------------------------------------------
# Step 2: Per-item Qdrant top-1 community lookup
# ---------------------------------------------------------------------------


FETCH_COMMUNITY_TOP_TYPECODES = """
MATCH (i:Item {source: 'supabase'})-[:MEMBER_OF]->(c:Community)
WHERE c.id IN $community_ids AND i.typecode_name IS NOT NULL
WITH c.id AS cid, i.typecode_name AS tc, count(*) AS freq
ORDER BY cid, freq DESC, tc ASC
WITH cid, collect(tc) AS typecodes
RETURN cid, typecodes
"""


def _build_item_type_filter(item_type: str | None) -> Filter | None:
    """Return a Qdrant filter for dominant_item_type based on the queried item's itemType.

    VEG items must match VEG communities only (veg users see only veg).
    NONVEG items have no restriction (nonveg users see any platter).
    EGG items match EGG or NONVEG communities (egg dishes won't appear in VEG communities).
    """
    if item_type == "VEG":
        return Filter(must=[FieldCondition(key="dominant_item_type", match=MatchValue(value="VEG"))])
    if item_type == "EGG":
        return Filter(must=[FieldCondition(key="dominant_item_type", match=MatchAny(any=["EGG", "NONVEG"]))])
    return None


def find_best_community_with_hint(
    qdrant, session, vector: list[float], category_hint: str | None, item_type: str | None = None
) -> dict[str, Any] | None:
    """Return best community, preferring one whose dominant typecode matches the category hint.

    Fetches top-10 Qdrant candidates, then does one Neo4j query to get each community's
    dominant Supabase typecode. Picks the highest-scoring candidate whose typecode family
    matches the hint. Falls back to raw top-1 if no match found.

    item_type (VEG/NONVEG/EGG) restricts Qdrant search to communities of matching type
    so VEG queries never surface NONVEG communities.
    """
    veg_filter = _build_item_type_filter(item_type)
    results = qdrant.query_points(
        collection_name=QDRANT_COLLECTION,
        query=vector,
        limit=10,
        score_threshold=QDRANT_SCORE_THRESHOLD,
        with_payload=True,
        query_filter=veg_filter,
    ).points
    if not results:
        return None

    def _to_result(hit: Any) -> dict[str, Any]:
        return {
            "community_id": hit.payload.get("community_id", ""),
            "name": hit.payload.get("name", ""),
            "score": hit.score,
            "members": hit.payload.get("members", []),
            "variant_names": hit.payload.get("variant_names", []),
        }

    if not category_hint:
        return _to_result(results[0])

    candidate_ids = [hit.payload.get("community_id", "") for hit in results]
    rows = session.run(FETCH_COMMUNITY_TOP_TYPECODES, community_ids=candidate_ids)
    # Map community_id → set of typecode families present in that community
    cid_to_families: dict[str, set[str]] = {}
    for r in rows:
        families = {typecode_family(tc) for tc in r["typecodes"] if typecode_family(tc)}
        cid_to_families[r["cid"]] = families

    for hit in results:
        cid = hit.payload.get("community_id", "")
        if category_hint in cid_to_families.get(cid, set()):
            return _to_result(hit)

    return _to_result(results[0])


def find_closest_in_platter(
    qdrant, vector: list[float], platter_community_ids: list[str], threshold: float
) -> str | None:
    """Return the community_id of the closest community within the platter's own community set."""
    if not platter_community_ids:
        return None
    results = qdrant.query_points(
        collection_name=QDRANT_COLLECTION,
        query=vector,
        limit=1,
        score_threshold=threshold,
        with_payload=True,
        query_filter=Filter(
            must=[FieldCondition(key="community_id", match=MatchAny(any=platter_community_ids))]
        ),
    ).points
    if not results:
        return None
    return results[0].payload.get("community_id")


# ---------------------------------------------------------------------------
# Query skeleton helpers
# ---------------------------------------------------------------------------

FETCH_QUERY_COMMUNITY_CATEGORIES = """
MATCH (i:Item {source: 'dynamodb'})-[:MEMBER_OF]->(c:Community)
WHERE c.id IN $community_ids AND i.itemCategory IS NOT NULL
WITH c.id AS community_id, i.itemCategory AS category, count(*) AS freq
ORDER BY community_id, freq DESC, category ASC
RETURN community_id, collect(category)[0] AS category
"""


def fetch_community_categories(
    session,
    community_ids: list[str],
) -> dict[str, str]:
    """Return the dominant category family for each matched community."""
    if not community_ids:
        return {}
    result = session.run(FETCH_QUERY_COMMUNITY_CATEGORIES, community_ids=community_ids)
    categories: dict[str, str] = {}
    for record in result:
        community_id = record["community_id"]
        family = category_family(record["category"])
        if community_id and family:
            categories[community_id] = family
    return categories


def build_item_category_map(
    items: list[str],
    item_to_community_id: dict[str, str | None],
    community_to_category: dict[str, str],
) -> dict[str, str | None]:
    """Map each query item to its best-known normalized category."""
    return {
        item: community_to_category.get(item_to_community_id.get(item) or "")
        for item in items
    }


def compute_skeleton_metrics(
    query_category_counts: dict[str, int],
    platter_category_counts: dict[str, int],
) -> tuple[float, list[str], list[str]]:
    """Return skeleton coverage score plus matched and missing query families."""
    if not query_category_counts:
        return 0.0, [], []

    matched_categories: list[str] = []
    missing_categories: list[str] = []
    matched_slots = 0

    for category, query_count in query_category_counts.items():
        platter_count = platter_category_counts.get(category, 0)
        if platter_count:
            matched_categories.append(category)
            matched_slots += min(query_count, platter_count)
        else:
            missing_categories.append(category)

    total_slots = sum(query_category_counts.values())
    coverage = round(matched_slots / total_slots, 2) if total_slots else 0.0
    return coverage, matched_categories, missing_categories


def build_family_item_candidates(
    platter_item_candidates: list[dict[str, Any]],
) -> dict[str, list[dict[str, str | None]]]:
    """Group actual platter items by category family."""
    grouped: dict[str, list[dict[str, str | None]]] = {}
    for candidate in platter_item_candidates:
        family = candidate.get("category_family")
        item_name = candidate.get("item_name")
        if not family or not item_name:
            continue
        grouped.setdefault(family, []).append(
            {
                "item_name": item_name,
                "community_id": candidate.get("community_id"),
                "category_raw": candidate.get("category_raw"),
            }
        )
    return grouped


def fallback_same_family_candidate(
    query_item: str,
    candidates: list[dict[str, str | None]],
) -> str | None:
    """Fallback substitute when no community-backed suggestion is available."""
    if not candidates:
        return None
    ranked = sorted(
        candidates,
        key=lambda candidate: (
            SequenceMatcher(None, query_item.lower(), (candidate.get("item_name") or "").lower()).ratio(),
            candidate.get("item_name") or "",
        ),
        reverse=True,
    )
    return ranked[0].get("item_name")


# ---------------------------------------------------------------------------
# Step 3: Neo4j platter ranking
# ---------------------------------------------------------------------------

FETCH_SUPABASE_TYPECODES_QUERY = """
MATCH (i:Item {source: 'supabase'})
WHERE i.name IN $names
RETURN i.name AS name, i.typecode_name AS typecode
"""

FETCH_ITEM_TYPES_QUERY = """
MATCH (i:Item)
WHERE i.name IN $names AND i.itemType IS NOT NULL
RETURN i.name AS name, i.itemType AS item_type
"""


RESOLVE_CANONICAL_QUERY = """
MATCH (canonical:Item {source: 'dynamodb'})-[:VARIANT_OF]->(alias:Item {source: 'supabase', name: $name})
RETURN canonical.name AS canonical_name
LIMIT 1
"""

FETCH_ITEM_METADATA_QUERY = """
MATCH (i:Item)
WHERE i.name IN $names
RETURN i.name AS name,
       i.itemType AS item_type,
       i.typecode_name AS typecode,
       i.category_name AS category,
       i.llm_description AS description,
       i.source AS source
"""


FIND_VARIANT_SUBSTITUTE = """
MATCH (q:Item {source: 'dynamodb'})
WHERE q.name = $item_name
MATCH (q)-[:VARIANT_OF*1..2]-(related:Item)-[:MEMBER_OF]->(c:Community)
WHERE c.id IN $community_ids
RETURN related.name AS item_name, c.id AS community_id
LIMIT 1
"""

FETCH_ALL_PLATTERS_QUERY = """
MATCH (p:Platter)
OPTIONAL MATCH (p)-[:HAS_COMMUNITY]->(c:Community)
WITH p,
     count(DISTINCT c.id) AS total_communities,
     collect(DISTINCT c.id) AS all_community_ids,
     collect(DISTINCT {id: c.id, name: c.name, summary: c.summary_json}) AS community_details
OPTIONAL MATCH (p)-[:CONTAINS]->(pi:Item)
WITH p, total_communities, all_community_ids, community_details,
     collect(DISTINCT pi.name) AS item_names
OPTIONAL MATCH (p)-[:CONTAINS]->(pi2:Item)-[:MEMBER_OF]->(pi_comm:Community)
WITH p, total_communities, all_community_ids, community_details, item_names,
     collect(DISTINCT {item_name: pi2.name, community_id: pi_comm.id, item_type: pi2.itemType}) AS item_comm_pairs
OPTIONAL MATCH (p)-[:HAS_CATEGORY]->(pc:PlatterCategory)
WITH p, total_communities, all_community_ids, community_details, item_names, item_comm_pairs,
     collect(
         CASE
             WHEN coalesce(pc.category_family, pc.category_name_normalized) IS NULL
                  OR coalesce(pc.category_family, pc.category_name_normalized) = ''
             THEN NULL
             ELSE coalesce(pc.category_family, pc.category_name_normalized)
         END
     ) AS platter_category_names,
     collect(
         CASE
             WHEN pc.category_name_raw IS NULL OR pc.category_name_raw = ''
             THEN NULL
             ELSE pc.category_name_raw
         END
     ) AS platter_category_labels
OPTIONAL MATCH (p)-[:HAS_CATEGORY]->(pc2:PlatterCategory)-[:CONTAINS_ITEM]->(pi3:Item)
OPTIONAL MATCH (pi3)-[:MEMBER_OF]->(pi3_comm:Community)
WITH p, total_communities, all_community_ids, community_details, item_names, item_comm_pairs,
     platter_category_names, platter_category_labels,
     collect(
         DISTINCT {
             item_name: pi3.name,
             community_id: pi3_comm.id,
             category_family: coalesce(pc2.category_family, pc2.category_name_normalized),
             category_raw: pc2.category_name_raw
         }
     ) AS platter_item_candidates
RETURN p.id          AS id,
       p.name        AS name,
       p.type        AS platter_type,
       p.mealType    AS meal_type,
       p.veg         AS veg,
       p.minPrice    AS min_price,
       p.maxPrice    AS max_price,
       total_communities,
       all_community_ids,
       community_details,
       item_names,
       item_comm_pairs,
       platter_category_names,
       platter_category_labels,
       platter_item_candidates
"""


def fetch_all_platters(
    session,
    item_to_category: dict[str, str | None],
    query_category_counts: dict[str, int],
) -> list[PlatterResult]:
    """Fetch every discounted platter from Neo4j with community + category metadata."""
    result = session.run(FETCH_ALL_PLATTERS_QUERY)

    platters = []
    for rec in result:
        total = rec["total_communities"]

        community_summaries: dict[str, dict[str, Any]] = {}
        for detail in (rec["community_details"] or []):
            cid = detail["id"]
            summary_raw = detail["summary"]
            if cid and summary_raw:
                try:
                    community_summaries[cid] = (
                        json.loads(summary_raw) if isinstance(summary_raw, str) else summary_raw
                    )
                except (json.JSONDecodeError, ValueError):
                    pass

        platter_category_names = [
            name for name in (rec["platter_category_names"] or []) if name
        ]
        platter_category_counts = dict(sorted(Counter(platter_category_names).items()))
        platter_category_labels = sorted(
            {name for name in (rec["platter_category_labels"] or []) if name}
        )
        skeleton_coverage_score, matched_query_categories, missing_query_categories = (
            compute_skeleton_metrics(query_category_counts, platter_category_counts)
        )
        platter_item_candidates = [
            candidate
            for candidate in (rec["platter_item_candidates"] or [])
            if candidate.get("item_name")
        ]
        family_item_candidates = build_family_item_candidates(platter_item_candidates)

        item_community_map: dict[str, str] = {}
        # Maps community_id → set of itemTypes present in that community within this platter
        community_item_types: dict[str, set[str]] = {}
        for pair in (rec["item_comm_pairs"] or []):
            cid = pair.get("community_id")
            name = pair.get("item_name")
            itype = pair.get("item_type")
            if cid and name:
                item_community_map[cid] = name
                if itype:
                    community_item_types.setdefault(cid, set()).add(itype)

        platters.append(
            PlatterResult(
                id=rec["id"],
                name=rec["name"] or "",
                platter_type=rec["platter_type"] or "",
                meal_type=list(rec["meal_type"] or []),
                veg=bool(rec["veg"]),
                min_price=rec["min_price"],
                max_price=rec["max_price"],
                matched_communities=0,
                total_communities=total,
                coverage_ratio=0.0,
                matched_community_names=[],
                query_community_count=0,
                item_to_community={},
                item_to_community_id={},
                item_to_category=item_to_category,
                query_category_counts=query_category_counts,
                platter_category_counts=platter_category_counts,
                platter_category_labels=platter_category_labels,
                skeleton_coverage_score=skeleton_coverage_score,
                final_score=0.0,
                matched_query_categories=matched_query_categories,
                missing_query_categories=missing_query_categories,
                missing_family_count=len(missing_query_categories),
                items=sorted(rec["item_names"] or []),
                all_community_ids=list(rec["all_community_ids"] or []),
                item_community_map=item_community_map,
                family_item_candidates=family_item_candidates,
                community_summaries=community_summaries,
                community_item_types=community_item_types,
            )
        )
    log.info("Fetched %d platters from Neo4j", len(platters))
    return platters


# ---------------------------------------------------------------------------
# Step 4: Coverage scoring via community-set intersection
# ---------------------------------------------------------------------------


def _community_satisfies_item_type(
    platter_item_types: set[str], query_item_type: str | None
) -> bool:
    """Return True if the platter's items in this community satisfy the query item's type constraint.

    VEG query item → platter must have at least one VEG item in that community.
    EGG query item → platter must have at least one EGG or NONVEG item in that community.
    NONVEG query item → any item type is fine.
    No itemType on query item → no constraint (pass through).
    """
    if not query_item_type or not platter_item_types:
        return True
    if query_item_type == "VEG":
        return "VEG" in platter_item_types
    if query_item_type == "EGG":
        return bool(platter_item_types & {"EGG", "NONVEG"})
    return True  # NONVEG — no restriction


def compute_coverage_from_seeds(
    platters: list[PlatterResult],
    items: list[str],
    seed_item_to_community_id: dict[str, str | None],
    item_community_name_map: dict[str, str],
    item_to_item_type: dict[str, str | None] | None = None,
) -> None:
    """Score each platter by community-intersection with the query's seed communities.

    For each query item, checks whether its globally-matched community_id is present
    in the platter's own community set. O(platters × items) — no Qdrant calls.
    For VEG query items, also verifies the matched community contains a VEG item in
    this specific platter (Option B veg matching).
    Mutates each PlatterResult with updated coverage metrics.
    """
    item_types = item_to_item_type or {}
    query_total = len(items)
    for platter in platters:
        platter_cids = set(platter.all_community_ids)
        matched_dish_count = 0
        matched_cids: set[str] = set()
        matched_names: list[str] = []
        new_item_to_community: dict[str, str | None] = {}
        new_item_to_community_id: dict[str, str | None] = {}

        for item in items:
            cid = seed_item_to_community_id.get(item)
            platter_types_for_cid = platter.community_item_types.get(cid or "", set())
            query_itype = item_types.get(item)
            if (
                cid
                and cid in platter_cids
                and _community_satisfies_item_type(platter_types_for_cid, query_itype)
            ):
                name = item_community_name_map.get(cid, cid)
                new_item_to_community[item] = name
                new_item_to_community_id[item] = cid
                matched_dish_count += 1
                if cid not in matched_cids:
                    matched_names.append(name)
                matched_cids.add(cid)
            else:
                new_item_to_community[item] = None
                new_item_to_community_id[item] = None

        platter.item_to_community = new_item_to_community
        platter.item_to_community_id = new_item_to_community_id
        platter.matched_communities = matched_dish_count
        platter.matched_community_names = matched_names
        platter.query_community_count = query_total
        platter.coverage_ratio = round(matched_dish_count / query_total, 2) if query_total else 0.0
        platter.final_score = round(
            (COMMUNITY_WEIGHT * platter.coverage_ratio)
            + (SKELETON_WEIGHT * platter.skeleton_coverage_score),
            3,
        )


def sort_platters_by_final_score(platters: list[PlatterResult]) -> list[PlatterResult]:
    """Sort candidates by final_score descending, with deterministic tiebreakers."""
    return sorted(
        platters,
        key=lambda platter: (
            -platter.final_score,
            -platter.matched_communities,
            -platter.coverage_ratio,
            platter.missing_family_count,
            platter.total_communities,
            platter.name.lower(),
        ),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_canonical_names(session, items: list[str]) -> dict[str, str]:
    """Resolve Supabase alias names to their DynamoDB canonical via VARIANT_OF.

    Returns a mapping of original item name → canonical name to embed.
    Items with no alias match are returned unchanged (they may already be canonical).
    """
    resolved: dict[str, str] = {}
    for item in items:
        row = session.run(RESOLVE_CANONICAL_QUERY, name=item).single()
        if row:
            canonical = row["canonical_name"]
            resolved[item] = canonical
            log.info("  %r → resolved canonical %r", item, canonical)
        else:
            resolved[item] = item
    return resolved


def search_platters(query: str) -> list[PlatterResult]:
    """Main entry point. Returns ranked PlatterResult list for a comma-separated dish query."""
    items = [i.strip() for i in query.split(",") if i.strip()]
    if not items:
        return []

    openai_client = OpenAI(api_key=OPENAI_API_KEY)
    qdrant = get_qdrant_client()

    # Resolve Supabase alias names → DynamoDB canonicals before embedding
    with neo4j_session() as session:
        item_to_canonical = resolve_canonical_names(session, items)
        typecode_rows = session.run(FETCH_SUPABASE_TYPECODES_QUERY, names=items)
        item_to_typecode: dict[str, str | None] = {r["name"]: r["typecode"] for r in typecode_rows}
        item_type_rows = session.run(FETCH_ITEM_TYPES_QUERY, names=items)
        item_to_item_type: dict[str, str | None] = {r["name"]: r["item_type"] for r in item_type_rows}

        # Fetch full metadata (incl. llm_description) for canonicals so we can
        # embed enriched text symmetric with the community-side vectors.
        # Prefer dynamodb source when the same name exists in both.
        canonical_names = list({item_to_canonical[i] for i in items})
        meta_rows = session.run(FETCH_ITEM_METADATA_QUERY, names=canonical_names)
        canonical_metadata: dict[str, dict[str, Any]] = {}
        for r in meta_rows:
            existing = canonical_metadata.get(r["name"])
            if existing and existing.get("source") == "dynamodb":
                continue
            canonical_metadata[r["name"]] = {
                "item_type": r["item_type"],
                "typecode": r["typecode"],
                "category": r["category"],
                "description": r["description"],
                "source": r["source"],
            }

    # Build category hints from typecodes up front — needed for community fallback below
    item_to_category: dict[str, str | None] = {}
    for item in items:
        family = typecode_family(item_to_typecode.get(item))
        if family:
            item_to_category[item] = family
            log.info("  %r category from typecode %r → %r", item, item_to_typecode.get(item), family)
        else:
            item_to_category[item] = None

    # Build enriched embedding text per canonical (name + metadata + llm_description).
    # Embedding rich text instead of bare names disambiguates same-token dishes
    # like Paneer Butter Masala (North Indian gravy) vs Paneer Manchurian (Indo-Chinese).
    canonical_items = [item_to_canonical[item] for item in items]
    enriched_texts: list[str] = []
    for canonical in canonical_items:
        meta = canonical_metadata.get(canonical, {})
        enriched_texts.append(
            build_item_embedding_text(
                name=canonical,
                item_type=meta.get("item_type"),
                typecode=meta.get("typecode"),
                category=meta.get("category"),
                llm_description=meta.get("description"),
            )
        )
    vectors = embed_items(openai_client, enriched_texts)

    # Global top-1 lookup per dish — determines community assignment and coverage
    seed_community_ids: list[str] = []
    seed_item_to_community_id: dict[str, str | None] = {}
    item_community_name_map: dict[str, str] = {}  # community_id → community name

    with neo4j_session() as session:
        for item, vector in zip(items, vectors):
            canonical = item_to_canonical[item]
            hint = item_to_category.get(item)
            itype = item_to_item_type.get(item)
            comm = find_best_community_with_hint(qdrant, session, vector, hint, itype)
            if comm:
                cid = comm["community_id"]
                if cid not in seed_community_ids:
                    seed_community_ids.append(cid)
                seed_item_to_community_id[item] = cid
                item_community_name_map[cid] = comm["name"]
                log.info("  %r (canonical: %r) → community %r (score %.3f)", item, canonical, comm["name"], comm["score"])
            else:
                seed_item_to_community_id[item] = None
                log.info("  %r (canonical: %r) → no community found above threshold", item, canonical)

        # Fallback: community-derived category for items without a Supabase typecode
        missing = [item for item, cat in item_to_category.items() if cat is None]
        if missing:
            missing_cids = [seed_item_to_community_id[m] for m in missing if seed_item_to_community_id.get(m)]
            community_to_category = fetch_community_categories(session, missing_cids)
            for item in missing:
                cid = seed_item_to_community_id.get(item)
                item_to_category[item] = community_to_category.get(cid or "") or None

        query_category_counts = build_category_family_counts(list(item_to_category.values()))
        candidates = fetch_all_platters(
            session,
            item_to_category=item_to_category,
            query_category_counts=query_category_counts,
        )

    # Coverage via community-set intersection — no additional Qdrant calls needed.
    compute_coverage_from_seeds(candidates, items, seed_item_to_community_id, item_community_name_map, item_to_item_type)
    results = sort_platters_by_final_score(candidates)[:TOP_N_RESULTS]

    item_vector_map = dict(zip(items, vectors))

    # For each missing query item, suggest a substitute from actual platter items.
    # Pass 1: same category family via vector search (precise).
    # Pass 2: VARIANT_OF graph traversal — find a semantic variant in the platter (broad fallback).
    with neo4j_session() as session:
        for platter in results:
            for item in items:
                per_platter_comm = platter.item_to_community.get(item)
                if per_platter_comm is not None:
                    continue
                vec = item_vector_map[item]
                suggestion = None

                # Pass 1: same-family vector search
                family = platter.item_to_category.get(item)
                if family:
                    same_family_candidates = platter.family_item_candidates.get(family, [])
                    same_family_community_ids = [
                        c["community_id"] for c in same_family_candidates if c.get("community_id")
                    ]
                    if same_family_community_ids:
                        closest_cid = find_closest_in_platter(
                            qdrant, vec, same_family_community_ids, SUGGESTION_THRESHOLD
                        )
                        if closest_cid:
                            for c in same_family_candidates:
                                if c.get("community_id") == closest_cid:
                                    suggestion = c.get("item_name")
                                    break
                    if not suggestion:
                        suggestion = fallback_same_family_candidate(item, same_family_candidates)

                # Pass 2: VARIANT_OF graph traversal — walk 1–2 hops to find a related item
                # whose community exists in this platter.
                if not suggestion and platter.all_community_ids:
                    row = session.run(
                        FIND_VARIANT_SUBSTITUTE,
                        item_name=item,
                        community_ids=platter.all_community_ids,
                    ).single()
                    if row:
                        suggestion = row["item_name"]

                # Pass 2 (alternative, unused): broad vector fallback across all platter communities.
                # Kept for reference — threshold tuning proved unreliable vs graph traversal.
                # if not suggestion and platter.all_community_ids:
                #     closest_cid = find_closest_in_platter(
                #         qdrant, vec, platter.all_community_ids, QDRANT_SCORE_THRESHOLD
                #     )
                #     if closest_cid:
                #         suggestion = platter.item_community_map.get(closest_cid)

                platter.suggested_alternatives[item] = suggestion
                log.info("  %r not in platter → substitute: %r", item, suggestion)

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _format_result(rank: int, r: PlatterResult) -> str:
    price_str = (
        f"₹{int(r.min_price)}–₹{int(r.max_price)}"
        if r.min_price and r.max_price
        else "price N/A"
    )
    veg_str = "VEG" if r.veg else "NON-VEG"
    coverage_str = f"{r.matched_communities}/{r.query_community_count} dishes ({r.coverage_ratio:.0%})"
    skeleton_str = f"{r.skeleton_coverage_score:.0%} menu fit"

    item_lines = []
    for item, comm_name in r.item_to_community.items():
        category_name = r.item_to_category.get(item)
        category_suffix = f" [{category_name}]" if category_name else ""
        matched = r.item_to_community_id.get(item) is not None
        if matched and comm_name:
            item_lines.append(f"  ✓ {item}{category_suffix} → {comm_name}")
        elif comm_name:
            suggestion = r.suggested_alternatives.get(item)
            if suggestion:
                item_lines.append(
                    f"  ~ {item}{category_suffix} → {comm_name} (same-family substitute: {suggestion})"
                )
            else:
                item_lines.append(f"  ~ {item}{category_suffix} → {comm_name} (platter doesn't have this)")
        else:
            item_lines.append(f"  ✗ {item}{category_suffix} (not found in any community)")

    return (
        f"#{rank}  {r.name}  [{r.platter_type} | {veg_str} | {price_str}]\n"
        f"     Coverage: {coverage_str} | {skeleton_str} | final={r.final_score:.2f}\n"
        + "\n".join(item_lines)
    )


def main() -> None:
    print("Item-Based Platter Search POC")
    print("Type dish names (comma-separated). Empty input to quit.\n")

    try:
        while True:
            query = input("Search: ").strip()
            if not query:
                break

            results = search_platters(query)

            if not results:
                print("  No matching platters found.\n")
                continue

            print(f"\nTop {len(results)} platters for: {query!r}\n")
            for i, r in enumerate(results, 1):
                print(_format_result(i, r))
            print()
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        close_connections()


if __name__ == "__main__":
    main()
