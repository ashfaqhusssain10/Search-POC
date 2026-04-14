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
import sys
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI
from qdrant_client.models import FieldCondition, Filter, MatchAny

from core.connections import close_connections, get_qdrant_client, neo4j_session
from core.settings import (
    EMBEDDING_MODEL,
    OPENAI_API_KEY,
    QDRANT_COLLECTION,
    QDRANT_SCORE_THRESHOLD,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

TOP_N_RESULTS = 3
SUGGESTION_THRESHOLD = 0.60  # Minimum similarity to suggest a culinary alternative

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
    items: list[str] = field(default_factory=list)  # all items in this platter
    all_community_ids: list[str] = field(default_factory=list)  # every community in this platter
    item_community_map: dict[str, str] = field(default_factory=dict)  # community_id → platter item name
    suggested_alternatives: dict[str, str | None] = field(default_factory=dict)  # item → closest item name
    community_summaries: dict[str, dict[str, Any]] = field(default_factory=dict)  # community_id → metadata


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

def find_best_community(qdrant, vector: list[float]) -> dict[str, Any] | None:
    """Return the single best community match for one item vector, or None if below threshold."""
    results = qdrant.query_points(
        collection_name=QDRANT_COLLECTION,
        query=vector,
        limit=1,
        score_threshold=QDRANT_SCORE_THRESHOLD,
        with_payload=True,
    ).points
    if not results:
        return None
    hit = results[0]
    return {
        "community_id": hit.payload.get("community_id", ""),
        "name": hit.payload.get("name", ""),
        "score": hit.score,
        "members": hit.payload.get("members", []),
        "variant_names": hit.payload.get("variant_names", []),
    }


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
# Step 3: Neo4j platter ranking
# ---------------------------------------------------------------------------

RANK_PLATTERS_QUERY = """
MATCH (p:Platter)-[:HAS_COMMUNITY]->(c:Community)
WHERE c.id IN $community_ids AND p.subType = 'DISCOUNTED'
WITH p,
     count(DISTINCT c.id) AS matched_communities,
     collect(DISTINCT {id: c.id, name: c.name, summary: c.summary_json}) AS matched_community_details
MATCH (p)-[:HAS_COMMUNITY]->(all_c:Community)
WITH p, matched_communities, matched_community_details,
     count(DISTINCT all_c.id) AS total_communities,
     collect(DISTINCT all_c.id) AS all_community_ids
OPTIONAL MATCH (p)-[:CONTAINS]->(pi:Item)
WITH p, matched_communities, matched_community_details, total_communities, all_community_ids,
     collect(DISTINCT pi.name) AS item_names
OPTIONAL MATCH (p)-[:CONTAINS]->(pi2:Item)-[:MEMBER_OF]->(pi_comm:Community)
WITH p, matched_communities, matched_community_details, total_communities, all_community_ids,
     item_names,
     collect(DISTINCT {item_name: pi2.name, community_id: pi_comm.id}) AS item_comm_pairs
RETURN p.id          AS id,
       p.name        AS name,
       p.type        AS platter_type,
       p.mealType    AS meal_type,
       p.veg         AS veg,
       p.minPrice    AS min_price,
       p.maxPrice    AS max_price,
       matched_communities,
       total_communities,
       matched_community_details,
       all_community_ids,
       item_names,
       item_comm_pairs
ORDER BY matched_communities DESC, total_communities ASC
LIMIT $limit
"""


def rank_platters(
    session,
    community_ids: list[str],
    community_name_map: dict[str, str],
    query_community_count: int,
    item_to_community: dict[str, str | None],
) -> list[PlatterResult]:
    """Query Neo4j to rank platters by how many of the query communities they cover."""
    result = session.run(
        RANK_PLATTERS_QUERY,
        community_ids=community_ids,
        limit=TOP_N_RESULTS,
    )

    platters = []
    for rec in result:
        matched = rec["matched_communities"]
        total = rec["total_communities"]
        matched_names = []
        community_summaries: dict[str, dict[str, Any]] = {}
        comm_name_to_id: dict[str, str] = {}

        for detail in (rec["matched_community_details"] or []):
            cid = detail["id"]
            name = detail["name"] or cid
            matched_names.append(name)
            comm_name_to_id[name] = cid
            
            summary_raw = detail["summary"]
            if summary_raw:
                try:
                    community_summaries[cid] = (
                        json.loads(summary_raw) if isinstance(summary_raw, str) else summary_raw
                    )
                except (json.JSONDecodeError, ValueError):
                    pass
        coverage = round(matched / query_community_count, 2) if query_community_count else 0.0
        # Build community_id → platter item name map (only for items with a known community)
        item_community_map: dict[str, str] = {}
        for pair in (rec["item_comm_pairs"] or []):
            cid = pair.get("community_id")
            name = pair.get("item_name")
            if cid and name:
                item_community_map[cid] = name

        platters.append(
            PlatterResult(
                id=rec["id"],
                name=rec["name"] or "",
                platter_type=rec["platter_type"] or "",
                meal_type=list(rec["meal_type"] or []),
                veg=bool(rec["veg"]),
                min_price=rec["min_price"],
                max_price=rec["max_price"],
                matched_communities=matched,
                total_communities=total,
                coverage_ratio=coverage,
                matched_community_names=matched_names,
                query_community_count=query_community_count,
                item_to_community=item_to_community,
                item_to_community_id={
                    item: (comm_name_to_id.get(name) if name else None)
                    for item, name in item_to_community.items()
                },
                items=sorted(rec["item_names"] or []),
                all_community_ids=list(rec["all_community_ids"] or []),
                item_community_map=item_community_map,
                community_summaries=community_summaries,
            )
        )
    return platters


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_platters(query: str) -> list[PlatterResult]:
    """Main entry point. Returns ranked PlatterResult list for a comma-separated dish query."""
    items = [i.strip() for i in query.split(",") if i.strip()]
    if not items:
        return []

    openai_client = OpenAI(api_key=OPENAI_API_KEY)
    qdrant = get_qdrant_client()

    # Single batch embed call for all items
    vectors = embed_items(openai_client, items)

    # Per-item: find best matching community
    community_ids: list[str] = []
    community_name_map: dict[str, str] = {}
    item_to_community: dict[str, str | None] = {}

    for item, vector in zip(items, vectors):
        comm = find_best_community(qdrant, vector)
        if comm:
            cid = comm["community_id"]
            if cid not in community_ids:
                community_ids.append(cid)
            community_name_map[cid] = comm["name"]
            item_to_community[item] = comm["name"]
            log.info("  %r → community %r (score %.3f)", item, comm["name"], comm["score"])
        else:
            item_to_community[item] = None
            log.info("  %r → no community found above threshold", item)

    if not community_ids:
        log.info("No communities matched any item.")
        return []

    log.info(
        "Matched %d unique communities for %d items",
        len(community_ids),
        len(items),
    )

    with neo4j_session() as session:
        results = rank_platters(
            session,
            community_ids,
            community_name_map,
            query_community_count=len(community_ids),
            item_to_community=item_to_community,
        )

    # For each ⚠️ item (community found but not in this platter), find the closest actual platter item
    item_vector_map = dict(zip(items, vectors))
    for platter in results:
        # Only search within communities that have actual CONTAINS items (prevents phantom suggestions)
        item_backed_community_ids = list(platter.item_community_map.keys())
        if not item_backed_community_ids:
            continue
        for item, comm_name in platter.item_to_community.items():
            if comm_name and comm_name not in platter.matched_community_names:
                vec = item_vector_map[item]
                closest_cid = find_closest_in_platter(
                    qdrant, vec, item_backed_community_ids, SUGGESTION_THRESHOLD
                )
                suggestion = platter.item_community_map.get(closest_cid) if closest_cid else None
                platter.suggested_alternatives[item] = suggestion
                log.info("  %r not in platter → closest alternative: %r", item, suggestion)

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

    item_lines = []
    for item, comm_name in r.item_to_community.items():
        if comm_name and comm_name in r.matched_community_names:
            item_lines.append(f"  ✓ {item} → {comm_name}")
        elif comm_name:
            item_lines.append(f"  ~ {item} → {comm_name} (platter doesn't have this)")
        else:
            item_lines.append(f"  ✗ {item} (not found in any community)")

    return (
        f"#{rank}  {r.name}  [{r.platter_type} | {veg_str} | {price_str}]\n"
        f"     Coverage: {coverage_str}\n"
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
