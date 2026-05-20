"""Shared helper that turns an Item's PM-spec metadata into the embedding blob.

Used by both ingest (item embedding) and any query-time enrichment so that
vector-space alignment between query items and stored documents is symmetric.

The blob is a single line built from the PM-spec fields stored on each Item
(produced by `scripts.enrich_items`):

    <Name>. <Cuisine> <category> <sub_category> dish.
    Made with <primary_ingredients>. <cooking_method>.
    <flavor_profile>. <texture>. <regional_variant> style.

Missing fields are silently skipped. The dish name stays first so name-based
matching still works; sensory and usage signal follows.
"""

from __future__ import annotations

import json
from typing import Any


def _parse_description(raw: str | dict | None) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _join(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return ", ".join(str(v).strip() for v in value if v)
    return str(value)


def build_item_embedding_text(
    name: str,
    llm_description: str | dict | None = None,
) -> str:
    """Build the PM-spec embedding blob for a single item.

    `llm_description` is the JSON (or already-parsed dict) produced by
    `scripts.enrich_items`. All fields except `name` are optional — missing
    metadata is silently skipped.
    """
    desc = _parse_description(llm_description)

    cuisine = (desc.get("cuisine") or "").strip()
    category = (desc.get("category") or "").strip()
    sub_category = (desc.get("sub_category") or "").strip()
    ingredients = _join(desc.get("primary_ingredients"))
    cooking = (desc.get("cooking_method") or "").strip()
    flavor = (desc.get("flavor_profile") or "").strip()
    texture = (desc.get("texture") or "").strip()
    regional = (desc.get("regional_variant") or "").strip()

    parts: list[str] = [f"{name}."]

    header_bits = [
        b for b in (cuisine, category.lower() if category else "", sub_category.lower() if sub_category else "")
        if b
    ]
    if header_bits:
        header = " ".join(header_bits)
        # Closed-vocab sub_category labels like "Sweet Dish"/"Rice Dish" already
        # end in "dish"; skip the suffix to avoid "sweet dish dish".
        suffix = "" if header.endswith("dish") else " dish"
        parts.append(f"{header}{suffix}.")

    if ingredients:
        parts.append(f"Made with {ingredients}.")
    if cooking:
        parts.append(f"{cooking}.")
    if flavor:
        parts.append(f"{flavor}.")
    if texture:
        parts.append(f"{texture}.")
    if regional:
        parts.append(f"{regional} style.")

    return " ".join(parts).strip()
