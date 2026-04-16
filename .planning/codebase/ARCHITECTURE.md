# Architecture

**Analysis Date:** 2026-04-15

## Pattern Overview

**Overall:** Multi-stage ETL pipeline feeding a zero-LLM query-time vector + graph search system.

**Key Characteristics:**
- Offline semantic work (community detection, embeddings, LLM summaries) decoupled from the query path
- Community-based ranking: users search by dish names; system maps dishes to communities, then ranks platters by coverage
- Three-layer stack: ETL (Neo4j + Qdrant), query-time logic (vectorization → community lookup → ranking), and UI (Streamlit)
- Immutable semantic artifacts: communities and embeddings are precomputed offline, never changed at query time

## Layers

**Data Ingestion & Storage**
- Purpose: Load canonical items (DynamoDB CSV), aliases (Supabase CSV), and platters into Neo4j
- Location: `scripts/load_items.py`, `scripts/load_platters.py`
- Depends on: `core/connections.py`, `core/settings.py`, `core/categories.py`

**Semantic Enrichment & Graph Construction**
- Purpose: Generate LLM-backed semantic edges (`VARIANT_OF`, `BRIDGE_TO`) and cluster items into communities
- Location: `scripts/generate_variants.py`, `scripts/add_canonical_bridges.py`, `scripts/detect_communities.py`
- Uses: Gemini for variant scoring, Qdrant for similarity lookups, graspologic Leiden

**Community Summarization & Embedding**
- Purpose: Generate human-readable narratives per community and embed them for retrieval
- Location: `scripts/generate_summaries.py`, `scripts/index_communities.py`
- Uses: Gemini for narratives, OpenAI `text-embedding-3-small` (1536-dim)

**Platter Linkage**
- Purpose: Pre-compute `HAS_COMMUNITY` edges so query time is pure graph traversal
- Location: `scripts/build_community_edges.py`

**Query-Time Search**
- Purpose: Convert user input (dish names) to ranked platter results with zero LLM calls
- Location: `scripts/search.py`
- Substeps: batch embed user items → per-item Qdrant top-1 → Neo4j ranking → fallback alternatives

**UI Layer**
- Purpose: Streamlit web interface for interactive search
- Location: `app.py`
- Uses: `scripts/search.py` for results, Neo4j for canonical item list

## Data Flow

**ETL Pipeline (run sequentially):**

1. `scripts/enrich_items.py` — Gemini enriches canonical items → cached CSV
2. `scripts/load_items.py` — DynamoDB + Supabase CSVs → Neo4j `Item` nodes (MERGE-based)
3. `scripts/generate_variants.py` — Per-canonical Gemini scoring → `VARIANT_OF` edges (score ≥ 0.7), LLM-cached
4. `scripts/load_platters.py` — Platter tables → `Platter` nodes + `CONTAINS` edges
5. `scripts/detect_communities.py` — Leiden on `VARIANT_OF` (weight 1.0) + `BRIDGE_TO` (weight 0.5) → `Community` + `MEMBER_OF`
6. `scripts/build_community_edges.py` — Traverse Platter→Item→Community → `HAS_COMMUNITY`
7. `scripts/generate_summaries.py` — Per-community Gemini narrative → `summary_json` on `Community`
8. `scripts/index_communities.py` — Embed summaries → Qdrant collection `item_search_communities`

**Query-Time (zero LLM):**

```
User dish names (comma-separated)
  ↓ OpenAI text-embedding-3-small (batch)
Per-item Qdrant top-1 (score_threshold=0.35) → community IDs
  ↓
Neo4j rank_platters() Cypher:
  MATCH Platter by HAS_COMMUNITY, rank by matched_communities count
  ↓
Compute coverage_ratio = matched / query_communities
Compute skeleton_coverage_score (category family overlap)
Final = 0.7·coverage_ratio + 0.3·skeleton_coverage_score
  ↓
Top 3 PlatterResult with per-item match metadata
```

**State Management:**
- **Neo4j:** Single source of truth for Items, Platters, Communities, edges. All writes idempotent (`MERGE`)
- **Qdrant:** Read-only at query time; upserted only in step 8
- **LLM cache:** `llm_cache/variants/` and `llm_cache/enrichment/` prevent expensive re-runs

## Key Abstractions

**Community**
- Neo4j `Community` nodes with `id`, `name`, `member_count`, `summary_json`
- Qdrant payloads mirror community metadata for fast lookup
- Immutable after clustering; rebuilt only on explicit re-run

**PlatterResult**
- Query-time dataclass in [scripts/search.py](scripts/search.py) bundling platter metadata + per-item match status
- Fields include `matched_community_names`, `coverage_ratio`, `item_to_community`, `suggested_alternatives`

**Variant & Bridge Edges**
- `VARIANT_OF`: LLM-backed canonical → alias linkage, weight 1.0 in clustering
- `BRIDGE_TO`: vector-geometric canonical ↔ canonical similarity, weight 0.5 (alternative-similarity within veg_type+form)

**Category Family Normalization**
- `core/categories.py` maps 92 raw categories → 15 normalized families
- Applied at ingest and at query time (skeleton coverage score). Deterministic, no LLM.

## Entry Points

- **Streamlit UI:** [app.py](app.py) — `streamlit run app.py`
- **CLI Search:** [scripts/search.py](scripts/search.py) — `python -m scripts.search`
- **ETL scripts:** each runnable as `python -m scripts.<name>`

## Error Handling

- **Strategy:** Fail-fast with logging; errors during ETL halt the pipeline. Query-time errors return empty results with a log message.
- **LLM API errors:** Exponential backoff with `MAX_RETRIES=3` in variants / summaries / indexing scripts
- **Neo4j:** Lazy singleton driver; `neo4j_session()` context manager auto-closes on exception
- **Qdrant below threshold:** Returns None; search logs the missing item and continues
- **Missing env vars:** Fail at import time in `core/settings.py` (`os.environ[...]` for critical keys)

## Cross-Cutting Concerns

**Logging**
- Every module: `logging.basicConfig(level=logging.INFO)` → module-level `log = logging.getLogger(__name__)`
- Format: `"%(levelname)s %(message)s"` (no timestamps)

**Validation**
- Only at system boundaries (CSV parsing, DynamoDB scans, Qdrant/Neo4j responses)
- No defensive null checks inside module internals

**Authentication**
- Credentials loaded once from `.env` via `core/settings.py`
- Singleton clients in `core/connections.py`; never refreshed

**Caching**
- Query time: Streamlit `@st.cache_data` for canonical item list
- ETL time: file-based LLM response cache keyed by prompt version + input hash

---
*Architecture analysis: 2026-04-15*
