"""Inspect the 287 dynamodb-source Items in Neo4j: which ones are missing
llm_description, and how does the count compare to the 246-item CSV.
"""

from __future__ import annotations

from core.connections import close_connections, neo4j_session


def main() -> None:
    with neo4j_session() as s:
        total = s.run("MATCH (i:Item {source:'dynamodb'}) RETURN count(i) AS c").single()["c"]
        no_desc = list(s.run("""
            MATCH (i:Item {source:'dynamodb'})
            WHERE i.llm_description IS NULL OR i.llm_description = ''
            RETURN i.name AS name, i.id AS id
            ORDER BY name
        """))
        with_desc = s.run("""
            MATCH (i:Item {source:'dynamodb'})
            WHERE i.llm_description IS NOT NULL AND i.llm_description <> ''
            RETURN count(i) AS c
        """).single()["c"]

    print(f"Total dynamodb Items in Neo4j : {total}")
    print(f"  with llm_description        : {with_desc}")
    print(f"  WITHOUT llm_description     : {len(no_desc)}")
    if no_desc:
        print("\nItems missing llm_description:")
        for r in no_desc:
            print(f"  - {r['name']}  (id={r['id']})")

    close_connections()


if __name__ == "__main__":
    main()
