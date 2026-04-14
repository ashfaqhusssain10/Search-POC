"""Step 3b: Add BRIDGE_TO edges between similar canonical (DynamoDB) items.

Purpose
-------
The VARIANT_OF graph has many "island" canonicals — items with valid alias
edges that share no aliases with sibling canonicals. Leiden has no signal to
merge these, so it produces too many singleton communities. BRIDGE_TO edges
inject canonical↔canonical similarity directly so Leiden can pull islands into
multi-canonical clusters.

Mechanism
---------
For each canonical, query its embedding against the searchpoc_canonicals
Qdrant collection with a hard veg_type+form payload filter. Take up to
TOP_K=3 OTHER canonicals with cosine ≥ THRESHOLD (0.80), then drop any
neighbor whose score is more than SCORE_GAP (0.05) below the top match.

Edge semantics
--------------
- New edge type: BRIDGE_TO (canonical → canonical, DynamoDB only)
- Property: score (cosine)
- Written bidirectionally (both directions MERGEd) so Leiden treats them
  symmetrically without needing undirected projection
- Weight in detect_communities.py: 0.5 (alias evidence dominates)

Idempotency
-----------
- Delete all existing BRIDGE_TO edges before writing (full replace)
- Safe to re-run

Sanity check
------------
Logs a warning if Qdrant canonical count != Neo4j canonical count, since
stale embeddings produce stale bridges.

Usage
-----
    python -m scripts.add_canonical_bridges               # dry-run
    python -m scripts.add_canonical_bridges --commit      # write to Neo4j
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from qdrant_client.http.models import FieldCondition, Filter, MatchValue

from core.connections import close_connections, get_qdrant_client, neo4j_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (tunable per architecture review)
# ---------------------------------------------------------------------------

COLLECTION_CANONICALS = "searchpoc_canonicals"

THRESHOLD: float = 0.80
TOP_K: int = 3
SCORE_GAP: float = 0.05  # drop neighbors > SCORE_GAP below top match
QDRANT_SEARCH_LIMIT: int = 20  # over-fetch then filter; excludes self

DRY_RUN_FILE = Path("llm_cache/dry_run_bridges.json")


# ---------------------------------------------------------------------------
# Load canonicals from Qdrant
# ---------------------------------------------------------------------------

def load_canonicals(qdrant) -> dict[str, dict[str, Any]]:
    """Scroll all points from searchpoc_canonicals.

    Returns {item_id: {vector, name, veg_type, form}}.
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
                    "veg_type": point.payload.get("veg_type", ""),
                    "form": point.payload.get("form", ""),
                }
        if next_offset is None:
            break
        offset = next_offset

    log.info("Loaded %d canonical vectors from '%s'.", len(all_points), COLLECTION_CANONICALS)
    return all_points


# ---------------------------------------------------------------------------
# Sanity: compare Qdrant vs Neo4j canonical counts
# ---------------------------------------------------------------------------

def warn_if_count_mismatch(qdrant_count: int, session) -> None:
    """Warn if Qdrant canonical count differs from Neo4j DynamoDB Item count."""
    result = session.run(
        "MATCH (i:Item {source: 'dynamodb'}) RETURN count(i) AS n"
    ).single()
    neo4j_count = int(result["n"]) if result else 0
    if neo4j_count != qdrant_count:
        log.warning(
            "Canonical count mismatch — Neo4j has %d DynamoDB items, "
            "Qdrant '%s' has %d. Bridges will only cover the Qdrant set; "
            "consider re-running embed_items.py.",
            neo4j_count,
            COLLECTION_CANONICALS,
            qdrant_count,
        )
    else:
        log.info("Sanity OK: Neo4j and Qdrant both report %d canonicals.", neo4j_count)


# ---------------------------------------------------------------------------
# Bridge retrieval: top-K with hard filter + score-gap cutoff
# ---------------------------------------------------------------------------

def find_bridges(
    qdrant,
    canonical_id: str,
    canonical: dict[str, Any],
) -> list[dict[str, Any]]:
    """Find up to TOP_K bridge candidates for one canonical.

    Filters
    -------
    - same veg_type (hard)
    - same form (hard)
    - excludes self
    - cosine >= THRESHOLD
    - score not more than SCORE_GAP below top match
    """
    veg_type = canonical.get("veg_type", "")
    form = canonical.get("form", "")
    vector = canonical["vector"]

    must_conditions: list[Any] = []
    if veg_type:
        must_conditions.append(FieldCondition(key="veg_type", match=MatchValue(value=veg_type)))
    if form:
        must_conditions.append(FieldCondition(key="form", match=MatchValue(value=form)))

    if not must_conditions:
        # Without veg_type/form we can't safely produce bridges — skip
        return []

    hits = qdrant.query_points(
        collection_name=COLLECTION_CANONICALS,
        query=vector,
        query_filter=Filter(must=must_conditions),
        limit=QDRANT_SEARCH_LIMIT,
        with_payload=True,
    ).points

    # Filter: exclude self, threshold, then score-gap cutoff
    scored: list[dict[str, Any]] = []
    for h in hits:
        other_id = h.payload.get("item_id", "")
        if not other_id or other_id == canonical_id:
            continue
        if h.score < THRESHOLD:
            continue
        scored.append({
            "item_id": other_id,
            "name": h.payload.get("name", ""),
            "score": round(float(h.score), 4),
        })

    if not scored:
        return []

    scored.sort(key=lambda x: x["score"], reverse=True)
    top_score = scored[0]["score"]
    cutoff = top_score - SCORE_GAP
    filtered = [s for s in scored if s["score"] >= cutoff]
    return filtered[:TOP_K]


# ---------------------------------------------------------------------------
# Neo4j writes
# ---------------------------------------------------------------------------

_DELETE_ALL_BRIDGES = """
MATCH (:Item {source: 'dynamodb'})-[r:BRIDGE_TO]->(:Item {source: 'dynamodb'})
DELETE r
"""

_WRITE_BRIDGES = """
UNWIND $rows AS row
MATCH (a:Item {id: row.src, source: 'dynamodb'})
MATCH (b:Item {id: row.dst, source: 'dynamodb'})
MERGE (a)-[r:BRIDGE_TO]->(b)
SET r.score = row.score
"""


def write_bridges_to_neo4j(rows: list[dict[str, Any]]) -> None:
    """Delete all BRIDGE_TO edges, then write the new set bidirectionally."""
    with neo4j_session() as session:
        before = session.run(
            "MATCH ()-[r:BRIDGE_TO]->() RETURN count(r) AS n"
        ).single()["n"]
        log.info("Deleting %d existing BRIDGE_TO edges...", int(before))
        session.run(_DELETE_ALL_BRIDGES)

        # Write both directions so Leiden / undirected traversal sees both nodes
        bidirectional: list[dict[str, Any]] = []
        for row in rows:
            bidirectional.append(row)
            bidirectional.append({
                "src": row["dst"],
                "dst": row["src"],
                "score": row["score"],
            })

        BATCH = 500
        for i in range(0, len(bidirectional), BATCH):
            session.run(_WRITE_BRIDGES, rows=bidirectional[i : i + BATCH])

        after = session.run(
            "MATCH ()-[r:BRIDGE_TO]->() RETURN count(r) AS n"
        ).single()["n"]
        log.info("Wrote %d BRIDGE_TO edges (bidirectional).", int(after))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def report_histogram(all_scores: list[float]) -> None:
    """Print a histogram of all bridge scores to help re-tune THRESHOLD."""
    if not all_scores:
        log.info("Score histogram: empty.")
        return

    buckets = Counter()
    for s in all_scores:
        bucket = round(s * 100) / 100  # 0.01 buckets
        buckets[round(bucket, 2)] += 1

    log.info("=== BRIDGE_TO score histogram (n=%d) ===", len(all_scores))
    for score in sorted(buckets.keys()):
        bar = "#" * min(buckets[score], 60)
        log.info("  %.2f  %3d  %s", score, buckets[score], bar)


def report_distribution(per_canonical_counts: list[int]) -> None:
    """Print how many canonicals got 0/1/2/3 bridges."""
    dist = Counter(per_canonical_counts)
    log.info("=== Bridges per canonical ===")
    for k in sorted(dist.keys()):
        log.info("  %d bridges: %d canonicals", k, dist[k])


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(dry_run: bool = True) -> None:
    qdrant = get_qdrant_client()
    canonicals = load_canonicals(qdrant)

    with neo4j_session() as session:
        warn_if_count_mismatch(len(canonicals), session)

    log.info(
        "Computing bridges  threshold=%.2f  top_k=%d  score_gap=%.2f  dry_run=%s",
        THRESHOLD, TOP_K, SCORE_GAP, dry_run,
    )

    dry_run_records: list[dict[str, Any]] = []
    write_rows: list[dict[str, Any]] = []
    all_scores: list[float] = []
    per_canonical_counts: list[int] = []

    for cid in sorted(canonicals.keys()):
        canonical = canonicals[cid]
        bridges = find_bridges(qdrant, cid, canonical)
        per_canonical_counts.append(len(bridges))

        for b in bridges:
            all_scores.append(b["score"])
            write_rows.append({
                "src": cid,
                "dst": b["item_id"],
                "score": b["score"],
            })

        dry_run_records.append({
            "canonical_id": cid,
            "canonical_name": canonical["name"],
            "veg_type": canonical["veg_type"],
            "form": canonical["form"],
            "bridges": [
                {
                    "item_id": b["item_id"],
                    "name": b["name"],
                    "score": b["score"],
                }
                for b in bridges
            ],
        })

    # Persist dry-run record
    DRY_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
    DRY_RUN_FILE.write_text(
        json.dumps(dry_run_records, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("Wrote dry-run records to %s", DRY_RUN_FILE)

    # Reports
    report_histogram(all_scores)
    report_distribution(per_canonical_counts)
    log.info(
        "Total directed bridge rows: %d  (will become %d after bidirectional MERGE)",
        len(write_rows),
        len(write_rows) * 2,
    )

    if not dry_run:
        write_bridges_to_neo4j(write_rows)
        log.info("COMMIT COMPLETE.")
    else:
        log.info("DRY RUN COMPLETE — review %s, then re-run with --commit.", DRY_RUN_FILE)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add BRIDGE_TO edges between similar canonical items."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Write BRIDGE_TO edges to Neo4j (default: dry-run).",
    )
    args = parser.parse_args()

    try:
        run(dry_run=not args.commit)
    finally:
        close_connections()


if __name__ == "__main__":
    main()
