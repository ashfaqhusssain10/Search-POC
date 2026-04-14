"""Step 7: Generate LLM narrative summaries for each Community node.

For each Community, collects:
  - Canonical member names (DynamoDB items via MEMBER_OF)
  - Variant names (Supabase aliases via VARIANT_OF from canonical items)
  - Hub items (highest-degree nodes — most VARIANT_OF connections)

Strategy:
  - Singleton communities (1 canonical, 0 variants): derive summary from
    existing llm_description on the Item node — no API call.
  - Multi-item communities: call Gemini to generate a narrative summary.

Stores result as `summary_json` on the Community node.

Usage:
    python -m scripts.generate_summaries
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from google import genai
from google.genai import types

from core.connections import close_connections, neo4j_session
from core.settings import GEMINI_API_KEY

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

MODEL = "gemini-2.5-flash"
MAX_RETRIES = 3
RETRY_DELAY = 2.0
WORKERS = 20  # parallel threads (Neo4j reads + Gemini calls)

SYSTEM_PROMPT = """You are a culinary naming expert for an Indian catering platform.
Given a cluster of dishes that are culinary equivalents (same dish, different names/regions),
write a SHORT summary (2-3 sentences) that:
1. Describes what kind of dish this community represents
2. Mentions popular names/variants
3. Notes any regional variations if present

Be factual and concise. No marketing language."""

USER_PROMPT_TEMPLATE = """Community ID: {community_id}
Canonical dish names: {canonical_names}
Also known as (variants): {variant_names}
Hub dishes (most connected): {hub_items}

Write the summary."""

# ---------------------------------------------------------------------------
# Fetch community data from Neo4j
# ---------------------------------------------------------------------------

FETCH_COMMUNITIES = """
MATCH (c:Community)
RETURN c.id AS community_id, c.member_count AS member_count
ORDER BY c.id
"""

FETCH_COMMUNITY_MEMBERS = """
MATCH (i:Item)-[:MEMBER_OF]->(c:Community {id: $community_id})
RETURN i.id AS id, i.name AS name, i.source AS source,
       i.llm_description AS llm_description
"""

FETCH_VARIANT_NAMES = """
MATCH (canonical:Item {source: 'dynamodb'})-[:MEMBER_OF]->(c:Community {id: $community_id})
MATCH (canonical)-[:VARIANT_OF]->(alias:Item)
RETURN DISTINCT alias.name AS name
"""

FETCH_HUB_ITEMS = """
MATCH (i:Item {source: 'dynamodb'})-[:MEMBER_OF]->(c:Community {id: $community_id})
OPTIONAL MATCH (i)-[:VARIANT_OF]->(alias:Item)
WITH i, count(alias) AS degree
ORDER BY degree DESC
LIMIT 3
RETURN i.name AS name, degree
"""


def fetch_community_data(session, community_id: str) -> dict[str, Any]:
    """Fetch all data needed to generate a community summary."""
    members = session.run(FETCH_COMMUNITY_MEMBERS, community_id=community_id)
    member_list = [dict(r) for r in members]

    canonical_members = [m for m in member_list if m["source"] == "dynamodb"]
    supabase_names = [m["name"] for m in member_list if m["source"] == "supabase"]

    variant_result = session.run(FETCH_VARIANT_NAMES, community_id=community_id)
    variant_names = [r["name"] for r in variant_result]
    all_variants = list(dict.fromkeys(supabase_names + variant_names))

    hub_result = session.run(FETCH_HUB_ITEMS, community_id=community_id)
    hub_items = [r["name"] for r in hub_result]

    return {
        "community_id": community_id,
        "canonical_names": [m["name"] for m in canonical_members],
        "canonical_llm_descriptions": [m["llm_description"] for m in canonical_members],
        "variant_names": all_variants,
        "hub_items": hub_items,
        "members": member_list,
    }


# ---------------------------------------------------------------------------
# Singleton summary (no API call)
# ---------------------------------------------------------------------------

def _parse_desc(llm_description: str | None) -> dict[str, Any]:
    if not llm_description:
        return {}
    try:
        return json.loads(llm_description)
    except (json.JSONDecodeError, ValueError):
        return {}


def summary_from_description(name: str, llm_description: str | None) -> str:
    """Build a plain-text summary from a single item's llm_description.

    Avoids any API call for singleton communities.
    """
    desc = _parse_desc(llm_description)
    ingredients = desc.get("ingredients", [])
    form = desc.get("form", "")
    cooking = desc.get("cooking_method", "")
    veg = desc.get("veg_type", "")
    regions = desc.get("regional_tags", [])
    also_known = desc.get("also_known_as", [])

    parts: list[str] = []

    # Sentence 1: what it is
    descriptor_parts = [p for p in [veg, form] if p]
    descriptor = " ".join(descriptor_parts) if descriptor_parts else "dish"
    ing_str = ", ".join(ingredients[:4]) if ingredients else ""
    if ing_str:
        parts.append(f"{name} is a {descriptor} made with {ing_str}.")
    else:
        parts.append(f"{name} is a {descriptor}.")

    # Sentence 2: cooking method + region
    detail_parts = []
    if cooking:
        detail_parts.append(f"{cooking}-prepared")
    if regions:
        detail_parts.append(f"popular in {', '.join(regions)}")
    if detail_parts:
        parts.append(" ".join(detail_parts).capitalize() + ".")

    # Sentence 3: alternate names
    if also_known:
        parts.append(f"Also known as {', '.join(also_known[:3])}.")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Gemini summary generation (multi-item communities)
# ---------------------------------------------------------------------------

def generate_summary(client: genai.Client, data: dict[str, Any]) -> str | None:
    """Call Gemini and return the narrative summary text."""
    prompt = USER_PROMPT_TEMPLATE.format(
        community_id=data["community_id"],
        canonical_names=", ".join(data["canonical_names"]) or "N/A",
        variant_names=", ".join(data["variant_names"]) or "N/A",
        hub_items=", ".join(data["hub_items"]) or "N/A",
    )

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.3,
                ),
            )
            return response.text.strip()
        except Exception as exc:
            log.warning(
                "Summary attempt %d/%d failed for %s: %s",
                attempt,
                MAX_RETRIES,
                data["community_id"],
                exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    return None


# ---------------------------------------------------------------------------
# Neo4j — persist summary
# ---------------------------------------------------------------------------

SAVE_SUMMARY = """
MATCH (c:Community {id: $community_id})
SET c.name         = $name,
    c.summary_json = $summary_json
"""


def community_name_from_data(data: dict[str, Any]) -> str:
    """Derive a human-readable name for the community from its hub items."""
    hub = data.get("hub_items", [])
    canonical = data.get("canonical_names", [])
    return hub[0] if hub else (canonical[0] if canonical else data["community_id"])


def save_summary(session, community_id: str, name: str, summary: str, data: dict[str, Any]) -> None:
    payload = {
        "narrative": summary,
        "members": data["canonical_names"],
        "variant_names": data["variant_names"],
        "hub_items": data["hub_items"],
        "member_count": len(data["members"]),
    }
    session.run(
        SAVE_SUMMARY,
        community_id=community_id,
        name=name,
        summary_json=json.dumps(payload, ensure_ascii=False),
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _process_one(
    client: genai.Client, community_id: str
) -> tuple[str, str, str, dict[str, Any]] | None:
    """Fetch + summarize one community. Returns (id, name, summary, data) or None on failure."""
    with neo4j_session() as session:
        data = fetch_community_data(session, community_id)

    if not data["canonical_names"]:
        log.warning("  %s has no canonical members — skipping", community_id)
        return None

    is_singleton = (
        len(data["canonical_names"]) == 1
        and len(data["variant_names"]) == 0
    )

    if is_singleton:
        item_name = data["canonical_names"][0]
        llm_desc = (data["canonical_llm_descriptions"] or [None])[0]
        summary = summary_from_description(item_name, llm_desc)
    else:
        summary = generate_summary(client, data)
        if not summary:
            log.error("  Failed to generate summary for %s", community_id)
            return None

    name = community_name_from_data(data)
    return community_id, name, summary, data


def main() -> None:
    client = genai.Client(api_key=GEMINI_API_KEY)

    with neo4j_session() as session:
        communities = [dict(r) for r in session.run(FETCH_COMMUNITIES)]

    total = len(communities)
    log.info("Processing %d communities with %d workers...", total, WORKERS)

    results: list[tuple[str, str, str, dict[str, Any]]] = []
    failed = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {
            executor.submit(_process_one, client, c["community_id"]): c["community_id"]
            for c in communities
        }
        for i, future in enumerate(as_completed(futures), 1):
            cid = futures[future]
            try:
                result = future.result()
                if result:
                    results.append(result)
                else:
                    failed += 1
            except Exception as exc:
                log.error("  %s raised: %s", cid, exc)
                failed += 1
            if i % 100 == 0:
                log.info("  Progress: %d / %d", i, total)

    # Batch-write all summaries in a single session
    log.info("Writing %d summaries to Neo4j...", len(results))
    with neo4j_session() as session:
        for community_id, name, summary, data in results:
            save_summary(session, community_id, name, summary, data)

    singleton_count = sum(
        1 for _, _, _, d in results
        if len(d["canonical_names"]) == 1 and len(d["variant_names"]) == 0
    )
    llm_count = len(results) - singleton_count

    log.info(
        "Done. success=%d  failed=%d  singleton_derived=%d  gemini_calls=%d",
        len(results),
        failed,
        singleton_count,
        llm_count,
    )
    close_connections()


if __name__ == "__main__":
    main()
