# Testing Patterns

**Analysis Date:** 2026-04-10

## Test Framework

**Runner:**
- Not detected — no testing framework installed or configured
- No `pytest.ini`, `setup.cfg`, `pyproject.toml`, or test requirements found
- `requirements.txt` contains only runtime dependencies: neo4j, qdrant-client, openai, graspologic, networkx, boto3, python-dotenv

**Assertion Library:**
- Not applicable — no test framework in use

**Run Commands:**
- Manual testing via `python -m scripts.<script_name>` (scripts are designed to run standalone)
- No automated test suite exists

## Test File Organization

**Location:**
- No test files present in codebase
- Scripts are designed for direct execution and data validation via logging

**Naming:**
- Not applicable

**Structure:**
- Not applicable

## Test Structure

**Suite Organization:**
- Not applicable — no tests written

**Patterns:**
- Not applicable

## Mocking

**Framework:**
- Not applicable — no mocking framework in use

**Patterns:**
- Not applicable

**What to Mock:**
- Not applicable

**What NOT to Mock:**
- Not applicable

## Fixtures and Factories

**Test Data:**
- Not applicable — no test fixtures exist
- Production data comes from CSV files: `DYNAMODB_CSV`, `SUPABASE_CSV` in `/core/settings.py`
- Data validation occurs in parsing functions: `load_dynamodb_items()`, `load_supabase_items()` in `/scripts/load_items.py`

**Location:**
- Not applicable

## Coverage

**Requirements:**
- Not enforced — no coverage tools configured

**View Coverage:**
- Not applicable

## Test Types

**Unit Tests:**
- Not implemented
- Individual parsing functions (`parse_meal_types()`, `parse_platter()`) could be unit tested but are not

**Integration Tests:**
- Not implemented
- Scripts perform integration testing manually by running against Neo4j, Qdrant, DynamoDB, and OpenAI
- Verification functions embedded in scripts: `verify(session)` in `/scripts/load_items.py:274-279`, `/scripts/load_platters.py:141-144`, `/scripts/build_community_edges.py:28-33`

**E2E Tests:**
- Not implemented
- Manual end-to-end testing via interactive search: `/scripts/search.py:224-251` contains interactive CLI
- Data validation is inline: CSV parsing with error logging, LLM batch processing with retry logic

## Current Testing Approach

**Inline Verification:**
- Each script includes `verify()` function that queries Neo4j after writes
- Verification logs counts of created nodes/edges to confirm success
- Example: `/scripts/load_items.py:274-279` prints item counts by source
- Example: `/scripts/load_platters.py:141-144` reports Platter nodes and CONTAINS edges

**Error Handling as Testing:**
- Retry logic with logging serves as a form of error detection
- Example: `/scripts/generate_variants.py:156-181` retries LLM calls 3 times, logs each attempt
- Example: `/scripts/index_communities.py:99-110` retries embeddings and raises on max attempts
- CSV parsing catches exceptions: `/scripts/load_items.py:88-93` catches JSON decode errors

**Logging-Based Validation:**
- Progress logs indicate expected vs actual counts
- Example: `/scripts/load_items.py:134` logs raw rows vs unique items
- Example: `/scripts/generate_variants.py:232-237` logs category summaries and candidate counts
- Example: `/scripts/search.py:191-195` logs matched communities

## Validation Patterns

**Data Parsing Validation:**
```python
# Type coercion with fallback (from load_items.py:143-147)
def _safe_float(val: str | None) -> float | None:
    try:
        return float(val) if val else None
    except (ValueError, TypeError):
        return None
```

**Conditional Processing:**
```python
# Check for required fields before processing (from load_items.py:109-110)
name = row["itemName"].strip()
if not name:
    continue
```

**JSON Parsing with Error Recovery:**
```python
# Graceful fallback on parse failure (from load_items.py:88-95)
try:
    parsed = json.loads(raw)
    if isinstance(parsed, list):
        return [item["S"] for item in parsed if isinstance(item, dict) and "S" in item]
except (json.JSONDecodeError, KeyError):
    pass
return [raw.strip()]  # Fallback
```

**LLM Response Validation:**
```python
# Check for list vs dict response shapes (from generate_variants.py:168-175)
parsed = json.loads(raw)
if isinstance(parsed, dict):
    for val in parsed.values():
        if isinstance(val, list):
            return val
    return []
return parsed
```

**Database State Verification:**
```python
# Query and log results (from detect_communities.py:169-180)
result = session.run(
    """
    MATCH (c:Community)
    RETURN c.id, c.member_count
    ORDER BY c.member_count DESC
    LIMIT 10
    """
)
log.info("Top 10 communities by member count:")
for rec in result:
    log.info("  %-15s  members=%d", rec["c.id"], rec["c.member_count"])
```

## Future Testing Recommendations

**What Should Be Tested:**
- CSV parsing logic: `parse_meal_types()`, `parse_platter()` functions
- Embedding text generation: `build_embedding_text()` in `/scripts/index_communities.py`
- Community data fetching and aggregation: `fetch_community_data()` in `/scripts/generate_summaries.py`
- Graph building from Neo4j edges: `build_networkx_graph()` in `/scripts/detect_communities.py`

**Testing Priority:**
- High: Parsing functions (catch malformed data early)
- High: LLM prompt engineering (validate output format consistency)
- Medium: Graph algorithms (Leiden community detection results)
- Low: Database connection code (assume Neo4j/Qdrant are stable)

---

*Testing analysis: 2026-04-10*
