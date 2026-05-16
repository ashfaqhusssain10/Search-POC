"""One-off: generate full PM-spec metadata for sample items and preview the blob.

Generates ALL PM-spec fields fresh via Gemini (does not reuse existing
llm_description). Target schema per item:

    Item Name
    Cuisine             e.g. "North Indian"
    Category            e.g. "Main Course"
    Sub-category        e.g. "Gravy"
    Primary Ingredients e.g. "Paneer, Tomato, Butter, Cream, Cashew"
    Cooking Method      e.g. "Slow cooked gravy"
    Flavor Profile      e.g. "Rich, Creamy, Mildly Spiced, Slightly Sweet"
    Texture             e.g. "Smooth gravy with soft paneer cubes"
    Regional Variant    e.g. "Punjabi"

Then composes the embedding blob in the PM's target format and prints both
the structured fields and the final blob for human review. Does NOT write
anything to Neo4j or Qdrant.

Usage:
    python -m scripts.preview_pm_embedding
    python -m scripts.preview_pm_embedding --names "Paneer Butter Masala,Dal Makhani"
"""

from __future__ import annotations

import argparse
import json
import logging

from google import genai
from google.genai import types

from core.settings import GEMINI_API_KEY

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

MODEL = "gemini-2.5-flash"

DEFAULT_SAMPLE_NAMES = [
    "Paneer Butter Masala",
    "Dal Makhani",
    "Chicken Biryani",
    "Garlic Naan",
    "Gulab Jamun",
]

SYSTEM_PROMPT = """You are a culinary expert specializing in Indian cuisine.

For each dish in the input list, generate a structured description with EXACTLY these keys:

  - item_name          : the dish name (echo back, cleaned up if needed)
  - cuisine            : high-level cuisine label (e.g. "North Indian", "South Indian", "Indo-Chinese", "Mughlai")
  - category           : course/meal slot (e.g. "Main Course", "Starter", "Dessert", "Bread", "Rice", "Beverage")
  - sub_category       : physical form (e.g. "Gravy", "Dry", "Flatbread", "Rice Dish", "Sweet", "Snack", "Soup")
  - primary_ingredients: short list of 4-6 KEY ingredients, title-cased (e.g. ["Paneer", "Tomato", "Butter", "Cream", "Cashew"])
  - cooking_method     : short phrase (e.g. "Slow cooked gravy", "Deep fried", "Tandoor baked", "Dum cooked")
  - flavor_profile     : 3-5 short adjectives (e.g. "Rich, Creamy, Mildly Spiced, Slightly Sweet")
  - texture            : one short phrase describing mouthfeel (e.g. "Smooth gravy with soft paneer cubes")
  - regional_variant   : specific regional style if applicable (e.g. "Punjabi", "Hyderabadi", "Chettinad"); empty string if none

Rules:
  - Be specific and sensory. "Rich, creamy" beats "delicious".
  - Don't invent facts. If unsure of a regional variant, leave it empty.
  - Match the order of the input list exactly.

Return a JSON array of objects with the keys above. No wrapper key, no commentary."""


def call_gemini(names: list[str]) -> list[dict]:
    client = genai.Client(api_key=GEMINI_API_KEY)
    lines = [f"{i + 1}. {n}" for i, n in enumerate(names)]
    response = client.models.generate_content(
        model=MODEL,
        contents="\n".join(lines),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )
    parsed = json.loads(response.text)
    if not isinstance(parsed, list):
        raise ValueError(f"Expected list, got {type(parsed).__name__}")
    return parsed


def compose_blob(item: dict) -> str:
    """Render the PM-spec embedding blob from generated fields."""
    name = item.get("item_name", "")
    cuisine = item.get("cuisine", "")
    category = item.get("category", "")
    sub = item.get("sub_category", "")
    ingredients = item.get("primary_ingredients", []) or []
    cooking = item.get("cooking_method", "")
    flavor = item.get("flavor_profile", "")
    texture = item.get("texture", "")
    regional = item.get("regional_variant", "")

    parts = [f"{name}."]
    header_bits = [b for b in (cuisine, category.lower() if category else "", f"{sub.lower()} dish" if sub else "") if b]
    if header_bits:
        parts.append(f"{' '.join(header_bits)}.")
    if ingredients:
        parts.append(f"Made with {', '.join(ingredients)}.")
    if cooking:
        parts.append(f"{cooking}.")
    if flavor:
        parts.append(f"{flavor}.")
    if texture:
        parts.append(f"{texture}.")
    if regional:
        parts.append(f"{regional} style.")
    return " ".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--names", type=str, default=None,
                        help="Comma-separated item names (default: 5 sample dishes).")
    args = parser.parse_args()

    names = (
        [n.strip() for n in args.names.split(",") if n.strip()]
        if args.names else DEFAULT_SAMPLE_NAMES
    )

    log.info("Generating PM-spec metadata for %d items via Gemini...", len(names))
    items = call_gemini(names)

    for item in items:
        print("=" * 88)
        print(f"Item Name          : {item.get('item_name')}")
        print(f"Cuisine            : {item.get('cuisine')}")
        print(f"Category           : {item.get('category')}")
        print(f"Sub-category       : {item.get('sub_category')}")
        print(f"Primary Ingredients: {', '.join(item.get('primary_ingredients', []) or [])}")
        print(f"Cooking Method     : {item.get('cooking_method')}")
        print(f"Flavor Profile     : {item.get('flavor_profile')}")
        print(f"Texture            : {item.get('texture')}")
        print(f"Regional Variant   : {item.get('regional_variant') or '—'}")
        print("-" * 88)
        print("Embedding blob:")
        print(f"  {compose_blob(item)}")
        print()


if __name__ == "__main__":
    main()
