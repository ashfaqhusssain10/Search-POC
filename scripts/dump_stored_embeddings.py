"""Dump everything we have stored about each embedded item.

For both Qdrant collections (`searchpoc_aliases`, `searchpoc_canonicals`)
this writes one CSV per side containing, per item:
  - name
  - source `llm_description` JSON (pulled live from Neo4j — the input that
    `build_item_embedding_text()` turned into the embedded text blob)
  - the full 1536-dim vector that Qdrant has stored (the output)

The text blob itself is not persisted anywhere — it's built on the fly
during embed. The closest "what we actually have" is the pair shown here:
the input JSON and the resulting vector.

Output files:
  diagnostics/stored_embeddings_supabase.csv
  diagnostics/stored_embeddings_dynamodb.csv
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any

from core.connections import close_connections, get_qdrant_client, neo4j_session

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ALIAS_COLLECTION = "searchpoc_aliases"
CANONICAL_COLLECTION = "searchpoc_canonicals"


def _scroll_all(qdrant, collection: str) -> list[dict[str, Any]]:
    """Pull every point from a Qdrant collection with payload + vector."""
    out: list[dict[str, Any]] = []
    next_offset = None
    while True:
        points, next_offset = qdrant.scroll(
            collection_name=collection,
            offset=next_offset,
            limit=200,
            with_payload=True,
            with_vectors=True,
        )
        for p in points:
            out.append({
                "id": p.id,
                "payload": p.payload or {},
                "vector": p.vector,
            })
        if next_offset is None:
            break
    return out


def _fetch_descriptions(session, names: list[str], source: str) -> dict[str, str | None]:
    """Pull the raw llm_description JSON for items by name and source."""
    query = """
    MATCH (i:Item {source: $source})
    WHERE i.name IN $names
    RETURN i.name AS name, i.llm_description AS desc
    """
    result: dict[str, str | None] = {}
    for r in session.run(query, names=names, source=source):
        result[r["name"]] = r["desc"]
    return result


def _dump(
    collection: str,
    source: str,
    out_path: Path,
) -> None:
    qdrant = get_qdrant_client()
    print(f"\nScrolling {collection} …")
    points = _scroll_all(qdrant, collection)
    print(f"  {len(points)} points")

    names = sorted({p["payload"].get("name", "") for p in points if p["payload"].get("name")})
    print(f"  pulling llm_description for {len(names)} names from Neo4j ({source}) …")
    with neo4j_session() as session:
        descs = _fetch_descriptions(session, names, source)
    have_desc = sum(1 for v in descs.values() if v)
    print(f"  got {have_desc}/{len(names)} llm_descriptions")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "veg_type", "form", "llm_description", "vector"])
        for p in sorted(points, key=lambda r: (r["payload"].get("name") or "")):
            name = p["payload"].get("name") or ""
            if not name:
                continue
            veg = p["payload"].get("veg_type") or ""
            form = p["payload"].get("form") or ""
            desc = descs.get(name) or ""
            vec = p["vector"]
            # Vector may be a dict for named-vector collections; cast to list
            if isinstance(vec, dict):
                # take the dense vector if present, else the first one we find
                vec = vec.get("dense") or next(iter(vec.values()), [])
            w.writerow([name, veg, form, desc, json.dumps(list(vec))])
            written += 1
    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"  wrote {written} rows to {out_path}  ({size_mb:.1f} MB)")


def main() -> None:
    _dump(
        collection=ALIAS_COLLECTION,
        source="supabase",
        out_path=Path("diagnostics/stored_embeddings_supabase.csv"),
    )
    _dump(
        collection=CANONICAL_COLLECTION,
        source="dynamodb",
        out_path=Path("diagnostics/stored_embeddings_dynamodb.csv"),
    )
    close_connections()


if __name__ == "__main__":
    main()
