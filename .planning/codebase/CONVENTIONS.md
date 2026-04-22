# Code Conventions

**Analysis Date:** 2026-04-15

## Language & Style

- **Python** with modern syntax: `X | None` unions, `list[str]`, `dict[str, Any]` (PEP 585/604)
- **Type hints** on all public function signatures (per CLAUDE.md requirement)
- **Docstrings:** one-liner for simple functions, full multi-line for complex ones (e.g. `rank_platters()`)
- **No formatter configured** — style is hand-enforced, trends toward PEP 8
- **Line length:** soft ~100 chars; no strict limit
- **Imports:** grouped — stdlib, third-party, local — with blank lines between groups

Example import block ([scripts/search.py:17](scripts/search.py#L17)):
```python
import json
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field

from openai import OpenAI
from qdrant_client.models import FieldCondition, Filter, MatchAny

from core.categories import build_category_family_counts, category_family
from core.connections import close_connections, get_qdrant_client, neo4j_session
from core.settings import EMBEDDING_MODEL, OPENAI_API_KEY
```

## Naming

- **Files / modules:** `snake_case.py`
- **Functions:** `snake_case` — prefixes signal intent:
  - `fetch_*`, `scan_*`, `query_*` — I/O bound reads
  - `build_*`, `compute_*` — pure transformation
  - `load_*`, `index_*`, `generate_*` — pipeline steps (write to sinks)
  - `is_*` — boolean predicates (e.g. `is_known_category` in `core/categories.py`)
  - `find_*` — threshold-gated lookups returning `None` on miss (e.g. `find_best_community`, `find_closest_in_platter`)
  - `_helper()` — private/internal
- **Constants:** `SCREAMING_SNAKE_CASE`, module-level
- **Cypher queries:** module-level `SCREAMING_SNAKE_CASE` string constants (e.g. `RANK_PLATTERS_QUERY`, `FETCH_VARIANT_OF_EDGES`)
- **Data classes:** `PascalCase` (e.g. `PlatterResult`)
- **Variables:** `snake_case`; domain-specific shorthand accepted (`rec`, `hit`, `vec`)
- **No custom exception classes** — codebase uses built-ins only

## Module Structure

Typical script layout:
1. Module docstring (purpose + usage examples)
2. `import` block
3. `logging.basicConfig(...)` + `log = logging.getLogger(__name__)`
4. Module-level constants (batch sizes, weights, thresholds)
5. Dataclass definitions (if any)
6. Cypher query constants
7. Functions, grouped by pipeline step with `# ---` separator comments
8. `main()` function
9. `if __name__ == "__main__": main()` block

Scripts close connections in `finally`:
```python
try:
    main()
finally:
    close_connections()
```

## Error Handling

- **Fail-fast:** no broad `except Exception` — exceptions propagate and halt the pipeline (note: `scripts/eval.py` catches `Exception` intentionally — captured in result dict, not silently swallowed)
- **LLM API retries:** exponential backoff (`MAX_RETRIES=3`, `RETRY_DELAY=2.0`), each retry logged with context
- **Optional results:** functions like `find_best_community()` return `None` below threshold rather than raising
- **Missing env vars:** `os.environ["KEY"]` (not `.get()`) — fails loudly at import if any required key is missing
- **CSV parsing:** bad rows logged and skipped; processing continues

## Logging

Every module:
```python
import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)
```
- **Level:** `INFO` default everywhere
- **No timestamps** in format string (assumes aggregator adds them)
- **What to log:** input counts, step boundaries, result counts, error context
- **`log.info("Embedding %d items: %s", len(items), items)`** — uses `%`-style, not f-strings

## Function Design

- **Flat over nested:** flat modular functions preferred; self-nesting (function-inside-function) only for intentional closure/encapsulation
- **Narrow responsibility:** one function = one step of the pipeline
- **Dataclasses** (`@dataclass`) for structured results with many fields (see `PlatterResult`, 30+ fields)
- **Default factories** (`field(default_factory=list)`, `field(default_factory=dict)`) for mutable defaults

## Neo4j Query Conventions

- Cypher strings defined as module-level constants in UPPER_SNAKE_CASE
- `MERGE` over `CREATE` — all writes idempotent
- Batched writes with `UNWIND $rows AS row` pattern, 500-row batches
- Session lifecycle via `with neo4j_session() as session:` context manager
- Parameters passed as dict, never string-interpolated (except trusted internal identifiers)

## Configuration Access

- Env vars loaded once in `core/settings.py`, imported by name elsewhere
- No runtime re-reading of `.env`
- Constants mixed into `core/settings.py` (not a separate `constants.py`)

## Comment Philosophy

- Minimal comments; code should read self-explanatory
- Commented-out alternative implementations are preserved with rationale when the tradeoff is non-obvious (e.g. broad vector fallback at `scripts/search.py:568–575`)
- Section separators used liberally in large files:
  ```python
  # ---------------------------------------------------------------------------
  # Step 2: Per-item Qdrant top-1 community lookup
  # ---------------------------------------------------------------------------
  ```
- Inline comments only for non-obvious invariants (magic numbers, hidden constraints)
- No TODOs left in committed code (per CLAUDE.md)

## Magic Numbers

Named constants preferred over inline literals:
- `TOP_N_RESULTS = 3`
- `CANDIDATE_POOL_SIZE = 15`
- `COMMUNITY_WEIGHT = 0.7`, `SKELETON_WEIGHT = 0.3`
- `SUGGESTION_THRESHOLD = 0.60`
- `QDRANT_SCORE_THRESHOLD = 0.35`

Some ETL scripts still have inline batch sizes (e.g. `500`, `50`) — see CONCERNS.md.

---
*Conventions analysis: 2026-04-15*
