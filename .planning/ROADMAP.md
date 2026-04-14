# Roadmap: SearchPOC — Item-Based Platter Search

## Overview

Three phases to validate the community-based platter ranking concept: stand up the isolated Neo4j AuraDB infrastructure, run the full ETL pipeline end-to-end, then validate search quality against real data and document the result for stakeholder handoff.

## Phases

- [ ] **Phase 1: Infrastructure** - Provision AuraDB and configure all credentials so scripts can connect
- [ ] **Phase 2: Pipeline Execution** - Run ETL Steps 1–8 to build the complete Neo4j + Qdrant graph
- [ ] **Phase 3: Search Validation** - Confirm search returns sensible results and document POC outcome

## Phase Details

### Phase 1: Infrastructure
**Goal**: All services are reachable and credentials are in place so ETL scripts can run without connection errors
**Depends on**: Nothing (first phase)
**Requirements**: INFRA-01, INFRA-02, INFRA-03
**Success Criteria** (what must be TRUE):
  1. `python -m scripts.load_items --dry-run` (or equivalent connection check) exits without Neo4j auth or connection errors
  2. Qdrant client connects and can list collections without error
  3. `boto3.client('dynamodb').scan(TableName='DefaultPlattersTable', Limit=1)` returns at least one record
  4. `.env` file contains AuraDB URI, Qdrant host, OpenAI key, and AWS credentials with no placeholder values
**Plans**: TBD

### Phase 2: Pipeline Execution
**Goal**: All ETL steps complete successfully and Neo4j + Qdrant contain the full graph required for search
**Depends on**: Phase 1
**Requirements**: PIPE-01, PIPE-02, PIPE-03, PIPE-04, PIPE-05
**Success Criteria** (what must be TRUE):
  1. Neo4j contains ~260 canonical Item nodes and ~700 alias Item nodes after Steps 1–2
  2. VARIANT_OF edges exist linking canonical items to their aliases (Step 3)
  3. Platter nodes with CONTAINS edges to items are present in Neo4j (Step 4)
  4. Community nodes exist with MEMBER_OF and HAS_COMMUNITY edges pre-computed (Steps 5–6)
  5. Qdrant collection `item_search_communities` is populated with community embeddings (Steps 7–8)
**Plans**: TBD

### Phase 3: Search Validation
**Goal**: `search_platters()` returns plausible ranked platters for real dish queries, validating the community-matching concept
**Depends on**: Phase 2
**Requirements**: SRCH-01, SRCH-02, SRCH-03, SRCH-04
**Success Criteria** (what must be TRUE):
  1. `search_platters("Chicken Fried Pieces")` returns at least one platter with a non-zero coverage ratio
  2. `search_platters("Dal Makhani, Garlic Naan")` returns platters ranked by how many of the two communities they cover
  3. Coverage ratios for returned platters are plausible (matched_communities ≤ total_communities, ratio > 0)
  4. Querying an alias dish name (Supabase-style, e.g. a regional spelling) returns the same top community as its canonical equivalent
**Plans**: TBD

## Progress

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Infrastructure | 0/? | Not started | - |
| 2. Pipeline Execution | 0/? | Not started | - |
| 3. Search Validation | 0/? | Not started | - |
