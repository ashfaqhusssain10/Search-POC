"""One-shot: manually correct sub_category for the 9 overlap MISS items.

Each item's alias (Supabase) and canonical (DynamoDB) twin disagree on
sub_category after re-enrichment. We pick the correct value per dish and
flip the wrong side to match — then re-embed just those affected items
in Qdrant.

Usage:
    python -m scripts.fix_overlap_subcategories
"""

from __future__ import annotations

import json
import logging

from openai import OpenAI
from qdrant_client.http.models import PointStruct

from core.connections import close_connections, get_qdrant_client, neo4j_session
from core.embedding_text import build_item_embedding_text
from core.settings import EMBEDDING_MODEL, OPENAI_API_KEY
from scripts.embed_items import (
    COLLECTION_ALIASES,
    COLLECTION_CANONICALS,
    _form,
    _ingredients,
    _point_id,
    _veg_type,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# (item_name, source_to_fix, correct_sub_category)
# `source_to_fix` is the side we flip; the other side is left untouched.
FIXES: list[tuple[str, str, str]] = [
    ("Chicken 65",         "supabase", "Dry Dish"),
    ("Chicken Lollipop",   "dynamodb", "Dry Dish"),
    ("Curd",               "dynamodb", "Dairy Dish"),
    ("Egg Manchurian",     "dynamodb", "Gravy Dish"),
    ("Ghee Cashew Pongal", "dynamodb", "Rice Dish"),
    ("Gobi Manchurian",    "supabase", "Dry Dish"),
    ("Idly",               "dynamodb", "Main Dish"),
    ("Paneer 65",          "supabase", "Dry Dish"),
    ("Pappu Charu",        "supabase", "Dal"),
]


FETCH_QUERY = """
MATCH (i:Item {source: $source})
WHERE toLower(i.name) = toLower($name)
RETURN i.id AS id,
       i.name AS name,
       i.itemType AS item_type,
       coalesce(i.category_name, i.itemCategory) AS category,
       i.llm_description AS llm_description
"""

UPDATE_QUERY = """
MATCH (i:Item {source: $source})
WHERE toLower(i.name) = toLower($name)
SET i.llm_description = $new_desc
RETURN i.id AS id
"""


def main() -> None:
    openai = OpenAI(api_key=OPENAI_API_KEY)
    qdrant = get_qdrant_client()

    affected: list[tuple[str, dict]] = []  # (source, fetched_row)

    # ── Patch Neo4j ────────────────────────────────────────────────────────
    with neo4j_session() as session:
        for name, source, new_sub in FIXES:
            rows = list(session.run(FETCH_QUERY, source=source, name=name))
            if not rows:
                log.warning("Skipping %s (%s) — no matching item in Neo4j", name, source)
                continue
            row = dict(rows[0])
            desc = {}
            if row["llm_description"]:
                try:
                    desc = json.loads(row["llm_description"])
                except (json.JSONDecodeError, TypeError):
                    desc = {}
            old_sub = desc.get("sub_category")
            desc["sub_category"] = new_sub
            new_desc_json = json.dumps(desc)
            session.run(UPDATE_QUERY, source=source, name=name, new_desc=new_desc_json)
            log.info("Neo4j: %s [%s]  sub_category: %r → %r",
                     name, source, old_sub, new_sub)
            row["llm_description"] = new_desc_json
            affected.append((source, row))

    if not affected:
        log.info("Nothing to re-embed.")
        return

    # ── Re-embed just these items ─────────────────────────────────────────
    log.info("")
    log.info("Re-embedding %d affected items via OpenAI...", len(affected))

    canonical_points: list[PointStruct] = []
    alias_points: list[PointStruct] = []

    for source, row in affected:
        text = build_item_embedding_text(name=row["name"], llm_description=row["llm_description"])
        vec = openai.embeddings.create(model=EMBEDDING_MODEL, input=[text]).data[0].embedding
        log.info("  Embedded %s [%s]", row["name"], source)

        if source == "dynamodb":
            canonical_points.append(
                PointStruct(
                    id=_point_id(row["id"], prefix="can_"),
                    vector=vec,
                    payload={
                        "item_id": row["id"],
                        "name": row["name"],
                        "category": row.get("category") or "",
                        "veg_type": _veg_type(row),
                        "form": _form(row),
                        "ingredients": _ingredients(row),
                    },
                )
            )
        else:
            alias_points.append(
                PointStruct(
                    id=_point_id(row["id"]),
                    vector=vec,
                    payload={
                        "item_id": row["id"],
                        "name": row["name"],
                        "veg_type": _veg_type(row),
                        "form": _form(row),
                    },
                )
            )

    if canonical_points:
        qdrant.upsert(collection_name=COLLECTION_CANONICALS, points=canonical_points)
        log.info("Upserted %d canonical points into '%s'.",
                 len(canonical_points), COLLECTION_CANONICALS)
    if alias_points:
        qdrant.upsert(collection_name=COLLECTION_ALIASES, points=alias_points)
        log.info("Upserted %d alias points into '%s'.",
                 len(alias_points), COLLECTION_ALIASES)

    close_connections()
    log.info("Done.")


if __name__ == "__main__":
    main()
