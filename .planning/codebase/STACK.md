# Technology Stack

**Analysis Date:** 2026-04-22

## Language & Runtime

- **Python** 3.11.14 (Homebrew `python@3.11`)
- **Package manager:** `pip` with `requirements.txt` (unpinned version ranges, no lockfile)
- **Entry points:** `streamlit run app.py`, `python -m scripts.<name>`

## Core Dependencies

| Package | Version | Purpose |
|---|---|---|
| `neo4j` | ≥ 5.14.0 | Graph database driver (Items, Platters, Communities) |
| `qdrant-client` | ≥ 1.8.0 | Vector database client (community embeddings) |
| `openai` | ≥ 1.30.0 | `text-embedding-3-small` (1536-dim, cosine) |
| `google-genai` | ≥ 1.0.0 | Gemini 2.5 Flash for enrichment, variant scoring, summaries |
| `graspologic` | ≥ 3.3.0 | Hierarchical Leiden community detection |
| `networkx` | ≥ 3.0 | Graph construction for Leiden input |
| `boto3` | ≥ 1.34.0 | DynamoDB client (source data scans) |
| `pandas` | ≥ 2.0.0 | CSV loading / transformation |
| `rapidfuzz` | ≥ 3.0.0 | Fuzzy string matching (alias mining, query-time suggestions) |
| `streamlit` | ≥ 1.35.0 | Interactive web UI (`app.py`) |
| `python-dotenv` | ≥ 1.0.0 | `.env` loading |

See `requirements.txt`.

## Configuration

**Loaded via `python-dotenv`** in `core/settings.py` from `.env` at repo root.

**Required env vars (fail at import if missing):**
- `NEO4J_URI`, `NEO4J_PASSWORD`
- `OPENAI_API_KEY`, `GEMINI_API_KEY`

**Optional env vars (with defaults):**
- `NEO4J_USER` (default: `neo4j`)
- `QDRANT_HOST` (default: `localhost`), `QDRANT_PORT` (default: `6333`), `QDRANT_API_KEY`
- `DYNAMODB_CSV`, `SUPABASE_CSV` (default filenames for CSV sources)
- `AWS_REGION` (default: `ap-south-1`), `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (optional; falls back to `~/.aws/credentials`)

**Hardcoded constants** in `core/settings.py`:
- `QDRANT_COLLECTION = "item_search_communities"`
- `EMBEDDING_MODEL = "text-embedding-3-small"`
- `EMBEDDING_DIM = 1536`
- `QDRANT_SCORE_THRESHOLD = 0.35`

**ETL-level constants (do not change without architecture review):**
- Leiden: `resolution=1.0`, `max_cluster_size=20`, `VARIANT_OF_WEIGHT=1.0`, `BRIDGE_TO_WEIGHT=0.5`
- Batch sizes: 500 nodes (Neo4j), 50 communities (Qdrant embed/upsert), 50 (Gemini enrichment)

## Build / Tooling

- **No linter / formatter configured** (no `ruff`, `black`, `.editorconfig`)
- **No CI config** in repo
- **No test framework** — validation is inline / via manual eval scripts (`scripts/eval.py`)
- **Git** — standard Git repo on `main` branch

## Frontend

- **Streamlit 1.35+** single-file app (`app.py`)
- `@st.cache_data` used for canonical item loading (no TTL configured)
- No separate JavaScript/CSS

---
*Stack analysis: 2026-04-22*
