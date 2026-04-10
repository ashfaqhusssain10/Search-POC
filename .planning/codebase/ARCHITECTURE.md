# Architecture

**Analysis Date:** 2026-04-10

## Pattern Overview

**Overall:** Multi-stage data pipeline with offline indexing (Neo4j + Qdrant) and zero-LLM-at-query-time search.

**Key Characteristics:**
- **Batch-first processing:** Data flows through sequential ETL scripts, not real-time APIs
- **Lazy singleton connections:** Neo4j and Qdrant clients initialized once per process
- **Embedding-driven vector search:** OpenAI text-embedding-3-small for semantic matching
- **Community-based ranking:** Item search is faceted through food communities detected via Leiden algorithm

## Layers

**Core Configuration Layer:**
- Purpose: Centralized environment loading and connection management
- Location: `core/settings.py`, `core/connections.py`
- Contains: Environment variable definitions, singleton Neo4j driver and Qdrant client factories
- Depends on: `python-dotenv`, `neo4j`, `qdrant-client`
- Used by: All scripts and the search service

**Data Loading & Enrichment Layer:**
- Purpose: Populate Neo4j with canonical items, aliases, and platters from external sources
- Location: `scripts/load_items.py`, `scripts/load_platters.py`
- Contains: DynamoDB CSV parsing, Supabase alias loading, Neo4j batch writes
- Depends on: `core.connections`, CSV files, AWS (optional for DynamoDB scans)
- Used by: Initial data seeding, pre-indexing steps

**Variant Mapping Layer:**
- Purpose: Link canonical dishes to regional/alias variants using LLM
- Location: `scripts/generate_variants.py`
- Contains: Category-based grouping, OpenAI batch LLM calls for variant detection, Neo4j VARIANT_OF edge writes
- Depends on: `openai`, `core.connections`
- Used by: Community detection pipeline (reads VARIANT_OF graph)

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

1. `load_items.py`: DynamoDB CSV + Supabase CSV → Item nodes in Neo4j
2. `load_platters.py`: DynamoDB platter/item tables (via AWS) → Platter nodes + CONTAINS edges
3. `generate_variants.py`: LLM-matches canonical items to aliases → VARIANT_OF edges
4. `detect_communities.py`: Leiden on VARIANT_OF graph → Community nodes + MEMBER_OF edges
5. `generate_summaries.py`: LLM narratives per community → Community.summary_json
6. `build_community_edges.py`: Platter → Item → Community traversal → HAS_COMMUNITY edges
7. `index_communities.py`: Community summaries embedded + upserted to Qdrant

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
- CSV files are input-only (not modified after initial load)

## Key Abstractions

**Community:**
- Purpose: Groups semantically-equivalent items and dishes (aliases/variants)
- Examples: `comm_0`, `comm_1`, ... (auto-generated IDs)
- Pattern: Neo4j node with `id`, `member_count`, `summary_json` (LLM-generated narrative), optional community-level metadata
- Relationships: `(Item)-[:MEMBER_OF]->(Community)`, `(Platter)-[:HAS_COMMUNITY]->(Community)`

**Item:**
- Purpose: A canonical dish or its regional/alias variant
- Examples: `core/connections.py` loads from two sources:
  - DynamoDB: `{"id": "uuid", "name": "Paneer Tikka", "itemCategory": "Starter", "source": "dynamodb"}`
  - Supabase: `{"id": "sub_xyz", "name": "Paneer Kebab", "source": "supabase"}`
- Pattern: Linked via VARIANT_OF (alias → canonical) or directly as MEMBER_OF community
- Storage: Neo4j Item node with indexed `source` property for filtering

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
- `python -m scripts.load_items` — Populate Item nodes (DynamoDB + Supabase)
- `python -m scripts.load_platters` — Populate Platter nodes + CONTAINS edges
- `python -m scripts.generate_variants` — LLM: canonical → aliases (VARIANT_OF edges)
- `python -m scripts.detect_communities` — Leiden clustering on VARIANT_OF graph
- `python -m scripts.generate_summaries` — LLM: per-community narrative
- `python -m scripts.build_community_edges` — Pre-compute Platter → Community edges
- `python -m scripts.index_communities` — Embed + Qdrant upsert

**Query Entrypoints:**
- `python -m scripts.search` — Interactive CLI search
- `search_platters(query: str) → list[PlatterResult]` — Programmatic API in `scripts/search.py`

## Error Handling

**Strategy:** Fail-fast with logging; retries only for transient API errors (OpenAI, Qdrant).

**Patterns:**
- **OpenAI API calls** (`scripts/generate_variants.py`, `scripts/generate_summaries.py`, `scripts/index_communities.py`): Retry up to 3 times with 2-second delay on rate-limits or transient errors
- **Neo4j operations** (`scripts/load_items.py`, etc.): No explicit retry; assume Neo4j is always available (local docker or production cluster)
- **CSV parsing** (`scripts/load_items.py`): Safe float conversion, null handling, category normalization; missing files cause `sys.exit(1)`
- **Search at query time** (`scripts/search.py`): Return empty list if Qdrant returns no results above threshold; log community misses for debugging

## Cross-Cutting Concerns

**Logging:** Standard Python logging to stdout
- Level: `INFO` for progress, `WARNING` for skipped items, `ERROR` for file not found
- Format: `"%(levelname)s %(message)s"`
- Usage: Every script logs row counts, batch progress, verification results

**Validation:** 
- **Input validation:** CSV column presence checked implicitly (KeyError on missing); safe float parsing for prices
- **Schema validation:** Neo4j constraints ensure uniqueness (`Item.id`, `Community.id`, `Platter.id`)
- **Output validation:** Qdrant search results checked for `score_threshold` (0.35 default)

**Authentication:**
- Neo4j: Credentials from `NEO4J_USER`, `NEO4J_PASSWORD` env vars
- Qdrant: Optional `QDRANT_API_KEY` for cloud instances
- OpenAI: `OPENAI_API_KEY` required
- AWS: Uses `~/.aws/credentials` by default; fallback to `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` env vars

---

*Architecture analysis: 2026-04-10*
