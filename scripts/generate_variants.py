"""Step 3 (v2): Generate VARIANT_OF edges via per-canonical Gemini scoring.

Redesigned from v1 (category-batch) to:
  - One Gemini call per canonical: focused prompts, no cross-contamination
  - Parallel execution via ThreadPoolExecutor (--workers, default 10)
  - Tiered Qdrant retrieval:
      Tier 1: veg_type + form filter, top 40
      Tier 2 (< TIER2_TRIGGER results): veg_type only, top 40
  - Score threshold 0.7 (v1 used 0.01 — effectively no filter)
  - Dry-run mode (default): writes to llm_cache/dry_run_variants.json
  - Commit mode (--commit): delete-then-write VARIANT_OF edges in Neo4j
  - Per-canonical LLM cache: llm_cache/variants/canonical_<id>.json
    Cache invalidated when candidate set changes or PROMPT_VERSION bumps.
  - Zero-edge canonicals logged to llm_cache/zero_edge_canonicals.json

Usage:
    python -m scripts.generate_variants                      # dry-run, 10 workers
    python -m scripts.generate_variants --workers 20         # faster dry-run
    python -m scripts.generate_variants --commit             # write edges after review
    python -m scripts.generate_variants --commit --workers 5 # conservative commit
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import hashlib
import json
import logging
import time
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

SCORE_THRESHOLD: float = 0.7
CANDIDATE_CAP: int = 25
TIER2_TRIGGER: int = 10     # drop form filter if strict hits < this
QDRANT_SEARCH_LIMIT: int = 60  # per-tier limit before cap (increased from 40)

PROMPT_VERSION = "v2.1"     # bump this string to invalidate all caches (v2.1 for enriched embeddings)
DEFAULT_WORKERS: int = 10

MODEL = "gemini-2.5-flash"
MAX_RETRIES: int = 3
RETRY_DELAY: float = 2.0

DRY_RUN_FILE = Path("llm_cache/dry_run_variants.json")
ZERO_EDGE_FILE = Path("llm_cache/zero_edge_canonicals.json")
CACHE_DIR = Path("llm_cache/variants")

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are evaluating whether alias dishes are spelling variants or regional \
equivalents of a canonical Indian dish.

Only score ≥ 0.7 if the candidate is genuinely the SAME DISH with a different \
name, spelling, or regional label.

Hard rules — any violation forces score 0.0:
- VEG must NOT match NONVEG (and vice versa)
- Different primary ingredient = different dish
  (chicken ≠ fish, chicken ≠ egg, paneer ≠ chicken, potato ≠ cabbage)
- Different dish form = different dish (curry ≠ rice, biryani ≠ bread, snack ≠ gravy)
- Preparation style alone is not enough — all tandoor dishes are NOT variants of each other

Score guide:
  1.0 = same dish, only spelling / transliteration differs (Pulka / Phulka / Roti)
  0.9 = same dish, regional name variant (Murgh Makhani / Butter Chicken)
  0.8 = same dish, minor variation in preparation label within name
  0.7 = same dish with reasonable confidence
  < 0.7 = related but distinct — do NOT return these
  0.0 = different dish

Return JSON array only. Include candidates you would score ≥ 0.5 (so near-misses are visible).
[{"candidate_id": "<id>", "score": 0.85, "reason": "<brief>"}]
Return [] if no candidates qualify."""

_USER_PROMPT_TEMPLATE = """\
Canonical dish: {name}
Category: {category}
Form: {form}
Veg type: {veg_type}
Ingredients: {ingredients}

Candidates (pre-filtered by veg_type{form_note}):
{candidate_lines}

Score each candidate. Return JSON array only."""


# ---------------------------------------------------------------------------
# Step 1: Load canonical vectors from Qdrant
# ---------------------------------------------------------------------------

def load_canonicals(qdrant) -> dict[str, dict[str, Any]]:
    """Scroll all points from searchpoc_canonicals.

    Returns {item_id: {vector, name, category, veg_type, form, ingredients}}.
    """
    all_points: dict[str, dict[str, Any]] = {}
    offset = None

    while True:
        results, next_offset = qdrant.scroll(
            collection_name=COLLECTION_CANONICALS,
            with_vectors=True,
            with_payload=True,
            limit=100,
            offset=offset,
        )
        for point in results:
            item_id = point.payload.get("item_id", "")
            if item_id:
                all_points[item_id] = {
                    "vector": point.vector,
                    "name": point.payload.get("name", ""),
                    "category": point.payload.get("category", ""),
                    "veg_type": point.payload.get("veg_type", ""),
                    "form": point.payload.get("form", ""),
                    "ingredients": point.payload.get("ingredients", []),
                }
        if next_offset is None:
            break
        offset = next_offset

    log.info("Loaded %d canonical vectors from '%s'.", len(all_points), COLLECTION_CANONICALS)
    return all_points


# ---------------------------------------------------------------------------
# Step 2: Tiered candidate retrieval
# ---------------------------------------------------------------------------

def retrieve_candidates(
    qdrant,
    canonical: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    """Tiered semantic search on searchpoc_aliases.

    Tier 1 (strict): filter by veg_type + form.
    Tier 2 (loose):  filter by veg_type only — triggered when Tier 1 < TIER2_TRIGGER.

    veg_type filter is NEVER dropped (product-safety requirement).

    Returns (candidates[:CANDIDATE_CAP], tier_used).
    Each candidate dict: {item_id, name, veg_type, form, qdrant_score}.
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

    # ── Tier 1: Relaxed Form Match ───────────────────────────────────────────
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

    # Tier 2: drop form filter if starving
    if len(results) < TIER2_TRIGGER and veg_type:
        tier = "loose"
        log.info(
            "  [%s] Tier-2 triggered (%d strict results < %d) — dropping form filter",
            canonical.get("name", ""),
            len(results),
            TIER2_TRIGGER,
        )
        results = _search(Filter(must=[
            FieldCondition(key="veg_type", match=MatchValue(value=veg_type))
        ]))

    return results[:CANDIDATE_CAP], tier


# ---------------------------------------------------------------------------
# Step 3: Per-canonical LLM cache
# ---------------------------------------------------------------------------

def _cache_key(canonical_id: str, candidate_ids: list[str]) -> str:
    """Stable key based on canonical ID, sorted candidate IDs, and prompt version."""
    ids_hash = hashlib.sha256(
        ",".join(sorted(candidate_ids)).encode()
    ).hexdigest()[:12]
    return f"{canonical_id}_{ids_hash}_{PROMPT_VERSION}"


def load_cache(canonical_id: str, cache_key: str) -> list[dict[str, Any]] | None:
    """Return cached LLM output if cache key matches, else None."""
    cache_file = CACHE_DIR / f"canonical_{canonical_id}.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        if data.get("cache_key") == cache_key:
            return data["scored"]
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def save_cache(canonical_id: str, cache_key: str, scored: list[dict[str, Any]]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"canonical_{canonical_id}.json"
    cache_file.write_text(
        json.dumps({"cache_key": cache_key, "scored": scored}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Step 4: Build prompt
# ---------------------------------------------------------------------------

def build_prompt(canonical: dict[str, Any], candidates: list[dict[str, Any]], tier: str) -> str:
    ingredients_str = ", ".join(canonical.get("ingredients", [])[:6]) or "unknown"
    form_note = " and form" if tier == "strict" else " only (form filter relaxed)"
    candidate_lines = "\n".join(
        f"{i + 1}. {c['item_id']}: {c['name']}"
        f" (form: {c['form'] or 'unknown'}, veg: {c['veg_type'] or 'unknown'},"
        f" similarity: {c['qdrant_score']})"
        for i, c in enumerate(candidates)
    )
    return _USER_PROMPT_TEMPLATE.format(
        name=canonical.get("name", ""),
        category=canonical.get("category", "") or "unknown",
        form=canonical.get("form", "") or "unknown",
        veg_type=canonical.get("veg_type", "") or "unknown",
        ingredients=ingredients_str,
        form_note=form_note,
        candidate_lines=candidate_lines,
    )


# ---------------------------------------------------------------------------
# Step 5: Gemini LLM call
# ---------------------------------------------------------------------------

def call_gemini(
    client: genai.Client,
    canonical_name: str,
    prompt: str,
) -> list[dict[str, Any]] | None:
    """Call Gemini with exponential-backoff retry.

    Returns parsed list on success, None after MAX_RETRIES failures.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0,
                ),
            )
            parsed = json.loads(response.text)
            if isinstance(parsed, list):
                return parsed
            # Unwrap {"result": [...]} or similar wrapper shapes
            if isinstance(parsed, dict):
                for val in parsed.values():
                    if isinstance(val, list):
                        return val
            log.warning(
                "Unexpected JSON shape for %r — attempt %d/%d",
                canonical_name, attempt, MAX_RETRIES,
            )
        except json.JSONDecodeError as exc:
            # Attempt to fix common LLM formatting issues (trailing commas)
            try:
                import re
                txt = re.sub(r",\s*([\]}])", r"\1", response.text)
                parsed = json.loads(txt)
                if isinstance(parsed, list): return parsed
                if isinstance(parsed, dict):
                    for val in parsed.values():
                        if isinstance(val, list): return val
            except:
                pass
            log.warning(
                "JSON parse error for %r attempt %d/%d: %s",
                canonical_name, attempt, MAX_RETRIES, exc,
            )
        except Exception as exc:
            log.warning(
                "Gemini call failed for %r attempt %d/%d: %s",
                canonical_name, attempt, MAX_RETRIES, exc,
            )

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY * attempt)

    log.error("Gemini failed for %r after %d attempts — skipping", canonical_name, MAX_RETRIES)
    return None


# ---------------------------------------------------------------------------
# Step 6: Apply score threshold
# ---------------------------------------------------------------------------

def apply_threshold(
    scored: list[dict[str, Any]],
    valid_candidate_ids: set[str],
) -> list[dict[str, Any]]:
    """Return entries with score >= SCORE_THRESHOLD and a recognised candidate_id."""
    passing: list[dict[str, Any]] = []
    for entry in scored:
        if not isinstance(entry, dict):
            continue
        try:
            score = float(entry.get("score", 0.0))
        except (TypeError, ValueError):
            continue
        cid = str(entry.get("candidate_id", "")).strip()
        # Re-attach sub_ prefix if the LLM stripped it
        if cid not in valid_candidate_ids:
            prefixed = f"sub_{cid}"
            if prefixed in valid_candidate_ids:
                cid = prefixed
        if score >= SCORE_THRESHOLD and cid in valid_candidate_ids:
            passing.append({
                "candidate_id": cid,
                "score": score,
                "reason": str(entry.get("reason", ""))[:200],
            })
    return passing


# ---------------------------------------------------------------------------
# Step 7: Neo4j delete-then-write (commit mode only)
# ---------------------------------------------------------------------------

_DELETE_EDGES = """
MATCH (c:Item {id: $canonical_id, source: 'dynamodb'})-[r:VARIANT_OF]->()
DELETE r
"""

_WRITE_EDGES = """
UNWIND $edges AS edge
MATCH (c:Item {id: $canonical_id, source: 'dynamodb'})
MATCH (a:Item {id: edge.candidate_id, source: 'supabase'})
MERGE (c)-[r:VARIANT_OF]->(a)
SET r.score = edge.score, r.reason = edge.reason
"""


def write_edges_for_canonical(
    session,
    canonical_id: str,
    edges: list[dict[str, Any]],
) -> None:
    """Delete all existing VARIANT_OF for this canonical, then write new ones.

    Always deletes first — even if edges is empty — to ensure stale entries
    are removed deterministically.
    """
    session.run(_DELETE_EDGES, canonical_id=canonical_id)
    if edges:
        session.run(_WRITE_EDGES, canonical_id=canonical_id, edges=edges)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Per-canonical worker (runs in thread pool)
# ---------------------------------------------------------------------------

def _process_one(
    qdrant,
    client: genai.Client,
    canonical_id: str,
    canonical: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    """Process a single canonical item. Thread-safe — no shared mutable state.

    Returns a result dict consumed by run() to aggregate stats and outputs.
    """
    canonical_name = canonical["name"]

    # ── Tiered retrieval ─────────────────────────────────────────────────────
    candidates, tier = retrieve_candidates(qdrant, canonical)

    if not candidates:
        log.info("  [%s] No candidates found — skipping", canonical_name)
        return {
            "canonical_id": canonical_id, "canonical_name": canonical_name,
            "tier": tier, "passing": [], "dry_run_entry": None,
            "zero_edge_reason": "no_candidates", "cache_hit": False, "llm_failure": False,
        }

    valid_candidate_ids = {c["item_id"] for c in candidates}

    # ── Cache check ──────────────────────────────────────────────────────────
    cache_key = _cache_key(canonical_id, list(valid_candidate_ids))
    cached = load_cache(canonical_id, cache_key)
    cache_hit = cached is not None

    if cache_hit:
        scored = cached
        log.info("  [%s] Cache hit (%d scored)", canonical_name, len(scored))
    else:
        # ── Gemini call ──────────────────────────────────────────────────────
        prompt = build_prompt(canonical, candidates, tier)
        scored = call_gemini(client, canonical_name, prompt)
        if scored is None:
            return {
                "canonical_id": canonical_id, "canonical_name": canonical_name,
                "tier": tier, "passing": [], "dry_run_entry": None,
                "zero_edge_reason": "llm_failure", "cache_hit": False, "llm_failure": True,
            }
        save_cache(canonical_id, cache_key, scored)

    # ── Apply threshold ──────────────────────────────────────────────────────
    passing = apply_threshold(scored, valid_candidate_ids)

    # ── Build dry-run record ─────────────────────────────────────────────────
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
        log.info("  [%s] 0 edges above threshold (tier=%s, candidates=%d)", canonical_name, tier, len(candidates))
        zero_edge_reason: str | None = "below_threshold"
    else:
        log.info("  [%s] %d edge(s) (tier=%s, candidates=%d)", canonical_name, len(passing), tier, len(candidates))
        zero_edge_reason = None

    # ── Commit to Neo4j (commit mode only) ───────────────────────────────────
    if not dry_run:
        with neo4j_session() as session:
            write_edges_for_canonical(session, canonical_id, passing)

    return {
        "canonical_id": canonical_id,
        "canonical_name": canonical_name,
        "tier": tier,
        "passing": passing,
        "dry_run_entry": dry_run_entry,
        "zero_edge_reason": zero_edge_reason,
        "cache_hit": cache_hit,
        "llm_failure": False,
    }


def run(dry_run: bool = True, workers: int = DEFAULT_WORKERS, target_ids: set[str] | None = None) -> None:
    """Execute the full generate-variants pipeline."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    qdrant = get_qdrant_client()

    canonicals = load_canonicals(qdrant)
    
    if target_ids is not None:
        canonical_ids = sorted([cid for cid in canonicals.keys() if cid in target_ids])
        if not canonical_ids:
            log.warning("No canonicals found matching the zero-edge list!")
            return
    else:
        canonical_ids = sorted(canonicals.keys())

    log.info(
        "Processing %d canonicals  dry_run=%s  threshold=%.1f  cap=%d  workers=%d",
        len(canonical_ids), dry_run, SCORE_THRESHOLD, CANDIDATE_CAP, workers,
    )

    stats: dict[str, int] = {
        "processed": 0,
        "edges_created": 0,
        "zero_edge_canonicals": 0,
        "tier2_retrievals": 0,
        "llm_failures": 0,
        "cache_hits": 0,
    }

    dry_run_results: list[dict[str, Any]] = []
    zero_edge_canonicals: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_process_one, qdrant, client, cid, canonicals[cid], dry_run): cid
            for cid in canonical_ids
        }
        done = 0
        for future in as_completed(futures):
            cid = futures[future]
            done += 1
            try:
                result = future.result()
            except Exception as exc:
                log.error("Worker crashed for %s: %s", cid, exc)
                stats["llm_failures"] += 1
                stats["processed"] += 1
                continue

            if done % 20 == 0:
                log.info("  Progress: %d / %d", done, len(canonical_ids))

            # Aggregate stats
            stats["processed"] += 1
            if result["tier"] == "loose":
                stats["tier2_retrievals"] += 1
            if result["cache_hit"]:
                stats["cache_hits"] += 1
            if result["llm_failure"]:
                stats["llm_failures"] += 1
            if result["zero_edge_reason"]:
                stats["zero_edge_canonicals"] += 1
                zero_edge_canonicals.append({
                    "canonical_id": result["canonical_id"],
                    "canonical_name": result["canonical_name"],
                    "reason": result["zero_edge_reason"],
                })
            else:
                stats["edges_created"] += len(result["passing"])

            if result["dry_run_entry"] is not None:
                dry_run_results.append(result["dry_run_entry"])

    dry_run_results.sort(key=lambda x: x["canonical_name"])

    # ── Persist output files ─────────────────────────────────────────────────
    DRY_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
    DRY_RUN_FILE.write_text(
        json.dumps(dry_run_results, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    ZERO_EDGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ZERO_EDGE_FILE.write_text(
        json.dumps(zero_edge_canonicals, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # ── Summary ──────────────────────────────────────────────────────────────
    successful = stats["processed"] - stats["zero_edge_canonicals"]
    avg = stats["edges_created"] / max(successful, 1)
    log.info(
        "\n=== Summary ===\n"
        "  processed:               %d\n"
        "  edges_created:           %d\n"
        "  avg_edges_per_canonical: %.2f\n"
        "  zero_edge_canonicals:    %d  → %s\n"
        "  tier2_retrievals:        %d\n"
        "  cache_hits:              %d\n"
        "  llm_failures:            %d",
        stats["processed"],
        stats["edges_created"],
        avg,
        stats["zero_edge_canonicals"],
        ZERO_EDGE_FILE,
        stats["tier2_retrievals"],
        stats["cache_hits"],
        stats["llm_failures"],
    )

    if dry_run:
        log.info(
            "\nDRY RUN COMPLETE — no Neo4j writes made.\n"
            "Review %s, then run with --commit to write edges.",
            DRY_RUN_FILE,
        )
    else:
        log.info("COMMIT COMPLETE — VARIANT_OF edges written to Neo4j.")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate VARIANT_OF edges via per-canonical Gemini scoring (v2)."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Write edges to Neo4j (default: dry-run, no writes)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Parallel worker threads (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--only-zeros",
        action="store_true",
        help="Only process canonicals from zero_edge_canonicals.json",
    )
    args = parser.parse_args()

    target_ids = None
    if args.only_zeros:
        zero_file = Path("llm_cache/zero_edge_canonicals.json")
        if zero_file.exists():
            with open(zero_file) as f:
                zeros = json.load(f)
                target_ids = {z["canonical_id"] for z in zeros}
            log.info("Filtering to %d items from zero-edge list", len(target_ids))

    try:
        run(dry_run=not args.commit, workers=args.workers, target_ids=target_ids)
    finally:
        close_connections()


if __name__ == "__main__":
    main()
