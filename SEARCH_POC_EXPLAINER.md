# Item-Based Platter Search POC — Technical Explainer

**Audience:** Tech Lead  
**Purpose:** Explain how the search system works end-to-end, what problems it solves, and the key design decisions made.

---

## The Problem

A user comes with a list of dishes they want — e.g. *Jackfruit Biryani, Beetroot Galouti Kebab, Kaju Paneer Masala Curry, Butter Naan*. We need to find the best-matching platters from our catalogue, even though:

- The user's dish names may not exactly match item names in our platters
- Platters contain dozens of items; we need semantic matching, not exact string lookup
- VEG users should never be matched to NONVEG dishes

---

## High-Level Architecture

```
User query (dish names)
        │
        ▼
  Resolve aliases          Neo4j: "Jackfruit Biryani" → "Veg Biryani" (DynamoDB canonical)
        │
        ▼
  Embed canonicals          OpenAI text-embedding-3-small
        │
        ▼
  Qdrant community search   Per dish: find best-matching food community
  (with veg filter)         VEG items → VEG communities only
        │
        ▼
  Neo4j platter ranking     Which platters cover the most matched communities?
        │
        ▼
  Ranked results            Top 3 platters with per-dish match explanation
```

**Zero LLM calls at query time.** All intelligence is pre-computed during the ETL pipeline.

---

## Core Concept: Food Communities

Instead of matching dish names directly to platter items, we group similar dishes into **communities** using graph-based clustering (Leiden algorithm).

**Example community — "Veg Biryani":**
- Members: Veg Biryani, Paneer Biryani, Veg Pulav, Mushroom Dum Biryani, Jackfruit Biryani, Veg Fried Rice, Bisi Bele Bath, ...
- These are all semantically the same "slot" in a meal

When a user asks for *Jackfruit Biryani*, we find the community it belongs to (*Veg Biryani*), then find platters that contain **any item from that community**. This tolerates naming variation and regional synonyms automatically.

---

## ETL Pipeline (8 Steps, Run Once)

| Step | Script | What it does |
|---|---|---|
| 1 | `enrich_items.py` | LLM enrichment of raw item CSVs |
| 2 | `load_items.py` | Load DynamoDB + Supabase items into Neo4j as `Item` nodes |
| 3 | `generate_variants.py` | Link equivalent items across sources with `VARIANT_OF` edges (LLM-scored, cached) |
| 4 | `load_platters.py` | Load platter + category data into Neo4j |
| 5 | `detect_communities.py` | Run Leiden clustering on the `VARIANT_OF` graph → `Community` nodes + `MEMBER_OF` edges |
| 6 | `build_community_edges.py` | Pre-compute `HAS_COMMUNITY` edges on each platter |
| 7 | `generate_summaries.py` | LLM narrative per community → stored as `summary_json` on Community node |
| 8 | `index_communities.py` | Embed community summaries → upsert to Qdrant `item_search_communities` collection |

After the ETL runs, all search queries are served with **no LLM calls** — just embeddings + vector search + graph traversal.

---

## Query-Time Flow (Detail)

### Step 1 — Alias Resolution
Neo4j resolves user-typed names to DynamoDB canonical names via `VARIANT_OF` edges.  
*"Jackfruit Biryani" (Supabase alias) → "Veg Biryani" (DynamoDB canonical)*  
This ensures we embed the authoritative name, not a regional variant.

### Step 2 — Embedding
Canonical names are embedded in a single batched OpenAI API call.

### Step 3 — Community Lookup (Qdrant)
Each item vector is searched against the `item_search_communities` Qdrant collection.

**VEG/NONVEG filtering** is applied here:
- VEG item → filter to communities where `dominant_item_type = VEG`
- NONVEG item → no filter
- EGG item → filter to EGG or NONVEG communities

This prevents "Jackfruit Biryani" (VEG) from matching the "Chicken Biryani" community even if that community scores higher in embedding space.

**Category hint matching** refines the top candidate: if the item has a known typecode (e.g. `Biryani`), we prefer a community whose members share that typecode family. This prevents "Jackfruit Biryani" from matching a "Coriander Rice" community just because the vectors are close.

### Step 4 — Platter Ranking (Neo4j)
All platters are fetched with their pre-computed community sets. Each platter is scored by how many of the query's matched communities it contains.

**Option B VEG constraint** applies here: a VEG query item only counts as "matched" in a platter if the platter's item in that community is itself VEG. Example: a platter has both Paneer Biryani (VEG) and Chicken Biryani (NONVEG) in the Veg Biryani community — this satisfies a VEG query for biryani.

**Final score** = 70% community coverage + 30% menu skeleton fit (does the platter have the right category mix: biryani + bread + curry + starter?)

---

## Data Model (Neo4j)

```
(Item {source:'dynamodb'}) -[:VARIANT_OF]-> (Item {source:'supabase'})
(Item) -[:MEMBER_OF]-> (Community)
(Platter) -[:CONTAINS]-> (Item)
(Platter) -[:HAS_COMMUNITY]-> (Community)   ← pre-computed shortcut
(Platter) -[:HAS_CATEGORY]-> (PlatterCategory)
```

Key item properties: `name`, `itemType` (VEG/NONVEG/EGG), `source`, `typecode_name`, `category_name`

---

## Community Composition

115 communities detected from 973 nodes:
- **85 pure VEG** communities
- **28 pure NONVEG** communities  
- **2 mixed** (edge cases: Pachi Mirchi Kodi Kebab, Egg Puff)

This clean separation is what makes the VEG filter reliable.

---

## Key Design Decisions

| Decision | Rationale |
|---|---|
| Communities as the matching unit | Tolerates synonym variation; one Qdrant lookup covers hundreds of item name variants |
| Zero LLM at query time | Latency and cost — all intelligence pre-computed in ETL |
| VEG filter on Qdrant community search | Prevents VEG items from semantically drifting into NONVEG communities at embedding time |
| Option B VEG matching at platter level | A mixed platter can still satisfy a VEG query if it contains a VEG item for each requested dish |
| DynamoDB canonicals as embedding input | More consistent naming than Supabase aliases; better community alignment |
| Leiden resolution=1.0, max_cluster_size=20 | Keeps communities tight enough to be meaningful (not too broad) |

---

## Tech Stack

| Component | Technology |
|---|---|
| Graph database | Neo4j (cloud) |
| Vector database | Qdrant (cloud) |
| Embedding model | OpenAI `text-embedding-3-small` (1536-dim, cosine) |
| Community detection | `graspologic` Leiden algorithm |
| LLM for ETL enrichment | Google Gemini (variant linking + community summaries) |
| UI | Streamlit |
| Language | Python 3.11 |
