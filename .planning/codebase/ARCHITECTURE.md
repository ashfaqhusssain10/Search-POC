# Architecture

**Analysis Date:** 2026-04-10

## Pattern Overview

**Overall:** Multi-stage data pipeline with offline indexing (Neo4j + Qdrant) and zero-LLM-at-query-time search.

**Key Characteristics:**
- **Batch-first processing:** Data flows through sequential ETL scripts, not real-time APIs
- **Lazy singleton connections:** Neo4j and Qdrant clients initialized once per process
- **Embedding-driven vector search:** OpenAI text-embedding-3-small for semantic matching
- **Community-based ranking:** Item search is faceted through food communities detected via Leiden algorithm
- **LLM-enriched item data:** Gemini generates structured descriptions before loading; enrichment is cached and idempotent

## Layers

**Core Configuration Layer:**
- Purpose: Centralized environment loading and connection management
- Location: `core/settings.py`, `core/connections.py`
- Contains: Environment variable definitions, singleton Neo4j driver and Qdrant client factories
- Depends on: `python-dotenv`, `neo4j`, `qdrant-client`
- Used by: All scripts and the search service

**CSV Enrichment Layer:**
- Purpose: Pre-process raw CSV exports with LLM-generated structured descriptions before they are loaded into Neo4j
- Location: `scripts/enrich_items.py`
- Contains: Gemini batch API calls (50 items/call), idempotent per-row enrichment, in-place CSV overwrite
- Depends on: `google-genai`, `pandas`, `core.settings` (DYNAMODB_CSV, SUPABASE_CSV, GEMINI_API_KEY)
- Used by: Must run before `load_items.py`; output is an `llm_description` JSON column in both CSVs
- Cache: Writes raw Gemini responses to `llm_cache/enrichment/` (gitignored)

**Data Loading Layer:**
- Purpose: Populate Neo4j with canonical items, aliases, and platters from enriched CSVs
- Location: `scripts/load_items.py`, `scripts/load_platters.py`
- Contains: DynamoDB CSV parsing, Supabase alias loading, Neo4j batch writes including `llm_description` property
- Depends on: `core.connections`, CSV files (must be enriched), AWS (optional for DynamoDB scans)
- Used by: Initial data seeding, pre-indexing steps

**Variant Mapping Layer:**
- Purpose: Link canonical dishes to regional/alias variants using scored LLM matching
- Location: `scripts/generate_variants.py`
- Contains: Category normalization (`CATEGORY_NORMALIZE` map), scored batch LLM calls (score ≥ 0.8 threshold), Neo4j VARIANT_OF edge writes
- Depends on: `openai`, `core.connections`
- Used by: Community detection pipeline (reads VARIANT_OF graph)
- Cache: Writes per-category batch responses to `llm_cache/variants/` (gitignored)

**Community Detection Layer:**
- Purpose: Cluster semantically-related items using Leiden partitioning
- Location: `scripts/detect_communities.py`
- Contains: NetworkX graph from VARIANT_OF edges, hierarchical Leiden clustering, community node creation
- Depends on: `graspologic`, `networkx`, `core.connections`
- Used by: Summary generation and query-time search ranking

**Summary & Indexing Layer:**
- Purpose: Generate human-readable narratives and embed communities
- Location: `scripts/generate_summaries.py`, `scripts/index_communities.py`, `scripts/build_community_edges.py`
- Contains: LLM narrative generation, text embedding, Qdrant collection management, platter-to-community edges
- Depends on: `openai`, `qdrant-client`, `core.connections`
- Used by: Query-time search (Qdrant) and platter ranking (Neo4j HAS_COMMUNITY edges)

**Query Layer:**
- Purpose: Execute zero-LLM dish search at query time
- Location: `scripts/search.py`
- Contains: Query embedding, Qdrant community search, Neo4j platter ranking, result formatting
- Depends on: `openai` (embedding only), `core.connections`
- Used by: End users or application integrations

## Data Flow

**Initialization Pipeline:**

1. `enrich_items.py`: DynamoDB CSV + Supabase CSV → Gemini batch enrichment → CSVs overwritten with `llm_description` column
2. `load_items.py`: Enriched CSVs → Item nodes in Neo4j (including `llm_description` property)
3. `load_platters.py`: DynamoDB platter/item tables (via AWS) → Platter nodes + CONTAINS edges
4. `generate_variants.py`: Scored LLM matching (using `llm_description` context) → VARIANT_OF edges (score ≥ 0.8)
5. `detect_communities.py`: Leiden on VARIANT_OF graph → Community nodes + MEMBER_OF edges
6. `generate_summaries.py`: LLM narratives per community → Community.summary_json
7. `build_community_edges.py`: Platter → Item → Community traversal → HAS_COMMUNITY edges
8. `index_communities.py`: Community summaries embedded + upserted to Qdrant

**Query-Time Flow:**

```
User input (dish name query)
    ↓
embed_query() — OpenAI embedding
    ↓
find_communities() — Qdrant cosine search → top 10 communities
    ↓
rank_platters() — Neo4j: find platters with HAS_COMMUNITY to matched communities
    ↓
PlatterResult[] — sorted by coverage ratio (matched_communities / total_communities)
```

**State Management:**
- Neo4j is the single source of truth for graph (nodes: Item, Platter, Community; edges: VARIANT_OF, CONTAINS, MEMBER_OF, HAS_COMMUNITY)
- Qdrant is a derived index of community embeddings, rebuilt when Neo4j communities change
- CSV files are mutated by `enrich_items.py` (adds `llm_description` column) and then read by `load_items.py`
- `llm_cache/` holds raw LLM responses for debugging and avoiding re-calls on idempotent re-runs

## Key Abstractions

**Community:**
- Purpose: Groups semantically-equivalent items and dishes (aliases/variants)
- Examples: `comm_0`, `comm_1`, ... (auto-generated IDs)
- Pattern: Neo4j node with `id`, `member_count`, `summary_json` (LLM-generated narrative), optional community-level metadata
- Relationships: `(Item)-[:MEMBER_OF]->(Community)`, `(Platter)-[:HAS_COMMUNITY]->(Community)`

**Item:**
- Purpose: A canonical dish or its regional/alias variant, enriched with structured culinary metadata
- Examples: `core/connections.py` loads from two sources:
  - DynamoDB: `{"id": "uuid", "name": "Paneer Tikka", "itemCategory": "Starter", "source": "dynamodb"}`
  - Supabase: `{"id": "sub_xyz", "name": "Paneer Kebab", "source": "supabase"}`
- Properties include `llm_description`: JSON string with keys `ingredients`, `form`, `cooking_method`, `veg_type`, `regional_tags`, `also_known_as`
- Pattern: Linked via VARIANT_OF (alias → canonical) or directly as MEMBER_OF community
- Storage: Neo4j Item node with indexed `source` property for filtering

**LLM Description:**
- Purpose: Structured culinary metadata enabling accurate variant matching (prevents cross-category false positives)
- Schema: `{"ingredients": [...], "form": "rice-dish|gravy|...", "cooking_method": "...", "veg_type": "VEG|NONVEG|EGG", "regional_tags": [...], "also_known_as": [...]}`
- Generated by: `scripts/enrich_items.py` using Gemini (model: `gemini-2.5-flash`)
- Stored as: `llm_description` column in both CSVs; `llm_description` property on Neo4j Item nodes

**Platter:**
- Purpose: A catering menu option composed of items across categories
- Examples: "Wedding Deluxe", "Corporate Lunch Box"
- Pattern: Neo4j node with `id`, `name`, `type`, `mealType`, `veg`, pricing; connected to items via CONTAINS
- Relationships: `(Platter)-[:CONTAINS]->(Item)`, `(Platter)-[:HAS_COMMUNITY]->(Community)`

**PlatterResult:**
- Purpose: Query result ranking: how well a platter covers matched communities
- Pattern: Dataclass in `scripts/search.py` with `id`, `name`, `coverage_ratio` (matched / total communities), `matched_community_names`

## Entry Points

**Batch Script Entrypoints:**
- `python -m scripts.enrich_items` — Step 0: Gemini enrichment of both CSVs (run once before load_items)
- `python -m scripts.load_items` — Step 1: Populate Item nodes (DynamoDB + Supabase, with llm_description)
- `python -m scripts.load_platters` — Step 2: Populate Platter nodes + CONTAINS edges
- `python -m scripts.generate_variants` — Step 3: Scored LLM matching → VARIANT_OF edges
- `python -m scripts.detect_communities` — Leiden clustering on VARIANT_OF graph
- `python -m scripts.generate_summaries` — LLM: per-community narrative
- `python -m scripts.build_community_edges` — Pre-compute Platter → Community edges
- `python -m scripts.index_communities` — Embed + Qdrant upsert

**Query Entrypoints:**
- `python -m scripts.search` — Interactive CLI search
- `search_platters(query: str) → list[PlatterResult]` — Programmatic API in `scripts/search.py`

## Error Handling

**Strategy:** Fail-fast with logging; retries only for transient API errors (Gemini, OpenAI, Qdrant).

**Patterns:**
- **Gemini API calls** (`scripts/enrich_items.py`): Retry up to 3 times with 5-second delay; on total failure writes empty `{}` per item (script continues rather than aborting)
- **OpenAI API calls** (`scripts/generate_variants.py`, `scripts/generate_summaries.py`, `scripts/index_communities.py`): Retry up to 3 times with 2-second delay on rate-limits or transient errors
- **Neo4j operations** (`scripts/load_items.py`, etc.): No explicit retry; assume Neo4j is always available (local docker or production cluster)
- **CSV parsing** (`scripts/load_items.py`): Safe float conversion, null handling, category normalization; missing files cause `sys.exit(1)`; `enrich_items.py` raises `FileNotFoundError` on missing CSVs
- **Search at query time** (`scripts/search.py`): Return empty list if Qdrant returns no results above threshold; log community misses for debugging

## Cross-Cutting Concerns

**Logging:** Standard Python logging to stdout
- Level: `INFO` for progress, `WARNING` for skipped items, `ERROR` for file not found
- Format: `"%(levelname)s %(message)s"`
- Usage: Every script logs row counts, batch progress, verification results

**Validation:**
- **Input validation:** CSV column presence checked implicitly (KeyError on missing); safe float parsing for prices
- **Schema validation:** Neo4j constraints ensure uniqueness (`Item.id`, `Community.id`, `Platter.id`)
- **Output validation:** Qdrant search results checked for `score_threshold` (0.35 default); variant matching gated at score ≥ 0.8

**Authentication:**
- Neo4j: Credentials from `NEO4J_USER`, `NEO4J_PASSWORD` env vars
- Qdrant: Optional `QDRANT_API_KEY` for cloud instances
- OpenAI: `OPENAI_API_KEY` required
- Gemini: `GEMINI_API_KEY` required (used by `enrich_items.py`)
- AWS: Uses `~/.aws/credentials` by default; fallback to `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` env vars

---

*Architecture analysis: 2026-04-10*
