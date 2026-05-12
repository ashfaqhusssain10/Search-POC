"""Shared helper that turns an Item's metadata into rich embedding text.

Used by both ingest (variant generation, community indexing) and query
(search) so that vector-space alignment between queries and stored
documents is symmetric.

The format keeps the dish name prominent (so name-based matching still
works) but adds discriminative attributes from llm_description JSON:
form, regional tags, cooking method, ingredients. This separates
semantically distinct dishes (e.g. Paneer Butter Masala vs Paneer
Manchurian) that would otherwise collide in name-only embeddings.
"""

from __future__ import annotations

import json
from typing import Any

MAX_INGREDIENTS = 8


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


def _join_list(value: Any, limit: int | None = None) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        items = [str(v).strip() for v in value if v]
        if limit:
            items = items[:limit]
        return ", ".join(items)
    return str(value)


def build_item_embedding_text(
    name: str,
    item_type: str | None = None,
    typecode: str | None = None,
    category: str | None = None,
    llm_description: str | dict | None = None,
) -> str:
    """Build a richly-attributed text blob for embedding a single item.

    All fields except `name` are optional — missing metadata is silently skipped.
    """
    parts: list[str] = [f"{name}."]

    type_bits: list[str] = []
    if item_type:
        type_bits.append(item_type)
    if typecode:
        type_bits.append(typecode)
    if type_bits:
        parts.append(f"{' '.join(type_bits)}.")

    if category:
        parts.append(f"Category: {category}.")

    desc = _parse_description(llm_description)
    if desc:
        regional = _join_list(desc.get("regional_tags"))
        form = desc.get("form")
        cooking = desc.get("cooking_method(recipe)") or desc.get("cooking_method")
        ingredients = _join_list(desc.get("ingredients"), limit=MAX_INGREDIENTS)
        also_known = _join_list(desc.get("also_known_as"), limit=5)

        profile_bits = [bit for bit in (regional, form, cooking) if bit]
        if profile_bits:
            parts.append(f"{' '.join(profile_bits)}.")
        if ingredients:
            parts.append(f"Ingredients: {ingredients}.")
        if also_known:
            parts.append(f"Also known as: {also_known}.")

    return " ".join(parts).strip()
