# Variant Matching Redesign — Spec
**Date:** 2026-04-10  
**Scope:** `enrich_items.py` (new), `load_items.py` (update), `generate_variants.py` (rewrite)

---

## Problem

The current `generate_variants.py` produces bad edges (e.g. Mutton Biryani ↔ Mutton Dalcha) due to three compounding issues:

1. **Category key mismatch** — `CATEGORY_CANDIDATES` uses clean keys ("Curry") but DynamoDB has 43 messy values ("Curries", "Biryani & Pulav", "Flavoured Rice/Dal/Liquid/Fry/Chutney"). 30+ categories get no filter → LLM receives all 774 Supabase items as candidates → noisy prompt → wrong matches.
2. **Thin item context** — prompt only sends `name | category | type`. The LLM has no ingredient/form signal to distinguish a rice-dish from a stew.
3. **Binary yes/no matching** — no score threshold, so any match the LLM guesses gets an edge.

---

## Solution

Three steps, in sequence:

### Step 1 — `scripts/enrich_items.py` (new file)

Generate a structured description for every item in both CSVs using GPT-4o-mini, then overwrite the CSVs in place.

**Description schema** (stored as a JSON string in the `llm_description` column):
```json
{
  "ingredients": ["mutton", "basmati rice", "whole spices"],
  "form": "rice-dish",
  "cooking_method": "dum-cooked",
  "veg_type": "NONVEG",
  "regional_tags": ["Hyderabadi", "Mughlai"],
  "also_known_as": ["Hyderabadi Biryani"]
}
```

**Form vocabulary** (prevents form/category ambiguity):
`rice-dish | gravy | dry-fry | stew | bread | dessert-sweet | snack | salad | drink | soup | side-accompaniment | fruit`

**Process:**
- Batch 50 items per LLM call (single call: "generate descriptions for this list")
- Two separate runs: DynamoDB CSV, then Supabase CSV
- Skip items that already have `llm_description` (idempotent re-runs)
- Write cache to `llm_cache/enrichment/<csv_name>_batch_<n>.json`
- Overwrite both CSVs in place once all batches complete

**DynamoDB** has `itemDescription` (partial — many null) → include in prompt as hint when present  
**Supabase** has almost no description → LLM generates from name + category + typecode alone

---

### Step 2 — `scripts/load_items.py` (update)

Add `llm_description` to the properties written to Neo4j `Item` nodes. No structural change — just an additional property on existing nodes.

```cypher
SET i.llm_description = $description
```

Re-running `load_items.py` after enrichment is idempotent (uses MERGE on `id`).

---

### Step 3 — `scripts/generate_variants.py` (rewrite)

Replace binary yes/no matching with scored batch matching.

**Category normalization** (replaces `CATEGORY_CANDIDATES` lookup table):

```python
CATEGORY_NORMALIZE = {
    "Curries": "Curry", "Gravy": "Curry", "Mains": "Curry",
    "Biryani & Pulav": "Biryani", "Special Rice": "Rice",
    "Flavoured Rice": "Rice", "Fried Rice/Noodles": "Rice", "Pulao": "Rice",
    "Starters": "Starter", "Savories": "Snack", "Hot/Starter": "Starter",
    "Desserts": "Dessert", "Sweet": "Dessert", "Traditional Sweet": "Dessert",
    "Side/Desserts": "Side", "Bread/Side": "Bread",
    "Accompaniments": "Accompaniment", "Fresh Chutney & Pickles": "Accompaniment",
    "Fresh Grinded Chutney": "Accompaniment", "Cocktail Sides": "Accompaniment",
    "Sides": "Side", "Hot & Cold Beverages": "Beverage", "Welcome Drink": "Beverage",
    "Soups": "Soup", "Liquids": "Soup", "Liquid": "Soup",
    "BBQ Skewers": "Starter", "Fruit/Sweet/Sides": "Fruit",
    "Flavoured Rice/Dal/Liquid/Fry/Chutney": "Rice",
}
```

Normalization applied at fetch time. Unmapped values pass through as-is. After normalization the existing `CATEGORY_CANDIDATES` filter works correctly.

**New scoring prompt:**

```
System: You are a culinary expert. Score each candidate dish 0.0–1.0 on how likely it is 
to be the same dish as the canonical, just named differently. 
Score 1.0 = same dish, different name/spelling/region.
Score 0.0 = different dish.
VEG items MUST NOT match NONVEG items.
Same form required: a rice-dish cannot match a gravy.

Return JSON array: [{"candidate_id": "...", "score": 0.95, "reason": "..."}]
Only return the array, no wrapper.

User:
Canonical: {name} | ingredients: {ingredients} | form: {form} | veg: {veg_type} | region: {regional_tags}

Candidates:
- id=X | name="..." | ingredients: [...] | form: ... | veg: ...
...
```

**Threshold:** score ≥ 0.8 → create `VARIANT_OF` edge  
**Cache:** each batch writes to `llm_cache/variants/<category>_<offset>.json` (already in place)

---

## File Changes

| File | Change |
|---|---|
| `scripts/enrich_items.py` | New — LLM enrichment, overwrites both CSVs |
| `scripts/load_items.py` | Add `llm_description` property to Neo4j write |
| `scripts/generate_variants.py` | Add `CATEGORY_NORMALIZE` map + rewrite prompt to scored batch |

---

## Run Order

```bash
python -m scripts.enrich_items        # enriches + overwrites both CSVs
python -m scripts.load_items          # re-loads items with descriptions into Neo4j
python -m scripts.generate_variants   # scored matching, threshold 0.8
```

---

## Verification

1. After `enrich_items`: check `llm_cache/enrichment/` — spot-check Mutton Biryani and Mutton Dalcha have different `form` values
2. After `load_items`: `MATCH (i:Item {name:"Mutton Biryani"}) RETURN i.llm_description` — should show `form: rice-dish`
3. After `generate_variants`: check `llm_cache/variants/` — Mutton Biryani should have NO match to Mutton Dalcha; Kodi Vepudu should score ≥ 0.8 against Andhra Chicken Fry
4. Check edge count: `MATCH ()-[:VARIANT_OF]->() RETURN count(*)` — should be similar to current 159 but with fewer false positives
