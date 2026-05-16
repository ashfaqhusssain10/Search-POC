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

SYSTEM_PROMPT = """You are a culinary expert specializing in Indian cuisine.
Given a list of dish names (with optional hints, including the catalog's
declared veg/non-veg classification), generate a structured description for
each.

For each dish return a JSON object with EXACTLY these keys:
  - cuisine             : high-level cuisine label (e.g. "North Indian", "South Indian", "Indo-Chinese", "Mughlai")
  - category            : course/meal slot (e.g. "Main Course", "Starter", "Dessert", "Bread", "Rice", "Beverage", "Side")
  - sub_category        : physical form (e.g. "Gravy", "Dry", "Flatbread", "Rice Dish", "Sweet", "Snack", "Soup", "Salad")
  - primary_ingredients : list of 4-6 KEY ingredients, title-cased (e.g. ["Paneer", "Tomato", "Butter", "Cream", "Cashew"])
  - cooking_method      : short phrase (e.g. "Slow cooked gravy", "Deep fried", "Tandoor baked", "Dum cooked")
  - flavor_profile      : 3-5 short adjectives, comma-joined (e.g. "Rich, Creamy, Mildly Spiced, Slightly Sweet")
  - texture             : one short phrase describing mouthfeel (e.g. "Smooth gravy with soft paneer cubes")
  - regional_variant    : specific regional style if applicable (e.g. "Punjabi", "Hyderabadi", "Chettinad"); "" if none
  - veg_type            : exactly "VEG", "NONVEG", or "EGG" (respect the hint when provided)

Rules:
  - Be specific and sensory. "Rich, creamy" beats "delicious".
  - Don't invent facts. If unsure of a regional variant, return an empty string.
  - Respect the veg_type hint when provided — don't reclassify the dish.

Respond with a JSON array, one object per input dish, in the same order as the input.
No explanation, no wrapper keys — just the array."""


# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------

def make_client() -> genai.Client:
    """Return a configured Gemini client."""
    return genai.Client(api_key=GEMINI_API_KEY)


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
