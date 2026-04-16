# SearchPOC Eval Queries — v1

**Purpose:** A frozen set of 20 realistic customer menu queries. Each query is a full menu (not a single item), grounded in items that actually appear in DISCOUNTED platters in Neo4j. Used to measure recall@3, MRR, and per-query regressions across ETL / ranking / embedding changes.

**How to use:**
1. Run every query through `search_platters()`
2. Record top 3 platter names + coverage ratio returned
3. Compare against `expected_top` (which you approve/edit once, then freeze)
4. Any run is strictly better than previous iff no query regresses AND ≥1 improves

**Catalog note:** Queries are built from DISCOUNTED platters only (matching current `search.py` filter). The catalog is Telugu/South-Indian heavy (Pulihora, Pappu Charu Annam, Bhendi Peanut Fry, Nethi Bobatlu, Cut Mirchi Bajji) with North Indian staples (Paneer Butter Masala, Butter Chicken Masala, Pulka). Dal Makhani / Garlic Naan / Gulab Jamun are canonical items but NOT present in DISCOUNTED platters — queries that include them intentionally test catalog-gap behavior.

**Expected platters are marked `TO CONFIRM`** — author must review and lock before first baseline run.

---

## Q1 — Simple North Indian veg lunch
**Query:** `Paneer Butter Masala, Veg Biryani, Pulka, Raitha`
**Tests:** exact-match sanity, common veg combo
**Expected top 3:** Classic Comfort Meal Box / Simple Platter / Basic Party *(TO CONFIRM)*
**Failure mode guarded:** baseline — if this fails, nothing works

---

## Q2 — Simple North Indian non-veg lunch
**Query:** `Butter Chicken Masala, Chicken Pulao, Pulka, Raitha`
**Tests:** exact-match non-veg combo
**Expected top 3:** Classic Rice and Curry Meal Box / Simple Platter / Basic Party *(TO CONFIRM)*
**Failure mode guarded:** non-veg routing

---

## Q3 — South Indian breakfast
**Query:** `Idly, Vada, Sambar, Peanut Chutney, Pongal`
**Tests:** full exact match on a specialised (breakfast) platter
**Expected top 3:** Premium Breakfast / Premium Breakfast Meal Box *(TO CONFIRM)*
**Failure mode guarded:** meal-type routing — breakfast items should not pull lunch platters

---

## Q4 — Telugu traditional lunch
**Query:** `Pulihora, Pappu Charu Annam, Bhendi Peanut Fry, Palak Dal, Plain Curd`
**Tests:** regional / rare items
**Expected top 3:** Premium Feast / Royal Feast / Mega Celebrations *(TO CONFIRM)*
**Failure mode guarded:** regional coverage — these items are rare across platters

---

## Q5 — Biryani-centric non-veg
**Query:** `Chicken Biryani, Raitha, Green Salad`
**Tests:** minimal biryani combo; Chicken Biryani appears in only 5 platters
**Expected top 3:** Biryani & Pulav Bowl (Fixed) / Standard Taste of Traditions *(TO CONFIRM)*
**Failure mode guarded:** sparse-catalog item (Chicken Biryani) — should not confuse with Chicken Pulao

---

## Q6 — Mixed veg + non-veg feast
**Query:** `Paneer Butter Masala, Butter Chicken Masala, Chicken Pulao, Veg Biryani`
**Tests:** mixed menu coverage
**Expected top 3:** Simple Platter / Festive Feast Meal Box / Classic Rice and Curry *(TO CONFIRM)*
**Failure mode guarded:** veg flag should NOT exclude matching non-veg platters when user asks for both

---

## Q7 — Starters-only order
**Query:** `Chilli Chicken, VEG Manchuria, Cut Mirchi Bajji`
**Tests:** starters-only; no rice/curry/bread
**Expected top 3:** Grand Event / Mega Celebrations / Royal Feast *(TO CONFIRM — may also hit a starters-specific platter)*
**Failure mode guarded:** narrow-category queries (don't default to "biggest platter wins")

---

## Q8 — Alias resolution: paneer variations
**Query:** `Paneer Makhani, Kaju Paneer Butter Masala, Pulka, Veg Biryani`
**Tests:** Supabase aliases must resolve to "Paneer Butter Masala" canonical
**Expected top 3:** Same as Q1 — Classic Comfort / Simple Platter / Basic Party *(TO CONFIRM)*
**Failure mode guarded:** VARIANT_OF / community layer — if this doesn't match Q1, aliases are broken

---

## Q9 — Alias resolution: biryani variations
**Query:** `Chicken Dum Biryani, Chicken Fry Piece Biryani, Raitha`
**Tests:** Supabase aliases for Chicken Biryani
**Expected top 3:** Biryani & Pulav Bowl (Fixed) / Standard Taste of Traditions *(TO CONFIRM)*
**Failure mode guarded:** alias resolution on a sparse-catalog canonical

---

## Q10 — Alias resolution: butter chicken variations
**Query:** `Chicken Tikka Masala, Butter Chicken Curry, Pulka, Chicken Pulao`
**Tests:** Supabase aliases for Butter Chicken; distinguish from Butter Chicken *Masala* canonical
**Expected top 3:** Standard Taste of Traditions / North Indian Meal Box / Simple Platter *(TO CONFIRM)*
**Failure mode guarded:** two similar canonicals ("Butter Chicken" vs "Butter Chicken Masala") — Leiden must not collapse them incorrectly

---

## Q11 — Casual / imprecise phrasing
**Query:** `paneer curry, chicken curry, rice, curd`
**Tests:** generic words; not canonical names
**Expected top 3:** Classic Rice and Curry Meal Box / Simple Platter *(TO CONFIRM)*
**Failure mode guarded:** embeddings must handle generic descriptors, not just exact catalog names

---

## Q12 — Kids party
**Query:** `Chicken Pulao, Green Salad, Tomato Ketchup, Cookie - 2pc`
**Tests:** rare items (Tomato Ketchup, Cookie) alongside common items
**Expected top 3:** Basic Party / Simple Platter *(TO CONFIRM)*
**Failure mode guarded:** rare-item recall — should not drop them from matching entirely

---

## Q13 — Full veg feast (8 items)
**Query:** `Paneer Butter Masala, Veg Biryani, VEG Pulao, VEG Manchuria, Sambar, Pulka, Raitha, Plain Curd`
**Tests:** large menu → should find a platter with high coverage
**Expected top 3:** Just Rice & Accompaniments + Simple Platter + Festive Feast (or a dedicated veg platter) *(TO CONFIRM)*
**Failure mode guarded:** coverage-based ranking on high-item queries

---

## Q14 — Full non-veg feast (8 items)
**Query:** `Paneer Butter Masala, Butter Chicken Masala, Chilli Chicken, Chicken Pulao, Veg Biryani, Pulka, Raitha, Green Salad`
**Tests:** near-perfect match to Simple Platter / Festive Feast
**Expected top 3:** Simple Platter / Festive Feast Meal Box / Basic Party *(TO CONFIRM)*
**Failure mode guarded:** top-1 should be close to 100% coverage

---

## Q15 — Minimal rice + curry
**Query:** `Jeera rice, Paneer Butter Masala, Butter Chicken Masala`
**Tests:** 3-item minimal menu; Jeera rice appears in only 1 platter
**Expected top 3:** Classic Rice and Curry Meal Box / Simple Platter *(TO CONFIRM)*
**Failure mode guarded:** exact-match on a singleton item must surface the right platter

---

## Q16 — Spec example (catalog-gap stress test)
**Query:** `Chicken Fried Drumsticks, Dal Makhani, Garlic Naan`
**Tests:** the original spec example — none of these items exist in DISCOUNTED platters
**Expected top 3:** very low coverage / near-empty / weak matches only
**Failure mode guarded:** system should NOT confidently return irrelevant platters when catalog doesn't cover the query; should surface "not available" cleanly

---

## Q17 — Sweets + breakfast
**Query:** `Gulab Jamun, Kesaribath, Badam Milk, Idly, Vada`
**Tests:** sweets rarely appear; breakfast items dominate
**Expected top 3:** Premium Breakfast Meal Box (partial match — breakfast only) *(TO CONFIRM)*
**Failure mode guarded:** partial-match behavior when half the menu is unobtainable

---

## Q18 — Regional Telugu festive
**Query:** `Pulihora, Nethi Bobatlu, Cut Mirchi Bajji, Palak Dal, Raitha`
**Tests:** festive/regional items (Nethi Bobatlu in only 6 platters)
**Expected top 3:** Grand Event / Mega Celebrations / Royal Feast *(TO CONFIRM)*
**Failure mode guarded:** deep catalog / festive platter routing

---

## Q19 — Negative / out-of-catalog
**Query:** `Sushi, Ramen, Pizza`
**Tests:** nothing should match
**Expected top 3:** `[]` (empty result, or all below 0.35 threshold)
**Failure mode guarded:** false-positive ranking — system must NOT return a random Indian platter for a Japanese query

---

## Q20 — Single item
**Query:** `Chicken Biryani`
**Tests:** absolute minimal query
**Expected top 3:** any platter containing Chicken Biryani (5 candidates) — Biryani & Pulav Bowl / Standard Taste of Traditions / Just Biryani *(TO CONFIRM)*
**Failure mode guarded:** minimal query must still rank sensibly; coverage ratio = 1/1 for any hit

---

# Failure Mode Coverage Matrix

| Failure mode | Queries |
|---|---|
| Exact match (sanity) | Q1, Q2, Q3, Q14, Q15 |
| Alias / VARIANT_OF resolution | Q8, Q9, Q10 |
| Rare / sparse-catalog items | Q4, Q5, Q12, Q15, Q18 |
| Regional (Telugu) routing | Q4, Q18 |
| Meal-type (breakfast) routing | Q3, Q17 |
| Veg ↔ non-veg mixing | Q6, Q14 |
| Casual / generic phrasing | Q11 |
| Similar-canonical disambiguation | Q10 (Butter Chicken vs Butter Chicken Masala) |
| Partial / catalog-gap match | Q16, Q17 |
| Negative case (should return nothing) | Q19 |
| Minimal query | Q20 |
| Large menu coverage | Q13, Q14 |

# Baseline Metrics (to be filled after first `eval.py` run)

| Metric | Target | Current |
|---|---|---|
| Recall@3 (query returns ≥1 expected platter in top 3) | ≥ 75% | TBD |
| MRR (mean reciprocal rank of first expected hit) | ≥ 0.6 | TBD |
| Per-query coverage ratio ≥ 0.5 | ≥ 15/20 | TBD |
| Negative-case correctness (Q19 returns 0) | 100% | TBD |
