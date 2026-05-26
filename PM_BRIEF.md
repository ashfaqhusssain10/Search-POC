# SearchPOC — Pre-Production Decisions

**Audience:** PM + eng + ops
**Time:** 60–90 min meeting
**Goal:** Decide answers to the questions below. Write them down. They become the spec the system is held to.

The system has been built as a POC over several sessions of iteration. Before we
ship it to production, we need answers we don't currently have. Without them,
every future change is faith-based — we can't say if the system is "good enough"
because we haven't agreed what "good enough" means.

---

## Part 1 — Who, what, when (product framing)

### Q1. Who is the user, and what are they trying to do?

Options to pick from (or define your own):

- **(a) Caterer placing a wedding order** — high stakes, expects exact items, won't accept silent substitutes
- **(b) Office-meal planner** — wants "a meal that includes biryani and roti," substitutes are fine if labeled
- **(c) Casual user browsing platters** — exploratory, generous matches feel helpful
- **(d) Internal ops tool** — sales/support staff finding matches for customer requests

The answer drives everything else. Don't pick more than one.

**Decision:** _________________________________________

### Q2. Recommender or menu builder?

- **Recommender:** "Here are platters that *kind of* match — close substitutes encouraged."
- **Menu builder:** "Here are platters that *contain* your selection. ❌ when they don't."

This is the philosophical fork. v5 (current default) is closer to a menu builder. v6 (LLM judge) is a recommender. **You can't be both** — choose one and tune for it.

**Decision:** _________________________________________

### Q3. What's the acceptable failure mode?

Pick the one that hurts your user least:

- **(a) Show ❌ when not exact** — user trusts the system but sees fewer results
- **(b) Show closest substitute always** — user sees more results but some are wrong
- **(c) Show substitute only with reason text** — slower (LLM call), but each substitute is explained

**Decision:** _________________________________________

---

## Part 2 — Quality bar (the numbers we have to hit)

### Q4. What does "right" mean for a query?

Define what counts as a correct top-1 answer. Examples:

- "User picks `Rumali Roti` → top-1 must be a flatbread or null"
- "User picks `Aloo Baingan Curry` → top-1 must be a veg gravy of Indian cuisine"
- "User picks `Tres Leches` → top-1 may be null (we don't sell it)"

We need ~10 example "rights" and ~10 example "wrongs" written down so the eval set has a north star.

**Decision:** _________________________________________

### Q5. What's the minimum recall@5 / precision@1 we have to hit?

| Metric | Means | Suggested target | Your call |
|---|---|---|---|
| Recall@5 | The right answer is in our top-5 X% of the time | ≥ 90% | __% |
| Precision@1 | Our top-1 is correct X% of the time | ≥ 75% | __% |
| No-result rate | We return zero matches X% of the time | ≤ 5% | __% |

Pick numbers you'd stake the launch on. We'll measure against them.

**Decision:** _________________________________________

### Q6. What's an acceptable substitute?

PM eyeballs these. Mark each ✓ acceptable or ✗ not:

| Query | Top-1 from system | Score | Acceptable? |
|---|---|---|---|
| Rumali Roti | Butter Naan | 0.52 | ✓ / ✗ |
| Rumali Roti | Pulka | 0.57 | ✓ / ✗ |
| Aloo Baingan Curry | Paneer Butter Masala | 0.62 | ✓ / ✗ |
| Aloo Baingan Curry | Veg Kurma | 0.63 | ✓ / ✗ |
| Tres Leches | Gulab Jamun | 0.44 | ✓ / ✗ |
| Mojito | Fruit Punch | 0.57 | ✓ / ✗ |
| Marinated Chicken | Chicken Malai Tikka | 0.74 | ✓ / ✗ |
| Prawn Biryani | Chicken Biryani | 0.78 | ✓ / ✗ |
| Carrot Juice | Fruit Punch | 0.61 | ✓ / ✗ |
| Pav Bhaji | Vada Pav | 0.76 | ✓ / ✗ |

This calibrates the thresholds for us.

---

## Part 3 — Latency, cost, freshness (non-functional)

### Q7. How fast must search be?

Real numbers:

- v5 (no LLM): ~200–400ms p50
- v6 (LLM judge): ~1.5–3s p50, depends on Bedrock latency

| Latency budget | Implication |
|---|---|
| < 500ms | v5 only, no LLM |
| < 2s | v5 default, v6 optional |
| < 5s | v6 default-on |

**Decision:** _________________________________________

### Q8. What's the per-query cost budget?

- v5 (current): ~$0.00002 (OpenAI embed call for query + Qdrant)
- v6 LLM judge: ~$0.0005–$0.002 (Bedrock Haiku)

Multiply by expected daily query volume. What's the monthly ceiling?

**Decision:** _________________________________________

### Q9. How often does the catalog change?

- If ops adds a dish, how soon must it appear in search?
  - **Within minutes** → keep live DDB cache, refresh per-request or every N min
  - **Daily** → scheduled refresh
  - **Weekly** → manual trigger, snapshot to JSON

Affects whether Render needs AWS credentials at runtime or not.

**Decision:** _________________________________________

---

## Part 4 — Catalog scope (this is ops, not eng)

### Q10. What's the canonical catalog?

We currently load **246 unique-by-name items** from the CSV. The platter database
references ~1,004 distinct itemIds. There's a gap of ~758 items the platters
expect that aren't in the canonical menu.

- **(a)** The 246 CSV is correct — anything outside it is legacy/test and platters that reference them are broken
- **(b)** The 246 is incomplete — we need to expand the canonical menu to cover what platters actually serve
- **(c)** Two-tier: 246 is the "promotable" menu; the rest are dark items only usable inside platters

Pick one. This determines what `load_items.py` should read.

**Decision:** _________________________________________

### Q11. Which items genuinely have no peer in our menu?

Hand the **ADD_CANONICAL list (40 items)** from `diagnostics/alias_coverage_report.csv` to ops. For each:

- ✓ Add this canonical to the menu
- ✗ We don't serve this and won't — search should return null

Examples on the list:
- Tres Leches, Tiramisu, Mojito, Pav Bhaji, Pizza variants, Croissant, Nutella

Ops decision per row. Eng can't make this call.

**Decision:** _________________________________________

---

## Part 5 — Architecture commitments

### Q12. Which search version is production?

We currently have v4 (item-to-item only), v5 (platter ranking, no LLM), v6 (platter
ranking + LLM judge). Pick **one** as the production default. The others either
get deleted or kept behind a feature flag with a clear "when to use" doc.

- **v5 only** — fast, deterministic, no AI dependency for retrieval beyond embedding
- **v6 only** — better quality on partial-match queries, slower, depends on Bedrock
- **v5 default + v6 toggle** — hedge, but doubles the surface area to maintain

**Decision:** _________________________________________

### Q13. Embedding model — commit or experiment?

OpenAI `text-embedding-3-small` is the current production model. Alternatives
considered: BGE-M3, Voyage-3-large, fine-tuned `all-mpnet-base-v2`.

- **(a)** Stick with text-embedding-3-small — no migration cost, known behavior
- **(b)** Fine-tune our own model (best long-term, 1–2 weeks)
- **(c)** Switch to BGE-M3 (open weights, free, comparable quality)

**Decision:** _________________________________________

### Q14. Do we keep Neo4j?

After the DDB migration of platter retrieval, Neo4j is only used by the ETL
pipeline for items. We could move that to a JSON or DDB-backed snapshot and
retire Neo4j entirely.

- **(a)** Drop Neo4j — saves the Aura subscription, fewer moving parts
- **(b)** Keep Neo4j — useful if we ever add cross-item relationships (variants, substitutes, cuisine bridges)

**Decision:** _________________________________________

---

## Part 6 — Observability and ops

### Q15. Where do logs and metrics go?

- Logs: Render's default streaming, or send to Datadog / Loki / similar?
- Errors: Sentry, or print and hope?
- Metrics: nothing today. Add Prometheus, or accept blind?

**Decision:** _________________________________________

### Q16. Who owns this in production?

- Who gets paged when no-result rate spikes 3x?
- Who decides when to retrain embeddings?
- Who maintains the eval set?
- Who owns the catalog gaps list (ops backlog)?

**Decision:** _________________________________________

---

## What we're delivering coming out of this meeting

A checklist that becomes the eng spec:

- [ ] User persona (Q1)
- [ ] Recommender vs builder (Q2)
- [ ] Failure-mode default (Q3)
- [ ] 10 example "rights" + 10 example "wrongs" (Q4)
- [ ] Recall / precision targets (Q5)
- [ ] Substitute calibration table filled (Q6)
- [ ] Latency budget (Q7)
- [ ] Cost ceiling (Q8)
- [ ] Catalog freshness SLA (Q9)
- [ ] Canonical catalog definition (Q10)
- [ ] ADD_CANONICAL list reviewed by ops (Q11)
- [ ] Production search version (Q12)
- [ ] Embedding model commitment (Q13)
- [ ] Neo4j keep/drop (Q14)
- [ ] Logging/error stack (Q15)
- [ ] On-call ownership (Q16)

When this checklist is signed off, eng builds against it. Until then, every
change is guesswork.

---

## Suggested meeting flow

1. **Read the brief silently** (5 min) — everyone arrives with the same context
2. **Part 1 — Product framing** (15 min) — biggest decisions, do these first
3. **Part 2 — Quality bar** (20 min) — PM walks through Q6 with eng explaining what the system does today
4. **Part 3 — Non-functional** (10 min) — usually fast once Q1 is decided
5. **Part 4 — Catalog scope** (15 min) — ops takes the lead; eng + PM listen
6. **Part 5 — Architecture** (10 min) — eng's call, others ratify
7. **Part 6 — Ops** (10 min) — short, but important
8. **Wrap-up** (5 min) — confirm checklist signoff date

If the meeting runs over, split: Parts 1–2 in one session, the rest in another.
Parts 1–2 are non-negotiable; the rest can be deferred a week.
