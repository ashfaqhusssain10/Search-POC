"""Step 3: Generate VARIANT_OF edges — unified forward + reverse pipeline.

Pass 1 (Forward): For each DynamoDB canonical, search Supabase aliases in
    Qdrant using the canonical's vector. Score candidates with Gemini.
    Threshold: 0.7. Tiered retrieval (veg+form → veg-only fallback).
    Writes (canonical)-[:VARIANT_OF]->(alias). Delete-then-write per canonical.

Pass 2 (Reverse): After the forward pass, detect Supabase aliases that still
    have no VARIANT_OF edge. For each orphan, search DynamoDB canonicals in
    Qdrant using the alias's vector. Score with Gemini. Threshold: 0.5.
    Writes (canonical)-[:VARIANT_OF]->(alias). Additive MERGE (no delete).

Both passes use the same edge direction, same Gemini model, shared cache dir.
Cache keys are direction-aware: canonical_{id}.json vs alias_{id}.json.

Usage:
    python -m scripts.generate_variants                    # dry-run both passes
    python -m scripts.generate_variants --commit           # write both passes to Neo4j
    python -m scripts.generate_variants --workers 20       # parallel workers
    python -m scripts.generate_variants --forward-only     # skip reverse pass
    python -m scripts.generate_variants --reverse-only     # skip forward pass
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types
from qdrant_client.http.models import FieldCondition, Filter, MatchValue

from core.connections import close_connections, get_qdrant_client, neo4j_session
from core.settings import GEMINI_API_KEY

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COLLECTION_CANONICALS = "searchpoc_canonicals"
COLLECTION_ALIASES = "searchpoc_aliases"

SCORE_THRESHOLD_FORWARD: float = 0.7   # forward pass: canonical → aliases
SCORE_THRESHOLD_REVERSE: float = 0.5   # reverse pass: orphan aliases → canonicals
CANDIDATE_CAP: int = 25
TIER2_TRIGGER: int = 10                # forward pass: drop form filter if < this
QDRANT_SEARCH_LIMIT: int = 60

PROMPT_VERSION = "v3.1"               # bump to invalidate all caches
DEFAULT_WORKERS: int = 10

MODEL = "gemini-2.5-flash"
MAX_RETRIES: int = 3
RETRY_DELAY: float = 2.0

CACHE_DIR = Path("llm_cache/variants")
FWD_DRY_RUN_FILE = Path("llm_cache/dry_run_variants.json")
REV_DRY_RUN_FILE = Path("llm_cache/dry_run_reverse.json")
ZERO_EDGE_CANONICALS_FILE = Path("llm_cache/zero_edge_canonicals.json")
ZERO_EDGE_ALIASES_FILE = Path("llm_cache/zero_edge_aliases.json")

# ---------------------------------------------------------------------------
# Prompts — Forward pass
# ---------------------------------------------------------------------------

_FWD_SYSTEM_PROMPT = """\
You are an expert in Indian cuisine linking alias dish names to canonical dish entries.

VARIANT_OF means: the two dishes share the same primary ingredient and are close \
enough that a customer ordering one would be satisfied by the other. Form, preparation \
style, and regional label can all differ — only the primary ingredient must match.

Scoring rules:
  1.0 = identical dish, only spelling / transliteration differs (Pulka / Phulka)
  0.9 = same dish, regional name (Murgh Makhani / Butter Chicken)
  0.8 = same primary ingredient, different preparation or regional style
        (Chicken Curry / Chicken Masala, Dal Tadka / Dal Fry, Paneer Butter Masala / Paneer Tikka Masala)
  0.7 = same primary ingredient, loosely related form — score with confidence
  0.5 = same primary ingredient but weak connection — include so near-misses are visible
  0.0 = different primary ingredient OR VEG vs NONVEG mismatch

Hard blockers — forces score 0.0:
- VEG must NOT match NONVEG (and vice versa)
- Different primary ingredient = 0.0
  (chicken != mutton, chicken != fish, chicken != egg, paneer != chicken, potato != cabbage)

Do NOT penalise for:
- Different dish form (curry vs fry vs masala vs gravy vs roast)
- Different cooking method (tandoor vs pan vs fried)
- Regional name differences

Return JSON array only. Include all candidates you would score >= 0.5.
[{"candidate_id": "<id>", "score": 0.85, "reason": "<brief>"}]
Return [] if no candidates share the same primary ingredient."""

_FWD_USER_PROMPT_TEMPLATE = """\
Canonical dish: {name}
Category: {category}
Form: {form}
Veg type: {veg_type}
Ingredients: {ingredients}

Candidates (pre-filtered by veg_type{form_note}):
{candidate_lines}

Score each candidate. Return JSON array only."""

# ---------------------------------------------------------------------------
# Prompts — Reverse pass
# ---------------------------------------------------------------------------

_REV_SYSTEM_PROMPT = """\
You are an expert in Indian cuisine linking orphan alias dishes to canonical dish entries.

VARIANT_OF means: the two dishes share the same primary ingredient and are close \
enough that a customer ordering one would be satisfied by the other. Form, preparation \
style, and regional label can all differ — only the primary ingredient must match.

Scoring rules:
  1.0 = identical dish, only spelling / transliteration differs (Pulka / Phulka)
  0.9 = same dish, regional name (Murgh Makhani / Butter Chicken)
  0.8 = same primary ingredient, different preparation or regional style
        (Chicken Curry / Chicken Masala, Dal Tadka / Dal Fry, Paneer Butter Masala / Paneer Tikka Masala)
  0.7 = same primary ingredient, loosely related form — score with confidence
  0.5 = same primary ingredient but weak connection — include so near-misses are visible
  0.0 = different primary ingredient OR VEG vs NONVEG mismatch

Hard blockers — forces score 0.0:
- VEG must NOT match NONVEG (and vice versa)
- Different primary ingredient = 0.0
  (chicken != mutton, chicken != fish, chicken != egg, paneer != chicken, potato != cabbage)

Do NOT penalise for:
- Different dish form (curry vs fry vs masala vs gravy vs roast)
- Different cooking method (tandoor vs pan vs fried)
- Regional name differences

Return JSON array only. Include all candidates you would score >= 0.5.
[{"candidate_id": "<id>", "score": 0.85, "reason": "<brief>"}]
Return [] if no candidates share the same primary ingredient."""

_REV_USER_PROMPT_TEMPLATE = """\
Alias dish (the one we want to classify): {alias_name}
Form: {alias_form}
Veg type: {alias_veg_type}
Ingredients: {alias_ingredients}

DynamoDB canonical candidates (pre-filtered by veg_type):
{candidate_lines}

For each candidate, decide if the alias is the same dish as that canonical.
Return JSON array only."""

# ---------------------------------------------------------------------------
# Shared: cache
# ---------------------------------------------------------------------------


def _cache_key(item_id: str, candidate_ids: list[str]) -> str:
    ids_hash = hashlib.sha256(
        ",".join(sorted(candidate_ids)).encode()
    ).hexdigest()[:12]
    return f"{item_id}_{ids_hash}_{PROMPT_VERSION}"


def _load_cache(prefix: str, item_id: str, cache_key: str) -> list[dict] | None:
    path = CACHE_DIR / f"{prefix}_{item_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("cache_key") == cache_key:
            return data["scored"]
    except Exception:
        pass
    return None


def _save_cache(prefix: str, item_id: str, cache_key: str, scored: list[dict]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{prefix}_{item_id}.json"
    path.write_text(
        json.dumps({"cache_key": cache_key, "scored": scored}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Shared: Gemini call
# ---------------------------------------------------------------------------


def call_gemini(
    client: genai.Client,
    item_name: str,
    prompt: str,
    system_prompt: str,
) -> list[dict] | None:
    """Call Gemini with exponential-backoff retry. Returns parsed list or None."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    temperature=0,
                ),
            )
            text = response.text.strip()
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                for val in parsed.values():
                    if isinstance(val, list):
                        return val
            log.warning("Unexpected JSON shape for %r attempt %d/%d", item_name, attempt, MAX_RETRIES)
        except json.JSONDecodeError as exc:
            try:
                import re
                txt = re.sub(r",\s*([\]}])", r"\1", response.text)
                parsed = json.loads(txt)
                if isinstance(parsed, list):
                    return parsed
                if isinstance(parsed, dict):
                    for val in parsed.values():
                        if isinstance(val, list):
                            return val
            except Exception:
                pass
            log.warning("JSON parse error for %r attempt %d/%d: %s", item_name, attempt, MAX_RETRIES, exc)
        except Exception as exc:
            log.warning("Gemini call failed for %r attempt %d/%d: %s", item_name, attempt, MAX_RETRIES, exc)

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY * attempt)

    log.error("Gemini failed for %r after %d attempts — skipping", item_name, MAX_RETRIES)
    return None


# ---------------------------------------------------------------------------
# Shared: apply threshold
# ---------------------------------------------------------------------------


def apply_threshold(
    scored: list[dict],
    valid_ids: set[str],
    threshold: float,
    id_field: str,
    sub_prefix_fix: bool = False,
) -> list[dict]:
    """Filter scored entries >= threshold. Deduplicates by matched ID.

    sub_prefix_fix: if True, try prepending 'sub_' when id not found (forward pass only).
    """
    passing: list[dict] = []
    seen: set[str] = set()
    for entry in scored:
        if not isinstance(entry, dict):
            continue
        try:
            score = float(entry.get("score", 0.0))
        except (TypeError, ValueError):
            continue
        cid = str(entry.get("candidate_id", "")).strip()
        if sub_prefix_fix and cid not in valid_ids:
            prefixed = f"sub_{cid}"
            if prefixed in valid_ids:
                cid = prefixed
        if score >= threshold and cid in valid_ids and cid not in seen:
            seen.add(cid)
            passing.append({
                id_field: cid,
                "score": score,
                "reason": str(entry.get("reason", ""))[:200],
            })
    return passing


# ---------------------------------------------------------------------------
# Forward pass: load canonicals
# ---------------------------------------------------------------------------


def load_canonicals(qdrant) -> dict[str, dict[str, Any]]:
    """Scroll all points from searchpoc_canonicals.

    Returns {item_id: {vector, name, category, veg_type, form, ingredients}}.
    """
    result: dict[str, dict[str, Any]] = {}
    offset = None
    while True:
        pts, next_offset = qdrant.scroll(
            collection_name=COLLECTION_CANONICALS,
            with_vectors=True,
            with_payload=True,
            limit=100,
            offset=offset,
        )
        for pt in pts:
            item_id = pt.payload.get("item_id", "")
            if item_id:
                result[item_id] = {
                    "vector": pt.vector,
                    "name": pt.payload.get("name", ""),
                    "category": pt.payload.get("category", ""),
                    "veg_type": pt.payload.get("veg_type", ""),
                    "form": pt.payload.get("form", ""),
                    "ingredients": pt.payload.get("ingredients", []),
                }
        if next_offset is None:
            break
        offset = next_offset
    log.info("Loaded %d canonical vectors from '%s'.", len(result), COLLECTION_CANONICALS)
    return result


# ---------------------------------------------------------------------------
# Forward pass: tiered candidate retrieval (canonical → aliases)
# ---------------------------------------------------------------------------


def retrieve_alias_candidates(
    qdrant,
    canonical: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    """Tiered semantic search on searchpoc_aliases.

    Tier 1 (strict): filter by veg_type + form.
    Tier 2 (loose):  filter by veg_type only — triggered when Tier 1 < TIER2_TRIGGER.
    veg_type filter is NEVER dropped (product-safety requirement).

    Returns (candidates[:CANDIDATE_CAP], tier_used).
    """
    veg_type = canonical.get("veg_type", "")
    form = canonical.get("form", "")
    vector = canonical["vector"]

    def _search(query_filter: Filter) -> list[dict[str, Any]]:
        hits = qdrant.query_points(
            collection_name=COLLECTION_ALIASES,
            query=vector,
            query_filter=query_filter,
            limit=QDRANT_SEARCH_LIMIT,
            with_payload=True,
        ).points
        return [
            {
                "item_id": h.payload.get("item_id", ""),
                "name": h.payload.get("name", ""),
                "veg_type": h.payload.get("veg_type", ""),
                "form": h.payload.get("form", ""),
                "qdrant_score": round(h.score, 4),
            }
            for h in hits
            if h.payload.get("item_id")
        ]

    form_matches = [form] if form else []
    if form == "stew":
        form_matches.append("gravy")
    elif form == "gravy":
        form_matches.append("stew")
    elif form in ["rice-dish", "grain-bowl"]:
        form_matches = ["rice-dish", "grain-bowl"]

    must_conditions: list[Any] = []
    if veg_type:
        must_conditions.append(FieldCondition(key="veg_type", match=MatchValue(value=veg_type)))
    if form_matches:
        must_conditions.append(Filter(should=[
            FieldCondition(key="form", match=MatchValue(value=f))
            for f in form_matches
        ]))

    tier = "strict"
    results = _search(Filter(must=must_conditions)) if must_conditions else []

    if len(results) < TIER2_TRIGGER and veg_type:
        tier = "loose"
        log.info(
            "  [%s] Tier-2 triggered (%d strict < %d) — dropping form filter",
            canonical.get("name", ""), len(results), TIER2_TRIGGER,
        )
        results = _search(Filter(must=[
            FieldCondition(key="veg_type", match=MatchValue(value=veg_type))
        ]))

    return results[:CANDIDATE_CAP], tier


# ---------------------------------------------------------------------------
# Forward pass: Neo4j write — delete-then-write per canonical
# ---------------------------------------------------------------------------

_FWD_DELETE_EDGES = """
MATCH (c:Item {id: $canonical_id, source: 'dynamodb'})-[r:VARIANT_OF]->()
DELETE r
"""

_FWD_WRITE_EDGES = """
UNWIND $edges AS edge
MATCH (c:Item {id: $canonical_id, source: 'dynamodb'})
MATCH (a:Item {id: edge.candidate_id, source: 'supabase'})
MERGE (c)-[r:VARIANT_OF]->(a)
SET r.score = edge.score, r.reason = edge.reason
"""


def write_edges_for_canonical(session, canonical_id: str, edges: list[dict]) -> None:
    """Delete all existing VARIANT_OF for this canonical, then write new ones."""
    session.run(_FWD_DELETE_EDGES, canonical_id=canonical_id)
    if edges:
        session.run(_FWD_WRITE_EDGES, canonical_id=canonical_id, edges=edges)


# ---------------------------------------------------------------------------
# Forward pass: per-canonical worker
# ---------------------------------------------------------------------------


def _process_canonical(
    qdrant,
    client: genai.Client,
    canonical_id: str,
    canonical: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    """Process one canonical. Thread-safe — no shared mutable state."""
    canonical_name = canonical["name"]

    candidates, tier = retrieve_alias_candidates(qdrant, canonical)
    if not candidates:
        log.info("  [%s] No alias candidates found — skipping", canonical_name)
        return {
            "canonical_id": canonical_id, "canonical_name": canonical_name,
            "tier": tier, "passing": [], "dry_run_entry": None,
            "zero_edge_reason": "no_candidates", "cache_hit": False, "llm_failure": False,
        }

    valid_ids = {c["item_id"] for c in candidates}
    ck = _cache_key(canonical_id, list(valid_ids))
    cached = _load_cache("canonical", canonical_id, ck)
    cache_hit = cached is not None

    if cache_hit:
        scored = cached
        log.info("  [%s] Cache hit (%d scored)", canonical_name, len(scored))
    else:
        ingredients_str = ", ".join(canonical.get("ingredients", [])[:6]) or "unknown"
        form_note = " and form" if tier == "strict" else " only (form filter relaxed)"
        candidate_lines = "\n".join(
            f"{i + 1}. {c['item_id']}: {c['name']}"
            f" (form: {c['form'] or 'unknown'}, veg: {c['veg_type'] or 'unknown'},"
            f" similarity: {c['qdrant_score']})"
            for i, c in enumerate(candidates)
        )
        prompt = _FWD_USER_PROMPT_TEMPLATE.format(
            name=canonical_name,
            category=canonical.get("category", "") or "unknown",
            form=canonical.get("form", "") or "unknown",
            veg_type=canonical.get("veg_type", "") or "unknown",
            ingredients=ingredients_str,
            form_note=form_note,
            candidate_lines=candidate_lines,
        )
        scored = call_gemini(client, canonical_name, prompt, _FWD_SYSTEM_PROMPT)
        if scored is None:
            return {
                "canonical_id": canonical_id, "canonical_name": canonical_name,
                "tier": tier, "passing": [], "dry_run_entry": None,
                "zero_edge_reason": "llm_failure", "cache_hit": False, "llm_failure": True,
            }
        _save_cache("canonical", canonical_id, ck, scored)

    passing = apply_threshold(scored, valid_ids, SCORE_THRESHOLD_FORWARD, "candidate_id", sub_prefix_fix=True)

    name_lookup = {c["item_id"]: c["name"] for c in candidates}
    dry_run_entry = {
        "canonical_id": canonical_id,
        "canonical_name": canonical_name,
        "tier_used": tier,
        "candidates_retrieved": len(candidates),
        "edges": [
            {
                "candidate_id": e["candidate_id"],
                "name": name_lookup.get(e["candidate_id"], e["candidate_id"]),
                "score": e["score"],
                "reason": e["reason"],
            }
            for e in passing
        ],
    }

    if not passing:
        log.info("  [%s] 0 edges (tier=%s, candidates=%d)", canonical_name, tier, len(candidates))
    else:
        log.info("  [%s] %d edge(s) (tier=%s)", canonical_name, len(passing), tier)

    if not dry_run:
        with neo4j_session() as session:
            write_edges_for_canonical(session, canonical_id, passing)

    return {
        "canonical_id": canonical_id, "canonical_name": canonical_name,
        "tier": tier, "passing": passing, "dry_run_entry": dry_run_entry,
        "zero_edge_reason": None if passing else "below_threshold",
        "cache_hit": cache_hit, "llm_failure": False,
    }


# ---------------------------------------------------------------------------
# Reverse pass: load orphan alias vectors
# ---------------------------------------------------------------------------


def load_orphan_aliases(qdrant, orphan_ids: set[str]) -> dict[str, dict[str, Any]]:
    """Scroll searchpoc_aliases, keep only orphan item IDs.

    Returns {item_id: {vector, name, veg_type, form, ingredients}}.
    """
    result: dict[str, dict[str, Any]] = {}
    offset = None
    while True:
        pts, next_offset = qdrant.scroll(
            collection_name=COLLECTION_ALIASES,
            with_vectors=True,
            with_payload=True,
            limit=200,
            offset=offset,
        )
        for pt in pts:
            item_id = pt.payload.get("item_id", "")
            if item_id in orphan_ids:
                result[item_id] = {
                    "vector": pt.vector,
                    "name": pt.payload.get("name", ""),
                    "veg_type": pt.payload.get("veg_type", ""),
                    "form": pt.payload.get("form", ""),
                    "ingredients": pt.payload.get("ingredients", []),
                }
        if next_offset is None:
            break
        offset = next_offset
    return result


# ---------------------------------------------------------------------------
# Reverse pass: candidate retrieval (alias → canonicals)
# ---------------------------------------------------------------------------


def retrieve_canonical_candidates(
    qdrant,
    alias: dict[str, Any],
) -> list[dict[str, Any]]:
    """Search searchpoc_canonicals using the alias vector, filtered by veg_type."""
    veg_type = alias.get("veg_type", "")
    vector = alias["vector"]

    query_filter = (
        Filter(must=[FieldCondition(key="veg_type", match=MatchValue(value=veg_type))])
        if veg_type else None
    )

    hits = qdrant.query_points(
        collection_name=COLLECTION_CANONICALS,
        query=vector,
        query_filter=query_filter,
        limit=QDRANT_SEARCH_LIMIT,
        with_payload=True,
    ).points

    return [
        {
            "item_id": h.payload.get("item_id", ""),
            "name": h.payload.get("name", ""),
            "veg_type": h.payload.get("veg_type", ""),
            "form": h.payload.get("form", ""),
            "category": h.payload.get("category", ""),
            "qdrant_score": round(h.score, 4),
        }
        for h in hits
        if h.payload.get("item_id")
    ][:CANDIDATE_CAP]


# ---------------------------------------------------------------------------
# Reverse pass: Neo4j write — additive MERGE (no delete)
# ---------------------------------------------------------------------------

_REV_WRITE_EDGES = """
UNWIND $edges AS edge
MATCH (c:Item {id: edge.canonical_id, source: 'dynamodb'})
MATCH (a:Item {id: $alias_id, source: 'supabase'})
MERGE (c)-[r:VARIANT_OF]->(a)
SET r.score = edge.score, r.reason = edge.reason
"""


def write_edges_for_alias(session, alias_id: str, edges: list[dict]) -> None:
    if edges:
        session.run(_REV_WRITE_EDGES, alias_id=alias_id, edges=edges)


# ---------------------------------------------------------------------------
# Reverse pass: per-alias worker
# ---------------------------------------------------------------------------


def _process_alias(
    qdrant,
    client: genai.Client,
    alias_id: str,
    alias: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    """Process one orphan alias. Thread-safe — no shared mutable state."""
    alias_name = alias["name"]

    candidates = retrieve_canonical_candidates(qdrant, alias)
    if not candidates:
        log.info("  [%s] No DynamoDB candidates found", alias_name)
        return {
            "alias_id": alias_id, "alias_name": alias_name,
            "passing": [], "dry_run_entry": None, "zero_edge_reason": "no_candidates",
        }

    valid_ids = {c["item_id"] for c in candidates}
    ck = _cache_key(alias_id, list(valid_ids))
    cached = _load_cache("alias", alias_id, ck)

    if cached is not None:
        scored = cached
        log.info("  [%s] Cache hit (%d scored)", alias_name, len(scored))
    else:
        candidate_lines = "\n".join(
            f'  candidate_id: "{c["item_id"]}" | name: "{c["name"]}" | '
            f'form: {c["form"]} | category: {c["category"]} | qdrant_score: {c["qdrant_score"]}'
            for c in candidates
        )
        prompt = _REV_USER_PROMPT_TEMPLATE.format(
            alias_name=alias_name,
            alias_form=alias.get("form", "unknown"),
            alias_veg_type=alias.get("veg_type", "unknown"),
            alias_ingredients=", ".join(alias.get("ingredients", [])) or "unknown",
            candidate_lines=candidate_lines,
        )
        scored = call_gemini(client, alias_name, prompt, _REV_SYSTEM_PROMPT)
        if scored is None:
            return {
                "alias_id": alias_id, "alias_name": alias_name,
                "passing": [], "dry_run_entry": None, "zero_edge_reason": "llm_failure",
            }
        _save_cache("alias", alias_id, ck, scored)

    passing = apply_threshold(scored, valid_ids, SCORE_THRESHOLD_REVERSE, "canonical_id")

    name_lookup = {c["item_id"]: c["name"] for c in candidates}
    dry_run_entry = {
        "alias_id": alias_id,
        "alias_name": alias_name,
        "alias_form": alias.get("form", ""),
        "alias_veg_type": alias.get("veg_type", ""),
        "candidates_retrieved": len(candidates),
        "edges": [
            {
                "canonical_id": e["canonical_id"],
                "canonical_name": name_lookup.get(e["canonical_id"], e["canonical_id"]),
                "score": e["score"],
                "reason": e["reason"],
            }
            for e in passing
        ],
    }

    if not passing:
        log.info("  [%s] 0 edges above threshold", alias_name)
    else:
        log.info("  [%s] %d edge(s) found", alias_name, len(passing))

    if not dry_run and passing:
        with neo4j_session() as session:
            write_edges_for_alias(session, alias_id, passing)

    return {
        "alias_id": alias_id, "alias_name": alias_name,
        "passing": passing, "dry_run_entry": dry_run_entry,
        "zero_edge_reason": None if passing else "below_threshold",
    }


# ---------------------------------------------------------------------------
# Detect orphan aliases from Neo4j
# ---------------------------------------------------------------------------

_LOAD_ORPHAN_ALIASES = """
MATCH (a:Item {source: 'supabase'})
WHERE NOT (:Item {source: 'dynamodb'})-[:VARIANT_OF]->(a)
RETURN a.id AS id, a.name AS name
ORDER BY a.name
"""


def get_orphan_alias_ids() -> set[str]:
    with neo4j_session() as session:
        rows = session.run(_LOAD_ORPHAN_ALIASES)
        return {r["id"] for r in rows}


# ---------------------------------------------------------------------------
# Forward pass orchestrator
# ---------------------------------------------------------------------------


def run_forward_pass(
    qdrant,
    client: genai.Client,
    dry_run: bool,
    workers: int,
) -> tuple[list[dict], list[dict]]:
    """Run forward pass over all canonicals.

    Returns (dry_run_results, zero_edge_canonicals).
    """
    canonicals = load_canonicals(qdrant)
    canonical_ids = sorted(canonicals.keys())

    log.info(
        "FORWARD PASS: %d canonicals | dry_run=%s | threshold=%.1f | cap=%d | workers=%d",
        len(canonical_ids), dry_run, SCORE_THRESHOLD_FORWARD, CANDIDATE_CAP, workers,
    )

    dry_run_results: list[dict] = []
    zero_edge_canonicals: list[dict] = []
    stats = {"edges": 0, "cache_hits": 0, "tier2": 0, "failures": 0, "zero": 0}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_process_canonical, qdrant, client, cid, canonicals[cid], dry_run): cid
            for cid in canonical_ids
        }
        done = 0
        for future in as_completed(futures):
            cid = futures[future]
            done += 1
            try:
                result = future.result()
            except Exception as exc:
                log.error("Worker crashed for canonical %s: %s", cid, exc)
                stats["failures"] += 1
                continue

            if done % 20 == 0:
                log.info("  Forward progress: %d / %d", done, len(canonical_ids))

            if result["cache_hit"]:
                stats["cache_hits"] += 1
            if result["tier"] == "loose":
                stats["tier2"] += 1
            if result["llm_failure"]:
                stats["failures"] += 1
            if result["zero_edge_reason"]:
                stats["zero"] += 1
                zero_edge_canonicals.append({
                    "canonical_id": result["canonical_id"],
                    "canonical_name": result["canonical_name"],
                    "reason": result["zero_edge_reason"],
                })
            else:
                stats["edges"] += len(result["passing"])

            if result["dry_run_entry"] is not None:
                dry_run_results.append(result["dry_run_entry"])

    dry_run_results.sort(key=lambda x: x["canonical_name"])

    log.info(
        "FORWARD PASS DONE — edges: %d | zero-edge canonicals: %d | "
        "tier2: %d | cache_hits: %d | failures: %d",
        stats["edges"], stats["zero"], stats["tier2"], stats["cache_hits"], stats["failures"],
    )
    return dry_run_results, zero_edge_canonicals


# ---------------------------------------------------------------------------
# Reverse pass orchestrator
# ---------------------------------------------------------------------------


def run_reverse_pass(
    qdrant,
    client: genai.Client,
    dry_run: bool,
    workers: int,
) -> tuple[list[dict], list[dict]]:
    """Detect orphan aliases then run reverse pass.

    Returns (dry_run_results, zero_edge_aliases).
    """
    log.info("Detecting orphan aliases (no VARIANT_OF edge)...")
    orphan_ids = get_orphan_alias_ids()
    log.info("Found %d orphan aliases.", len(orphan_ids))

    if not orphan_ids:
        log.info("No orphans — reverse pass skipped.")
        return [], []

    alias_data = load_orphan_aliases(qdrant, orphan_ids)
    log.info(
        "Loaded %d alias vectors (missing from Qdrant: %d).",
        len(alias_data), len(orphan_ids) - len(alias_data),
    )

    log.info(
        "REVERSE PASS: %d orphans | dry_run=%s | threshold=%.1f | cap=%d | workers=%d",
        len(alias_data), dry_run, SCORE_THRESHOLD_REVERSE, CANDIDATE_CAP, workers,
    )

    dry_run_results: list[dict] = []
    zero_edge_aliases: list[dict] = []
    total_edges = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_process_alias, qdrant, client, aid, alias_data[aid], dry_run): aid
            for aid in alias_data
        }
        done = 0
        for future in as_completed(futures):
            aid = futures[future]
            done += 1
            try:
                result = future.result()
            except Exception as exc:
                log.error("Worker crashed for alias %s: %s", aid, exc)
                continue

            if done % 20 == 0:
                log.info("  Reverse progress: %d / %d", done, len(alias_data))

            entry = result.get("dry_run_entry")
            if entry is not None:
                dry_run_results.append(entry)
                edge_count = len(entry.get("edges", []))
                total_edges += edge_count
                if not edge_count:
                    zero_edge_aliases.append({
                        "alias_id": result["alias_id"],
                        "alias_name": result["alias_name"],
                        "reason": result["zero_edge_reason"],
                    })

    dry_run_results.sort(key=lambda e: (-len(e.get("edges", [])), e.get("alias_name", "")))

    log.info(
        "REVERSE PASS DONE — edges: %d | still-orphaned: %d / %d",
        total_edges, len(zero_edge_aliases), len(alias_data),
    )
    return dry_run_results, zero_edge_aliases


# ---------------------------------------------------------------------------
# Top-level run
# ---------------------------------------------------------------------------


def run(
    dry_run: bool = True,
    workers: int = DEFAULT_WORKERS,
    forward_only: bool = False,
    reverse_only: bool = False,
) -> None:
    client = genai.Client(api_key=GEMINI_API_KEY)
    qdrant = get_qdrant_client()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    FWD_DRY_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)

    fwd_results: list[dict] = []
    zero_canonicals: list[dict] = []
    rev_results: list[dict] = []
    zero_aliases: list[dict] = []

    if not reverse_only:
        fwd_results, zero_canonicals = run_forward_pass(qdrant, client, dry_run, workers)
        FWD_DRY_RUN_FILE.write_text(
            json.dumps(fwd_results, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        ZERO_EDGE_CANONICALS_FILE.write_text(
            json.dumps(zero_canonicals, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.info("Forward dry-run written to %s", FWD_DRY_RUN_FILE)

    if not forward_only:
        rev_results, zero_aliases = run_reverse_pass(qdrant, client, dry_run, workers)
        REV_DRY_RUN_FILE.write_text(
            json.dumps(rev_results, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        ZERO_EDGE_ALIASES_FILE.write_text(
            json.dumps(zero_aliases, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.info("Reverse dry-run written to %s", REV_DRY_RUN_FILE)

    total_fwd_edges = sum(len(r.get("edges", [])) for r in fwd_results)
    total_rev_edges = sum(len(r.get("edges", [])) for r in rev_results)

    log.info(
        "\n=== FINAL SUMMARY ===\n"
        "  Forward edges:        %d\n"
        "  Zero-edge canonicals: %d\n"
        "  Reverse edges:        %d\n"
        "  Still-orphaned aliases: %d",
        total_fwd_edges, len(zero_canonicals),
        total_rev_edges, len(zero_aliases),
    )

    if dry_run:
        log.info("DRY RUN — no Neo4j writes. Re-run with --commit to write edges.")
    else:
        log.info("COMMIT COMPLETE — VARIANT_OF edges written to Neo4j.")

    close_connections()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate VARIANT_OF edges — unified forward + reverse pipeline."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Write edges to Neo4j (default: dry-run)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Parallel worker threads (default: {DEFAULT_WORKERS})")
    parser.add_argument("--forward-only", action="store_true",
                        help="Run forward pass only, skip reverse")
    parser.add_argument("--reverse-only", action="store_true",
                        help="Run reverse pass only, skip forward")
    args = parser.parse_args()

    try:
        run(
            dry_run=not args.commit,
            workers=args.workers,
            forward_only=args.forward_only,
            reverse_only=args.reverse_only,
        )
    finally:
        close_connections()


if __name__ == "__main__":
    main()
