"""Generate natural-language prose descriptions per item and store on Item nodes.

For each Item in Neo4j, calls Gemini Flash to produce a single-paragraph
description that captures:
    - what the dish IS (cuisine, course, form)
    - what it TASTES like (flavor profile, texture, spice level)
    - what it PAIRS WITH (typical accompaniments / context)
    - any well-known regional context or alternate names

The output is stored on the Item node as `llm_description_prose`. This is
intended to be concatenated with the existing structured embedding text
(`build_item_embedding_text`) and embedded as one richer blob, so the
vector encodes flavor/texture/usage signals beyond the structured fields.

Sample-mode (default 20 items) lets us validate quality and impact on
search scores before paying to enrich all 1000+ items.

Usage:
    python -m scripts.generate_prose_descriptions                 # sample 20
    python -m scripts.generate_prose_descriptions --limit 50      # sample 50
    python -m scripts.generate_prose_descriptions --all           # all items
    python -m scripts.generate_prose_descriptions --names "Paneer Butter Masala,Sambar"
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from google import genai
from google.genai import types

from core.connections import close_connections, neo4j_session
from core.settings import GEMINI_API_KEY

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

MODEL = "gemini-2.5-flash"
BATCH_SIZE = 10
MAX_RETRIES = 3
RETRY_DELAY = 2.0
DEFAULT_WORKERS = 8

SYSTEM_PROMPT = """\
You are a food description writer for an Indian catering catalogue.

For each dish, write ONE paragraph (3–5 sentences, max ~80 words) capturing:
 1. What the dish IS — cuisine, region, course/meal slot, physical form
 2. What it TASTES like — flavor profile, texture, spice level, key sensations
 3. How it's TYPICALLY SERVED — common accompaniments or usage context
 4. Any well-known alternate name or regional variant if applicable

Rules:
 - Plain prose. No bullet points, no headings, no markdown.
 - Concrete and sensory: "creamy, mildly spiced, slightly sweet" beats "delicious"
 - Don't repeat the dish name more than once
 - Don't invent facts; if structured metadata says VEG, don't describe meat
 - Keep it natural — write like a knowledgeable menu writer, not a robot

Return a JSON array. Each element is {"name": "<dish name>", "prose": "<paragraph>"}.
The array MUST be the same length and order as the input list.
"""


def make_client() -> genai.Client:
    return genai.Client(api_key=GEMINI_API_KEY)


# ---------------------------------------------------------------------------
# Neo4j fetch / store
# ---------------------------------------------------------------------------

FETCH_ITEMS_QUERY = """
MATCH (i:Item)
WHERE i.llm_description IS NOT NULL
  AND ($names IS NULL OR i.name IN $names)
  AND ($skip_if_set = false OR i.llm_description_prose IS NULL)
RETURN i.name AS name,
       i.itemType AS item_type,
       coalesce(i.category_name, i.itemCategory) AS category,
       i.typecode_name AS typecode,
       i.llm_description AS llm_description,
       i.source AS source
ORDER BY i.name, i.source
"""

WRITE_PROSE_QUERY = """
UNWIND $rows AS row
MATCH (i:Item {name: row.name, source: row.source})
SET i.llm_description_prose = row.prose
"""


def fetch_items(
    session,
    names: list[str] | None,
    skip_if_set: bool,
    limit: int | None,
) -> list[dict[str, Any]]:
    result = session.run(FETCH_ITEMS_QUERY, names=names, skip_if_set=skip_if_set)
    items = [dict(r) for r in result]
    if limit is not None:
        items = items[:limit]
    return items


def write_prose(session, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    session.run(WRITE_PROSE_QUERY, rows=rows)


# ---------------------------------------------------------------------------
# Gemini call
# ---------------------------------------------------------------------------

def _format_item_input(item: dict[str, Any]) -> str:
    """Build a compact one-line input for the LLM with all known structured signal."""
    desc_raw = item.get("llm_description")
    desc: dict[str, Any] = {}
    if desc_raw:
        try:
            desc = json.loads(desc_raw) if isinstance(desc_raw, str) else desc_raw
        except (json.JSONDecodeError, TypeError):
            desc = {}

    bits = [f'name="{item["name"]}"']
    if item.get("item_type"):
        bits.append(f'veg={item["item_type"]}')
    if item.get("category"):
        bits.append(f'category={item["category"]}')
    if item.get("typecode"):
        bits.append(f'typecode={item["typecode"]}')
    if desc.get("form"):
        bits.append(f'form={desc["form"]}')
    if desc.get("regional_tags"):
        bits.append(f'region={",".join(desc["regional_tags"])}')
    if desc.get("cooking_method(recipe)"):
        bits.append(f'cooking={desc["cooking_method(recipe)"]}')
    if desc.get("ingredients"):
        bits.append(f'ingredients=[{", ".join(desc["ingredients"][:8])}]')
    if desc.get("also_known_as"):
        bits.append(f'aka=[{", ".join(desc["also_known_as"][:3])}]')
    return " | ".join(bits)


def generate_batch(
    client: genai.Client,
    items: list[dict[str, Any]],
) -> list[str]:
    """Generate prose descriptions for a batch. Returns one string per item (in order)."""
    lines = [f"{i + 1}. {_format_item_input(it)}" for i, it in enumerate(items)]
    user_prompt = "\n".join(lines)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )
            parsed = json.loads(response.text)
            if not isinstance(parsed, list):
                raise ValueError(f"Expected list, got {type(parsed).__name__}")
            # Align by index; pad with empty if short
            proses: list[str] = []
            for i, item in enumerate(items):
                if i < len(parsed) and isinstance(parsed[i], dict):
                    p = parsed[i].get("prose", "")
                    proses.append(p.strip() if isinstance(p, str) else "")
                else:
                    proses.append("")
            return proses
        except Exception as exc:
            log.warning("Attempt %d/%d failed: %s", attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    log.error("Batch failed after %d attempts — using empty descriptions", MAX_RETRIES)
    return ["" for _ in items]


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def run(
    names: list[str] | None,
    limit: int | None,
    skip_if_set: bool,
    dry_run: bool,
    workers: int = DEFAULT_WORKERS,
) -> None:
    client = make_client()

    with neo4j_session() as session:
        items = fetch_items(session, names, skip_if_set, limit)

    if not items:
        log.info("No items to process (matching filters).")
        return

    # Slice into batches up front so we can dispatch them concurrently.
    batches: list[list[dict[str, Any]]] = [
        items[start : start + BATCH_SIZE] for start in range(0, len(items), BATCH_SIZE)
    ]
    log.info(
        "Generating prose for %d items in %d batches (workers=%d)",
        len(items), len(batches), workers,
    )

    all_rows: list[dict[str, Any]] = []
    completed_batches = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_batch = {pool.submit(generate_batch, client, b): b for b in batches}
        for future in as_completed(future_to_batch):
            batch = future_to_batch[future]
            try:
                proses = future.result()
            except Exception as exc:
                log.error("Batch crashed: %s — skipping %d items", exc, len(batch))
                proses = ["" for _ in batch]
            for item, prose in zip(batch, proses):
                log.info(
                    "  %-40s [%s] → %s",
                    item["name"], item["source"],
                    prose[:80] + ("…" if len(prose) > 80 else ""),
                )
                if prose:
                    all_rows.append({
                        "name": item["name"], "source": item["source"], "prose": prose,
                    })
            completed_batches += 1
            log.info("Progress: %d/%d batches done", completed_batches, len(batches))

    if dry_run:
        log.info("Dry-run: not writing back to Neo4j. Generated %d rows.", len(all_rows))
        return

    with neo4j_session() as session:
        write_prose(session, all_rows)
    log.info("Wrote llm_description_prose to %d Item nodes.", len(all_rows))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20, help="Process at most N items (default 20).")
    parser.add_argument("--all", action="store_true", help="Process all items (overrides --limit).")
    parser.add_argument("--names", type=str, default=None, help="Comma-separated item names to process.")
    parser.add_argument("--rerun", action="store_true", help="Overwrite existing llm_description_prose (default: skip set).")
    parser.add_argument("--dry-run", action="store_true", help="Don't write back to Neo4j.")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"Concurrent Gemini batches (default {DEFAULT_WORKERS}).")
    args = parser.parse_args()

    names: list[str] | None = None
    if args.names:
        names = [n.strip() for n in args.names.split(",") if n.strip()]

    limit: int | None = args.limit
    if args.all:
        limit = None

    run(
        names=names,
        limit=limit,
        skip_if_set=not args.rerun,
        dry_run=args.dry_run,
        workers=args.workers,
    )
    close_connections()


if __name__ == "__main__":
    main()
