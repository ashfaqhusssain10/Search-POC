# Elphie Infrastructure & Recommendation System — Reference Document

> **Purpose**: Complete reference for infrastructure, connection details, community detection pipeline, and recommendation flow as implemented in the Elphie chatbot POC (`GraphRAG-using-Llama-Index/`). Use this as the foundation for building the Item-Based Platter Search POC.

---

## 1. AWS Infrastructure Overview

### Account & Region
| Detail | Value |
|---|---|
| AWS Account ID | `500057333546` |
| Region | `ap-south-1` (Mumbai) |
| Environment name | `dev` |

### VPC
| Detail | Value |
|---|---|
| VPC ID | `vpc-0a48c00ee7960aeb5` |
| VPC CIDR | `10.0.0.0/16` |
| VPC Name | `CacheStack/CacheVpc` (pre-existing, not created by Elphie CDK) |
| Bastion / Deploy EC2 | `deploy_routine` at `13.127.233.50` (already in VPC, used for admin access) |

### Subnets
| Name | Type | AZ | Subnet ID | CIDR |
|---|---|---|---|---|
| PublicSubnet | Public | ap-south-1a | `subnet-08b21049452127ea4` | `10.0.2.0/24` |
| PublicSubnet | Public | ap-south-1b | `subnet-060c41cc99b3ce1f0` | `10.0.3.0/24` |
| PrivateSubnet | Private | ap-south-1a | `subnet-057c519004af82659` | `10.0.0.0/24` |
| PrivateSubnet | Private | ap-south-1b | `subnet-06c644e158e29d26a` | `10.0.1.0/24` |

All stateful services (Neo4j, Qdrant, ECS API) run in **private subnets** with NAT egress.

---

## 2. ECS Cluster & Service Discovery

### ECS Cluster
| Detail | Value |
|---|---|
| Cluster name | `elphie-dev` |
| Container insights | Enabled |
| Cloud Map namespace | `elphie.local` (private DNS) |

### Services (all Fargate, private subnets)
| Service Name | Cloud Map DNS | Port | Description |
|---|---|---|---|
| `elphie-neo4j-dev` | `neo4j.elphie.local` | 7687 (Bolt), 7474 (Browser) | Graph database |
| `elphie-qdrant-dev` | `qdrant.elphie.local` | 6333 (REST), 6334 (gRPC) | Vector database |
| `elphie-api-dev` | `elphie-api.elphie.local` | 8000 (FastAPI) | Main API service |

### ECR Repositories
| Repo | ARN |
|---|---|
| `elphie-neo4j` | `arn:aws:ecr:ap-south-1:500057333546:repository/elphie-neo4j` |
| `elphie-qdrant` | `arn:aws:ecr:ap-south-1:500057333546:repository/elphie-qdrant` |
| `elphie-api` | `arn:aws:ecr:ap-south-1:500057333546:repository/elphie-api` |
| `elphie-etl` | `arn:aws:ecr:ap-south-1:500057333546:repository/elphie-etl` |

---

## 3. Security Groups

All security groups are created by `VpcStack` (`infra/stacks/vpc_stack.py`).

| SG Name | CDK ID | Inbound Rules | Used By |
|---|---|---|---|
| `SgAlb` | Internal ALB SG | 443, 80 from `0.0.0.0/0` | Internal ALB (REST via API Gateway) |
| `SgWsAlb` | WebSocket ALB SG | 80, 443 from `0.0.0.0/0` | Public WebSocket ALB |
| `SgEcsApi` | ECS API SG | 8000 from `SgAlb`, 8000 from `SgWsAlb` | `elphie-api-dev` Fargate service |
| `SgNeo4j` | Neo4j SG | 7687/7474 from `SgEcsApi`, 7687/7474 from `SgLambda`, 7687/7474 from `10.0.0.0/16` (bastion) | `elphie-neo4j-dev` |
| `SgQdrant` | Qdrant SG | 6333/6334 from `SgEcsApi`, 6333/6334 from `SgLambda`, 6333 from `10.0.0.0/16` (bastion) | `elphie-qdrant-dev` |
| `SgRedis` | Redis SG | 6379 from `SgEcsApi` | ElastiCache Redis |
| `SgLambda` | Lambda SG | All outbound | ETL Lambda functions (VPC-attached) |

---

## 4. Load Balancers

| ALB | Type | Subnets | Purpose |
|---|---|---|---|
| Internal ALB | Internet-facing: **No** | Private | REST traffic via API Gateway VPC Link → ECS port 8000 |
| WebSocket ALB (`elphie-ws-dev`) | Internet-facing: **Yes** | Public | Direct browser WebSocket → ECS port 8000 |

> **Why two ALBs**: API Gateway HTTP API cannot upgrade HTTP → WebSocket. The public ALB handles WebSocket directly; REST goes through the internal ALB + API Gateway.

---

## 5. Data Services

### Neo4j
| Detail | Value |
|---|---|
| Internal DNS (production) | `bolt://neo4j.elphie.local:7687` |
| Local (docker-compose) | `bolt://localhost:7687` |
| Auth | `neo4j` / `Catering2026@` (prod), `neo4j` / `password` (local) |
| Storage | EFS: `elphie-neo4j-dev` (encrypted, RETAIN on delete) |
| EFS access point path | `/neo4j/data` (uid/gid 7474) |
| Heap | 512m initial, 1536m max |
| Logs | `/ecs/elphie-neo4j-dev` (CloudWatch, 1 week retention) |
| Bolt env var | `NEO4J_URI` |

### Qdrant
| Detail | Value |
|---|---|
| Internal DNS (production) | `http://qdrant.elphie.local:6333` |
| Local (docker-compose) | `localhost:6333` (REST), `localhost:6334` (gRPC) |
| Cloud connection | via `QDRANT_HOST` (URL) + `QDRANT_API_KEY` |
| Storage | EFS: `elphie-qdrant-dev` (encrypted, RETAIN on delete) |
| EFS access point path | `/qdrant/storage` (uid/gid 1000) |
| Logs | `/ecs/elphie-qdrant-dev` (CloudWatch, 1 week retention) |
| Env vars | `QDRANT_HOST`, `QDRANT_PORT` (default `6333`), `QDRANT_API_KEY` |

### Redis (ElastiCache)
| Detail | Value |
|---|---|
| Purpose | Session cache for API |
| Engine | Redis 7 |
| Local port | `6380:6379` (external:internal) |
| Container name (local) | `elphie-redis` |
| Env vars | `REDIS_HOST`, `REDIS_PORT` (default `6380` local / `6379` prod), `REDIS_URL` |

### PostgreSQL
| Detail | Value |
|---|---|
| Purpose | LangGraph state persistence (checkpointer) |
| Local port | `5433:5432` |
| Container name (local) | `elphie-postgres` |
| DB / User / Pass | `elphie` / `elphie` / `elphie_secret` |
| Env vars | `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` |

### DynamoDB
| Detail | Value |
|---|---|
| Table | Configured via `config["dynamodb_checkpoint_table"]` |
| Purpose | LangGraph checkpoint persistence |
| Access | API task role — GetItem, PutItem, UpdateItem, DeleteItem, Query, BatchGet/Write |

### S3 Buckets
| Bucket config key | Purpose |
|---|---|
| `s3_data_bucket` → `elphie-data-dev` | ETL input data, community summaries, policy files |
| `s3_backup_bucket` | Backups |
| `s3_attachments_bucket` → `elphie-attachments` | User attachment uploads |

### Redshift (Analytics Source for Community Detection)
| Detail | Value |
|---|---|
| Host | `craftmyplate-data-warehouse.cvk2ka8ootp9.ap-south-1.rds.amazonaws.com` |
| Port | `5432` |
| Database | `postgres` |
| User | `postgres` |
| Env vars | `RDS_HOST`, `RDS_PORT`, `RDS_DATABASE`, `RDS_USER` |
| Purpose | Historical order co-occurrence data → community detection input |

---

## 6. Secrets Manager (Production)

All secrets live under the `elphie/` prefix in AWS Secrets Manager.

| Secret Name | Keys |
|---|---|
| `elphie/openai-api-key` | `key` |
| `elphie/google-api-key` | `key` |
| `elphie/portkey-credentials` | `PORTKEY_API_KEY`, `PORTKEY_GEMINI_VIRTUAL_KEY`, `PORTKEY_OPENAI_VIRTUAL_KEY` |
| `elphie/cohere-api-key` | `key` |
| `elphie/sarvam-api-key` | `key` |
| `elphie/cognito-credentials` | `CMP_COGNITO_CLIENT_ID`, `CMP_COGNITO_CLIENT_SECRET` |

---

## 7. Neo4j Graph Schema

### Node Labels & Key Properties
| Label | Key Properties |
|---|---|
| `Platter` | `id`, `name`, `type` (DELIVERYBOX/MEALBOX/BBQ/BOWLS/SNACKBOX), `sub_type` (REGULAR/DISCOUNTED), `meal_type`, `event_types`, `min_price`, `max_price`, `proven_score` |
| `Variation` | `index`, `min_price`, `max_price` |
| `Item` | `id`, `name`, `type` (VEG/NON-VEG), `category`, `price`, `premium`, `trending_score`, `proven_score`, `is_veg` |
| `Category` | `name`, `id` (UUID) |
| `MealType` | `name` (breakfast/lunch/dinner/snacks) |
| `EventType` | `name` (Wedding/Birthday/Corporate/Party/etc.) |
| `Community` | `id` (e.g. `comm_0`), `member_count`, `summary_json` |

### Relationships
| From → To | Type | Properties |
|---|---|---|
| Platter → Variation | `HAS_VARIATION` | — |
| Variation → Item | `CONTAINS` | `category` (category name) |
| Variation → Category | `IN_CATEGORY` | — |
| Item → Category | `IN_CATEGORY` | — |
| Item → Community | `MEMBER_OF` | — |
| Item ↔ Item | `CO_OCCURS` | `count` (co-occurrence frequency from orders) |
| Platter → MealType | `FOR_MEAL` | — |
| Platter → EventType | `FOR_EVENT` | — |
| Platter → Category | `HAS_LIMIT` | `items_limit`, `premium_limit`, `category_order`, `is_combo`, `active` |
| Platter → MealType | `ORDERED_AT` | `count` (from Redshift sync) |
| Platter → EventType | `POPULAR_FOR` | `count` (from Redshift sync) |

### Constraints & Indexes
```cypher
CONSTRAINT item_id FOR (i:Item) REQUIRE i.id IS UNIQUE
CONSTRAINT platter_id FOR (p:Platter) REQUIRE p.id IS UNIQUE
INDEX variation_idx FOR (v:Variation) ON (v.index)
INDEX item_veg_idx FOR (i:Item) ON (i.is_veg)
INDEX item_type_idx FOR (i:Item) ON (i.type)
```

### Key Cypher Patterns
```cypher
-- Items in a platter by category
MATCH (p:Platter {id: $platter_id})-[:HAS_VARIATION]->(v:Variation)
      -[r:CONTAINS]->(i:Item)
RETURN r.category as category, i.*
ORDER BY category, i.name

-- Community membership for an item
MATCH (i:Item {name: $item_name})-[:MEMBER_OF]->(c:Community)
RETURN c.id as community_id, c.member_count

-- Co-occurrence graph edges
MATCH (a:Item)-[r:CO_OCCURS]-(b:Item)
WHERE id(a) < id(b)
RETURN a.name, b.name, r.count as weight

-- Platters by community IDs (item-search query pattern)
MATCH (p:Platter)-[:HAS_VARIATION]->(v:Variation)
      -[:CONTAINS]->(i:Item)-[:MEMBER_OF]->(c:Community)
WHERE c.id IN $community_ids
RETURN p, count(DISTINCT c.id) as matched_communities
ORDER BY matched_communities DESC
```

---

## 8. Qdrant Collections

### Collection 1: `platter_variations`
| Detail | Value |
|---|---|
| Embedding model | OpenAI `text-embedding-3-small` |
| Vector size | 1536 |
| Distance | Cosine |
| Quantization | INT8 scalar (RAM-optimized for ~200K vectors) |
| Search strategy | `group_by=platter_id`, up to 5 variations per platter |

**Payload schema per vector:**
```json
{
  "platter_id": "string",
  "platter_name": "string",
  "platter_type": "DELIVERYBOX | BBQ | MEALBOX | BOWLS | SNACKBOX",
  "platter_sub_type": "REGULAR | DISCOUNTED",
  "variation_index": 0,
  "meal_type": "lunch | dinner | breakfast | snacks",
  "veg": true,
  "max_price": 450.0,
  "min_price": 350.0,
  "items_by_category": { "Starter": [{"name": "...", "price": ..., "veg": true}] },
  "category_names": ["Starter", "Main", "Dessert"],
  "event_types": ["Wedding", "Party"],
  "event_relevance": 0.85,
  "meal_relevance": 0.90,
  "proven_score": 120.0
}
```

**Payload indexes (for filtered search):**
`platter_id` (KEYWORD), `meal_type` (KEYWORD), `event_types` (KEYWORD), `veg` (BOOL), `max_price` (FLOAT), `min_price` (FLOAT), `platter_type` (KEYWORD)

**Embedding text format:**
```
"Rajasthani Delights platter lunch, dinner wedding, party non-vegetarian
 appetizers: Samosa, Pakora mains: Dal Makhani, Paneer Butter Masala..."
```
> Meal expansion: `dinner → ["lunch", "dinner"]` for embedding (broader semantic match), but metadata filter stays canonical.

---

### Collection 2: `graphrag_communities`
| Detail | Value |
|---|---|
| Embedding model | OpenAI `text-embedding-3-small` |
| Vector size | 1536 |
| Distance | Cosine |
| Score threshold (search) | 0.35 |

**Payload schema per vector:**
```json
{
  "community_id": "comm_7",
  "name": "Premium Non-Veg Starters",
  "members": ["Chicken Tikka", "Seekh Kebab", ...],
  "hub_items": ["Chicken Tikka"],
  "top_pairings": [{"item_a": "Chicken Tikka", "item_b": "Seekh Kebab", "lift": 2.3}],
  "event_distribution": {"Wedding": 0.45, "Party": 0.35},
  "narrative": "Premium appetizers popular at weddings..."
}
```

**Embedding text format (built in `_summary_to_text()`):**
```
Community: Premium Non-Veg Starters. Members: Chicken Tikka, Seekh Kebab, ...
Hub items: Chicken Tikka. Popular for: Wedding, Party, Corporate. <narrative>
```

---

## 9. Community Detection Pipeline

**File:** `elphie/scripts/detect_communities.py`

### Algorithm
- **Library:** `graspologic` — `hierarchical_leiden()`
- **Input:** NetworkX weighted graph (items as nodes, co-occurrence as edges)
- **Parameters:**
  - `max_cluster_size=20` (max items per community)
  - `resolution=1.0` (higher = more, smaller communities)
- **Output:** `Dict[item_name → community_id]` e.g. `{"Chicken Tikka": "comm_0", ...}`

### Data Source Priority
1. **Redshift** (`RDS_HOST` env var required) — `analytics.fact_orders_unified`, min 5+ co-occurring orders, limit 10,000 pairs
2. **Neo4j `CO_OCCURS` edges** — used if Redshift not available or already loaded
3. **Platter structure fallback** — items in same variation category treated as co-occurring (weight = times they share a category across all variations)

### Storage
Communities stored in Neo4j:
```cypher
MERGE (c:Community {id: $community_id})
SET c.member_count = $count

MATCH (i:Item {name: $item_name})
MATCH (c:Community {id: $community_id})
MERGE (i)-[:MEMBER_OF]->(c)
```

### Run command
```bash
# Full pipeline (Redshift → Leiden → Neo4j)
python -m elphie.scripts.detect_communities

# Skip Redshift, use Neo4j/platter fallback
python -m elphie.scripts.detect_communities --skip-redshift

# Dry run (print communities, don't store)
python -m elphie.scripts.detect_communities --dry-run

# Also sync Redshift stats (meal type, event type, proven scores) into Neo4j
python -m elphie.scripts.detect_communities --sync-stats
```

---

## 10. Community Summary Generation & Indexing

**Files:** `elphie/scripts/generate_summaries.py` (LLM narrative generation) → `elphie/scripts/index_communities.py` (embed + store in Qdrant)

### Summary structure stored in Neo4j (`c.summary_json`):
```json
{
  "community_id": "comm_7",
  "name": "Premium Non-Veg Starters",
  "members": ["Chicken Tikka", "Seekh Kebab"],
  "hub_items": ["Chicken Tikka"],
  "top_pairings": [{"item_a": "...", "item_b": "...", "lift": 2.3}],
  "event_distribution": {"Wedding": 0.45, "Party": 0.35},
  "narrative": "<LLM-generated text>"
}
```

### Indexing flow
```
Neo4j (c.summary_json) → JSON parse → text serialization → OpenAI embed → Qdrant upsert
```

Fallback chain if Neo4j unavailable:
1. S3: `s3://elphie-data-dev/output/community_summaries.json`
2. Local: `/tmp/community_summaries.json`

### Run command
```bash
python -m elphie.scripts.index_communities
```

---

## 11. Variation Indexing Pipeline

**File:** `elphie/scripts/index_variations.py`

- Source: DynamoDB (1.87M total variations)
- Prefilter: ~200K variations kept (active, priced, complete)
- Embedding: OpenAI `text-embedding-3-small` in batches
- Storage: Qdrant `platter_variations` collection
- Memory: 4GB Fargate task (`index_vars_memory_mb: 4096`)

```bash
# Run with prefilter (recommended)
python -m elphie.scripts.index_variations --prefilter
```

---

## 12. ETL Pipeline (AWS Step Functions)

**File:** `elphie/infra/stacks/etl_stack.py`

```
Step Functions: elphie-etl-pipeline-dev
│
├── Lambda: elphie-etl-ingest-dev
│   └── DynamoDB → Neo4j graph (platters, items, categories, relationships)
│
└── Parallel after ingest:
    ├── Lambda: elphie-etl-enrich-dev
    │   └── Neo4j → community detection → generate summaries → Qdrant index
    │
    └── ECS Fargate Task: elphie-index-vars-dev
        └── DynamoDB → prefilter 200K → embed → Qdrant platter_variations
```

All ETL components use:
- **VPC**: private subnets, `SgLambda` security group
- **Service discovery**: `bolt://neo4j.elphie.local:7687`, `qdrant.elphie.local:6333`
- **S3 trigger**: New file at `s3://elphie-data-dev/policies/*.json` → auto-triggers `lambda_enrich` (embed_policies mode)

---

## 13. Connection Pool Architecture

**File:** `elphie/core/connections.py`

All pools are **singleton instances** initialized lazily on first use.

```python
from elphie.core.connections import get_neo4j_pool, get_qdrant_pool

neo4j_pool = get_neo4j_pool()    # Neo4jConnectionPool
qdrant_pool = get_qdrant_pool()  # QdrantConnectionPool

# Neo4j usage
with neo4j_pool.session() as session:
    result = session.run("MATCH (n:Platter) RETURN n LIMIT 5")

# Qdrant usage
client = qdrant_pool.get_client()  # QdrantClient
```

### Pool configuration defaults
| Pool | Setting | Default |
|---|---|---|
| Neo4j | `max_connection_pool_size` | 50 |
| Neo4j | `connection_acquisition_timeout` | 10s |
| Neo4j | `max_transaction_retry_time` | 10s |
| HTTP | `max_connections` | 100 |
| HTTP | `max_keepalive_connections` | 50 |
| HTTP | `timeout` | 30s |
| Redis | `max_connections` | 50 |
| Redis | `socket_timeout` | 5s |

### Qdrant cloud vs local detection
```python
if self.api_key and self.host.startswith("http"):
    # Cloud: QdrantClient(url=host, api_key=api_key)
else:
    # Local: QdrantClient(host=host, port=port)
```

---

## 14. Environment Variables Reference

All loaded via `.env` file at project root using `pydantic-settings`.

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | **Required** | Embeddings + LLM fallback |
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j connection URI |
| `NEO4J_USERNAME` | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | `password` | Neo4j password |
| `QDRANT_HOST` | `localhost` | Qdrant host (URL for cloud) |
| `QDRANT_PORT` | `6333` | Qdrant REST port |
| `QDRANT_API_KEY` | `None` | Qdrant cloud API key |
| `REDIS_HOST` | `localhost` | Redis host |
| `REDIS_PORT` | `6380` | Redis port |
| `REDIS_PASSWORD` | `None` | Redis password |
| `POSTGRES_HOST` | `localhost` | PostgreSQL host |
| `POSTGRES_PORT` | `5433` | PostgreSQL port |
| `GOOGLE_API_KEY` | `None` | Gemini LLM |
| `PORTKEY_API_KEY` | `None` | Portkey LLM gateway |
| `PORTKEY_GEMINI_VIRTUAL_KEY` | `None` | Portkey virtual key for Gemini |
| `PORTKEY_OPENAI_VIRTUAL_KEY` | `None` | Portkey virtual key for OpenAI |
| `SARVAM_API_KEY` | `None` | Sarvam multilingual STT |
| `FRESHCHAT_API_URL` | `None` | Freshchat escalation |
| `FRESHCHAT_API_TOKEN` | `None` | Freshchat escalation |
| `RDS_HOST` | — | Redshift host (ETL only) |
| `RDS_PORT` | `5432` | Redshift port |
| `RDS_DATABASE` | `postgres` | Redshift database |
| `RDS_USER` | `postgres` | Redshift user |
| `ELPHIE_ENV` | `development` | Environment name |
| `USE_LLM_SELECTION` | `true` | LLM platter scoring |

---

## 15. Recommendation Flow

This is the core query-time pipeline. Entry point: `GraphRAGService` (`elphie/services/graphrag_service.py`).

### Stages
| Stage | Detail Level | Community Search | Use Case |
|---|---|---|---|
| `flow0` | summary | No | Very early — only meal/event known, no budget |
| `shortlist` | summary | No | Budget known, building shortlist |
| `preview` | preview | **Yes** | User reviewing options |
| `final` | full | **Yes** | Final platter recommendation |

### Full Query Flow

```
User message
    │
    ▼
discovery_agent.py — ReAct loop
    │  Entity extraction (occasion, meal_type, guest_count, budget, veg_preference)
    │  derive_expectation_schema() → PlatterExpectationSchema (pure Python, no LLM)
    │
    ▼
recommendation_flow.py — Stage resolution
    │  Maps entity completeness → technical_stage (flow0/shortlist/preview/final)
    │
    ▼
GraphRAGService.get_recommendations_for_stage()
    │
    ├─[flow0]─► Neo4j: get_platter_summaries() → PlatterRecommendation (summary)
    │
    └─[shortlist/preview/final]─►
        │
        ├── _build_search_query()
        │   Combines: preferences + event_type + meal_type + veg + schema categories
        │   Avoids generic dilution when specific items or schema categories exist
        │
        ├─[parallel]─────────────────────────────────────────────────
        │   │                                                        │
        │   ▼                                                        ▼
        │ QdrantVariationStore.search_variations()      [preview/final only]
        │   Strict → Relaxed → Closest fallback ladder   QdrantCommunityStore.search()
        │   Returns ~25 candidate variations             Returns top 3 communities
        │   Filters: meal_type, veg, budget, event_type  Builds LLM context string
        │
        ├── CohereReranker.rerank()
        │   Reranks candidates by event/meal/veg/budget signals
        │   Returns top 15 candidates
        │
        ├── GeminiClient.select_best_platters()
        │   LLM scores candidates using consultant persona (if enabled)
        │   Scoring order: skeleton_fit > occasion_fit > meal_fit > preference_fit > scale_fit > budget
        │   Returns top_k selected candidates with llm_reasoning
        │
        └── _build_recommendation_from_candidate()
            Assembles PlatterRecommendation with full items_by_category, pricing, reasoning
```

### Qdrant Variation Search — Relaxation Ladder
```
Strict:   budget + meal_type + veg filters + score threshold 0.25
Relaxed:  remove score threshold, keep hard filters
Closest:  remove budget filter, only meal_type + veg filters
```

### Search Query Building (`_build_search_query`)
```python
# Priority order (no generic padding when specific signals exist):
1. preferences (specific items user mentioned)
2. event_type
3. meal_type + " meal"
4. veg_preference
5. schema required_categories → "platter with Salad Starter Pulao Curry Dal Dessert"
6. [only if no preferences AND no schema categories] → stage hint
   shortlist: "popular platter catering options"
   preview:   "best platter catering menu"
   final:     "best platter catering menu for final recommendation"
```

### LLM Selection — `_build_selection_prompt()` (llm_client.py)

When `use_consultant_persona: true` in `business_rules.json` AND `expectation_schema` is present:

```
STEP 1 — READ MENU EXPECTATION (schema anchor from derive_expectation_schema)
STEP 2 — SKELETON FIT (required_categories from schema)
STEP 3 — TONE FIT (style: traditional / fun / corporate)
STEP 4 — SCALE FIT (variety_level, safety_profile from schema)
STEP 5 — PREFERENCE + BUDGET FIT (tie-breakers only)
```

When toggle is `false` (default): flat persona — "You are an expert catering consultant selecting the best platters."

### `PlatterExpectationSchema` (progressive, derived per turn)
```python
@dataclass
class PlatterExpectationSchema:
    heaviness: Optional[str]       # "light" | "medium" | "full" | None
    style: str                     # "traditional" | "fun" | "corporate" | "general"
    required_categories: List[str] # from business_rules.json meal_skeletons
    excluded_categories: List[str]
    variety_level: Optional[str]   # "low" | "medium" | "high" | None
    safe_vs_experimental: str      # "safe" | "balanced" | "experimental"
    reasoning: str
```

Progressive derivation per turn:
```
Turn 1: { occasion: "wedding" }
  → style: "traditional", safe: "safe", heaviness: None, categories: [], variety: None

Turn 2: + { guest_count: 50 }
  → style: "traditional", safe: "safe", heaviness: None, categories: [], variety: "medium"

Turn 3: + { meal_type: "lunch" }
  → style: "traditional", safe: "safe", heaviness: "full", categories: [...], variety: "medium"
```

---

## 16. Key Source Files

| File | Purpose |
|---|---|
| `elphie/core/settings.py` | All env var definitions + validation |
| `elphie/core/connections.py` | Connection pool singletons |
| `elphie/core/consultant_schema.py` | PlatterExpectationSchema dataclass + deriver |
| `elphie/core/business_rules.json` | Domain rules, consultant_heuristics, feature toggles |
| `elphie/core/state.py` | ElphieState (LangGraph state definition) |
| `elphie/core/recommendation_flow.py` | Stage resolution logic |
| `elphie/agents/discovery_agent.py` | Entity extraction, schema derivation |
| `elphie/agents/recommendation_agent.py` | Recommendation orchestration |
| `elphie/services/graphrag_service.py` | Main recommendation service (Qdrant + Neo4j + LLM) |
| `elphie/services/llm_client.py` | LLM platter selection + consultant persona |
| `elphie/services/qdrant_variations.py` | Variation vector search |
| `elphie/services/qdrant_community.py` | Community vector search |
| `elphie/services/neo4j_client.py` | Neo4j Cypher queries |
| `elphie/services/reranker.py` | Cohere reranker |
| `elphie/scripts/detect_communities.py` | Leiden community detection |
| `elphie/scripts/generate_summaries.py` | LLM narrative generation for communities |
| `elphie/scripts/index_communities.py` | Community embed → Qdrant |
| `elphie/scripts/index_variations.py` | Variation embed → Qdrant |
| `elphie/scripts/etl/load_graph_data.py` | DynamoDB → Neo4j ETL |
| `elphie/infra/stacks/vpc_stack.py` | VPC import + security groups |
| `elphie/infra/stacks/compute_stack.py` | ECS cluster + Neo4j/Qdrant/API services |
| `elphie/infra/stacks/data_stack.py` | DynamoDB, S3, ElastiCache Redis |
| `elphie/infra/stacks/networking_stack.py` | ALBs, target groups |
| `elphie/infra/stacks/etl_stack.py` | Lambda ETL functions + Step Functions |
| `elphie/docker-compose.yml` | Local dev stack (Redis, Postgres, Qdrant) |

---

## 17. Local Development Setup

```yaml
# docker-compose.yml — services for local dev
elphie-redis:    redis:7-alpine       port 6380→6379   (session cache)
elphie-postgres: postgres:15-alpine   port 5433→5432   (state persistence)
elphie-qdrant:   qdrant/qdrant:latest port 6333, 6334  (vector DB)

# Neo4j is NOT in docker-compose — connect to cloud/staging instance
# or run Neo4j separately: docker run -p 7474:7474 -p 7687:7687 neo4j
```

**Minimum `.env` for local dev:**
```env
OPENAI_API_KEY=sk-...
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=password
QDRANT_HOST=localhost
QDRANT_PORT=6333
REDIS_HOST=localhost
REDIS_PORT=6380
```

---

## 18. What the New Item-Search POC Needs From This Infra

The Item-Based Platter Search POC connects to the **same Neo4j and Qdrant instances** — it does not need its own databases.

| Need | How |
|---|---|
| Neo4j connection | Same `bolt://localhost:7687` or cloud URI — `Item`, `Community`, `Platter` nodes already exist |
| Qdrant connection | Same `localhost:6333` — `graphrag_communities` collection already indexed |
| Community IDs | Already stored as `(Item)-[:MEMBER_OF]->(Community)` in Neo4j |
| Embeddings | Same `text-embedding-3-small` via OpenAI |
| No chatbot code needed | POC is a standalone search service — no agents, no state, no LangGraph |

**New things the POC needs to build:**
1. `VARIANT_OF` edges in Neo4j (LLM-generated synonyms for 250 canonical items)
2. A query script: `embed(items) → Qdrant community search → Neo4j platter lookup → rank by coverage`
3. Optionally: a new Qdrant collection for item-level embeddings (if going beyond community-level search)
