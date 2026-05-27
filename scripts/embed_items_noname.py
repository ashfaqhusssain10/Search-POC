"""Mirror of scripts.embed_items but with the dish name dropped from the
embedding text. Writes to TEST collections so production is untouched.

Used to A/B test whether name-dropped embeddings improve cross-language
synonym matching (Pulihora ↔ Tamarind Rice, Bagara ↔ Ghee Rice, etc.)
without committing to a re-index of production.

Output collections:
  searchpoc_canonicals_noname  (246 items)
  searchpoc_aliases_noname     (774 items)

Payload schema matches production exactly so search_v4/v5 can query the
test collections with zero code changes (point them at the new names).

Usage:
    python -m scripts.embed_items_noname
"""

from __future__ import annotations

import logging

from openai import OpenAI

from core.connections import close_connections, get_qdrant_client, neo4j_session
from core.embedding_text import build_item_embedding_text
from core.settings import OPENAI_API_KEY
from scripts.embed_items import (
    build_alias_points,
    build_canonical_points,
    embed_all,
    ensure_collection,
    fetch_aliases,
    fetch_canonicals,
    upsert_points,
    verify_collection,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

COLLECTION_CANONICALS_NONAME = "searchpoc_canonicals_noname"
COLLECTION_ALIASES_NONAME = "searchpoc_aliases_noname"


def noname_text(item: dict) -> str:
    """PM-spec blob with the leading '<Name>. ' prefix removed."""
    full = build_item_embedding_text("", item.get("llm_description"))
    return full.lstrip(". ").strip()


def main() -> None:
    client = OpenAI(api_key=OPENAI_API_KEY)
    qdrant = get_qdrant_client()

    with neo4j_session() as session:
        canonicals = fetch_canonicals(session)
        aliases = fetch_aliases(session)

    canon_before, alias_before = len(canonicals), len(aliases)
    canonicals = [i for i in canonicals if i.get("llm_description")]
    aliases = [i for i in aliases if i.get("llm_description")]
    log.info(
        "Fetched %d canonicals (%d dropped, no llm_description), %d aliases (%d dropped).",
        len(canonicals), canon_before - len(canonicals),
        len(aliases), alias_before - len(aliases),
    )

    # ── Canonicals (no-name) ───────────────────────────────────────────────
    log.info("Embedding %d canonicals without name prefix...", len(canonicals))
    canon_texts = [noname_text(i) for i in canonicals]
    canon_vecs = embed_all(client, canon_texts)

    ensure_collection(qdrant, COLLECTION_CANONICALS_NONAME, ["item_id", "veg_type", "form"])
    upsert_points(qdrant, COLLECTION_CANONICALS_NONAME, build_canonical_points(canonicals, canon_vecs))
    verify_collection(qdrant, COLLECTION_CANONICALS_NONAME)

    # ── Aliases (no-name) ──────────────────────────────────────────────────
    log.info("Embedding %d aliases without name prefix...", len(aliases))
    alias_texts = [noname_text(i) for i in aliases]
    alias_vecs = embed_all(client, alias_texts)

    ensure_collection(qdrant, COLLECTION_ALIASES_NONAME, ["item_id", "veg_type", "form"])
    upsert_points(qdrant, COLLECTION_ALIASES_NONAME, build_alias_points(aliases, alias_vecs))
    verify_collection(qdrant, COLLECTION_ALIASES_NONAME)

    log.info("Done. Test collections ready: %s, %s",
             COLLECTION_CANONICALS_NONAME, COLLECTION_ALIASES_NONAME)
    close_connections()


if __name__ == "__main__":
    main()
