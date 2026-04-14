"""Step 5: Run Leiden community detection on the VARIANT_OF + BRIDGE_TO graph.

Graph used for detection:
  - Nodes  : all Item nodes (source='dynamodb' + source='supabase')
  - Edges  : VARIANT_OF (canonical→alias, weight 1.0)
             BRIDGE_TO  (canonical↔canonical, weight 0.5)

VARIANT_OF carries human-grounded LLM evidence (≥0.7 confidence) and gets
full weight. BRIDGE_TO is purely vector-geometric and gets half weight so
alias signal dominates on canonicals that have any direct alias evidence.
On island canonicals (no shared aliases), bridges become the only signal
Leiden has to merge them into multi-canonical communities.

Result:
  - Community nodes created with id='comm_N', member_count
  - MEMBER_OF edges from every item in each community → Community node

Uses graspologic hierarchical_leiden for partitioning.

Usage:
    python -m scripts.detect_communities
"""

import logging

import networkx as nx
from graspologic.partition import hierarchical_leiden

from core.connections import close_connections, neo4j_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Leiden parameters
# ---------------------------------------------------------------------------

MAX_CLUSTER_SIZE: int = 20
RESOLUTION: float = 1.0

VARIANT_OF_WEIGHT: float = 1.0
BRIDGE_TO_WEIGHT: float = 0.5

# ---------------------------------------------------------------------------
# Neo4j — fetch input edges
# ---------------------------------------------------------------------------

FETCH_VARIANT_OF_EDGES = """
MATCH (a:Item)-[:VARIANT_OF]->(b:Item)
RETURN a.id AS src, b.id AS dst
"""

FETCH_BRIDGE_TO_EDGES = """
MATCH (a:Item {source: 'dynamodb'})-[:BRIDGE_TO]->(b:Item {source: 'dynamodb'})
RETURN a.id AS src, b.id AS dst
"""

FETCH_DYNAMODB_ITEMS = """
MATCH (i:Item {source: 'dynamodb'})
RETURN i.id AS id
"""


def build_networkx_graph(session) -> nx.Graph:
    """Build weighted undirected graph from VARIANT_OF and BRIDGE_TO edges.

    Only DynamoDB (canonical) items are seeded as standalone nodes so that every
    node is guaranteed to produce a Community with a summary and a Qdrant vector.
    Supabase alias items are included automatically when a VARIANT_OF edge connects
    them to a canonical item. Supabase items with no VARIANT_OF edges are excluded —
    they cannot produce a meaningful community summary and would create orphaned
    Community nodes that are never indexed in Qdrant.

    Edge weighting:
      VARIANT_OF = 1.0  (LLM-judged human-grounded evidence)
      BRIDGE_TO  = 0.5  (vector-geometric canonical↔canonical signal)

    BRIDGE_TO is loaded after VARIANT_OF and never overwrites a VARIANT_OF
    weight on the same node pair (guard via existing-edge check).
    """
    G = nx.Graph()

    # Seed with canonical DynamoDB items so every isolated canonical gets its
    # own singleton community (backed by llm_description → summary).
    item_result = session.run(FETCH_DYNAMODB_ITEMS)
    for rec in item_result:
        G.add_node(rec["id"])

    # VARIANT_OF — weight 1.0
    variant_count = 0
    for rec in session.run(FETCH_VARIANT_OF_EDGES):
        G.add_edge(rec["src"], rec["dst"], weight=VARIANT_OF_WEIGHT)
        variant_count += 1

    # BRIDGE_TO — weight 0.5; do not downgrade an existing VARIANT_OF edge
    bridge_count = 0
    bridge_skipped = 0
    for rec in session.run(FETCH_BRIDGE_TO_EDGES):
        if G.has_edge(rec["src"], rec["dst"]):
            bridge_skipped += 1
            continue
        G.add_edge(rec["src"], rec["dst"], weight=BRIDGE_TO_WEIGHT)
        bridge_count += 1

    log.info(
        "Graph built: %d nodes  variant_of=%d  bridge_to=%d (skipped %d duplicates)",
        G.number_of_nodes(),
        variant_count,
        bridge_count,
        bridge_skipped,
    )
    return G


# ---------------------------------------------------------------------------
# Leiden detection
# ---------------------------------------------------------------------------

def run_leiden(G: nx.Graph) -> dict[str, str]:
    """Run hierarchical Leiden and return {node_id: community_id} mapping."""
    # graspologic expects node IDs as ints or strings — NetworkX graph works directly
    partitions = hierarchical_leiden(
        G,
        max_cluster_size=MAX_CLUSTER_SIZE,
        resolution=RESOLUTION,
        random_seed=42,
    )

    # hierarchical_leiden returns a PartitionHierarchy; take the final level
    # The object is iterable — each element is a NodePartition(node, cluster, ...)
    node_to_community: dict[str, str] = {}
    for partition in partitions:
        node_to_community[str(partition.node)] = f"comm_{partition.cluster}"

    # Isolated nodes (no VARIANT_OF edges) — assign singleton communities
    next_id = max(
        (int(c.split("_")[1]) for c in node_to_community.values() if c.startswith("comm_")),
        default=-1,
    ) + 1
    for node in G.nodes():
        if str(node) not in node_to_community:
            node_to_community[str(node)] = f"comm_{next_id}"
            next_id += 1

    unique_communities = set(node_to_community.values())
    log.info(
        "Leiden: %d nodes → %d communities",
        len(node_to_community),
        len(unique_communities),
    )
    return node_to_community


# ---------------------------------------------------------------------------
# Neo4j — write communities
# ---------------------------------------------------------------------------

SETUP_CONSTRAINT = (
    "CREATE CONSTRAINT community_id_unique IF NOT EXISTS FOR (c:Community) REQUIRE c.id IS UNIQUE"
)

UPSERT_COMMUNITIES = """
UNWIND $rows AS row
MERGE (c:Community {id: row.community_id})
SET c.member_count = row.member_count
"""

UPSERT_MEMBER_OF = """
UNWIND $pairs AS pair
MATCH (i:Item {id: pair.item_id})
MATCH (c:Community {id: pair.community_id})
MERGE (i)-[:MEMBER_OF]->(c)
"""

BATCH_SIZE = 200


def write_communities_to_neo4j(
    session,
    node_to_community: dict[str, str],
) -> None:
    session.run(SETUP_CONSTRAINT)

    # Compute member counts
    community_members: dict[str, list[str]] = {}
    for node_id, comm_id in node_to_community.items():
        community_members.setdefault(comm_id, []).append(node_id)

    # Upsert Community nodes
    community_rows = [
        {"community_id": cid, "member_count": len(members)}
        for cid, members in community_members.items()
    ]
    for i in range(0, len(community_rows), BATCH_SIZE):
        session.run(UPSERT_COMMUNITIES, rows=community_rows[i : i + BATCH_SIZE])
    log.info("Wrote %d Community nodes.", len(community_rows))

    # Upsert MEMBER_OF edges
    pairs = [
        {"item_id": nid, "community_id": cid}
        for nid, cid in node_to_community.items()
    ]
    for i in range(0, len(pairs), BATCH_SIZE):
        session.run(UPSERT_MEMBER_OF, pairs=pairs[i : i + BATCH_SIZE])
    log.info("Wrote %d MEMBER_OF edges.", len(pairs))


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify(session) -> None:
    result = session.run(
        """
        MATCH (c:Community)
        RETURN c.id, c.member_count
        ORDER BY c.member_count DESC
        LIMIT 10
        """
    )
    log.info("Top 10 communities by member count:")
    for rec in result:
        log.info("  %-15s  members=%d", rec["c.id"], rec["c.member_count"])


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    with neo4j_session() as session:
        G = build_networkx_graph(session)

    node_to_community = run_leiden(G)

    with neo4j_session() as session:
        write_communities_to_neo4j(session, node_to_community)
        verify(session)

    close_connections()
    log.info("Done.")


if __name__ == "__main__":
    main()
