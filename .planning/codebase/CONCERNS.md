# Concerns & Technical Debt

**Analysis Date:** 2026-04-15

## Severity Legend

- **🔴 Critical** — affects correctness or blocks progress
- **🟡 Moderate** — degrades quality or maintainability
- **🟢 Minor** — nice to address

---

## Test Coverage

### 🔴 No automated test suite
- No `pytest` / CI / regression gate anywhere in the repo
- Only `scripts/eval.py` + manual log-watching
- Risk: ranking / scoring regressions can ship silently; refactors have no safety net
- **Where:** entire codebase
- **Impact:** every change to `scripts/search.py`, `core/categories.py`, or Leiden parameters is high-risk

## Error Handling

### 🟡 Silent `None` returns mask failures at query time
- `find_best_community()` returns `None` below `QDRANT_SCORE_THRESHOLD=0.35`
- Search logs the dropped item but produces no distinguishable user-facing signal
- UI renders coverage_ratio without showing how many query items were dropped entirely
- **Where:** [scripts/search.py](scripts/search.py), [app.py](app.py)

### 🟡 LLM JSON parsing with regex fallback
- Several scripts (`generate_variants.py`, `generate_summaries.py`, `enrich_items.py`) parse Gemini output with a regex fallback when JSON.loads fails
- Risk: bad LLM output can coerce into wrong-shaped records without raising
- **Where:** `scripts/generate_variants.py`, `scripts/generate_summaries.py`, `scripts/enrich_items.py`

### 🟢 No validation of CSV encoding
- `load_items.py` and `load_platters.py` load CSVs without explicit encoding declaration
- Non-UTF-8 input (e.g. CP-1252 from Excel export) will raise mid-load after partial progress
- **Where:** [scripts/load_items.py](scripts/load_items.py), [scripts/load_platters.py](scripts/load_platters.py)

## Magic Numbers & Hidden Tuning

### 🟡 Pipeline-critical constants scattered across scripts
- Leiden: `resolution=1.0`, `max_cluster_size=20`, `VARIANT_OF_WEIGHT=1.0`, `BRIDGE_TO_WEIGHT=0.5` live only in `scripts/detect_communities.py`
- Coverage scoring: `COMMUNITY_WEIGHT=0.7`, `SKELETON_WEIGHT=0.3`, `SUGGESTION_THRESHOLD=0.60` live only in `scripts/search.py`
- Variant score cutoff (`≥ 0.7`) hardcoded in `scripts/generate_variants.py`
- **Why it matters:** these are the knobs that control search quality; splitting them across files makes tuning error-prone
- **Suggestion:** consolidate into `core/settings.py` or a new `core/tuning.py`

### 🟢 Inline batch sizes
- `500` (Neo4j MERGE batch), `50` (Qdrant upsert + Gemini enrichment), `25` (Qdrant embed batch)
- Some appear as literals inside loops rather than named constants

## Data Quality & Integrity

### 🟡 `llm_cache/` invalidation is manual
- Cache files in `llm_cache/variants/` and `llm_cache/enrichment/` keyed by prompt version hash
- If the prompt is edited *without* bumping the version constant, stale results silently persist
- **Where:** `scripts/enrich_items.py`, `scripts/generate_variants.py`
- Risk: silent semantic drift between old and new runs

### 🟡 `BRIDGE_TO` vs `VARIANT_OF` weights are load-bearing but undocumented in code
- CLAUDE.md notes `VARIANT_OF=1.0`, `BRIDGE_TO=0.5` but these only appear as literals in `detect_communities.py`
- There is no unit test or assertion that community detection is actually using both

## Performance

### 🟢 Streamlit cache has no TTL
- `@st.cache_data` on `load_canonical_items()` in `app.py` never expires
- After a fresh ETL run, the UI needs a manual reload / process restart to pick up new items
- **Where:** [app.py](app.py)

### 🟢 Full DynamoDB scans on every load
- `load_items.py` / `load_platters.py` read CSV exports, not direct DynamoDB scans, so this is currently an offline-only concern
- But `scripts/inspect_dynamo.py` does a full scan with no pagination safeguards

## Security

### 🟢 Env vars loaded at import time
- `core/settings.py` uses `os.environ["KEY"]` — missing keys fail fast, which is good
- But there's no `.env.example` in the repo, so new contributors must reverse-engineer required keys
- **Suggestion:** add `.env.example` (non-secret)

### 🟢 No secrets scanner / pre-commit
- API keys could be accidentally committed; no guard rail

## Architectural Debt

### 🟡 `scripts/search.py` is 600 lines
- Single file holds: embedding, Qdrant lookup, Neo4j Cypher, coverage scoring, skeleton scoring, alternatives suggestion, CLI entry point
- Refactor candidates: split into `search/query.py`, `search/ranking.py`, `search/alternatives.py`
- **Where:** [scripts/search.py](scripts/search.py)

### 🟡 `scripts/generate_variants.py` is 678 lines
- Largest single file in the repo; mixes Gemini prompting, caching, Qdrant candidate retrieval, and Neo4j writes
- **Where:** [scripts/generate_variants.py](scripts/generate_variants.py)

### 🟢 No single source-of-truth for the pipeline step order
- Step order lives only in CLAUDE.md and docstrings
- A runnable `scripts/run_pipeline.py` orchestrator would eliminate human error

## Documentation

### 🟢 No README.md
- Only `CLAUDE.md` describes the project; there's no human-oriented onboarding doc
- New contributors must read CLAUDE.md to understand the ETL order

### 🟢 Architecture specs live in `docs/superpowers/specs/`
- Design decisions (e.g. `2026-04-10-variant-matching-redesign.md`) are recorded but not linked from any index

## Known Behavioural Quirks

- **`BRIDGE_TO` is alternative-similarity**, not ingredient overlap (see user memory). Cross-ingredient bridges within same `veg_type + form` are intentional — do not add ingredient-overlap filters
- Query-time drops any user item whose best Qdrant match is below 0.35. There is no user-visible "we didn't understand this item" hint
- `coverage_ratio` denominator is `query_community_count` (unique communities) not `len(items)` — two synonymous items count as one in the denominator

---
## Additional Concerns (2026-04-22 refresh)

### 🟡 `CANDIDATE_POOL_SIZE` dead constant
- Defined at `scripts/search.py:41` as `15`, never referenced anywhere in the file
- **Where:** [scripts/search.py:41](scripts/search.py#L41)

### 🟡 Non-thread-safe singletons
- `core/connections.py` globals `_neo4j_driver` / `_qdrant_client` have race conditions under concurrent Streamlit requests
- No lock around the `if _neo4j_driver is None` check
- **Where:** [core/connections.py](core/connections.py)

### 🟡 `OpenAI` client recreated per query
- `scripts/search.py:482` instantiates `OpenAI(api_key=...)` inside `search_platters()` on every call
- Should be a module-level or cached singleton
- **Where:** [scripts/search.py:482](scripts/search.py#L482)

### 🟡 `logging.basicConfig` in every script
- First-call wins semantics — when scripts import each other, only the first `basicConfig` call takes effect
- `eval.py` already works around this; other scripts may silently lose config
- **Where:** every script under `scripts/`

### 🟡 Bare `except:` in generate_variants.py
- `scripts/generate_variants.py:335` catches bare `except:`, which includes `SystemExit` and `KeyboardInterrupt`
- **Where:** [scripts/generate_variants.py:335](scripts/generate_variants.py#L335)

### 🟡 Full platter scan on every query — no Cypher filtering
- `FETCH_ALL_PLATTERS_QUERY` returns all 192+ platters; community-intersection filtering is done in Python
- For POC scale this is fine; will not scale to large platter catalogs
- **Where:** [scripts/search.py](scripts/search.py)

### 🟡 N×M serial Qdrant calls in suggestion pass
- Suggestion pass runs up to 3 platters × N missing items = 3N serial Qdrant round-trips
- These are already sequential (not batched); no parallelism
- **Where:** [scripts/search.py:529–578](scripts/search.py#L529-L578)

### 🟡 Silently drops corrupted community summaries
- `scripts/search.py:337–342` catches `json.JSONDecodeError` with no log line
- A corrupt `summary_json` silently disappears from the UI
- **Where:** [scripts/search.py:337](scripts/search.py#L337)

### 🟢 `@st.cache_data` with no TTL (duplicate, see Performance above)
- Also note: `load_canonical_items()` caches the Supabase item list indefinitely; stale after `load_items.py` reruns

### 🟢 No input validation on `search_platters()`
- No max item count or max string length guard on the query input
- **Where:** [scripts/search.py:476](scripts/search.py#L476)

### 🟢 `graspologic` dependency risk
- Heavyweight academic library used only for `hierarchical_leiden` in `scripts/detect_communities.py`
- Unpredictable release cadence; only one function used from it

---
*Concerns analysis refreshed: 2026-04-22*
