# Requirements: SearchPOC — Item-Based Platter Search

**Defined:** 2026-04-10
**Core Value:** A customer can type any dish name and get back the most relevant platters — even when the dish name doesn't exactly match the platter catalog.

## v1 Requirements

### Infrastructure

- [ ] **INFRA-01**: Neo4j AuraDB instance provisioned and connection string available
- [ ] **INFRA-02**: `.env` file configured with AuraDB URI, Qdrant host, OpenAI key, AWS region
- [ ] **INFRA-03**: AWS DynamoDB access verified (boto3 can scan `DefaultPlattersTable`)

### Pipeline Execution

- [ ] **PIPE-01**: ETL Steps 1–2 complete — Item nodes in Neo4j (~260 canonical + ~700 Supabase aliases)
- [ ] **PIPE-02**: ETL Step 3 complete — VARIANT_OF edges generated via LLM
- [ ] **PIPE-03**: ETL Step 4 complete — Platter nodes + CONTAINS edges loaded from DynamoDB
- [ ] **PIPE-04**: ETL Steps 5–6 complete — Community nodes, MEMBER_OF edges, HAS_COMMUNITY edges
- [ ] **PIPE-05**: ETL Steps 7–8 complete — Community summaries generated, embedded, indexed in Qdrant

### Search Validation

- [ ] **SRCH-01**: `search_platters()` returns results for a non-veg dish query (e.g. "Chicken Fried Pieces")
- [ ] **SRCH-02**: `search_platters()` returns results for a multi-dish query (e.g. "Dal Makhani, Garlic Naan")
- [ ] **SRCH-03**: Coverage ratios are plausible (matched/total communities makes sense per platter)
- [ ] **SRCH-04**: Alias dish names (Supabase-style) return same communities as canonical names

## v2 Requirements

### Hardening

- **HARD-01**: Incremental ETL (re-run only changed records)
- **HARD-02**: REST API wrapper around `search_platters()`
- **HARD-03**: Observability — query latency, community hit distribution logging

## Out of Scope

| Feature | Reason |
|---------|--------|
| Web UI | CLI sufficient to validate concept |
| Production deployment | POC only — validate first, harden later |
| Integration with Elphie graph | Fully separate instance by design |
| Real-time DynamoDB sync | Manual re-run acceptable for POC scale |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| INFRA-01 | Phase 1 | Pending |
| INFRA-02 | Phase 1 | Pending |
| INFRA-03 | Phase 1 | Pending |
| PIPE-01 | Phase 2 | Pending |
| PIPE-02 | Phase 2 | Pending |
| PIPE-03 | Phase 2 | Pending |
| PIPE-04 | Phase 2 | Pending |
| PIPE-05 | Phase 2 | Pending |
| SRCH-01 | Phase 3 | Pending |
| SRCH-02 | Phase 3 | Pending |
| SRCH-03 | Phase 3 | Pending |
| SRCH-04 | Phase 3 | Pending |

**Coverage:**
- v1 requirements: 12 total
- Mapped to phases: 12
- Unmapped: 0 ✓

---
*Requirements defined: 2026-04-10*
*Last updated: 2026-04-10 after initial definition*
