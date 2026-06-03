# Plan: Two-Layer Precomputed Search + Production Hardening

## Context

Today's runtime (`search_v5.py`) is good at quality but not production-ready:
- Every API call hits Qdrant for per-dish vector search.
- Every API call may scroll Qdrant again for the in-platter substitute fallback.
- Cosine top-1 can be semantically wrong even when a better match sits at rank 3 (token-overlap collisions like Paneer Manchurian vs Paneer Butter Masala).
- No tests, no observability, no rollback path, name-only join key with no audit anchor.

We want to ship a system where:
- Per-request work is **dict + math only** (no Qdrant, no Neo4j, no LLM at request time).
- The expensive semantic decision (which canonical best matches which alias) is **made once by an LLM**, offline, and cached.
- Platter scoring (which depends on the user's dish combo) stays **on-the-fly** — it can't be precomputed combinatorially.
- The whole thing is observable, versioned, and rollbackable.

## Decisions already made

| Decision | Choice | Reason |
|---|---|---|
| Deployment | **AWS Lambda + container image, behind API Gateway** | Spiky traffic, low ops, cheap |
| Vector storage at runtime | **In-memory numpy, loaded from S3 on cold start** | ~36 MB total, fits easily; drops Qdrant from runtime |
| Resolution table | **DynamoDB** (`alias → best_canonical, top_k, confidence`) | Hot key-value lookup |
| LLM for rerank | **Bedrock Claude Haiku 4.5** | Cheap, fast, available |
| LLM input | **All candidates above the per-form threshold** (not capped K) | Maximize recall before LLM judges |
| Substitute fallback | **On-the-fly** at request time, using in-memory vectors | User's dish combo unknown until query |
| Precompute trigger | **EventBridge cron** (start with daily); DDB Streams added later | No real-time freshness requirement |
| Artifact versioning | **S3 versioned folders + `current.json` pointer, atomic swap** | Zero-downtime updates, rollback-capable |
| Architecture | arm64 (Graviton) | Cheaper, faster numpy |

## Architecture

```
                     ┌──────────────────┐
                     │ Precompute job   │  (EventBridge cron, ECS Fargate task)
                     │                  │   1. Scan items from DDB
                     │                  │   2. Build alias vectors + canonical vectors
                     │                  │   3. For each alias: filter (veg+form+per-form floor)
                     │                  │   4. Bedrock Haiku reranks survivors → best_canonical
                     │                  │   5. Write artifacts to S3, flip pointer
                     └────────┬─────────┘
                              │
                              ▼
                     ┌──────────────────┐
                     │  S3 bucket       │
                     │  /versions/vNN/  │  vectors_*.npy, names_*.json, meta_*.json, manifest.json
                     │  /current.json   │  {"version": "vNN"}
                     └────────┬─────────┘
                              │ init-time GET
                              ▼
   client ─► API GW ─► ┌──────────────────┐
                       │  Lambda          │  Container image, Python 3.12, arm64
                       │  (in-memory:     │  - canonical vectors (numpy)
                       │   alias vectors, │  - alias vectors (numpy)
                       │   meta lookups)  │  - meta dicts
                       └────────┬─────────┘
                                │ per-request reads
                                ▼
                       ┌──────────────────┐
                       │  DynamoDB        │
                       │   ResolutionTbl  │  alias → best_canonical, top_k, confidence
                       │   PlatterTbl     │  platter → items, type, meta
                       │   ItemPlatterTbl │  item → list of platter_ids (reverse index)
                       └──────────────────┘
```

## Phase 1 — Precompute layer (the brain)

### 1.1 New script: `scripts/precompute_alias_resolution.py`

Inputs:
- Supabase alias list (canonical truth for which aliases exist).
- DynamoDB canonical items (with `llm_description` metadata).
- Existing Qdrant collections (`searchpoc_aliases_noname`, `searchpoc_canonicals_noname`) for the embeddings — *or* re-embed inline from the metadata using the existing `core.embedding_text.build_item_embedding_text`. **Decision: reuse Qdrant for the precompute job only.** Qdrant stays as an offline tool; not in the runtime path.

Algorithm per alias:
1. Fetch alias vector + form + veg from Qdrant alias collection.
2. Query Qdrant canonical collection with v4-style filters (veg compat + form-family).
3. Keep **all** hits with `score ≥ FORM_THRESHOLDS[alias.form]`.
4. If 0 survivors → record `best_canonical = None, confidence = 0, reason = "no_candidates_above_floor"`.
5. If 1 survivor → record it directly (no LLM call needed).
6. If ≥2 survivors → call **Bedrock Haiku** with the structured prompt below.

LLM prompt (Bedrock Haiku 4.5):
```
You are matching a customer-facing dish name to its closest canonical dish in our catalog.

Query dish: {alias_name}
Query metadata: {alias.llm_description}

Candidates (sorted by cosine, all above semantic-similarity floor):
1. {canonical_name_1} — {canonical.llm_description_1} (cosine: {score_1})
2. {canonical_name_2} — {canonical.llm_description_2} (cosine: {score_2})
...

Pick the candidate that is the best semantic match for the query dish.
A high cosine score is informative but not decisive — prefer candidates whose
ingredients, form, and cuisine align with the query, even if cosine is lower.

Return strict JSON:
{
  "best_idx": <1-based index>,
  "confidence": <0.0-1.0>,
  "reason": "<one sentence>"
}
```

Output row written to DynamoDB `ResolutionTbl`:
```python
{
    "alias": "Kaju Paneer Curry",
    "best_canonical": "Paneer Butter Masala",
    "best_canonical_score": 0.80,         # cosine of the chosen one
    "confidence": 0.92,                    # LLM-assigned
    "reason": "Both are creamy North Indian paneer gravies; ingredients align.",
    "top_k": [
        {"name": "Paneer Manchurian", "score": 0.85, "form": "dry-fry"},
        {"name": "Shahi Paneer",      "score": 0.83, "form": "gravy"},
        {"name": "Paneer Butter Masala", "score": 0.80, "form": "gravy"},
    ],
    "form": "gravy",
    "veg": "VEG",
    "version": "v42",
    "computed_at": "2026-05-30T10:00:00Z",
    "llm_model": "anthropic.claude-haiku-4-5-20251001-v1:0",
    "prompt_hash": "sha256:...",
}
```

### 1.2 LLM cache: `llm_cache/alias_resolution/`

Key: `sha256(alias_name + sorted_candidate_set + prompt_version)`.
Value: the LLM response JSON.

Re-runs only call Bedrock when the input has actually changed (new alias, new candidate set, or prompt version bump). Drops Bedrock cost to near-zero on incremental rebuilds.

### 1.3 Vector artifact builder: `scripts/build_runtime_artifacts.py`

Pulls all alias + canonical vectors from Qdrant and writes:
```
artifacts/v{N}/
  ├── canonical_vectors.npy      # shape (N_canon, 1536), float32
  ├── canonical_names.json       # parallel list, index → name
  ├── canonical_meta.json        # name → {form, veg, itemId}
  ├── alias_vectors.npy
  ├── alias_names.json
  ├── alias_meta.json
  └── manifest.json              # {version, computed_at, n_canon, n_alias,
                                  #  llm_model, prompt_hash, sha256s,
                                  #  form_threshold_hash, source_versions}
```

Then uploads to S3 and writes a new `current.json`.

**Atomic swap protocol:**
1. Upload all files to `s3://.../versions/v{N+1}/`.
2. Read back manifest, verify SHA256s.
3. **Only then** overwrite `current.json` with `{"version": "v{N+1}"}`.
4. Keep last 5 versions for rollback; lifecycle-delete older.

### 1.4 Precompute entrypoint: `scripts/run_precompute.py`

Top-level orchestrator:
```python
def main():
    # 1. Re-embed if source items changed (calls existing embed scripts)
    # 2. Run precompute_alias_resolution → write to a staging DDB table
    # 3. Run build_runtime_artifacts → write S3 v{N+1}
    # 4. Verify all checks pass (counts, sample queries, score distribution sanity)
    # 5. Promote: copy staging DDB → live, flip S3 current.json
    # 6. Emit metrics
```

This is what EventBridge triggers daily and what a human triggers ad-hoc.

## Phase 2 — Runtime layer (the API)

### 2.1 New module: `core/runtime_index.py`

Loaded once per Lambda cold start:
```python
class RuntimeIndex:
    canonical_vectors: np.ndarray    # (N_canon, 1536)
    canonical_names: list[str]
    canonical_name_to_idx: dict[str, int]
    canonical_meta: dict[str, dict]  # form, veg, itemId

    alias_vectors: np.ndarray
    alias_names: list[str]
    alias_name_to_idx: dict[str, int]
    alias_meta: dict[str, dict]

    version: str

    @classmethod
    def load_from_s3(cls, bucket: str) -> "RuntimeIndex":
        current = json.loads(s3.get_object(Bucket=bucket, Key="current.json")["Body"].read())
        prefix = f"versions/{current['version']}"
        # parallel GETs via ThreadPoolExecutor
        # verify SHA256s against manifest
        # return populated instance
```

### 2.2 New module: `core/ddb_resolution.py`

Thin wrapper for DynamoDB `ResolutionTbl`:
```python
def resolve(aliases: list[str]) -> dict[str, ResolutionRecord]:
    # BatchGetItem in chunks of 100
    # returns alias → record (or None if not found)
```

### 2.3 New module: `core/ddb_platters.py`

Two access patterns needed:
1. `fetch_platters_containing(canonical_names: list[str], service_types: list[str] | None) → list[Platter]`
2. `get_platter(platter_id: str) → Platter`

Implementation: GSI on `item_name` → `platter_id`, then BatchGetItem for the platter rows. Same shape as today's in-memory cache, but DDB-backed.

### 2.4 New search module: `scripts/search_v6.py`

```python
def search_platters_v6(
    user_dishes: list[str],
    top_n: int = 10,
    service_types: list[str] | None = None,
    enable_fallback: bool = True,
) -> list[PlatterResultV6]:

    # 1. Lookup precomputed resolution per alias (DDB batch get)
    resolutions = ddb_resolution.resolve(user_dishes)

    # 2. Collect best canonicals (skip dishes with no resolution)
    canonicals = [r.best_canonical for r in resolutions.values() if r and r.best_canonical]

    # 3. Fetch candidate platters (DDB)
    platters = ddb_platters.fetch_platters_containing(canonicals, service_types)

    # 4. Per platter, build DishMatch list (same logic as v5, just sourced from precomputed scores)
    for platter in platters:
        for dish in user_dishes:
            res = resolutions.get(dish)
            if res and res.best_canonical in platter.items:
                # Direct match — use precomputed score & confidence
                ...
            elif enable_fallback:
                # On-the-fly substitute: cosine over platter items in-memory
                # Uses RuntimeIndex.alias_vectors[dish] + canonical_vectors[platter items]
                ...

    # 5. Aggregate coverage / quality / specificity (identical to v5)
    # 6. Sort, return top_n
```

The substitute fallback uses the in-memory numpy vectors — no Qdrant call.

### 2.5 Lambda entrypoint: `lambda/handler.py`

```python
import json
from core.runtime_index import RuntimeIndex
from scripts.search_v6 import search_platters_v6

# Cold-start init
INDEX = RuntimeIndex.load_from_s3(os.environ["ARTIFACT_BUCKET"])

def handler(event, context):
    body = json.loads(event.get("body", "{}"))
    dishes = body.get("dishes", [])
    top_n = body.get("top_n", 10)
    service_types = body.get("service_types")

    results = search_platters_v6(dishes, top_n=top_n, service_types=service_types)
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "version": INDEX.version,
            "results": [r.to_dict() for r in results],
        }),
    }
```

### 2.6 Container image

`Dockerfile`:
- Base: `public.ecr.aws/lambda/python:3.12-arm64`
- Install: numpy, boto3, anthropic-bedrock (or boto3 directly for Bedrock), pydantic
- Copy code: `lambda/`, `core/`, `scripts/search_v6.py`
- Entrypoint: `lambda.handler.handler`

### 2.7 API Gateway

- HTTP API (cheaper than REST API).
- Single route: `POST /search` → Lambda.
- Optional: API key for partner access; for the Streamlit UI, use IAM auth or a service token.

### 2.8 Streamlit app integration

Update `app.py` to call the API endpoint instead of `search_v5` directly. Feature flag (`USE_API`) so we can A/B v5 (in-process) vs v6 (API) during cutover.

## Phase 3 — Production hardening

### 3.1 Eval set & CI regression check

- `eval/golden_set.jsonl`: 100 hand-curated `(query_dishes, expected_top_platter_ids)` tuples.
- `eval/run_eval.py`: runs both v5 and v6 against the set, computes:
  - Top-1 platter match rate.
  - Top-3 platter match rate.
  - Mean alias resolution confidence.
  - Per-form breakdown.
- GitHub Actions runs eval on every PR that touches search code. Fail PR if top-3 match rate drops >2 points.

### 3.2 itemId as audit anchor

- Add `itemId` to Qdrant payloads (re-embed once).
- Add `itemId` to `canonical_meta.json` and DDB `ResolutionTbl`.
- Startup integrity check: count items in DDB MenuItemsTable, count entries in `canonical_names.json`, log mismatch. Refuse to start if mismatch > 5%.

### 3.3 Observability

Structured logs from Lambda (JSON to CloudWatch):
```json
{
  "request_id": "...",
  "version": "v42",
  "dishes": ["..."],
  "n_dishes": 3,
  "n_resolved": 3,
  "n_unresolved": 0,
  "low_confidence_dishes": [],
  "fallback_fired": ["Garlic Naan"],
  "top_platter_score": 0.83,
  "p_latency_ms": 12,
  "service_types": []
}
```

CloudWatch metrics (custom):
- `LowConfidenceRate` — % requests with any dish at confidence < 0.7.
- `UnresolvedAliasRate` — % requests with any dish missing from DDB.
- `FallbackFireRate` — % requests where the substitute path fired.
- `ColdStartCount` — Lambda init duration > 200ms.

Dashboards: per-form quality, per-version regression compared to previous version.

### 3.4 Low-confidence review UI

Streamlit page: lists aliases where `ResolutionTbl.confidence < 0.7`, shows the top-K candidates and LLM reasoning side-by-side, lets an operator pick the right one. Writes to `manual_overrides` DDB table; next precompute respects those overrides before calling the LLM.

### 3.5 Drift detection

Weekly job: re-resolve a 5% sample of aliases with the current LLM + vectors, compare to live `ResolutionTbl`. Alert if `best_canonical` differs for >X% of sampled aliases → triggers full precompute.

### 3.6 Versioning & manifest hashes

Every precompute writes:
```json
{
  "version": "v42",
  "computed_at": "2026-05-30T10:00:00Z",
  "source_versions": {
    "supabase_aliases_count": 3024,
    "dynamodb_canonicals_count": 2891,
    "qdrant_alias_collection_points": 3024,
    "qdrant_canonical_collection_points": 2891
  },
  "llm_model": "anthropic.claude-haiku-4-5-20251001-v1:0",
  "prompt_hash": "sha256:abc...",
  "form_threshold_hash": "sha256:def...",
  "file_sha256": {
    "canonical_vectors.npy": "...",
    "alias_vectors.npy": "...",
    "canonical_meta.json": "...",
    "alias_meta.json": "..."
  }
}
```

If anything looks wrong post-promotion, `current.json` can be reverted to the previous version in one S3 PUT.

### 3.7 Cost guardrails

- Bedrock spend alarm at $50/month (precompute should be <$10).
- DynamoDB on-demand mode initially; switch to provisioned + autoscaling once steady-state QPS is known.
- S3 lifecycle: delete `versions/v{N}` older than 30 days (keep last 5 always).

## Phase 4 — Cutover

1. **Week 1:** Run v6 in parallel with v5. Streamlit app gets a toggle. Both endpoints log to the same eval format. Compare divergence on the eval set + on real traffic.
2. **Week 2:** Default to v6 for all reads. v5 remains callable behind a feature flag for emergency rollback.
3. **Week 3:** Audit logs for any regressions; review low-confidence flagged items; tune prompt or thresholds if needed.
4. **Week 4:** Decommission v5 (delete `search_v5.py`, remove community/qdrant runtime deps, remove app.py v5 path). Qdrant stays for the precompute job only.

## Files modified / created

| File | Status | Purpose |
|---|---|---|
| `scripts/precompute_alias_resolution.py` | NEW | Layer 1+2: filter + LLM rerank, write to DDB |
| `scripts/build_runtime_artifacts.py` | NEW | Build .npy + .json artifacts, push to S3 |
| `scripts/run_precompute.py` | NEW | Orchestrator entrypoint |
| `scripts/search_v6.py` | NEW | DDB+numpy-backed runtime search |
| `core/runtime_index.py` | NEW | S3-loaded in-memory index |
| `core/ddb_resolution.py` | NEW | DDB wrapper for ResolutionTbl |
| `core/ddb_platters.py` | NEW | DDB wrapper for platter membership |
| `core/bedrock_client.py` | NEW | Bedrock Haiku client + retry |
| `lambda/handler.py` | NEW | Lambda entrypoint |
| `lambda/Dockerfile` | NEW | Container image definition |
| `infra/cdk/` or `infra/terraform/` | NEW | API Gateway + Lambda + DDB + S3 + IAM |
| `eval/golden_set.jsonl` | NEW | Regression test set |
| `eval/run_eval.py` | NEW | Eval runner |
| `.github/workflows/eval.yml` | NEW | CI eval gate |
| `app.py` | MODIFIED | Add USE_API toggle, call API endpoint |
| `scripts/search_v5.py` | UNCHANGED (kept for rollback) | — |

## Verification

After each phase, verifiable checks:

**Phase 1 done when:**
- `python -m scripts.run_precompute` completes without error.
- `aws s3 ls s3://searchpoc-artifacts/versions/v1/` shows 7 files.
- `aws dynamodb scan --table-name ResolutionTbl --select COUNT` matches alias count.
- Spot-check 5 aliases in the table — `best_canonical` looks right.

**Phase 2 done when:**
- `docker build` + `docker run` locally → handler returns valid response for a sample payload.
- Deployed Lambda → `curl https://.../search -d '{"dishes":["Paneer Butter Masala"]}'` returns same top platter as v5.
- Cold-start time < 1 second (CloudWatch logs).

**Phase 3 done when:**
- Eval set: v6 top-3 match rate ≥ v5 top-3 match rate.
- Manifest verification fails closed (corrupt a file → Lambda init refuses to start).
- Low-confidence dashboard populated with real data.

**Phase 4 done when:**
- 7 days of parallel running, divergence rate < 1%.
- No production incidents traceable to v6.
- v5 path safely deleted, Qdrant deps removed from Lambda image.

## Open questions for future sessions

1. Event-driven precompute via DynamoDB Streams (deferred per current decision).
2. Multi-region deployment if latency requirements emerge.
3. Per-tenant or per-region threshold tables if catalog diverges across markets.
4. Whether to expose the top-K + confidence to the UI (helps explainability) or hide it (simpler UX).
