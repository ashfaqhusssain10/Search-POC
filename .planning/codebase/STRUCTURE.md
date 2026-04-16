# Codebase Structure

**Analysis Date:** 2026-04-15

## Directory Layout

```
SearchPOC/
├── app.py                          # Streamlit UI entry point
├── core/                           # Shared utilities
│   ├── connections.py              # Neo4j + Qdrant singleton clients
│   ├── settings.py                 # Environment variable loading + constants
│   └── categories.py               # Category normalization / family mapping
├── scripts/                        # ETL + query pipeline (~19 scripts)
│   ├── enrich_items.py             # Step 0: LLM item enrichment
│   ├── load_items.py               # Step 1: DynamoDB + Supabase → Neo4j
│   ├── generate_variants.py        # Step 3: VARIANT_OF edges (Gemini)
│   ├── add_canonical_bridges.py    # Step 3b: BRIDGE_TO edges (vector geometry)
│   ├── load_platters.py            # Step 4: DynamoDB platters → Neo4j
│   ├── detect_communities.py       # Step 5: Leiden clustering
│   ├── build_community_edges.py    # Step 6: HAS_COMMUNITY pre-computation
│   ├── generate_summaries.py       # Step 7: Per-community narratives
│   ├── index_communities.py        # Step 8: Embed + Qdrant upsert
│   ├── search.py                   # Query-time: embed → Qdrant → Neo4j ranking
│   ├── embed_items.py              # Item-level embeddings (utility)
│   ├── mine_also_known_as.py       # Alias mining utility
│   ├── eval.py                     # Evaluation harness
│   ├── inspect_dynamo.py           # DynamoDB table inspector
│   ├── verify_bridges.py           # Bridge edge sanity check
│   ├── cleanup_communities.py      # Community cleanup utility
│   ├── diag_linkage.py             # Linkage diagnostics
│   └── diag_alternatives.py        # Alternatives diagnostics
├── llm_cache/                      # On-disk LLM response caching
│   ├── enrichment/                 # enrich_items.py results
│   └── variants/                   # generate_variants.py per-canonical cache
├── .planning/                      # Project planning + analysis
│   ├── codebase/                   # This directory: codebase map docs
│   ├── debug/                      # Debug notes
│   └── search_eval/                # Evaluation queries
├── docs/                           # Architecture specs
│   └── superpowers/specs/          # System design docs
├── .env                            # Secrets (git-ignored)
└── requirements.txt                # Python dependencies
```

## Directory Purposes

**`app.py`** — Streamlit UI: multi-select dish picker, result rendering, metric cards. 132 lines.

**`core/`** — Shared infrastructure:
- `connections.py`: Neo4j driver singleton, Qdrant client singleton, context managers
- `settings.py`: `.env` loading; constants (`QDRANT_SCORE_THRESHOLD=0.35`, `EMBEDDING_MODEL=text-embedding-3-small`, `EMBEDDING_DIM=1536`, `QDRANT_COLLECTION=item_search_communities`)
- `categories.py`: 92-raw → 15-family `CATEGORY_FAMILY_MAP`; helpers `category_family()`, `normalize_category()`

**`scripts/`** — ETL pipeline and query-time logic. All scripts runnable via `python -m scripts.<name>`. All call `close_connections()` in `finally`.

**`llm_cache/`** — Persistent LLM response cache, git-ignored. Invalidated by manual delete or prompt version bump.

**`.planning/`** — GSD planning structure:
- `codebase/`: dynamic codebase analysis docs (this file lives here)
- `debug/`: ephemeral debug notes
- `search_eval/`: evaluation query sets

**`docs/superpowers/specs/`** — System design documents (e.g. `2026-04-10-variant-matching-redesign.md`).

## Key File Locations

**Entry Points**
- [app.py](app.py) — Streamlit UI
- [scripts/search.py](scripts/search.py) — CLI search
- Any `scripts/*.py` — standalone ETL step

**Configuration**
- `.env` — secrets (git-ignored)
- [core/settings.py](core/settings.py) — env var loading + constants
- [requirements.txt](requirements.txt) — deps: neo4j, qdrant-client, openai, graspologic, networkx, boto3, python-dotenv, google-genai, pandas, rapidfuzz, streamlit

**Core Logic**
- [scripts/search.py](scripts/search.py) — `rank_platters()` Cypher, `compute_skeleton_metrics()`, coverage scoring (600 lines — largest hot path)
- [scripts/generate_variants.py](scripts/generate_variants.py) — 678 lines, largest ETL module
- [scripts/detect_communities.py](scripts/detect_communities.py) — Leiden via `graspologic.partition.hierarchical_leiden`
- [core/categories.py](core/categories.py) — category family normalization

**Evaluation / Diagnostics**
- [scripts/eval.py](scripts/eval.py) — evaluation harness (199 lines)
- [.planning/search_eval/](.planning/search_eval/) — test query sets
- `scripts/diag_*.py`, `scripts/verify_bridges.py` — debug utilities

## Naming Conventions

**Files / modules:** `snake_case.py`
**Functions:** `fetch_*`, `scan_*`, `build_*`, `compute_*`, private `_helper()`
**Cypher query strings:** module-level `SCREAMING_SNAKE_CASE` constants (e.g. `FETCH_VARIANT_OF_EDGES`, `RANK_PLATTERS_QUERY`)
**Config constants:** `SCREAMING_SNAKE_CASE` (`QDRANT_SCORE_THRESHOLD`, `EMBEDDING_MODEL`, `MAX_CLUSTER_SIZE`)
**Data classes:** `PascalCase` (e.g. `PlatterResult`)
**No custom exception classes** — uses built-ins only

## Where to Add New Code

**New ETL step** — create `scripts/new_step.py`, import from `core.*`, define module-level Cypher constants, provide `main()`, call `close_connections()` in `finally`.

**New query feature** — modify `scripts/search.py`; update `PlatterResult` dataclass if new fields; update `app.py` rendering.

**New diagnostic** — create `scripts/diag_<name>.py` or `verify_<name>.py`. No pipeline integration required.

**New category mapping** — edit `CATEGORY_FAMILY_MAP` in `core/categories.py`.

**Shared utility** — if cross-script, add to `core/` (new module or extend existing).

## Special Directories

| Directory | Generated | Committed | Purpose |
|---|---|---|---|
| `llm_cache/` | Yes (scripts) | No | LLM response cache |
| `.planning/codebase/` | Yes (gsd) | Yes | Codebase map |
| `.planning/debug/` | Manual | Yes | Debug notes |
| `docs/superpowers/specs/` | Manual | Yes | Design docs |

---
*Structure analysis: 2026-04-15*
