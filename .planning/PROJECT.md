# SearchPOC — Item-Based Platter Search

## What This Is

A standalone search POC for CraftMyPlate's "Find My Platter" feature. Users type dish names (e.g. "Chicken Fried Pieces, Dal Makhani, Garlic Naan") and get back the top matching platters ranked by how many of their requested food communities are covered. Zero LLM at query time — embed → Qdrant → Neo4j → ranked results.

This is a self-contained system (separate Neo4j + Qdrant instances from the existing Elphie chatbot), purpose-built to validate the community-based platter ranking concept before integrating into the product.

## Core Value

A customer can type any dish name and get back the most relevant platters — even when the dish name doesn't exactly match what's in the platter catalog.

## Requirements

### Validated

- ✓ Core infrastructure scaffolded — `core/settings.py`, `core/connections.py` with singleton Neo4j + Qdrant connections
- ✓ ETL Step 1+2: Item nodes loaded from DynamoDB CSV (canonical, deduped by name) + Supabase CSV (aliases) — `scripts/load_items.py`
- ✓ ETL Step 3: LLM-generated VARIANT_OF edges (canonical → alias) via GPT-4o-mini — `scripts/generate_variants.py`
- ✓ ETL Step 4: Platter nodes + CONTAINS edges from live DynamoDB — `scripts/load_platters.py`
- ✓ ETL Step 5: Leiden community detection on VARIANT_OF graph → Community nodes + MEMBER_OF edges — `scripts/detect_communities.py`
- ✓ ETL Step 6: HAS_COMMUNITY edge pre-computation (Platter → Community) — `scripts/build_community_edges.py`
- ✓ ETL Step 7: LLM community narrative summaries — `scripts/generate_summaries.py`
- ✓ ETL Step 8: Community embeddings → Qdrant collection `item_search_communities` — `scripts/index_communities.py`
- ✓ Query-time search: embed → Qdrant → Neo4j ranked platters — `scripts/search.py`

### Active

- [ ] Neo4j AuraDB instance provisioned (cloud, separate from Elphie)
- [ ] `.env` configured with AuraDB connection string + other secrets
- [ ] ETL pipeline run end-to-end successfully (Steps 1–8 in sequence)
- [ ] Search validated against real data (3+ manual test queries return sensible results)
- [ ] POC result documented for stakeholder handoff

### Out of Scope

- Real-time DynamoDB change propagation — manual re-run sufficient for POC
- Web UI or REST API layer — CLI (`scripts/search.py`) is sufficient to validate the concept
- Integration with Elphie / existing chatbot graph — fully separate instance
- Production hardening (rate limiting, auth, monitoring) — POC only

## Context

CraftMyPlate offers platters (catering menus) composed of items (dishes). The existing "Find My Platter" screen has no reverse search — customers can't start from dish names and find matching platters. This POC validates the approach before building into the product.

**Technical approach:**
- Graph structure in Neo4j: Item → VARIANT_OF → Item (alias), Item → MEMBER_OF → Community, Platter → CONTAINS → Item, Platter → HAS_COMMUNITY → Community
- Community detection via Leiden algorithm on VARIANT_OF graph groups semantically-equivalent items (canonical + all regional aliases) into communities
- Qdrant stores community embeddings; cosine search finds relevant communities from a dish-name query
- Neo4j then counts HAS_COMMUNITY matches per platter → ranking

**Infrastructure:**
- Current on-prem: Community Edition Neo4j (bolt://neo4j.elphie.local:7687) — shared with Elphie, cannot add second database
- Target: Neo4j AuraDB Free Tier (cloud-managed, isolated instance)
- Qdrant: localhost or existing hosted instance; collection `item_search_communities`
- OpenAI: `text-embedding-3-small` (1536-dim) for embeddings; GPT-4o-mini for LLM steps

**Data:**
- ~260 canonical Item nodes (from DynamoDB CSV, deduped by name)
- ~700 Supabase alias Item nodes
- ~50–100 communities expected after Leiden detection
- Platters: unknown count (sourced live from `DefaultPlattersTable` DynamoDB)

## Constraints

- **Infrastructure**: Neo4j must be isolated from Elphie — Community Edition single-database limit means AuraDB or new ECS task required
- **Scope**: POC only — no production hardening, no UI, just CLI validation
- **Cost**: AuraDB Free Tier has 200k node limit — sufficient for POC scale

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Separate Neo4j instance from Elphie | Community Edition = 1 database; schema would conflict | — Pending (AuraDB chosen) |
| Zero LLM at query time | Latency requirement; all semantic work done offline in ETL | — Pending |
| Leiden community detection on VARIANT_OF graph | Groups canonical + all aliases together so any alias name finds the community | — Pending |
| text-embedding-3-small for communities | Good cost/quality ratio; 1536-dim; cosine similarity | — Pending |
| GPT-4o-mini for variant matching + summaries | Sufficient quality at low cost for offline batch work | — Pending |

---
*Last updated: 2026-04-10 after initialization*
