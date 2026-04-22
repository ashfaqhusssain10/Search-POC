# Codebase Structure

**Analysis Date:** 2026-04-22

## Directory Layout

```
SearchPOC/
├── app.py                    # Streamlit UI entry point
├── inspect_zero_edges.py     # Standalone diagnostic utility
├── requirements.txt          # Python dependencies
├── .env                      # Environment variables (not committed)
├── CLAUDE.md                 # Project instructions for Claude
├── core/                     # Shared infrastructure (settings, connections, helpers)
│   ├── __init__.py
│   ├── settings.py           # All env var loading + constants
│   ├── connections.py        # Neo4j + Qdrant singleton clients
│   └── categories.py        # Category normalization taxonomy
├── scripts/                  # ETL pipeline steps + search engine
│   ├── __init__.py
│   ├── enrich_items.py       # Step 1: LLM enrichment of CSV items
│   ├── load_items.py         # Step 2: CSV → Neo4j Item nodes
│   ├── generate_variants.py  # Step 3: VARIANT_OF edges via LLM
│   ├── load_platters.py      # Step 4: Platter nodes + CONTAINS edges
│   ├── detect_communities.py # Step 5: Leiden community detection
│   ├── build_community_edges.py # Step 6: HAS_COMMUNITY pre-compute
│   ├── generate_summaries.py # Step 7: LLM narratives per community
│   ├── index_communities.py  # Step 8: Embed summaries → Qdrant
│   ├── search.py             # Query engine (used by app.py)
│   ├── add_canonical_bridges.py  # Utility: add BRIDGE_TO edges
│   ├── cleanup_communities.py    # Utility: remove stale Community nodes
│   ├── diag_alternatives.py      # Diagnostic: alternative search paths
│   ├── diag_linkage.py           # Diagnostic: community linkage inspection
│   ├── embed_items.py            # Utility: embed individual items
│   ├── eval.py                   # Search quality evaluation
│   ├── inspect_dynamo.py         # DynamoDB scan/pagination utilities
│   ├── mine_also_known_as.py     # Mine alias patterns
│   ├── update_item_community_payloads.py  # Backfill Qdrant payloads
│   └── verify_bridges.py         # Verify BRIDGE_TO edge correctness
├── llm_cache/                # Cached LLM call outputs (avoid re-billing)
│   ├── enrichment/           # Per-item enrichment JSON cache
│   └── variants/             # Per-pair variant scoring cache
├── .planning/                # GSD planning artifacts (not committed to product)
│   ├── codebase/             # Codebase map documents (this file)
│   ├── debug/                # Debug session logs
│   │   └── resolved/
│   └── search_eval/          # Search evaluation results
├── docs/                     # Design documents
│   └── superpowers/
│       └── specs/
└── venv/                     # Python virtual environment (not committed)
```

## Directory Purposes

**`core/`:**
- Purpose: Shared infrastructure imported by all pipeline scripts and the app
- Contains: Environment settings, database client singletons, category normalization
- Key files: `core/settings.py` (all constants), `core/connections.py` (Neo4j + Qdrant), `core/categories.py` (taxonomy)
- Rule: No pipeline logic here — only configuration and shared helpers

**`scripts/`:**
- Purpose: All runnable modules — 8 ETL pipeline steps, the search engine, diagnostic/utility scripts
- Contains: One module per concern; each script is independently runnable via `python -m scripts.<name>`
- Key files: `scripts/search.py` (query engine), `scripts/detect_communities.py` (Leiden), `scripts/index_communities.py` (Qdrant indexing)
- Rule: Pipeline scripts are numbered 1–8 conceptually (see `CLAUDE.md` Project State Reference); run in order

**`llm_cache/`:**
- Purpose: File-based cache to avoid redundant LLM API calls during re-runs
- Generated: Yes (created by ETL scripts)
- Committed: Possibly yes (avoids re-billing on fresh clone)
- Sub-dirs: `enrichment/` (step 1 output), `variants/` (step 3 output)

**`.planning/`:**
- Purpose: GSD workflow artifacts — phase plans, debug logs, evaluation results
- Generated: Yes (by GSD commands)
- Committed: Yes (planning artifacts are version controlled)

## Key File Locations

**Entry Points:**
- `app.py`: Streamlit UI — `streamlit run app.py`
- `scripts/search.py`: Search engine — `from scripts.search import search_platters`

**Configuration:**
- `core/settings.py`: All env vars and constants (QDRANT_SCORE_THRESHOLD, EMBEDDING_MODEL, etc.)
- `core/connections.py`: Database client factory functions
- `.env`: Secrets (not committed)

**Core Logic:**
- `scripts/search.py`: `search_platters()`, `PlatterResult`, `rank_platters`, `compute_coverage_from_seeds`
- `scripts/detect_communities.py`: Leiden parameters, graph build, community write
- `core/categories.py`: `CATEGORY_FAMILY_MAP`, `category_family()`, `build_category_family_counts()`

**Diagnostics:**
- `scripts/eval.py`: Search quality evaluation
- `scripts/diag_alternatives.py`, `scripts/diag_linkage.py`: Community and alternative path inspection
- `inspect_zero_edges.py`: Root-level diagnostic for items with no edges

## Naming Conventions

**Files:**
- `snake_case.py` throughout — both `core/` and `scripts/`
- ETL scripts named by action: `load_items.py`, `generate_variants.py`, `detect_communities.py`
- Diagnostic scripts prefixed with `diag_`: `diag_alternatives.py`, `diag_linkage.py`
- Utility scripts use imperative: `cleanup_communities.py`, `verify_bridges.py`

**Modules:**
- `scripts/` and `core/` both have `__init__.py` (importable as packages)
- All scripts use `python -m scripts.<name>` invocation pattern

**Functions:**
- `snake_case` throughout
- Action verbs: `fetch_`, `build_`, `compute_`, `write_`, `run_`, `find_`
- Public API function: `search_platters()` in `scripts/search.py`

**Constants:**
- `UPPER_SNAKE_CASE` for module-level constants: `QDRANT_SCORE_THRESHOLD`, `TOP_N_RESULTS`, `COMMUNITY_WEIGHT`

**Cypher queries:**
- Module-level string constants in `UPPER_SNAKE_CASE`: `FETCH_ALL_PLATTERS_QUERY`, `UPSERT_COMMUNITIES`, `FIND_VARIANT_SUBSTITUTE`

## Where to Add New Code

**New ETL pipeline step:**
- Implementation: `scripts/<verb>_<noun>.py`
- Must include `main()` function and `if __name__ == "__main__": main()` guard
- Must import from `core/connections.py` and `core/settings.py`
- Run with: `python -m scripts.<name>`

**New shared helper / taxonomy change:**
- Add to `core/categories.py` if category-related
- Add new module to `core/` only if shared by 2+ scripts

**New search ranking signal:**
- Add to `scripts/search.py` — modify `compute_coverage_from_seeds` or `sort_platters_by_final_score`
- Update `PlatterResult` dataclass fields if new data needs to be surfaced to UI

**New UI feature:**
- Add to `app.py` — UI only, no business logic; delegate to `search_platters()` or new `scripts/` functions

**New diagnostic script:**
- Add to `scripts/diag_<topic>.py`
- Runnable standalone; no side effects on production graph

**New Cypher query:**
- Define as module-level string constant in the script that uses it
- Keep Cypher queries adjacent to the function that calls `session.run()`

## Special Directories

**`llm_cache/`:**
- Purpose: Avoid re-paying LLM API costs on re-runs
- Generated: Yes (by `enrich_items.py` and `generate_variants.py`)
- Committed: Check `.gitignore` — likely committed to preserve cache across machines

**`venv/`:**
- Purpose: Python virtual environment
- Generated: Yes
- Committed: No

**`.planning/`:**
- Purpose: GSD workflow artifacts
- Generated: Yes (by `/gsd:` commands)
- Committed: Yes

---

*Structure analysis: 2026-04-22*
