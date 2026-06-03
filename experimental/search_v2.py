"""Community-free platter search.

Approach: direct item-vector matching against `searchpoc_canonicals` instead
of `item_search_communities`. The community layer added clustering errors
(e.g. Paneer Butter Masala and Paneer Manchurian merged into one community
because both are "VEG gravy paneer") — by dropping it we get crisper matches
backed by the richer per-item metadata embeddings.

Flow:
    1. Resolve user input → DynamoDB canonical names
    2. Build enriched embedding text per canonical (name + form + region + ingredients)
    3. Embed (one OpenAI call for the whole query)
    4. For each query item, search searchpoc_canonicals with veg filter
       → top-K canonical items
    5. For each canonical match, find platters that CONTAIN it (or items
       VARIANT_OF it)
    6. Score platters by how many query items they satisfy
    7. Return top-3 platters

Programmatic entry point:
    from scripts.search_v2 import search_platters_v2
    results = search_platters_v2("Paneer Butter Masala, Veg Biryani, Butter Naan")
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI
from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from core.connections import close_connections, get_qdrant_client, neo4j_session
from core.embedding_text import build_item_embedding_text
from core.settings import EMBEDDING_MODEL, OPENAI_API_KEY

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

CANONICAL_COLLECTION = "searchpoc_canonicals"
TOP_K_CANONICALS = 5  # how many canonical items to keep per query item
ITEM_SCORE_THRESHOLD = 0.45  # minimum cosine score for a canonical match
TOP_N_PLATTERS = 3

# Coverage = fraction of query items satisfied.
# Quality = avg cosine score of the satisfying matches.
COVERAGE_WEIGHT = 0.7
QUALITY_WEIGHT = 0.3


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class CanonicalMatch:
    """One canonical item retrieved as a candidate for a query item."""
    canonical_name: str
    score: float


@dataclass
class ItemMatch:
    """What satisfied (or didn't satisfy) a single query item in a platter."""
    query_item: str
    canonical: str | None
    platter_item_name: str | None
    score: float | None


@dataclass
class PlatterResultV2:
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

# Find canonical names that, via VARIANT_OF, link to any item in the given platter.
# (Lets a canonical "match" a platter even if the platter only contains an alias
# variant, e.g. canonical "Veg Biryani" matched by platter item "Jackfruit Biryani".)
FETCH_PLATTER_VARIANTS_QUERY = """
UNWIND $canonical_names AS cname
MATCH (canonical:Item {source: 'dynamodb', name: cname})
OPTIONAL MATCH (canonical)-[:VARIANT_OF]->(alias:Item {source: 'supabase'})
RETURN cname AS canonical_name,
       collect(DISTINCT canonical.name) + collect(DISTINCT alias.name) AS satisfying_names
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_canonicals(session, items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        row = session.run(RESOLVE_CANONICAL_QUERY, name=item).single()
        out[item] = row["canonical_name"] if row else item
    return out


def _fetch_canonical_metadata(session, canonicals: list[str]) -> dict[str, dict[str, Any]]:
    """Return name → {item_type, typecode, category, description}. Prefer dynamodb source."""
    rows = session.run(FETCH_ITEM_METADATA_QUERY, names=list({c for c in canonicals}))
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        existing = out.get(r["name"])
        if existing and existing.get("source") == "dynamodb":
            continue
        out[r["name"]] = {
            "item_type": r["item_type"],
            "typecode": r["typecode"],
            "category": r["category"],
            "description": r["description"],
            "source": r["source"],
        }
    return out


def _veg_filter(item_type: str | None) -> Filter | None:
    """VEG queries → match only VEG canonicals. EGG → EGG or NONVEG. NONVEG → no filter."""
    if item_type == "VEG":
        return Filter(must=[FieldCondition(key="veg_type", match=MatchValue(value="VEG"))])
    if item_type == "EGG":
        return Filter(must=[FieldCondition(key="veg_type", match=MatchAny(any=["EGG", "NONVEG"]))])
    return None


# ---------------------------------------------------------------------------
# Stage 1: top-K canonical matches per query item
# ---------------------------------------------------------------------------

def _find_canonical_matches(
    qdrant,
    openai_client: OpenAI,
    items: list[str],
    item_to_canonical: dict[str, str],
    canonical_metadata: dict[str, dict[str, Any]],
) -> dict[str, list[CanonicalMatch]]:
    """For each query item, return its top-K canonical matches above threshold."""
    # Build enriched embedding text per query item
    enriched_texts: list[str] = []
    for item in items:
        canonical = item_to_canonical[item]
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

    log.info("Embedding %d query items", len(enriched_texts))
    response = openai_client.embeddings.create(model=EMBEDDING_MODEL, input=enriched_texts)
    vectors = [d.embedding for d in response.data]

    per_item: dict[str, list[CanonicalMatch]] = {}
    for item, vec in zip(items, vectors):
        meta = canonical_metadata.get(item_to_canonical[item], {})
        hits = qdrant.query_points(
            collection_name=CANONICAL_COLLECTION,
            query=vec,
            limit=TOP_K_CANONICALS,
            score_threshold=ITEM_SCORE_THRESHOLD,
            with_payload=True,
            query_filter=_veg_filter(meta.get("item_type")),
        ).points
        matches = [
            CanonicalMatch(canonical_name=h.payload.get("name", ""), score=float(h.score))
            for h in hits if h.payload.get("name")
        ]
        per_item[item] = matches
        if matches:
            top = matches[0]
            log.info("  %r → top: %r (score %.3f), %d candidates", item, top.canonical_name, top.score, len(matches))
        else:
            log.info("  %r → no matches above threshold %.2f", item, ITEM_SCORE_THRESHOLD)
    return per_item


# ---------------------------------------------------------------------------
# Stage 2: Resolve each canonical → set of platter-item names that satisfy it
# ---------------------------------------------------------------------------

def _resolve_satisfying_names(session, canonical_names: set[str]) -> dict[str, set[str]]:
    """For each canonical, the set of item names that 'satisfy' it (canonical + its aliases)."""
    if not canonical_names:
        return {}
    rows = session.run(FETCH_PLATTER_VARIANTS_QUERY, canonical_names=list(canonical_names))
    out: dict[str, set[str]] = {}
    for r in rows:
        names = {n for n in (r["satisfying_names"] or []) if n}
        out[r["canonical_name"]] = names
    return out


# ---------------------------------------------------------------------------
# Stage 3: Score platters
# ---------------------------------------------------------------------------

def _score_platters(
    session,
    items: list[str],
    per_item_matches: dict[str, list[CanonicalMatch]],
    canonical_metadata: dict[str, dict[str, Any]],
    item_to_canonical: dict[str, str],
) -> list[PlatterResultV2]:
    # Gather all canonical names referenced anywhere
    all_canonicals: set[str] = set()
    for matches in per_item_matches.values():
        for m in matches:
            all_canonicals.add(m.canonical_name)

    satisfying = _resolve_satisfying_names(session, all_canonicals)

    rows = session.run(FETCH_PLATTERS_WITH_ITEMS_QUERY)
    platters: list[PlatterResultV2] = []

    for rec in rows:
        item_names_in_platter = [n for n in (rec["item_names"] or []) if n]
        item_name_set = set(item_names_in_platter)
        item_type_lookup = {row["name"]: row["item_type"] for row in (rec["item_data"] or []) if row.get("name")}

        match_list: list[ItemMatch] = []
        match_scores: list[float] = []

        for item in items:
            query_item_type = canonical_metadata.get(item_to_canonical[item], {}).get("item_type")
            best: ItemMatch | None = None
            best_score = -1.0
            for cand in per_item_matches.get(item, []):
                cand_names = satisfying.get(cand.canonical_name, {cand.canonical_name})
                overlap = cand_names & item_name_set
                if not overlap:
                    continue
                # Option B: a VEG query needs a VEG item in the platter
                if query_item_type == "VEG":
                    overlap = {n for n in overlap if item_type_lookup.get(n) == "VEG"}
                elif query_item_type == "EGG":
                    overlap = {n for n in overlap if item_type_lookup.get(n) in ("EGG", "NONVEG")}
                if not overlap:
                    continue
                platter_item_name = sorted(overlap)[0]
                if cand.score > best_score:
                    best_score = cand.score
                    best = ItemMatch(
                        query_item=item,
                        canonical=cand.canonical_name,
                        platter_item_name=platter_item_name,
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

        platters.append(PlatterResultV2(
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

def search_platters_v2(query: str) -> list[PlatterResultV2]:
    """Community-free platter search. Returns top-3 ranked PlatterResultV2."""
    items = [i.strip() for i in query.split(",") if i.strip()]
    if not items:
        return []

    openai_client = OpenAI(api_key=OPENAI_API_KEY)
    qdrant = get_qdrant_client()

    with neo4j_session() as session:
        # ── Canonical resolution disabled per PM (item-to-item match preferred) ──
        # Previously: user-selected "Garlic Naan" → resolves to canonical
        # "Rumali Roti" → embeds canonical metadata → searches canonicals.
        # The "matched as Rumali Roti" intermediate confused users.
        # Now: embed the user-selected name's OWN metadata, search canonicals
        # directly. UI surfaces "Garlic Naan → Butter Naan (score)" with no hop.
        # To revert, uncomment the original two lines and remove the identity map.
        #
        # item_to_canonical = _resolve_canonicals(session, items)
        # canonical_metadata = _fetch_canonical_metadata(
        #     session, [item_to_canonical[i] for i in items]
        # )
        item_to_canonical = {i: i for i in items}
        canonical_metadata = _fetch_canonical_metadata(session, items)

        per_item_matches = _find_canonical_matches(
            qdrant, openai_client, items, item_to_canonical, canonical_metadata,
        )

        platters = _score_platters(
            session, items, per_item_matches, canonical_metadata, item_to_canonical,
        )

    return platters[:TOP_N_PLATTERS]


# ---------------------------------------------------------------------------
# CLI for quick testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "Paneer Butter Masala, Veg Biryani, Butter Naan"
    log.info("Query: %r", q)
    results = search_platters_v2(q)
    for i, r in enumerate(results, 1):
        veg = "VEG" if r.veg else "NON-VEG"
        log.info(
            "#%d %s [%s] | matched %d/%d | score=%.3f (coverage=%.2f, quality=%.3f)",
            i, r.name, veg, r.matched_count, len(r.matches), r.final_score, r.coverage, r.quality,
        )
        for m in r.matches:
            if m.canonical:
                log.info("    ✅ %s → %s (via %s, score=%.3f)",
                         m.query_item, m.platter_item_name, m.canonical, m.score)
            else:
                log.info("    ❌ %s → no match in this platter", m.query_item)
    close_connections()
