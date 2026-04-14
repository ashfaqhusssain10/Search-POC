# Architecture

**Analysis Date:** 2026-04-14

## Pattern Overview

**Overall:** Multi-stage data pipeline with offline indexing (Neo4j + Qdrant) and zero-LLM-at-query-time search.

**Key Characteristics:**
- **Batch-first processing:** Data flows through sequential ETL scripts, not real-time APIs
- **Lazy singleton connections:** Neo4j and Qdrant clients initialized once per process
- **Embedding-driven vector search:** OpenAI text-embedding-3-small for semantic matching
- **Community-based ranking:** Item search is faceted through food communities detected via Leiden algorithm
- **LLM-enriched item data:** Gemini generates structured descriptions before loading; enrichment is cached and idempotent
- **Hybrid edge graph for clustering:** Leiden runs on a weighted union of LLM-grounded VARIANT_OF edges and vector-geometric BRIDGE_TO edges

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

**Canonical Bridging Layer:**
- Purpose: Inject canonical↔canonical similarity edges so Leiden can merge "island" canonicals that share no aliases
- Location: `scripts/add_canonical_bridges.py`
- Contains: Per-canonical Qdrant queries against `searchpoc_canonicals` collection, hard `veg_type`+`form` payload filter, TOP_K=3 neighbors with cosine ≥ 0.80, score-gap cutoff of 0.05 below the top match, bidirectional `BRIDGE_TO` MERGE
- Semantics: BRIDGE_TO is "customer-alternative similarity" (loose), NOT strict variant equivalence — cross-ingredient bridges within the same `veg_type+form` are acceptable (see `memory/feedback_bridge_semantics.md`)
- Idempotency: Deletes all existing BRIDGE_TO edges before writing (full replace); supports `--commit` flag with dry-run default
- Depends on: `core.connections`, `qdrant-client`, populated `searchpoc_canonicals` Qdrant collection
- Used by: `detect_communities.py` (reads BRIDGE_TO alongside VARIANT_OF)
- Artifact: Dry-run plan written to `llm_cache/dry_run_bridges.json`

**Community Detection Layer:**
- Purpose: Cluster semantically-related items using weighted Leiden partitioning
- Location: `scripts/detect_communities.py`
- Contains: NetworkX graph loaded from VARIANT_OF (weight 1.0) + BRIDGE_TO (weight 0.5) into a single weighted graph, hierarchical Leiden clustering, community node creation
- Edge weighting: VARIANT_OF carries human-grounded LLM evidence and gets full weight; BRIDGE_TO is vector-geometric and gets half weight; if both exist between the same pair, the BRIDGE_TO weight never downgrades the existing VARIANT_OF weight (max-merge semantics)
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
5. `add_canonical_bridges.py` (Step 3b): Qdrant cosine search on canonicals with veg_type+form filter → BRIDGE_TO edges (cosine ≥ 0.80, TOP_K=3, score-gap 0.05, bidirectional MERGE)
6. `detect_communities.py`: Weighted Leiden on VARIANT_OF (1.0) ∪ BRIDGE_TO (0.5) → Community nodes + MEMBER_OF edges
7. `generate_summaries.py`: LLM narratives per community → Community.summary_json
8. `build_community_edges.py`: Platter → Item → Community traversal → HAS_COMMUNITY edges
9. `index_communities.py`: Community summaries embedded + upserted to Qdrant

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
- Neo4j is the single source of truth for graph (nodes: Item, Platter, Community; edges: VARIANT_OF, BRIDGE_TO, CONTAINS, MEMBER_OF, HAS_COMMUNITY)
- Qdrant holds two derived collections: community summary embeddings (query-time) and `searchpoc_canonicals` canonical-item embeddings (consumed offline by `add_canonical_bridges.py`)
- CSV files are mutated by `enrich_items.py` (adds `llm_description` column) and then read by `load_items.py`
- `llm_cache/` holds raw LLM responses for debugging and idempotent re-runs; also stores `dry_run_bridges.json` from `add_canonical_bridges.py`

**Current graph snapshot (post Step 3b):** 639 nodes · 587 VARIANT_OF · 310 BRIDGE_TO · 132 Leiden communities · 13.6% singletons (down from ~90% before bridges).

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
- Pattern: Linked via VARIANT_OF (alias → canonical), BRIDGE_TO (canonical ↔ canonical similarity), or directly as MEMBER_OF community
- Storage: Neo4j Item node with indexed `source` property for filtering

**LLM Description:**
- Purpose: Structured culinary metadata enabling accurate variant matching (prevents cross-category false positives)
- Schema: `{"ingredients": [...], "form": "rice-dish|gravy|...", "cooking_method": "...", "veg_type": "VEG|NONVEG|EGG", "regional_tags": [...], "also_known_as": [...]}`
- Generated by: `scripts/enrich_items.py` using Gemini (model: `gemini-2.5-flash`)
- Stored as: `llm_description` column in both CSVs; `llm_description` property on Neo4j Item nodes
- Bridge filter use: `veg_type` and `form` are also pushed into the Qdrant `searchpoc_canonicals` payload and used as a hard filter when computing BRIDGE_TO neighbors

**BRIDGE_TO Edge:**
- Purpose: Canonical↔canonical loose-similarity edges that give Leiden signal to merge "island" canonicals sharing no aliases
- Scope: DynamoDB items only (`source='dynamodb'` on both endpoints)
- Property: `score` (cosine similarity from Qdrant)
- Direction: Bidirectional MERGE — both `(a)-[:BRIDGE_TO]->(b)` and `(b)-[:BRIDGE_TO]->(a)` written so Leiden treats them symmetrically without undirected projection
- Semantics: "Customer-alternative similarity" — looser than VARIANT_OF; cross-ingredient bridges within a `veg_type+form` cell are acceptable and intentional
- Leiden weight: 0.5 (VARIANT_OF dominates wherever it exists)

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
- `python -m scripts.add_canonical_bridges [--commit]` — Step 3b: Qdrant-based BRIDGE_TO edges (dry-run by default)
- `python -m scripts.detect_communities` — Weighted Leiden clustering on VARIANT_OF + BRIDGE_TO
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
- **Qdrant queries** (`scripts/add_canonical_bridges.py`): Logs a warning if Qdrant canonical count != Neo4j canonical count (stale embeddings produce stale bridges); dry-run mode is the default to avoid silently mutating the graph
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
- **Output validation:** Qdrant search results checked for `score_threshold` (0.35 default for community search; 0.80 for canonical bridge similarity); variant matching gated at score ≥ 0.8

**Authentication:**
- Neo4j: Credentials from `NEO4J_USER`, `NEO4J_PASSWORD` env vars
- Qdrant: Optional `QDRANT_API_KEY` for cloud instances
- OpenAI: `OPENAI_API_KEY` required
- Gemini: `GEMINI_API_KEY` required (used by `enrich_items.py`)
- AWS: Uses `~/.aws/credentials` by default; fallback to `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` env vars

---

*Architecture analysis: 2026-04-14*
