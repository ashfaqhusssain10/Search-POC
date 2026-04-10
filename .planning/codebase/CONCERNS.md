# Codebase Concerns

**Analysis Date:** 2026-04-10

## Tech Debt

**Global singleton connection management:**
- Issue: Global module-level variables (`_neo4j_driver`, `_qdrant_client`) in `core/connections.py` manage stateful connections without proper lifecycle management or thread safety
- Files: `core/connections.py` (lines 18-19, 40-49, 52-60)
- Impact: Connection leaks in long-running processes; no automatic reconnection on failure; difficult to test in parallel; no timeout configuration
- Fix approach: Migrate to dependency injection or context managers; consider connection pooling with explicit lifecycle hooks

**LLM API error handling is brittle:**
- Issue: `generate_summaries.py` and `index_communities.py` catch broad `Exception` and retry, but lose context about transient vs permanent failures
- Files: `scripts/generate_summaries.py` (lines 118-140), `scripts/index_communities.py` (lines 99-110), `scripts/generate_variants.py` (lines 156-181)
- Impact: Retries can mask real errors (auth failures, malformed responses); silent skips mean data gaps without visibility; rate-limit detection not explicit
- Fix approach: Catch specific OpenAI exceptions; log failure reason categorically (rate-limit vs auth vs API error); return structured results with retry hints

**Hardcoded batch sizes and magic numbers throughout:**
- Issue: `BATCH_SIZE`, `EMBED_BATCH_SIZE`, `MAX_RETRIES`, `MAX_CLUSTER_SIZE`, `QDRANT_SCORE_THRESHOLD` are scattered across scripts with no central configuration
- Files: `scripts/load_items.py` (line 208), `scripts/detect_communities.py` (lines 32-33), `scripts/generate_summaries.py` (lines 29-31), `scripts/index_communities.py` (lines 36-39), `core/settings.py` (line 30)
- Impact: Changing tuning parameters requires modifying multiple files; easy to get inconsistent configs; no way to override at runtime
- Fix approach: Consolidate all tuning constants to `core/settings.py` with documented rationale

**Missing input validation in CSV parsers:**
- Issue: `load_items.py` relies on `.strip()` and basic type conversions but doesn't validate field presence or data consistency
- Files: `scripts/load_items.py` (lines 102-135, 154-175); particularly lines 109, 123-132
- Impact: Malformed CSV can silently produce incomplete node data; silent failures on missing required fields; inconsistent state between DynamoDB and Supabase records
- Fix approach: Add schema validation before loading; log skipped rows with reason; produce validation report

---

## Known Bugs

**CSV path resolution assumes relative working directory:**
- Symptoms: Scripts fail if run from a different directory
- Files: `scripts/load_items.py` (lines 287-296)
- Trigger: Running `python -m scripts.load_items` from a subdirectory or with PYTHONPATH pointing elsewhere
- Workaround: Always run from project root; use absolute paths
- Fix: Use `Path(__file__).parent.parent` consistently (already done in load_items.py); verify in __main__ block

**Qdrant point ID collision risk:**
- Symptoms: Community IDs like `comm_5` are parsed to integer 5; hash fallback for non-standard IDs can produce duplicates
- Files: `scripts/index_communities.py` (lines 131-139)
- Trigger: Two communities with different names hash to same integer; ID format changes
- Workaround: Ensure all community IDs follow `comm_N` pattern
- Fix approach: Use UUID-based point IDs or pre-allocate ID ranges per run

**VARIANT_OF edge duplication possible:**
- Symptoms: If `generate_variants.py` is run twice on same data, duplicate edges created (MERGE doesn't prevent this at Neo4j level without uniqueness constraints)
- Files: `scripts/generate_variants.py` (lines 188-193)
- Trigger: Rerun the variant generation script
- Workaround: None — requires manual cleanup in Neo4j
- Fix: Add relationship uniqueness constraint; or DELETE existing edges before regenerating

**Parse errors in meal type JSON silently fall back to raw string:**
- Symptoms: Meal type fields with malformed JSON are stored as unparseable strings
- Files: `scripts/load_items.py` (lines 78-95)
- Trigger: Unexpected JSON structure in DynamoDB itemMealType field
- Impact: Query filters on mealType may fail or exclude valid records
- Fix: Log which rows had parse failures; validate schema before write

---

## Security Considerations

**Environment variables not validated at startup:**
- Risk: Missing or invalid `NEO4J_PASSWORD`, `OPENAI_API_KEY` only discovered at first use, not at process start
- Files: `core/settings.py` (lines 10-30)
- Current mitigation: `os.environ[...]` raises KeyError if missing (immediate failure)
- Recommendations: Add explicit startup validation; log which vars are loaded (not values); implement read-only config after startup

**No rate limiting on OpenAI API calls:**
- Risk: Batch processing scripts can burst requests and trigger account-wide rate limits or overspend
- Files: `scripts/generate_summaries.py` (line 31), `scripts/index_communities.py` (lines 36-37)
- Current mitigation: Hard-coded `CONCURRENCY_DELAY` and `EMBED_BATCH_SIZE` provide implicit throttling
- Recommendations: Add explicit rate limiter with backoff; monitor token spend; use OpenAI's usage API to alert on high spend

**DynamoDB scan unprotected from large result sets:**
- Risk: `scan_table()` in `load_platters.py` loads entire table into memory without limits
- Files: `scripts/load_platters.py` (lines 39-54)
- Current mitigation: Pagination prevents timeout but not OOM
- Recommendations: Add memory guard or streaming write; implement progress indicator for large scans

**Neo4j connections not encrypted by default:**
- Risk: Credentials in `core/settings.py` used over Bolt protocol without TLS verification option
- Files: `core/connections.py` (lines 22-29)
- Current mitigation: VPC-private Neo4j in production (reference.md)
- Recommendations: For dev/test, enforce `encrypted=True` in driver config; document production TLS setup

---

## Performance Bottlenecks

**Community detection runs hierarchical Leiden on full graph in memory:**
- Problem: `detect_communities.py` builds entire NetworkX graph before partitioning; no streaming or incremental updates
- Files: `scripts/detect_communities.py` (lines 50-69, 76-108)
- Cause: graspologic expects full graph; no incremental community detection API
- Current capacity: Tested with ~5k items; likely OOMs at 100k+ items
- Improvement path: Partition by category first; use local Leiden variants; stream graph construction

**LLM summaries generated serially with network latency:**
- Problem: `generate_summaries.py` processes one community per LLM call with fixed 0.3s delay between requests
- Files: `scripts/generate_summaries.py` (lines 192-216)
- Cause: Sequential processing; fear of rate limits; no batching of LLM calls
- Current capacity: ~200 communities/minute; 10k communities takes ~50 minutes
- Improvement path: Batch summaries per call (5-10 per request); use concurrent requests with proper backoff; cache embeddings

**Qdrant upsertion happens in serial batches:**
- Problem: `index_communities.py` embeds then upsets in 100-point batches with no parallelization
- Files: `scripts/index_communities.py` (lines 155-159)
- Cause: Simple for-loop design; no async I/O
- Current capacity: ~5k points/minute; 100k points takes ~20 minutes
- Improvement path: Parallel upsert requests; batch larger (1000+); use gRPC instead of REST

**Neo4j query for platter ranking is unpaginated and unbounded:**
- Problem: `search.py` queries Neo4j with LIMIT but no query timeout or execution plan optimization
- Files: `scripts/search.py` (lines 105-126)
- Cause: Query costs grow with total platter/community count
- Current capacity: Untested at scale
- Improvement path: Add query timeout; index on HAS_COMMUNITY edges; consider materialized view pattern

---

## Fragile Areas

**LLM-based variant matching (generate_variants.py):**
- Files: `scripts/generate_variants.py` (lines 135-181)
- Why fragile: Depends on GPT-4o-mini producing valid JSON; no schema validation on response; category hints hardcoded (line 92-109); prompt changes break parsing
- Safe modification: Add response schema validation; test with sample data before full run; log all LLM inputs/outputs to debug file; version control prompt templates
- Test coverage: No unit tests for variant matching logic; no validation of JSON response shape; no tests for category filtering logic

**Community name derivation (generate_summaries.py):**
- Files: `scripts/generate_summaries.py` (lines 154-159)
- Why fragile: Uses hub items with fallback to canonical names to compute community name; empty lists cause undefined behavior
- Safe modification: Add explicit null checks; log when fallback is used; validate name is non-empty before save
- Test coverage: No tests for name generation with edge cases (no hub items, empty canonical list)

**Platter-item-community edges (build_community_edges.py):**
- Files: `scripts/build_community_edges.py` (lines 22-25)
- Why fragile: Cypher query has no error handling; assumes all edges can be materialized (no memory limits); orphaned platters silently skipped
- Safe modification: Add EXPLAIN plan check; log summary of matched/unmatched platters; verify edge counts match expected ratio
- Test coverage: No verification that all platters got edges; no test for missing items

---

## Scaling Limits

**Neo4j query performance (Item-Community-Platter traversal):**
- Current capacity: Not documented; no load test data
- Limit: Query cost grows O(platters * communities) without proper indexing
- Scaling path: Add index on Platter.id, Community.id, relationship types; use query plan analysis; implement query caching layer

**Qdrant collection size and search latency:**
- Current capacity: Untested; QDRANT_SCORE_THRESHOLD fixed at 0.35
- Limit: Vector similarity search degrades as collection grows; threshold may need tuning per dataset
- Scaling path: Implement adaptive thresholding; partition by category; monitor search latency at scale

**LLM API token budget for batch summarization:**
- Current capacity: ~200 communities sustainable with throttling
- Limit: No token budget enforcement; long-form narratives can exceed token limits
- Scaling path: Pre-calculate token counts; implement token pool; use cheaper models for summaries

**CSV file sizes for initial load:**
- Current capacity: CSV files in project root are ~80-110MB; loaded entirely into memory
- Limit: Will OOM on 1GB+ files
- Scaling path: Stream CSV parsing; batch-load to database; implement checkpoints for restart

---

## Dependencies at Risk

**graspologic.partition.hierarchical_leiden API stability:**
- Risk: Library is research-grade; no semantic versioning; API may change
- Impact: Community detection breaks silently if version constraints not enforced
- Migration plan: Pin graspologic==3.3.0 explicitly; implement alternative using NetworkX-native algorithms (greedy modularity optimization); add fallback to simple clustering

**boto3 DynamoDB client:**
- Risk: Credentials sourced from AWS environment; no explicit error recovery
- Impact: If AWS credentials expire, full scan fails mid-run
- Migration plan: Implement credential refresh logic; use STS assume-role for better control; add explicit credential validation before scan

**OpenAI API versioning:**
- Risk: `response_format={"type": "json_object"}` in `generate_variants.py` (line 164) is beta feature that may change
- Impact: LLM calls fail if API evolves
- Migration plan: Check OpenAI API changelog quarterly; implement feature detection; provide fallback JSON parsing

**neo4j driver connection stability:**
- Risk: No explicit keepalive, connection timeout, or pool size configuration
- Impact: Long-running jobs may hit connection timeouts; driver may leak connections
- Migration plan: Add explicit pool configuration (max_pool_size, acquisition_timeout); implement health checks; graceful reconnect logic

---

## Missing Critical Features

**No data audit trail or change log:**
- Problem: Scripts are re-entrant and idempotent (MERGE/UPSERT patterns) but no record of when data was last updated or what changed
- Blocks: Version tracking; rollback; compliance audits
- Recommendation: Add timestamp fields to Community, Item nodes; log all UPSERT operations with before/after hashes

**No incremental/delta processing:**
- Problem: Every run reprocesses entire dataset; no way to update just changed records
- Blocks: Efficiency at scale; real-time indexing
- Recommendation: Add modified_at timestamps; implement delta detection; skip unchanged records

**No observability of query execution:**
- Problem: `search.py` logs at INFO but no metrics on query latency, cache hits, or community match distribution
- Blocks: Performance optimization; debugging slow queries
- Recommendation: Add timing instrumentation; track platter coverage ratios; log community hit distribution

**No health checks or readiness probes:**
- Problem: No way to verify Neo4j/Qdrant are healthy before running scripts
- Blocks: Reliable automation; container orchestration
- Recommendation: Add `--healthcheck` mode to each script; verify database connectivity and schema; return exit codes

---

## Test Coverage Gaps

**No unit tests for CSV parsing logic:**
- What's not tested: `load_items.py` category normalization, meal type parsing, field validation
- Files: `scripts/load_items.py` (lines 73-95, 102-135)
- Risk: Silent failures on CSV schema changes or missing fields
- Priority: **High** — data quality issue

**No tests for LLM response parsing:**
- What's not tested: `generate_variants.py` and `generate_summaries.py` JSON response shape validation; edge cases like empty arrays, missing fields
- Files: `scripts/generate_variants.py` (lines 156-175), `scripts/generate_summaries.py` (lines 109-129)
- Risk: Malformed LLM responses cause silent data loss or crashes
- Priority: **High** — production failure mode

**No integration tests for end-to-end pipeline:**
- What's not tested: Full flow from CSV load → variant generation → community detection → Qdrant indexing
- Files: All scripts
- Risk: Breaks go undetected until runtime; difficult to debug
- Priority: **Medium** — development velocity

**No load tests for query performance:**
- What's not tested: `search.py` latency with large Neo4j graphs; Qdrant similarity search speed
- Files: `scripts/search.py`
- Risk: Performance degradation discovered in production
- Priority: **Medium** — scaling risk

**No tests for error recovery:**
- What's not tested: Behavior when external services fail (DynamoDB timeout, OpenAI rate limit, Neo4j connection drop)
- Files: All scripts
- Risk: Partial state writes; unclear recovery path; manual intervention required
- Priority: **Low** — operational burden but rare

---

*Concerns audit: 2026-04-10*
