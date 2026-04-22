# External Integrations

**Analysis Date:** 2026-04-22

## Databases

### Neo4j (graph store — source of truth)
- **Client:** `neo4j` Python driver (≥ 5.14), singleton in `core/connections.py`
- **Connection:** `NEO4J_URI` + basic auth (`NEO4J_USER`, `NEO4J_PASSWORD`)
- **Session pattern:** `neo4j_session()` context manager (`core/connections.py:33`)
- **Node labels:** `Item`, `Platter`, `Community`, `PlatterCategory`
- **Edge types:** `VARIANT_OF` (Item→Item, LLM-scored), `BRIDGE_TO` (Item→Item, vector-geometric), `MEMBER_OF` (Item→Community), `CONTAINS` (Platter→Item), `HAS_COMMUNITY` (Platter→Community, pre-computed)
- **Write pattern:** idempotent `MERGE` for all nodes and edges
- **Batch sizes:** 500 nodes per batch in load scripts

### Qdrant (vector store — query-time lookup)
- **Client:** `qdrant-client` (≥ 1.8), singleton in `core/connections.py`
- **Connection:** Supports both local (`host`+`port`) and cloud (full `https://` URL via `QDRANT_HOST`); API key optional
- **Collection:** `item_search_communities` (`QDRANT_COLLECTION` constant)
- **Vector config:** 1536-dim, cosine metric (`text-embedding-3-small`)
- **Query threshold:** `QDRANT_SCORE_THRESHOLD = 0.35` — matches below are discarded at query time
- **Upsert batch size:** 50 communities per call (in `scripts/index_communities.py`)
- **Role:** read-only at query time; written only during ETL step 8

### AWS DynamoDB (source data, read-only)
- **Client:** `boto3` (≥ 1.34)
- **Tables:** `craftmyplate-platters`, `craftmyplate-variations` (configured via `PLATTERS_TABLE`, `VARIATIONS_TABLE` env vars)
- **Access pattern:** full scans, paginated, exported to CSV and loaded via pandas
- **Used by:** `scripts/load_items.py`, `scripts/load_platters.py`, `scripts/inspect_dynamo.py`
- **Auth:** `~/.aws/credentials` or explicit `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`; region `ap-south-1`

## LLM Providers

### OpenAI (embeddings only)
- **Client:** `openai` (≥ 1.30); auth via `OPENAI_API_KEY`
- **Model:** `text-embedding-3-small` (1536 dimensions)
- **Called in:**
  - `scripts/index_communities.py` — embed community summaries (batch size 50)
  - `scripts/search.py` — embed user query items at query time (batched)
- **No chat completions used**

### Google Gemini (LLM reasoning)
- **Client:** `google-genai` (≥ 1.0); auth via `GEMINI_API_KEY`
- **Model:** `gemini-2.5-flash` (set as constant `MODEL` in `scripts/enrich_items.py`)
- **Used for:**
  - `scripts/enrich_items.py` — structured item descriptions (ingredients, form, regional tags, veg_type, also_known_as)
  - `scripts/generate_variants.py` — per-canonical alias scoring against Qdrant candidates
  - `scripts/generate_summaries.py` — per-community narrative generation
- **Retry config:** `MAX_RETRIES=3`, `RETRY_DELAY=5.0s` in enrichment, `RETRY_DELAY=2.0s` elsewhere
- **Caching:** on-disk JSON in `llm_cache/enrichment/` and `llm_cache/variants/`, keyed by batch offset; invalidated by manual delete or prompt version change

## File-Based Integrations

### CSV Sources
- **Dynamo master data:** `DYNAMODB_CSV` env var (default: `Search -POC data - Active DynamoDB Master Data.csv`)
- **Supabase aliases:** `SUPABASE_CSV` env var (default: `Search -POC data - Supabase Master Data.csv`)
- **Loaded via:** pandas in `scripts/load_items.py`

### LLM Cache Directories
- `llm_cache/enrichment/` — `enrich_items.py` results (JSON, one file per batch offset)
- `llm_cache/variants/` — `generate_variants.py` per-canonical JSON cache
- **Invalidation:** manual delete, or automatic when prompt version constant bumps
- **Not committed to git**

## Frontend Integration

- **Streamlit UI** (`app.py`) calls into `scripts/search.py` directly (no HTTP boundary)
- **Caching:** `@st.cache_data` on canonical item loader (no TTL — reloads only on code change)

## Runtime Flow Summary

**ETL (offline, sequential):**
```
CSVs → Neo4j Item/Platter → Gemini variants → Leiden communities
     → Gemini summaries → OpenAI embeddings → Qdrant collection
```

**Query-time (zero LLM):**
```
User dish names → OpenAI embeddings (batched) → Qdrant top-1/item
                → Neo4j rank_platters Cypher → PlatterResult[]
```

## No Integrations Present

- No auth provider (no users / sessions)
- No webhook receivers or publishers
- No message queue / pub-sub
- No external monitoring / APM
- No feature-flag service
- No Supabase SDK (Supabase data arrives as CSV export only)

---
*Integrations analysis: 2026-04-22*
