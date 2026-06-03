"""Step 0: Enrich DynamoDB and Supabase CSVs with LLM-generated item descriptions.

For each item, Gemini generates a structured description in the PM-spec schema:
  {
    cuisine, category, sub_category, primary_ingredients,
    cooking_method, flavor_profile, texture, regional_variant, veg_type
  }

These fields define what the dish IS, TASTES like, and how it's SERVED, and
are concatenated into a single embedding blob downstream (see
core.embedding_text). `veg_type` is retained as a structured field because the
search path filters on it.

This description is written to a new `llm_description` column and the CSVs are
overwritten in place. load_items.py then loads this field into Neo4j.

Usage:
    python -m scripts.enrich_items
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import pandas as pd
from google import genai
from google.genai import types

from core.settings import DYNAMODB_CSV, GEMINI_API_KEY, SUPABASE_CSV

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL = "gemini-2.5-flash"
BATCH_SIZE = 50
MAX_RETRIES = 3
RETRY_DELAY = 5.0

CACHE_DIR = Path("llm_cache/enrichment")
VOCAB_DIR = Path("vocab")

# Fields constrained by closed vocabularies discovered via `discover_vocab.py`.
# Each value here is loaded from vocab/<field>.json as {canonical: [synonyms]}.
CLOSED_VOCAB_FIELDS = ("cuisine", "category", "sub_category", "cooking_method", "regional_variant")


def _load_vocab() -> dict[str, dict[str, list[str]]]:
    """Load vocab/{field}.json into a {field: {canonical: [synonyms]}} map."""
    vocabs: dict[str, dict[str, list[str]]] = {}
    for field in CLOSED_VOCAB_FIELDS:
        path = VOCAB_DIR / f"{field}.json"
        if not path.exists():
            raise FileNotFoundError(
                f"Missing {path}. Run `python -m scripts.discover_vocab` first."
            )
        vocabs[field] = json.loads(path.read_text())
    return vocabs


def _build_normalizer(vocabs: dict[str, dict[str, list[str]]]) -> dict[str, dict[str, str]]:
    """Reverse vocab into a {field: {raw_value_lower: canonical}} lookup."""
    normalizer: dict[str, dict[str, str]] = {}
    for field, clusters in vocabs.items():
        lookup: dict[str, str] = {}
        for canonical, synonyms in clusters.items():
            lookup[canonical.strip().lower()] = canonical
            for syn in synonyms:
                lookup[syn.strip().lower()] = canonical
        normalizer[field] = lookup
    return normalizer


def _format_vocab_for_prompt(vocabs: dict[str, dict[str, list[str]]]) -> str:
    """Render the closed vocab as a bullet list for the system prompt."""
    sections = []
    for field in CLOSED_VOCAB_FIELDS:
        labels = list(vocabs[field].keys())
        sections.append(f"  {field}: {', '.join(repr(l) for l in labels)}")
    return "\n".join(sections)


VOCABS = _load_vocab()
NORMALIZER = _build_normalizer(VOCABS)

SYSTEM_PROMPT = f"""You are a culinary expert specializing in Indian cuisine.
Given a list of dish names (with optional hints, including the catalog's
declared veg/non-veg classification), generate a structured description for
each.

For each dish return a JSON object with EXACTLY these keys:
  - cuisine             : pick ONE value from the closed list below
  - category            : pick ONE value from the closed list below
  - sub_category        : pick ONE value from the closed list below
  - primary_ingredients : list of 4-6 KEY ingredients, title-cased (e.g. ["Paneer", "Tomato", "Butter", "Cream", "Cashew"])
  - cooking_method      : pick ONE value from the closed list below
  - flavor_profile      : 3-5 short adjectives, comma-joined (e.g. "Rich, Creamy, Mildly Spiced, Slightly Sweet")
  - texture             : one short phrase describing mouthfeel (e.g. "Smooth gravy with soft paneer cubes")
  - regional_variant    : pick ONE value from the closed list below, or "" if none applies
  - veg_type            : exactly "VEG", "NONVEG", or "EGG" (respect the hint when provided)

Closed vocabularies (the values for these fields MUST come from these lists,
copied verbatim — same casing, same spelling):
{_format_vocab_for_prompt(VOCABS)}

Rules:
  - For the 5 closed-vocab fields, pick the single best fit. Do NOT invent new
    values, do NOT combine values, do NOT pluralize or rephrase.
  - If no value in a closed list fits well, pick the closest reasonable one.
  - Be specific and sensory for flavor_profile and texture. "Rich, creamy"
    beats "delicious".
  - Respect the veg_type hint when provided — don't reclassify the dish.

Respond with a JSON array, one object per input dish, in the same order as the input.
No explanation, no wrapper keys — just the array."""


# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------

def make_client() -> genai.Client:
    """Return a configured Gemini client."""
    return genai.Client(api_key=GEMINI_API_KEY)


def _normalize_closed_fields(desc: dict[str, Any], batch_label: str) -> None:
    """Snap closed-vocab values onto the canonical label in-place.

    If a value can't be matched even after lowercasing/stripping, leave it
    untouched and log a warning — we want visibility into LLM drift rather
    than silently mapping to a default.
    """
    for field in CLOSED_VOCAB_FIELDS:
        raw = desc.get(field)
        if not raw or not isinstance(raw, str):
            continue
        key = raw.strip().lower()
        canonical = NORMALIZER[field].get(key)
        if canonical is None and field == "regional_variant" and key == "":
            continue
        if canonical is None:
            log.warning("Batch %s: '%s' for field '%s' not in vocab — leaving as-is",
                        batch_label, raw, field)
            continue
        if canonical != raw:
            desc[field] = canonical


# ---------------------------------------------------------------------------
# LLM enrichment
# ---------------------------------------------------------------------------

def enrich_batch(
    client: genai.Client,
    items: list[dict[str, Any]],
    batch_label: str,
) -> list[dict[str, Any]]:
    """Call Gemini to generate descriptions for a batch of items.

    Returns list of description dicts in the same order as input.
    On failure returns empty dicts for all items in the batch.
    """
    lines = []
    for i, item in enumerate(items):
        hint = f' | hint: "{item["hint"]}"' if item.get("hint") else ""
        lines.append(f'{i + 1}. name="{item["name"]}" | veg={item["veg_type"]}{hint}')
    user_prompt = "\n".join(lines)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0,
                ),
            )
            raw = response.text
            parsed = json.loads(raw)

            # Normalize closed-vocab fields against vocab/*.json so anything the
            # LLM drifted on gets snapped back to the canonical label.
            if isinstance(parsed, list):
                for desc in parsed:
                    if isinstance(desc, dict):
                        _normalize_closed_fields(desc, batch_label)

            # Cache raw response
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            safe = batch_label.replace(" ", "_").replace("/", "-")
            (CACHE_DIR / f"{safe}.json").write_text(
                json.dumps({"items": [i["name"] for i in items], "descriptions": parsed}, indent=2)
            )

            if isinstance(parsed, list) and len(parsed) == len(items):
                return parsed
            log.warning("Batch %s: got %d results for %d items — padding", batch_label, len(parsed), len(items))
            # Pad or trim to match input length
            while len(parsed) < len(items):
                parsed.append({})
            return parsed[:len(items)]

        except Exception as exc:
            log.warning("Attempt %d/%d for batch %s failed: %s", attempt, MAX_RETRIES, batch_label, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    log.error("Batch %s failed after %d attempts — using empty descriptions", batch_label, MAX_RETRIES)
    return [{} for _ in items]


# ---------------------------------------------------------------------------
# CSV processing
# ---------------------------------------------------------------------------

def process_dynamodb_csv(client: genai.Client, csv_path: str) -> None:
    """Enrich DynamoDB CSV and overwrite in place."""
    df = pd.read_csv(csv_path, dtype=str)

    if "llm_description" not in df.columns:
        df["llm_description"] = ""

    # Find rows that need enrichment
    needs_enrichment = df["llm_description"].isna() | (df["llm_description"].str.strip() == "")
    todo = df[needs_enrichment].copy()
    log.info("DynamoDB: %d / %d items need enrichment", len(todo), len(df))

    if todo.empty:
        log.info("DynamoDB: all items already enriched — skipping")
        return

    # Build item dicts for LLM
    rows = []
    for _, row in todo.iterrows():
        hint = row.get("itemDescription", "")
        hint = "" if pd.isna(hint) else str(hint).strip()
        rows.append({
            "idx": row.name,
            "name": str(row["itemName"]),
            "veg_type": str(row.get("itemType", "")),
            "hint": hint[:200] if hint else "",  # truncate long descriptions
        })

    # Process in batches
    total = 0
    for batch_start in range(0, len(rows), BATCH_SIZE):
        batch = rows[batch_start : batch_start + BATCH_SIZE]
        label = f"dynamodb_{batch_start}"
        log.info("DynamoDB batch %s: %d items", label, len(batch))

        descriptions = enrich_batch(client, batch, label)

        for item, desc in zip(batch, descriptions):
            df.at[item["idx"], "llm_description"] = json.dumps(desc) if desc else ""
            total += 1

        log.info("  → wrote %d descriptions (running total: %d)", len(batch), total)

    df.to_csv(csv_path, index=False)
    log.info("DynamoDB CSV overwritten: %s", csv_path)


def process_supabase_csv(client: genai.Client, csv_path: str) -> None:
    """Enrich Supabase CSV and overwrite in place."""
    df = pd.read_csv(csv_path, dtype=str)

    if "llm_description" not in df.columns:
        df["llm_description"] = ""

    needs_enrichment = df["llm_description"].isna() | (df["llm_description"].str.strip() == "")
    todo = df[needs_enrichment].copy()
    log.info("Supabase: %d / %d items need enrichment", len(todo), len(df))

    if todo.empty:
        log.info("Supabase: all items already enriched — skipping")
        return

    rows = []
    for _, row in todo.iterrows():
        hint = row.get("item_description", "")
        hint = "" if pd.isna(hint) else str(hint).strip()
        rows.append({
            "idx": row.name,
            "name": str(row["item_name"]),
            "veg_type": str(row.get("item_type", "")),
            "hint": hint[:200] if hint else "",
        })

    total = 0
    for batch_start in range(0, len(rows), BATCH_SIZE):
        batch = rows[batch_start : batch_start + BATCH_SIZE]
        label = f"supabase_{batch_start}"
        log.info("Supabase batch %s: %d items", label, len(batch))

        descriptions = enrich_batch(client, batch, label)

        for item, desc in zip(batch, descriptions):
            df.at[item["idx"], "llm_description"] = json.dumps(desc) if desc else ""
            total += 1

        log.info("  → wrote %d descriptions (running total: %d)", len(batch), total)

    df.to_csv(csv_path, index=False)
    log.info("Supabase CSV overwritten: %s", csv_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Enrich both CSVs with Gemini-generated item descriptions."""
    client = make_client()

    dynamodb_path = DYNAMODB_CSV
    supabase_path = SUPABASE_CSV

    if not os.path.exists(dynamodb_path):
        raise FileNotFoundError(f"DynamoDB CSV not found: {dynamodb_path}")
    if not os.path.exists(supabase_path):
        raise FileNotFoundError(f"Supabase CSV not found: {supabase_path}")

    process_dynamodb_csv(client, dynamodb_path)
    process_supabase_csv(client, supabase_path)

    log.info("Done. Both CSVs enriched with llm_description column.")

if __name__ == "__main__":
    main()
