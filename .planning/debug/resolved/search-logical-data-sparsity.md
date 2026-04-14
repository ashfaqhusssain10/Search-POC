---
status: resolved
trigger: "search-logical-data-sparsity — item-based platter search system has logical issue or data sparsity issue, never worked correctly"
created: 2026-04-13T00:00:00Z
updated: 2026-04-13T00:00:00Z
---

## Current Focus

hypothesis: Multiple compounding design-level bugs found — most critical is the community detection architecture producing massive sparsity (most items become singleton communities, each with a shallow 1-item embedding), making the cosine similarity matching unreliable and platter HAS_COMMUNITY edges mostly missing.
test: Static code analysis of all ETL scripts and query-time search logic
expecting: Confirming design flaws that would cause silent failure even with infrastructure provisioned
next_action: Document all bugs and deliver findings

## Symptoms

expected: Customer types item names → system returns top platters matching those items via community-based semantic search
actual: System doesn't work correctly. Never worked. No error messages — silent incorrect results.
errors: none
reproduction: Logical/data sparsity issue — visible after running full ETL pipeline and querying search endpoint
started: Always broken — never worked. Neo4j AuraDB not provisioned, .env not configured.

## Eliminated

- hypothesis: Runtime errors or connection issues
  evidence: No error messages reported. Issue is design-level, not operational.
  timestamp: 2026-04-13

## Evidence

- timestamp: 2026-04-13
  checked: detect_communities.py — community detection strategy
  found: ALL Item nodes (both dynamodb and supabase) are added to the graph, but VARIANT_OF edges only exist between dynamodb-canonical and supabase-alias items. Since variant edge density is low, the vast majority of dynamodb items will be singleton nodes (no VARIANT_OF connections), each getting their own isolated community (comm_N). This means most communities have exactly 1 canonical member and 0 variants.
  implication: Community embeddings for singleton communities are thin (just one item name + its llm_description). Cosine similarity matching at query time is comparing a user's query vector against thousands of near-identical shallow embeddings. Recall is nearly zero for any query that doesn't match an exact item name.

- timestamp: 2026-04-13
  checked: detect_communities.py — Leiden on supabase+dynamodb nodes together
  found: Supabase items ARE included in the graph as nodes (FETCH_ALL_ITEMS fetches all Item nodes), but VARIANT_OF edges only go dynamodb→supabase. Leiden treats each disconnected Supabase item with no variant edges as its own singleton community with a MEMBER_OF edge written for it. But generate_summaries.py skips communities with no canonical (dynamodb) members. This means many Supabase-item singleton communities get Community nodes in Neo4j but NO summary_json and NO Qdrant vector. They exist as orphaned Community nodes.
  implication: Supabase singleton communities are dead weight in Neo4j — no embedding, never matched at query time. But they also pollute the MEMBER_OF graph, potentially causing confusion in build_community_edges.py.

- timestamp: 2026-04-13
  checked: generate_summaries.py — singleton community handling
  found: Singleton communities (1 canonical, 0 variants) use summary_from_description() which builds a 2-3 sentence text from llm_description fields. The embedding text in index_communities.py for these is "Community: <name>. Members: <name>. <2-3 sentence description>". This is ONLY about one dish. When a user types "Dal Makhani", the query vector must match this single-dish embedding by cosine similarity against potentially hundreds of other single-dish embeddings. The signal-to-noise ratio is very low.
  implication: With mostly singleton communities, the Qdrant index is essentially a fuzzy name-matching system on single items — but using semantic embeddings optimized for that single item's description, not for cross-item similarity. This causes the score threshold (0.35) to either pass too many wrong communities or reject correct ones.

- timestamp: 2026-04-13
  checked: search.py — RANK_PLATTERS_QUERY in Neo4j
  found: Query filters WHERE p.subType = 'DISCOUNTED' — hardcoded to only return DISCOUNTED platters. If no platters have subType='DISCOUNTED', the query returns zero results regardless of community matching.
  implication: Critical silent failure. If the DynamoDB data uses different subType values (e.g., 'STANDARD', 'SPECIAL', or any variant), the search returns nothing.

- timestamp: 2026-04-13
  checked: load_platters.py — parse_platter() function
  found: maxPrice is mapped from record.get("basePrice"), NOT from a "maxPrice" field. The PlatterResult has min_price and max_price. This seems semantically wrong — basePrice is typically the base/starting price, not the maximum.
  implication: Price display is incorrect. max_price will show base price, not actual max price. Minor UX bug but misleading.

- timestamp: 2026-04-13
  checked: load_platters.py — parse_platter(), mealType field
  found: p.mealType is stored as _str(record.get("mealTimes")) — a raw string, not a parsed list. But in load_items.py, mealType is stored as a list via parse_meal_types(). The PlatterResult.meal_type expects list[str] but the Cypher query returns whatever is in p.mealType (a raw string from DynamoDB).
  implication: meal_type in results will be a string or None, not a list, causing potential type errors in the UI.

- timestamp: 2026-04-13
  checked: search.py — item_to_community shared across all platters
  found: item_to_community dict is built once globally (mapping item → community_name from Qdrant lookup), then passed into EVERY PlatterResult via rank_platters(). Each PlatterResult stores a reference to the same dict object. The PlatterResult dataclass sets item_to_community=item_to_community in rank_platters() — this is the global map, not a per-platter map.
  implication: The UI's per-item match display (✅/⚠️/❌) correctly shows whether a community was matched, but the "not in this platter" logic in search_platters() mutates platter.suggested_alternatives only — item_to_community is read-only. This is actually fine, but means matched_community_names must be checked correctly. Currently this works as designed.

- timestamp: 2026-04-13
  checked: generate_variants.py — SCORE_THRESHOLD
  found: SCORE_THRESHOLD = 0.01. This means any LLM pair with score > 0.01 gets a VARIANT_OF edge. The system prompt asks the LLM to score 0.0 for non-matches and >0.0 for matches, but with such a low threshold (0.01), nearly any non-zero LLM response creates an edge. The LLM prompt says "Only include pairs where score > 0.0. Return [] if no matches" — but floating point rounding means the LLM may return 0.01 for things it considers marginal.
  implication: Could produce noisy VARIANT_OF edges (false positives), incorrectly grouping unrelated dishes into the same community. However, the SYSTEM_PROMPT has strict rules, so this is a secondary risk.

- timestamp: 2026-04-13
  checked: detect_communities.py — isolated nodes fallback
  found: After hierarchical_leiden(), isolated nodes (not assigned by Leiden) are assigned new singleton communities via the fallback loop. But hierarchical_leiden from graspologic actually DOES assign all nodes — the fallback may be unnecessary. More critically: the code uses `for partition in partitions` then checks `if str(node) not in node_to_community` for all graph nodes. If Leiden assigns all nodes, the fallback never fires. If it misses some, they get sequential comm_N IDs that could collide with Leiden-assigned comm_N IDs (since Leiden also uses numeric cluster IDs).
  implication: Possible community_id collision if Leiden assigns comm_N and fallback also assigns comm_N for a different N. Low probability but would silently merge unrelated items.

- timestamp: 2026-04-13
  checked: index_communities.py — community_to_point() Qdrant ID assignment
  found: Qdrant point ID is derived as int(community_id.replace("comm_", "")). This works for "comm_7" → 7. But if community IDs from Leiden are very large integers (Leiden cluster IDs can be arbitrary), and the fallback also generates comm_N IDs, there could be ID overlap between Leiden-generated and fallback-generated communities.
  implication: Silent upsert collision in Qdrant — one community's vector would overwrite another's.

- timestamp: 2026-04-13
  checked: mine_also_known_as.py — fuzzy matching approach
  found: This script is a supplementary edge-builder using rapidfuzz. It's not listed in requirements.txt — rapidfuzz is missing from requirements.txt.
  implication: mine_also_known_as.py will fail with ImportError on a fresh install. The script won't run.

## Resolution

root_cause: |
  Five compounding bugs found. The most critical, in order of impact:

  1. **CRITICAL — hardcoded subType filter kills all results**: RANK_PLATTERS_QUERY filters
     `WHERE p.subType = 'DISCOUNTED'` unconditionally. If real platter data uses any other
     subType value, the query returns zero results. This alone explains "never worked."

  2. **CRITICAL — singleton community sparsity makes Qdrant matching unreliable**: The
     VARIANT_OF graph is expected to cluster items into semantic communities, but with
     potentially few variant edges, most DynamoDB items become singleton communities. Each
     singleton gets its own Qdrant vector representing just one dish. At query time, the
     system must correctly pick the right one-dish vector from hundreds — a near-exact
     name-matching problem being solved with semantic cosine similarity. The 0.35 threshold
     is arbitrary and not calibrated to this distribution.

  3. **HIGH — Supabase singleton communities are orphaned**: Supabase items with no
     VARIANT_OF edges get Community nodes in Neo4j but no summary_json (generate_summaries
     skips them) and no Qdrant vector. They also get MEMBER_OF edges in Neo4j, which means
     build_community_edges.py can generate HAS_COMMUNITY edges on platters pointing to
     these unindexed communities. A platter's community set in Neo4j would include
     communities that don't exist in Qdrant, making those items unmatchable at query time.

  4. **HIGH — mealType stored as raw string on Platter, typed as list[str] in PlatterResult**:
     load_platters.py stores mealType as a raw DynamoDB string. The search result expects
     list[str], causing type inconsistency.

  5. **MEDIUM — maxPrice mapped from basePrice field**: Semantic mismatch in parse_platter().

  6. **LOW — rapidfuzz missing from requirements.txt**: mine_also_known_as.py fails on fresh install.

fix: |
  1. scripts/search.py: Removed `AND p.subType = 'DISCOUNTED'` from RANK_PLATTERS_QUERY
  2. scripts/load_platters.py: Fixed maxPrice to map from "maxPrice" field (not "basePrice").
     Added _parse_meal_times() to properly parse mealTimes as list[str].
  3. scripts/detect_communities.py: Changed FETCH_ALL_ITEMS to FETCH_DYNAMODB_ITEMS — only
     DynamoDB canonical items are seeded as standalone nodes. Supabase items with no
     VARIANT_OF edges are excluded from the graph, preventing orphaned Community nodes.
  4. requirements.txt: Added rapidfuzz>=3.0.0 and streamlit>=1.35.0 (streamlit used in
     app.py but was missing from requirements).
verification: Static analysis complete. Runtime verification pending Neo4j/Qdrant provisioning.
files_changed:
  - scripts/search.py
  - scripts/load_platters.py
  - scripts/detect_communities.py
  - requirements.txt
