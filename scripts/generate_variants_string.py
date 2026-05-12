"""Step 3b: String-similarity VARIANT_OF edge pass.

Fills gaps left by the LLM pipeline (generate_variants.py) by linking
Supabase aliases to DynamoDB canonicals using exact + fuzzy name matching.
Bypasses Qdrant retrieval and Gemini scoring entirely.

Runs AFTER generate_variants.py so it only touches orphaned aliases (no
existing VARIANT_OF edge). The LLM pipeline's delete-then-write is
idempotent, so this script's edges survive future LLM re-runs only if the
LLM also scores them ≥ 0.7. For guaranteed persistence, run this script
last in the variant step.

Usage:
    python -m scripts.generate_variants_string            # dry-run (default)
    python -m scripts.generate_variants_string --commit   # write edges to Neo4j
"""

from __future__ import annotations

import argparse
import json
import logging
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from core.connections import close_connections, get_qdrant_client, neo4j_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FUZZY_THRESHOLD: float = 0.90  # SequenceMatcher ratio; 1.0 = exact (case-insensitive)
# At 0.90, only very close spelling variants pass (e.g. Phulka/Pulka).
# Dosakaya/Sorakaya (0.857) and similar ingredient-name collisions are excluded.
COLLECTION_CANONICALS = "searchpoc_canonicals"
COLLECTION_ALIASES = "searchpoc_aliases"
OUTPUT_FILE = Path("llm_cache/string_match_variants.json")

# ---------------------------------------------------------------------------
# Cypher
# ---------------------------------------------------------------------------

_LOAD_CANONICALS = """
MATCH (c:Item {source: 'dynamodb'})
RETURN c.id AS id, c.name AS name
"""

_LOAD_ORPHAN_ALIASES = """
MATCH (a:Item {source: 'supabase'})
WHERE NOT (:Item {source: 'dynamodb'})-[:VARIANT_OF]->(a)
RETURN a.id AS id, a.name AS name
"""

_DELETE_EDGES = """
MATCH (c:Item {id: $canonical_id, source: 'dynamodb'})-[r:VARIANT_OF]->()
WHERE EXISTS { MATCH (c)-[r]->(:Item {id: r.end_id, source: 'supabase'}) }
DELETE r
"""

# Targeted delete: only remove edges to the specific alias we're about to write
_DELETE_EDGE_PAIR = """
MATCH (c:Item {id: $canonical_id, source: 'dynamodb'})
      -[r:VARIANT_OF]->
      (a:Item {id: $alias_id, source: 'supabase'})
DELETE r
"""

_WRITE_EDGES = """
UNWIND $edges AS edge
MATCH (c:Item {id: $canonical_id, source: 'dynamodb'})
MATCH (a:Item {id: edge.alias_id, source: 'supabase'})
MERGE (c)-[r:VARIANT_OF]->(a)
SET r.score = edge.score, r.reason = edge.reason
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Words that indicate dish form — if the differing word is one of these,
# the two names are different dishes (not spelling variants).
_FORM_WORDS: frozenset[str] = frozenset({
    "curry", "fry", "dry", "gravy", "rice", "biryani", "pulao", "bread",
    "roti", "naan", "paratha", "soup", "salad", "snack", "wrap", "roll",
    "burger", "sandwich", "pizza", "noodles", "pasta", "masala", "roast",
    "stir", "fried", "grilled", "baked", "steamed",
})


def normalize(name: str) -> str:
    return name.strip().lower()


def _word_set(name: str) -> set[str]:
    return set(normalize(name).split())


def has_form_word_change(canonical_name: str, alias_name: str) -> bool:
    """Return True if the names differ only in a form-indicating word.

    E.g. "Mutton Curry" vs "Mutton Fry" → True (different dish).
    "Chicken Manchurian Gravy" vs "Chicken Manchurian" → False (subset, ok).
    """
    cwords = _word_set(canonical_name)
    awords = _word_set(alias_name)
    # Words present in one but not the other
    c_only = cwords - awords
    a_only = awords - cwords
    # If unique words on either side are form words, reject
    return bool((c_only | a_only) & _FORM_WORDS)


def string_score(canonical_name: str, alias_name: str) -> float:
    """Return 1.0 for case-insensitive exact match, else SequenceMatcher ratio."""
    cn = normalize(canonical_name)
    an = normalize(alias_name)
    if cn == an:
        return 1.0
    return SequenceMatcher(None, cn, an).ratio()


def scroll_veg_types(qdrant, collection: str) -> dict[str, str]:
    """Return {item_id: veg_type} for all points in the collection."""
    result: dict[str, str] = {}
    offset = None
    while True:
        points, next_offset = qdrant.scroll(
            collection_name=collection,
            with_payload=True,
            with_vectors=False,
            limit=200,
            offset=offset,
        )
        for point in points:
            item_id = point.payload.get("item_id", "")
            if item_id:
                result[item_id] = point.payload.get("veg_type", "")
        if next_offset is None:
            break
        offset = next_offset
    return result


# ---------------------------------------------------------------------------
# Core matching
# ---------------------------------------------------------------------------


def find_string_matches(
    canonicals: list[dict[str, str]],
    orphans: list[dict[str, str]],
    canonical_veg: dict[str, str],
    alias_veg: dict[str, str],
) -> list[dict[str, Any]]:
    """Return all (canonical, alias) pairs that pass threshold + veg_type guard."""
    matches: list[dict[str, Any]] = []

    for canon in canonicals:
        cid = canon["id"]
        cname = canon["name"]
        cveg = canonical_veg.get(cid, "")

        canon_matches: list[dict[str, Any]] = []
        for alias in orphans:
            aid = alias["id"]
            aname = alias["name"]
            aveg = alias_veg.get(aid, "")

            # Hard veg_type guard — skip if one is VEG and other is NONVEG
            if cveg and aveg and cveg != aveg:
                continue

            score = string_score(cname, aname)
            if score < FUZZY_THRESHOLD:
                continue

            # Reject if the names differ only by a form-indicating word
            # (e.g. "Mutton Curry" vs "Mutton Fry" — different dish)
            if score < 1.0 and has_form_word_change(cname, aname):
                continue

            reason = (
                "exact name match (case-insensitive)"
                if score == 1.0
                else f"fuzzy name match (ratio={score:.3f})"
            )
            canon_matches.append({
                "canonical_id": cid,
                "canonical_name": cname,
                "alias_id": aid,
                "alias_name": aname,
                "score": round(score, 4),
                "reason": reason,
            })

        matches.extend(canon_matches)

    return matches


# ---------------------------------------------------------------------------
# Neo4j write
# ---------------------------------------------------------------------------


def write_matches(session, matches: list[dict[str, Any]]) -> None:
    """Write VARIANT_OF edges grouped by canonical. Uses MERGE (no double-delete)."""
    by_canonical: dict[str, list[dict[str, Any]]] = {}
    for m in matches:
        by_canonical.setdefault(m["canonical_id"], []).append(m)

    total = 0
    for canonical_id, group in by_canonical.items():
        edges = [
            {"alias_id": m["alias_id"], "score": m["score"], "reason": m["reason"]}
            for m in group
        ]
        session.run(_WRITE_EDGES, canonical_id=canonical_id, edges=edges)
        total += len(edges)
        log.info(
            "  Wrote %d edge(s) for canonical '%s'",
            len(edges),
            group[0]["canonical_name"],
        )

    log.info("Committed %d VARIANT_OF edge(s) total.", total)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(commit: bool) -> None:
    qdrant = get_qdrant_client()

    log.info("Loading canonical veg_types from Qdrant...")
    canonical_veg = scroll_veg_types(qdrant, COLLECTION_CANONICALS)
    log.info("Loading alias veg_types from Qdrant...")
    alias_veg = scroll_veg_types(qdrant, COLLECTION_ALIASES)

    log.info("Loading canonical items from Neo4j...")
    with neo4j_session() as session:
        canonicals = [dict(r) for r in session.run(_LOAD_CANONICALS)]
        log.info("Loaded %d canonicals.", len(canonicals))

        log.info("Loading orphan Supabase aliases (no existing VARIANT_OF)...")
        orphans = [dict(r) for r in session.run(_LOAD_ORPHAN_ALIASES)]
        log.info("Found %d orphan aliases.", len(orphans))

    log.info("Running string matching (threshold=%.2f)...", FUZZY_THRESHOLD)
    matches = find_string_matches(canonicals, orphans, canonical_veg, alias_veg)
    log.info("Found %d match(es) above threshold.", len(matches))

    # Always write the dry-run JSON for review
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(matches, indent=2, ensure_ascii=False))
    log.info("Dry-run output written to %s", OUTPUT_FILE)

    if not commit:
        log.info("Dry-run complete. Re-run with --commit to write edges.")
        return

    log.info("Committing edges to Neo4j...")
    with neo4j_session() as session:
        write_matches(session, matches)

    close_connections()
    log.info("Done.")


def main() -> None:
    parser = argparse.ArgumentParser(description="String-similarity VARIANT_OF pass")
    parser.add_argument(
        "--commit",
        action="store_true",
        default=False,
        help="Write VARIANT_OF edges to Neo4j (default: dry-run only)",
    )
    args = parser.parse_args()
    run(commit=args.commit)


if __name__ == "__main__":
    main()
