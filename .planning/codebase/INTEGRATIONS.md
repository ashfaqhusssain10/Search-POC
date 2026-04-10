# External Integrations

**Analysis Date:** 2026-04-10

## APIs & External Services

**AI/LLM:**
- OpenAI API - Used for:
  - Generating embeddings via `text-embedding-3-small` model (1536-dim vectors)
  - Generating narrative summaries for dish communities via GPT-4o-mini
  - SDK/Client: `openai` (1.30.0+)
  - Auth: `OPENAI_API_KEY` environment variable
  - Rate limiting: Built-in retry logic with 2-second delays in `scripts/generate_summaries.py` and `scripts/index_communities.py`

- Google Gemini API - Used for:
  - Enriching item data via `scripts/enrich_items.py`
  - Model: `gemini-2.5-flash`
  - SDK/Client: `google-genai` (1.0.0+) via `genai.Client`
  - Auth: `GEMINI_API_KEY` environment variable (exported from `core/settings.py`)

## Data Storage

**Databases:**

**Graph Database (Neo4j):**
- Purpose: Stores dish items, platters, communities, and their relationships
- Connection: `NEO4J_URI` (bolt://localhost:7687 default)
- Auth: `NEO4J_USER` + `NEO4J_PASSWORD` environment variables
- Client: `neo4j` Python driver (5.14.0+)
- Connection Pattern: Singleton driver with session context manager in `core/connections.py`
- Nodes stored:
  - `Item` - Individual dishes (from DynamoDB or Supabase)
  - `Platter` - Meal combinations (from DynamoDB)
  - `Community` - Semantically related dish groups (detected via Louvain algorithm)
- Relationships:
  - `(Item)-[:VARIANT_OF]->(Item)` - Alias relationships
  - `(Platter)-[:CONTAINS]->(Item)` - Platter composition
  - `(Item)-[:MEMBER_OF]->(Community)` - Community membership
  - `(Platter)-[:HAS_COMMUNITY]->(Community)` - Coverage by communities
- Key operations:
  - Item loading: `scripts/load_items.py` - Reads from CSV files and creates Item nodes
  - Platter loading: `scripts/load_platters.py` - Scans DynamoDB tables, creates Platter nodes and CONTAINS edges
  - Community detection: `scripts/build_community_edges.py` + `scripts/detect_communities.py` - Builds graph, runs Louvain algorithm
  - Summary generation: `scripts/generate_summaries.py` - Fetches communities, calls GPT-4o-mini, stores summary_json
  - Item enrichment: `scripts/enrich_items.py` - Calls Gemini API to enrich item data, stores results back to Neo4j

**Vector Database (Qdrant):**
- Purpose: Enables semantic search of dish communities by embedding similarity
- Connection: `QDRANT_HOST` (localhost default) + `QDRANT_PORT` (6333 default)
- Optional Auth: `QDRANT_API_KEY` environment variable
- Client: `qdrant-client` (1.8.0+)
- Collection: `item_search_communities`
  - Vector dimension: 1536 (text-embedding-3-small)
  - Distance metric: Cosine similarity
  - Score threshold: 0.35
- Key operations:
  - Collection creation/management: `scripts/index_communities.py` - Creates collection if needed, upserts community vectors
  - Search: `scripts/search.py` - Embeds user query, performs vector search with threshold filtering
  - Embedded text format: `"Community: {name}. Members: {canonical_names}. Also known as: {variants}. Hub items: {hub_items}. {LLM_narrative}"`

**File Storage:**
- CSV files (local filesystem):
  - `Search -POC data - Active DynamoDB Master Data.csv` - Canonical dishes and platter data
  - `Search -POC data - Supabase Master Data.csv` - Variant/alias names for dishes
  - Accessed via `DYNAMODB_CSV` and `SUPABASE_CSV` environment variables

**Caching:**
- None detected - Direct database queries on each operation

## Authentication & Identity

**Auth Provider:**
- Custom: Environment variable-based secrets
  - Neo4j: User/password in `.env`
  - OpenAI: API key in `.env`
  - Gemini: API key in `.env` (`GEMINI_API_KEY`)
  - AWS: Via ~/.aws/credentials or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` environment variables
  - Qdrant: Optional API key in `.env`

**Implementation:**
- Centralized in `core/settings.py` - Loads `.env` using python-dotenv; exports `GEMINI_API_KEY`
- Singleton clients in `core/connections.py` - Lazy initialization with global references

## Monitoring & Observability

**Error Tracking:**
- None detected - No external error tracking service (Sentry, Datadog, etc.)

**Logs:**
- Approach: Python `logging` module with basicConfig in each script
- Format: `"%(levelname)s %(message)s"` (level + message only)
- Output: stdout (console)
- No persistent logging backend

## CI/CD & Deployment

**Hosting:**
- Not applicable - SearchPOC is a standalone data pipeline, not a service

**CI Pipeline:**
- None detected

**Deployment Notes:**
- Scripts intended to be run locally or on a compute instance with:
  - Network access to Neo4j, Qdrant, OpenAI, Gemini, and AWS
  - Environment variables configured
  - Virtual environment with dependencies installed

## Environment Configuration

**Required env vars (must be set):**
- `NEO4J_PASSWORD` - Critical for graph database
- `OPENAI_API_KEY` - Critical for embeddings and LLM
- `GEMINI_API_KEY` - Critical for item enrichment via Gemini API

**Optional env vars (have defaults):**
- `NEO4J_URI` - Default: bolt://localhost:7687
- `NEO4J_USER` - Default: neo4j
- `QDRANT_HOST` - Default: localhost
- `QDRANT_PORT` - Default: 6333
- `QDRANT_API_KEY` - Default: None (skipped if not set)
- `AWS_REGION` - Default: ap-south-1
- `PLATTERS_TABLE` - Default: craftmyplate-platters
- `VARIATIONS_TABLE` - Default: craftmyplate-variations
- `DYNAMODB_CSV` - Default: Search -POC data - Active DynamoDB Master Data.csv
- `SUPABASE_CSV` - Default: Search -POC data - Supabase Master Data.csv

**Secrets location:**
- `.env` file at project root (loaded by `core/settings.py`)
- Example template: `.env.example`
- AWS credentials: ~/.aws/credentials (standard AWS SDK location)

## AWS Services

**DynamoDB:**
- Purpose: Source of truth for platter and canonical item data
- Region: ap-south-1 (configured in `.env`)
- Tables accessed:
  - `DefaultPlattersTable` - Platter definitions (PK: platterId)
  - `DefaultPlatterItemsTable` - Platter → Item mappings (PK: platterId, SK: itemId)
  - `DefaultPlattersTable`, `DefaultPlatterItemsTable` - Queried in `scripts/load_platters.py`
- Access method:
  - boto3 resource (not client)
  - Full table scan with pagination (LastEvaluatedKey)
  - No filters applied at read time
- Read pattern: One-time batch scan per script run

**Other AWS:**
- No S3, Lambda, SQS, or other AWS services detected

## Webhooks & Callbacks

**Incoming:**
- None - SearchPOC is a data pipeline, not a service with exposed endpoints

**Outgoing:**
- None detected

---

*Integration audit: 2026-04-10*
