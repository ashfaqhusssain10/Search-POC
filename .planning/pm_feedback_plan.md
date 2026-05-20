# Plan: PM Feedback on Search POC Platters

**Status:** research complete, awaiting your review before any code changes.

---

## Feedback items at a glance

| # | PM ask | Scope | Risk | Effort |
|---|---|---|---|---|
| 1 | Show the platter skeleton | UI + Cypher | Low | ~1h |
| 2 | Specificity from item-count-limit, not all items | Scoring + Cypher | Low | ~1h |
| 3 | Service type multi-select filter | UI + Cypher | Low | ~30min |
| 4 | "sweet dish dish" duplication | embedding_text.py | Low | 5 min + re-embed (15 min) |
| 5 | Score threshold recommendation | Scoring + UI | Low | ~30min |

Total: ~3–4 hours of work + one re-embed (~15 min).

---

## 1. Show the platter skeleton

**What PM wants:** A category breakdown of what each platter contains — e.g. "2 Curries · 1 Rice · 1 Bread · 1 Dessert."

**Key finding:** This data already exists and is loaded. We just don't use it in v5.
- `PlatterCategory` nodes carry `items_limit`, `category_family`, `category_order` (loaded from DynamoDB by `scripts/load_platters.py:204-217`).
- A platter's skeleton = its `HAS_CATEGORY` edges grouped by `category_family`, with each slot's count = `PlatterCategory.items_limit`.
- v3 had this logic — it's commented out in `app.py` (the `platter_category_counts` block) and we can adapt the pattern.

**Plan:**
- Extend `FETCH_PLATTERS_QUERY` in `scripts/search_v5.py` to also collect `PlatterCategory` nodes via `HAS_CATEGORY` with their `category_family`, `items_limit`, `category_order`.
- Build a per-platter `skeleton: dict[family, slot_count]` (sum of `items_limit` per family).
- Add a new field `skeleton: dict` on `PlatterResultV5`.
- In `app.py` Platters view, render skeleton in the platter expander as a caption: `"2 Curry · 1 Rice · 1 Bread · 1 Dessert"`, ordered by `category_order`.

**Files touched:** `scripts/search_v5.py`, `app.py`.

---

## 2. Specificity from item-count-limit, not total items

**What PM wants:** Specificity should measure "did the user's selection fill the platter's intended slots" — not "what fraction of every item is matched." A 30-slot catering platter shouldn't be penalized for being 30 slots.

**Key finding:** `intended_slot_count = SUM(PlatterCategory.items_limit)` over `HAS_CATEGORY` edges. Already loaded, never queried.

**Plan:**
- In the same Cypher change as (1), aggregate `intended_slot_count = sum(pc.items_limit)`.
- In `scripts/search_v5.py:score_platter`, change:
  ```
  specificity = matched_count / len(all_items)        # current
  specificity = matched_count / intended_slot_count   # new
  ```
- Fallback: if `intended_slot_count` is null/0 for some platters (legacy data), fall back to `len(all_items)` and log a warning so we see how often this happens.
- Re-run smoke test — expect catering platters to rank higher relative to meal boxes when the user has lots of dishes.

**Files touched:** `scripts/search_v5.py`.

**Risk:** Some platters may not have `PlatterCategory` edges loaded. Need to verify coverage in Neo4j before/after — quick check: `MATCH (p:Platter) WHERE NOT (p)-[:HAS_CATEGORY]->() RETURN count(p)`.

---

## 3. Service type multi-select filter

**What PM wants:** Multi-select filter for Delivery box / Meal box / Snack box / Catering.

**Key findings:**
- `Platter.type` is the right field — single-valued, uppercase.
- Documented values (per `reference.md` + loader): `DELIVERYBOX, MEALBOX, BBQ, BOWLS, SNACKBOX`.
- **"Catering" does not exist** as a `type` value in current data.

**Open question for you/PM:**
- Where does "Catering" live? Three options:
  - (a) Confirm in production: does DynamoDB have a `CATERING` type we haven't ingested? Quick check needed before deciding.
  - (b) If truly absent, add it upstream as a new `platterType` and re-ingest. Cleanest.
  - (c) Stopgap: derive Catering from a heuristic (e.g. `maxPrice > X`, or all of BBQ + BOWLS lumped together). Ugly but unblocks the demo.

**Plan (assuming we resolve the Catering question):**
- Add `service_types: list[str] | None = None` parameter to `search_platters_v5(...)`.
- Add `WHERE p.type IN $service_types` to the Cypher (only if list is non-empty).
- Filter at **query time, not post-rank** — otherwise `top_n=10` is starved by post-filtering.
- In `app.py` Platters view, add a multiselect above the search button, default = all 4 PM categories. Labels: "Delivery Box / Meal Box / Snack Box / Catering" mapped to the raw enum values.

**Files touched:** `scripts/search_v5.py`, `app.py`.

---

## 4. "Sweet dish dish" duplication

**What PM wants:** Fix the embedding blob duplication.

**Key findings:**
- Bug confirmed in `core/embedding_text.py:73` — unconditional `" dish"` suffix on the header.
- Affected sub_category labels (closed vocab): Dry Dish, Gravy Dish, Rice Dish, Sweet Dish, Dairy Dish, Main Dish, Egg Dish — likely 60-80% of items.
- Estimated cosine impact: cosine similarity between buggy and fixed blob ≈ 0.995–0.999 (single repeated token in a ~30-token blob).
- Small in absolute terms, but **directionally biases every "*-Dish" item toward the "dish" axis**, slightly compressing separability between Rice/Gravy/Dry — exactly the discriminative axes that matter for ranking.

**Plan:**
- One-line fix in `core/embedding_text.py`:
  ```python
  header = " ".join(header_bits)
  suffix = "" if header.endswith("dish") else " dish"
  parts.append(f"{header}{suffix}.")
  ```
- Re-embed both Qdrant collections (~15 min).
- Re-run overlap diagnostic to confirm no regression (expect 119/120 → still 119/120 or better).

**Files touched:** `core/embedding_text.py`. Pipeline: re-run `scripts/embed_items`.

---

## 5. Score threshold recommendation

**What PM wants:** A defensible threshold — below what does the result stop making sense?

**Key findings (analysis of 774 alias→top1 pairs in `diagnostics/search_quality.csv`):**

| Tier | Score range | What it looks like | Show in UI? |
|---|---|---|---|
| **Excellent** | ≥ 0.90 | Same dish or named variant (Mutton Ghee Roast → itself, Chicken Pakora → Chicken Pakoda) | Yes, prominently |
| **Good** | 0.80 – 0.90 | Close family member (Garlic Naan → Butter Naan @ 0.84) | Yes, primary |
| **Substitute** | 0.65 – 0.80 | Same form, related ingredient (Achari Chicken Curry → Chicken Curry @ 0.78) | Yes, with "similar to" caveat |
| **Weak** | 0.50 – 0.65 | Generic category fallback (Chole Curry → Kadhi @ 0.67, Aloo Paratha → Butter Naan @ 0.64) | Hide by default; reveal under "Show more" |
| **Noise** | < 0.50 | Tokens-only match (Bacon → Raw Banana Fry @ 0.46, every baked good → Osmania Biscuits) | Never show |

**The knee:** Quality is solid through 0.75. Between 0.70-0.75, match identity shifts from "same dish" to "same category, different dish." Below 0.65, results become generic category fallbacks. Below 0.55 they're token-collision noise.

**Plan:**
- Add a `QUALITY_TIERS` constant to `scripts/search_v4.py` exposing the 4 thresholds.
- In `app.py` Item matches view: tag each hit with its tier label and color/icon (green ≥0.90, blue 0.80-0.90, grey 0.65-0.80, faded 0.50-0.65 hidden under expander).
- In `app.py` Platters view: aggregate the tier breakdown per platter (e.g. "3 Excellent + 1 Substitute").
- Adjust v5 ranking: hide platters where average match score < 0.65 (or where every matched dish is in the Weak/Noise tier). Optionally weight the `quality` component by tier.
- Bump `ITEM_SCORE_THRESHOLD` in `search_v4.py` from 0.0 to 0.50 — never return Noise tier hits at all. The pipeline floor `QDRANT_SCORE_THRESHOLD=0.35` is still appropriate for ETL; this is the *display* floor.

**Files touched:** `scripts/search_v4.py`, `scripts/search_v5.py`, `app.py`.

---

## Suggested execution order

1. **(4) Embedding fix + re-embed** — does the most for downstream quality (cleans up the data every other change uses). 15 min code + 15 min pipeline.
2. **(5) Quality tiers** — pure UI/scoring overlay, no data changes. 30 min.
3. **(1) + (2) Skeleton + specificity** — single Cypher change covers both. 1 hour.
4. **(3) Service filter** — needs your input on the Catering question first. 30 min once decided.

---

## Decisions (locked in)

1. **Catering** — skip for now. Service-type multi-select shows Delivery Box, Meal Box, Snack Box only.
2. **Threshold** — hide everything below **0.80**. Simplified tier scheme:
   - **Excellent** ≥ 0.90 — exact / named variant, show prominently
   - **Good** 0.80 – 0.90 — close family member, show as primary
   - **Hidden** < 0.80 — never surfaced; pipeline `ITEM_SCORE_THRESHOLD` bumped to 0.80
3. **Specificity fallback** — not needed. Assume every Platter has a `HAS_CATEGORY` skeleton.
4. **Overlap diagnostic** — yes, re-run after the embedding fix. Cheap (~1 min), gives us a safety signal that the fix is pure-positive and doesn't flip any borderline cases.
