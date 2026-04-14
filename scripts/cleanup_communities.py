"""Wipe all community state from Neo4j.

Deletes HAS_COMMUNITY edges, MEMBER_OF edges, and Community nodes in
dependency order. Leaves Item nodes, Platter nodes, CONTAINS edges, and
VARIANT_OF edges untouched.

Run before re-executing the detect_communities → build_community_edges →
generate_summaries → index_communities pipeline to ensure a clean slate.

Usage:
    python -m scripts.cleanup_communities
"""

from __future__ import annotations

from core.connections import close_connections, neo4j_session


def _count(session, query: str) -> int:
    return session.run(query).single()[0]


def _report_counts(session, label: str) -> None:
    communities = _count(session, "MATCH (c:Community) RETURN count(c)")
    member_of = _count(session, "MATCH ()-[r:MEMBER_OF]->() RETURN count(r)")
    has_community = _count(
        session, "MATCH ()-[r:HAS_COMMUNITY]->() RETURN count(r)"
    )
    print(f"[{label}]")
    print(f"  Community nodes:        {communities}")
    print(f"  MEMBER_OF edges:        {member_of}")
    print(f"  HAS_COMMUNITY edges:    {has_community}")


def cleanup() -> None:
    """Delete all community-related graph state."""
    with neo4j_session() as session:
        _report_counts(session, "BEFORE")

        print("\nDeleting HAS_COMMUNITY edges...")
        session.run("MATCH ()-[r:HAS_COMMUNITY]->() DELETE r")

        print("Deleting MEMBER_OF edges...")
        session.run("MATCH ()-[r:MEMBER_OF]->() DELETE r")

        print("Deleting Community nodes...")
        session.run("MATCH (c:Community) DELETE c")

        print()
        _report_counts(session, "AFTER")

        # Sanity: preserved nodes/edges
        items = _count(session, "MATCH (i:Item) RETURN count(i)")
        platters = _count(session, "MATCH (p:Platter) RETURN count(p)")
        contains = _count(session, "MATCH ()-[r:CONTAINS]->() RETURN count(r)")
        variant_of = _count(session, "MATCH ()-[r:VARIANT_OF]->() RETURN count(r)")
        print("\n[PRESERVED]")
        print(f"  Item nodes:             {items}")
        print(f"  Platter nodes:          {platters}")
        print(f"  CONTAINS edges:         {contains}")
        print(f"  VARIANT_OF edges:       {variant_of}")


if __name__ == "__main__":
    try:
        cleanup()
    finally:
        close_connections()
