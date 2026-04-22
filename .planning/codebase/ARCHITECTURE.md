# Architecture

**Analysis Date:** 2026-04-22

## Pattern Overview

**Overall:** ETL pipeline → Knowledge graph + vector index → Query-time retrieval with zero LLM inference

**Key Characteristics:**
- All expensive computation (LLM calls, graph analysis) happens offline in numbered pipeline steps
- Query time is purely: embed → vector lookup → graph traversal → rank; no LLM, no external AI at runtime
- Neo4j is the source of truth for graph relationships; Qdrant is the semantic lookup index
- Community abstraction bridges raw dish names to platter membership

## Layers

**Configuration / Connections (`core/`):**
- Purpose: Singleton clients and environment settings shared by all scripts and the app
- Location: `core/settings.py`, `core/connections.py`, `core/categories.py`
- Contains: Env var loading, Neo4j driver singleton, Qdrant client singleton, category normalization helpers
- Depends on: `.env` file, `neo4j`, `qdrant_client`, `dotenv`
- Used by: Every script and `app.py`

**ETL Pipeline (`scripts/`):**
- Purpose: Offline data transformation from raw CSVs through graph construction to Qdrant indexing
- Location: `scripts/` — 8 numbered pipeline scripts plus diagnostic utilities
- Contains: One Python module per pipeline step, each runnable standalone via `python -m scripts.<name>`
- Depends on: `core/connections.py`, `core/settings.py`, external APIs (OpenAI, Gemini, DynamoDB)
- Used by: Manually — run in order to build/rebuild the graph

**Query Engine (`scripts/search.py`):**
- Purpose: Public search API consumed by the Streamlit UI
- Location: `scripts/search.py`
- Contains: `search_platters(query: str) -> list[PlatterResult]`, `PlatterResult` dataclass, all ranking helpers
- Depends on: `core/connections.py`, `core/settings.py`, `core/categories.py`, OpenAI embeddings, Qdrant, Neo4j
- Used by: `app.py`

**UI Layer (`app.py`):**
- Purpose: Streamlit web interface — user input and result rendering
- Location: `app.py`
- Contains: Single-page Streamlit app; no business logic
- Depends on: `scripts/search.py`, `core/connections.py`
- Used by: End users via `streamlit run app.py`

## Data Flow

**ETL Pipeline (run once / re-run to rebuild):**

1. `scripts/enrich_items.py` — CSV items → LLM-enriched descriptions cached in `llm_cache/enrichment/`
2. `scripts/load_items.py` — enriched CSVs → Neo4j `Item` nodes (source: 'dynamodb' = canonical, source: 'supabase' = aliases)
3. `scripts/generate_variants.py` — pairwise LLM scoring → `VARIANT_OF` edges (score ≥0.8, cached in `llm_cache/variants/`)
4. `scripts/load_platters.py` — platter CSV → Neo4j `Platter` nodes + `CONTAINS` edges to `Item` nodes
5. `scripts/detect_communities.py` — reads `VARIANT_OF` + `BRIDGE_TO` edges → runs Leiden (graspologic) → writes `Community` nodes + `MEMBER_OF` edges
6. `scripts/build_community_edges.py` — pre-computes `HAS_COMMUNITY` edges: `Platter → Community`
7. `scripts/generate_summaries.py` — per community: Gemini narrative for multi-item; `llm_description` field for singletons → stores `summary_json` on Community node
8. `scripts/index_communities.py` — reads `summary_json` → OpenAI embeddings → upserts to Qdrant collection `item_search_communities`

**Query Time (zero LLM):**

1. User selects dish names → `search_platters(query)` in `scripts/search.py`
2. All query items embedded in one batch call to OpenAI (`embed_items`)
3. Per-item Qdrant top-1 lookup → one `community_id` per item (`find_best_community`)
4. Neo4j: fetch community→category mappings, fetch all platters with `HAS_COMMUNITY` + `CONTAINS` metadata (`fetch_all_platters`)
5. `compute_coverage_from_seeds` — O(platters × items) set-intersection, no additional Qdrant calls
6. `sort_platters_by_final_score` — weighted sum: 70% community coverage + 30% category skeleton match
7. Substitute suggestion for unmatched items: Pass 1 = vector search within same-family platter communities; Pass 2 = `VARIANT_OF` graph traversal 1–2 hops
8. Return top `TOP_N_RESULTS=3` `PlatterResult` objects

**State Management:**
- No in-memory app state beyond `@st.cache_data` on canonical item list load in `app.py`
- Neo4j and Qdrant clients are module-level singletons in `core/connections.py`

## Key Abstractions

**Community:**
- Purpose: Groups semantically equivalent dishes (same dish, different regional names/spellings) into one searchable unit
- Node label: `Community` in Neo4j; indexed as a point in Qdrant `item_search_communities`
- Payload stored in Neo4j: `summary_json` (narrative, variant_names, hub_items)
- Qdrant payload: `community_id`, `name`, `members`, `variant_names`

**PlatterResult:**
- Purpose: Rich result object carrying match metadata per query item, per platter
- Location: `scripts/search.py` (dataclass)
- Key fields: `coverage_ratio`, `skeleton_coverage_score`, `final_score`, `item_to_community`, `suggested_alternatives`, `community_summaries`

**Category Family:**
- Purpose: Normalized meal-course taxonomy used for skeleton matching (query structure vs platter structure)
- Location: `core/categories.py` — `CATEGORY_FAMILY_MAP`, `category_family()`, `build_category_family_counts()`
- Canonical families: Curry, Dal, Rice, Biryani, Bread, Starter, Snack, Fry, Side, Accompaniment, Dessert, Fruit, Salad, Soup, Beverage, Pasta

## Entry Points

**Streamlit UI:**
- Location: `app.py`
- Triggers: `streamlit run app.py`
- Responsibilities: Load canonical item list, collect user dish selection, call `search_platters`, render `PlatterResult` list

**Search API:**
- Location: `scripts/search.py` — `search_platters(query: str) -> list[PlatterResult]`
- Triggers: Called from `app.py`; also has `main()` CLI for interactive terminal use (`python -m scripts.search`)
- Responsibilities: Full query pipeline from raw text to ranked results

**ETL Scripts:**
- Location: `scripts/` — each script has `main()` and `if __name__ == "__main__"` guard
- Triggers: `python -m scripts.<name>` (manually, in pipeline order 1–8)

## Error Handling

**Strategy:** Logging + explicit exceptions; no broad `except Exception` without logging context.

**Patterns:**
- LLM API failures in ETL scripts: `MAX_RETRIES` + `RETRY_DELAY` sleep loops with per-attempt logging
- Missing community data: `None` propagated into `PlatterResult` fields; UI renders graceful fallbacks (✅ / ⚠️ / ❌)
- Qdrant below threshold: `find_best_community` returns `None`; item gets `seed_item_to_community_id[item] = None`

## Cross-Cutting Concerns

**Logging:** `logging.basicConfig(level=logging.INFO)` per script; `log = logging.getLogger(__name__)` pattern throughout
**Validation:** External data (CSV rows, LLM JSON responses) validated at script boundaries; internal dataclass fields trusted
**Authentication:** All credentials loaded from `.env` via `core/settings.py`; no credentials in code

## Neo4j Schema (Node Labels and Relationships)

**Node Labels:**
- `Item` — dish/menu item; properties: `id`, `name`, `source` ('dynamodb'|'supabase'), `itemCategory`, `llm_description`
- `Community` — Leiden cluster; properties: `id` (e.g. 'comm_42'), `name`, `member_count`, `summary_json`
- `Platter` — catering package; properties: `id`, `name`, `type`, `mealType`, `veg`, `minPrice`, `maxPrice`
- `PlatterCategory` — category slot within a platter; properties: `category_name_raw`, `category_name_normalized`, `category_family`

**Relationships:**
- `(Item)-[:VARIANT_OF]->(Item)` — alias→canonical; weight 1.0 for Leiden
- `(Item)-[:BRIDGE_TO]->(Item)` — canonical↔canonical vector similarity; weight 0.5 for Leiden
- `(Item)-[:MEMBER_OF]->(Community)` — item to its Leiden community
- `(Platter)-[:CONTAINS]->(Item)` — platter menu composition
- `(Platter)-[:HAS_COMMUNITY]->(Community)` — pre-computed; built by `build_community_edges.py`
- `(Platter)-[:HAS_CATEGORY]->(PlatterCategory)` — platter's category slots
- `(PlatterCategory)-[:CONTAINS_ITEM]->(Item)` — items within a category slot

---

*Architecture analysis: 2026-04-22*
