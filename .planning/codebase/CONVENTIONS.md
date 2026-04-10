# Coding Conventions

**Analysis Date:** 2026-04-10

## Naming Patterns

**Files:**
- Lowercase with underscores for script files: `load_items.py`, `generate_variants.py`, `search.py`
- Core modules follow same pattern: `settings.py`, `connections.py`
- `__init__.py` used for package initialization (currently empty in `/core` and `/scripts`)

**Functions:**
- Lowercase with underscores: `normalize_category()`, `parse_meal_types()`, `get_neo4j_driver()`
- Descriptive names indicating action: `load_dynamodb_items()`, `embed_query()`, `rank_platters()`
- Helper functions prefixed with underscore: `_safe_float()`, `_count_rows()`, `_str()`, `_float_or_none()`
- Context manager functions use `snake_case`: `neo4j_session()`, `build_networkx_graph()`

**Variables:**
- Lowercase with underscores: `category_map`, `node_to_community`, `embedding_model`
- Constants in UPPERCASE: `MAX_RETRIES`, `BATCH_SIZE`, `EMBEDDING_DIM`, `QDRANT_SCORE_THRESHOLD`
- Dictionary/mapping suffixes: `category_map`, `community_name_map`, `by_category`
- Prefixes for private/internal: Single underscore for module-level private state (`_neo4j_driver`, `_qdrant_client`)

**Types:**
- Union types use pipe syntax: `str | None`, `dict[str, str]`
- Imported type hints: `list[dict[str, Any]]`, `Generator[Session, None, None]`
- Dict keys are often string tuples in payloads: `{"canonical_id": str, "variant_ids": list}`
- Return type hints on all functions: `-> list[PlatterResult]`, `-> None`

## Code Style

**Formatting:**
- No explicit formatter detected (no `.prettierrc`, `black` config, or `ruff` rules found)
- Code uses consistent 4-space indentation
- String quotes: Double quotes preferred (`"string"`)
- Line length not strictly enforced but generally under 100 characters
- Imports organized by sections (stdlib, third-party, local)

**Linting:**
- No `.eslintrc`, `.pylintrc`, or `pyproject.toml` found — no enforced linting
- Code follows general PEP 8 conventions informally

## Import Organization

**Order:**
1. Standard library: `import os`, `import sys`, `import json`, `import csv`, `import logging`, `from pathlib import Path`, `from contextlib import contextmanager`, `from typing import ...`
2. Third-party: `from neo4j import ...`, `from qdrant_client import ...`, `from openai import ...`, `from dotenv import load_dotenv`, `import boto3`, `import networkx as nx`, `from graspologic.partition import ...`
3. Local: `from core.connections import ...`, `from core.settings import ...`

**Path Aliases:**
- No path aliases detected; all local imports use full relative paths: `from core.settings import ...`, `from scripts.search import search_platters`

## Error Handling

**Patterns:**
- Catch specific exceptions, not broad `Exception` alone:
  - `except (json.JSONDecodeError, KeyError)` in `/scripts/load_items.py:88-93`
  - `except (ValueError, TypeError)` in `/scripts/load_items.py:144-146`
  - `except (BotoCoreError, ClientError)` in `/scripts/load_platters.py:166-168`
- Retry logic with exponential backoff concept:
  - `MAX_RETRIES = 3`, `RETRY_DELAY = 2.0` constants defined at module level
  - Loop with attempt counter: `for attempt in range(1, MAX_RETRIES + 1)` in `/scripts/generate_variants.py:156` and `/scripts/index_communities.py:99`
  - Re-raise after max retries: `raise RuntimeError(...)` in `/scripts/index_communities.py:110`
- Graceful fallback on parse failure:
  - Return empty list or None on error: `return []` in `/scripts/generate_variants.py:174-175`
  - Return default/empty value: `return None` in `/scripts/index_communities.py:140`
- Interactive CLI wraps in try/except for cleanup:
  - `try/except (KeyboardInterrupt, EOFError)` with `finally: close_connections()` in `/scripts/search.py:228-247`

## Logging

**Framework:** `logging` (Python standard library)

**Patterns:**
- Module-level logger: `log = logging.getLogger(__name__)` in every script
- Basic config at module start: `logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")`
- Log levels used:
  - `log.info()` for progress and milestones: `"Loaded %d items"`, `"Wrote %d edges"`, `"Done."`
  - `log.warning()` for recoverable issues: `"LLM attempt %d/%d failed"`, `"Invalid summary_json for %s — skipping"`
  - `log.error()` for failures requiring attention: `"DynamoDB CSV not found"`, `"Failed to generate summary"`
- Format strings with context: `"Category %-20s | %d canonical | %d candidates"`
- Parameterized logging: `log.info("  source=%-10s  count=%d", record["source"], record["cnt"])`
- No structured logging (JSON); plain text only

## Comments

**When to Comment:**
- Module docstrings required for all scripts: describe purpose, input data, output, usage example
  - Example: `/scripts/load_items.py:1-5` shows Usage pattern with Python -m invocation
  - Example: `/scripts/search.py:1-15` includes programmatic usage example
- Section comments to organize code: `# ---------------------------------------------------------------------------` dividers separate logical sections
- No inline comments for obvious code
- Complex logic documented with docstrings

**JSDoc/TSDoc:**
- Functions use docstrings (Python style, not TypeScript):
  - One-line for simple: `"""Return a singleton Neo4j driver."""`
  - Multi-line for complex: Shows parameter behavior in `/scripts/load_items.py:78-85` (parse_meal_types)
  - Return type documented: `"""...: Returns list of {community_id, name, score, members, variant_names}."""` in `/scripts/search.py:76-80`

## Function Design

**Size:**
- Small utility functions: 5-10 lines (e.g., `normalize_category()`, `_safe_float()`)
- Moderate functions: 20-40 lines (e.g., `load_dynamodb_items()`, `parse_platter()`)
- Larger functions: 50-80 lines when handling complex orchestration (e.g., `search_platters()`, `main()` entrypoints)
- No function exceeds 100 lines

**Parameters:**
- Functions take specific parameters, not \*args or \*\*kwargs
- Session objects passed as first parameter to functions operating on Neo4j: `def fetch_dynamodb_items(session)`, `def write_communities_to_neo4j(session, ...)`
- Query parameters named explicitly: `community_ids`, `community_name_map`, `query_vector`
- LLM clients passed when needed: `def call_llm_for_variants(client: OpenAI, ...)`
- Dataclass arguments used for complex returns: `PlatterResult` in `/scripts/search.py:42-56`

**Return Values:**
- Functions return specific types, not tuples without naming: `-> list[PlatterResult]`, `-> dict[str, str]`
- None used for void operations: `-> None`
- Empty containers returned on failure: `return []`, `return {}` (not None)
- Nullable returns explicitly typed: `-> str | None`, `-> float | None`

## Module Design

**Exports:**
- Scripts have `main()` function as public entry point
- Utility functions exported without prefix; no `__all__` used
- Private module state uses underscore prefix: `_neo4j_driver`, `_qdrant_client` in `/core/connections.py`
- Constants defined at module level for reuse across functions

**Barrel Files:**
- No barrel files (no index re-exports)
- Direct imports from modules: `from core.connections import ...`, `from scripts.search import search_platters`
- `/core/__init__.py` and `/scripts/__init__.py` are empty

---

*Convention analysis: 2026-04-10*
