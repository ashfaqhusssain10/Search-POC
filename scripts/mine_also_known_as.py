"""Mine also_known_as aliases from llm_description into VARIANT_OF edges.

Reads the `also_known_as` list Gemini generated for every DynamoDB canonical item,
fuzzy-matches each alias against all Supabase item names, and optionally writes
VARIANT_OF edges to Neo4j.

Run in dry-run mode first to review all candidate matches before writing.

Usage:
    python -m scripts.mine_also_known_as             # dry-run (default) — prints table, no writes
    python -m scripts.mine_also_known_as --commit    # write new VARIANT_OF edges to Neo4j
"""

import argparse
import json
import logging
from typing import Any

from rapidfuzz import fuzz, process as rfprocess

from core.connections import close_connections, neo4j_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

FUZZY_THRESHOLD = 85  # token_sort_ratio score (0–100)

# ---------------------------------------------------------------------------
# Neo4j fetchers
# ---------------------------------------------------------------------------

FETCH_DYNAMODB_ITEMS = """
MATCH (i:Item {source: 'dynamodb'})
RETURN i.id AS id, i.name AS name, i.llm_description AS llm_description
ORDER BY i.name
"""

FETCH_SUPABASE_ITEMS = """
MATCH (i:Item {source: 'supabase'})
RETURN i.id AS id, i.name AS name
"""

FETCH_EXISTING_VARIANT_IDS = """
MATCH (c:Item {source: 'dynamodb'})-[:VARIANT_OF]->(a:Item)
RETURN c.id AS canonical_id, a.id AS alias_id
"""


def fetch_dynamodb_items(session) -> list[dict[str, Any]]:
    return [dict(r) for r in session.run(FETCH_DYNAMODB_ITEMS)]


def fetch_supabase_items(session) -> list[dict[str, Any]]:
    return [dict(r) for r in session.run(FETCH_SUPABASE_ITEMS)]


def fetch_existing_edges(session) -> set[tuple[str, str]]:
    """Return set of (canonical_id, alias_id) pairs that already have VARIANT_OF edges."""
    result = session.run(FETCH_EXISTING_VARIANT_IDS)
    return {(r["canonical_id"], r["alias_id"]) for r in result}


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------

def _parse_also_known_as(llm_description: str | None) -> list[str]:
    if not llm_description:
        return []
    try:
        desc = json.loads(llm_description)
        return [s.strip() for s in desc.get("also_known_as", []) if s and s.strip()]
    except (json.JSONDecodeError, ValueError):
        return []


def find_candidates(
    dynamodb_items: list[dict[str, Any]],
    supabase_items: list[dict[str, Any]],
    existing_edges: set[tuple[str, str]],
    threshold: int = FUZZY_THRESHOLD,
) -> list[dict[str, Any]]:
    """For each also_known_as alias, find the best Supabase name match above threshold."""
    # Build {name → id} lookup for Supabase
    supabase_name_to_id: dict[str, str] = {item["name"]: item["id"] for item in supabase_items}
    supabase_names = list(supabase_name_to_id.keys())

    candidates: list[dict[str, Any]] = []

    for item in dynamodb_items:
        aliases = _parse_also_known_as(item.get("llm_description"))
        if not aliases:
            continue

        for alias in aliases:
            match = rfprocess.extractOne(
                alias,
                supabase_names,
                scorer=fuzz.token_sort_ratio,
                score_cutoff=threshold,
            )
            if not match:
                continue

            matched_name, score, _ = match
            matched_id = supabase_name_to_id[matched_name]

            # Skip if edge already exists
            if (item["id"], matched_id) in existing_edges:
                continue

            candidates.append({
                "canonical_id": item["id"],
                "canonical_name": item["name"],
                "alias": alias,
                "supabase_name": matched_name,
                "supabase_id": matched_id,
                "score": score,
            })

    # Sort by canonical name then score desc
    candidates.sort(key=lambda x: (x["canonical_name"], -x["score"]))
    return candidates


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_table(candidates: list[dict[str, Any]]) -> None:
    """Print candidates as a readable table."""
    if not candidates:
        print("\nNo new candidate matches found above threshold.\n")
        return

    col_widths = {
        "canonical_id": max(12, max(len(c["canonical_id"]) for c in candidates)),
        "canonical_name": max(15, max(len(c["canonical_name"]) for c in candidates)),
        "alias": max(20, max(len(c["alias"]) for c in candidates)),
        "supabase_name": max(20, max(len(c["supabase_name"]) for c in candidates)),
        "supabase_id": max(11, max(len(c["supabase_id"]) for c in candidates)),
        "score": 5,
    }

    header = (
        f"{'canonical_id':<{col_widths['canonical_id']}} | "
        f"{'canonical_name':<{col_widths['canonical_name']}} | "
        f"{'also_known_as alias':<{col_widths['alias']}} | "
        f"{'supabase_match':<{col_widths['supabase_name']}} | "
        f"{'supabase_id':<{col_widths['supabase_id']}} | "
        f"score"
    )
    sep = "-" * len(header)

    print(f"\nCandidate VARIANT_OF edges (threshold={FUZZY_THRESHOLD}, new only):\n")
    print(header)
    print(sep)
    for c in candidates:
        print(
            f"{c['canonical_id']:<{col_widths['canonical_id']}} | "
            f"{c['canonical_name']:<{col_widths['canonical_name']}} | "
            f"{c['alias']:<{col_widths['alias']}} | "
            f"{c['supabase_name']:<{col_widths['supabase_name']}} | "
            f"{c['supabase_id']:<{col_widths['supabase_id']}} | "
            f"{c['score']}"
        )
    print(sep)
    print(f"Total: {len(candidates)} candidate edges\n")


# ---------------------------------------------------------------------------
# Neo4j writer
# ---------------------------------------------------------------------------

CREATE_VARIANT_OF = """
UNWIND $pairs AS pair
MATCH (canonical:Item {id: pair.canonical_id})
MATCH (alias:Item {id: pair.supabase_id})
MERGE (canonical)-[:VARIANT_OF]->(alias)
"""


def write_edges(session, candidates: list[dict[str, Any]]) -> int:
    if not candidates:
        return 0
    session.run(CREATE_VARIANT_OF, pairs=candidates)
    return len(candidates)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Mine also_known_as into VARIANT_OF edges.")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Write new edges to Neo4j (default: dry-run only)",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=FUZZY_THRESHOLD,
        help=f"Fuzzy match threshold 0–100 (default: {FUZZY_THRESHOLD})",
    )
    args = parser.parse_args()

    threshold = args.threshold

    with neo4j_session() as session:
        log.info("Fetching items and existing edges from Neo4j...")
        dynamodb_items = fetch_dynamodb_items(session)
        supabase_items = fetch_supabase_items(session)
        existing_edges = fetch_existing_edges(session)

    log.info(
        "Loaded %d DynamoDB items, %d Supabase items, %d existing VARIANT_OF edges",
        len(dynamodb_items),
        len(supabase_items),
        len(existing_edges),
    )

    candidates = find_candidates(dynamodb_items, supabase_items, existing_edges, threshold)
    print_table(candidates)

    if not args.commit:
        print("Dry-run complete. Run with --commit to write edges to Neo4j.")
        close_connections()
        return

    log.info("Committing %d new VARIANT_OF edges to Neo4j...", len(candidates))
    with neo4j_session() as session:
        written = write_edges(session, candidates)
    log.info("Done. %d VARIANT_OF edges written.", written)
    close_connections()


if __name__ == "__main__":
    main()
