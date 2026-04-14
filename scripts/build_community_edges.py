"""Step 6: Pre-compute HAS_COMMUNITY edges on Platter nodes.

Traverses:
  (Platter)-[:CONTAINS]->(Item)-[:MEMBER_OF]->(Community)

and writes:
  (Platter)-[:HAS_COMMUNITY]->(Community)

This is the edge used at query time for fast platter ranking.

Usage:
    python -m scripts.build_community_edges
"""

import logging

from core.connections import close_connections, neo4j_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

BUILD_HAS_COMMUNITY = """
MATCH (p:Platter)-[:CONTAINS]->(i:Item)-[:MEMBER_OF]->(c:Community)
WITH p, c
MERGE (p)-[:HAS_COMMUNITY]->(c)
"""

VERIFY_QUERY = """
MATCH (p:Platter)-[:HAS_COMMUNITY]->(c:Community)
RETURN p.name AS platter, count(DISTINCT c.id) AS community_count
ORDER BY community_count DESC
LIMIT 10
"""


def main() -> None:
    with neo4j_session() as session:
        log.info("Building HAS_COMMUNITY edges...")
        session.run(BUILD_HAS_COMMUNITY)
        log.info("HAS_COMMUNITY edges written.")

        log.info("Verification — top 10 platters by community coverage:")
        result = session.run(VERIFY_QUERY)
        for rec in result:
            log.info("  %-40s  communities=%d", rec["platter"], rec["community_count"])

    close_connections()
    log.info("Done.")


if __name__ == "__main__":
    main()
