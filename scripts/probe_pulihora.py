"""One-off: test whether tweaking embedding-text format closes the
Pulihora ↔ Tamarind Rice gap.

We embed three text variants per item via OpenAI and print pairwise cosines:
  A. CURRENT — exactly what's in Qdrant today (build_item_embedding_text)
  B. NAME_DROPPED — same prose but with the dish name omitted
  C. SYNONYM_INJECTED — name expanded with a common-English alias

If C jumps to ~0.85+, regenerating with synonym-aware names is worth it.
If nothing moves much, the bottleneck is the embedding model itself.
"""

from __future__ import annotations

import json

import numpy as np
from openai import OpenAI

from core.embedding_text import build_item_embedding_text
from core.settings import EMBEDDING_MODEL, OPENAI_API_KEY

PULIHORA_DESC = {
    "cuisine": "Indian Regional",
    "category": "Rice Dish",
    "sub_category": "Rice Dish",
    "primary_ingredients": ["Rice", "Tamarind", "Peanuts", "Mustard Seeds", "Curry Leaves", "Turmeric"],
    "cooking_method": "Prepared & Combined",
    "flavor_profile": "Tangy, Spicy, Savory, Aromatic",
    "texture": "Fluffy, separate rice grains with a slight crunch",
    "regional_variant": "Andhra",
    "veg_type": "VEG",
}

TAMARIND_DESC = {
    "cuisine": "Indian Regional",
    "category": "Rice Dish",
    "sub_category": "Rice Dish",
    "primary_ingredients": ["Rice", "Tamarind Pulp", "Peanuts", "Curry Leaves", "Mustard Seeds", "Red Chillies"],
    "cooking_method": "Prepared & Combined",
    "flavor_profile": "Tangy, Spicy, Savory, Aromatic",
    "texture": "Fluffy rice with a slight chew from peanuts",
    "regional_variant": "South Indian",
    "veg_type": "VEG",
}


def _name_dropped(desc: dict) -> str:
    return build_item_embedding_text("", desc).lstrip(". ").strip()


def _cos(a: list[float], b: list[float]) -> float:
    av, bv = np.asarray(a), np.asarray(b)
    return float(av @ bv / (np.linalg.norm(av) * np.linalg.norm(bv)))


def main() -> None:
    variants = {
        "A_CURRENT": (
            build_item_embedding_text("Pulihora", PULIHORA_DESC),
            build_item_embedding_text("Tamarind Rice", TAMARIND_DESC),
        ),
        "B_NAME_DROPPED": (
            _name_dropped(PULIHORA_DESC),
            _name_dropped(TAMARIND_DESC),
        ),
        "C_SYNONYM_INJECTED": (
            build_item_embedding_text("Pulihora (Tamarind Rice)", PULIHORA_DESC),
            build_item_embedding_text("Tamarind Rice (Pulihora)", TAMARIND_DESC),
        ),
    }

    client = OpenAI(api_key=OPENAI_API_KEY)

    print(f"model={EMBEDDING_MODEL}\n")
    for label, (t1, t2) in variants.items():
        print(f"── {label} ──")
        print(f"  Pulihora       : {t1}")
        print(f"  Tamarind Rice  : {t2}")
        resp = client.embeddings.create(model=EMBEDDING_MODEL, input=[t1, t2])
        v1, v2 = resp.data[0].embedding, resp.data[1].embedding
        print(f"  cosine = {_cos(v1, v2):.4f}\n")


if __name__ == "__main__":
    main()
