# Testing

**Analysis Date:** 2026-04-15

## Status

**No automated test framework is configured.** There is no `pytest`, `unittest`, `tests/` directory, or CI-driven test run. Validation is entirely manual, inline, or via hand-run evaluation scripts.

## What Exists in Place of Tests

### 1. Inline logging assertions
Every script logs counts and progress, making it easy to spot-check pipeline integrity by reading output. Example:
```python
log.info("Loaded %d items from CSV", len(rows))
log.info("Merged %d Item nodes", merged_count)
```

### 2. Diagnostic scripts
Several files exist for manual verification:
- [scripts/verify_bridges.py](scripts/verify_bridges.py) — sanity-check `BRIDGE_TO` edges after `add_canonical_bridges.py`
- [scripts/diag_linkage.py](scripts/diag_linkage.py) — inspect variant / bridge linkage between specific items
- [scripts/diag_alternatives.py](scripts/diag_alternatives.py) — debug alternative suggestions from `search.py`
- [scripts/inspect_dynamo.py](scripts/inspect_dynamo.py) — dump DynamoDB table contents

Run: `python -m scripts.<name>` and read the log output.

### 3. Evaluation harness
[scripts/eval.py](scripts/eval.py) (199 lines) runs search queries against a fixed evaluation set in [.planning/search_eval/](.planning/search_eval/) and reports result quality. This is the closest thing to a regression test — used manually after changes to ranking logic, embeddings, or community detection.

Evaluation inputs live in `.planning/search_eval/eval_queries.md`.

### 4. LLM response cache
`llm_cache/variants/` and `llm_cache/enrichment/` serve as a quasi-snapshot: changing a prompt invalidates the cache (via prompt version bump) and forces fresh LLM calls, which makes regressions visible.

## Validation Patterns

- **CSV loading:** pandas + log row count; bad rows logged and skipped
- **Neo4j writes:** idempotent `MERGE`; post-write logging of affected-row counts
- **Qdrant upserts:** batch result logging; no post-upsert verification query
- **LLM outputs:** JSON parsed with fallback regex extraction; failures logged but do not raise
- **Query-time:** `find_best_community()` returns `None` below `QDRANT_SCORE_THRESHOLD=0.35`; search logs the dropped item and continues

## What's Missing

- **Unit tests** — no coverage for `core/categories.py`, `rank_platters()`, coverage scoring
- **Integration tests** — no end-to-end ETL dry-run harness
- **Regression gate** — `scripts/eval.py` is not wired to any pass/fail threshold or CI
- **Mocks / fixtures** — no `conftest.py`, no test data fixtures
- **CI** — no GitHub Actions, no pre-commit hook test run

## Recommended Future Structure

If tests are added:
- `tests/unit/test_categories.py` — CATEGORY_FAMILY_MAP completeness, normalization idempotency
- `tests/unit/test_search_scoring.py` — coverage_ratio, skeleton_coverage_score, final_score math
- `tests/integration/test_eval_thresholds.py` — wrap `scripts/eval.py` with regression thresholds
- `pytest` + `pytest-mock`; mock Neo4j/Qdrant clients at `core.connections` boundary

## How to Verify Changes Today

Per CLAUDE.md: all scripts runnable standalone with `python -m scripts.<name>`. The current verification loop is:

1. Re-run affected ETL step(s) in order
2. Watch log counts for regressions
3. Run `python -m scripts.eval` against the eval query set
4. Spot-check `python -m scripts.search` with known-good queries
5. Manual UI check via `streamlit run app.py`

---
*Testing analysis: 2026-04-15*
