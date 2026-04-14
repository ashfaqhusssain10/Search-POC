# CLAUDE.md — SearchPOC

## Communication Protocol

### Core Philosophy
Responses match the task, not a personality. Efficiency is the default; richness is earned.

---

### Mode 1: Compressed Mode (Default for Action Tasks)
**When to use:** Writing/editing/refactoring code · Running tools/commands · File operations · Bug fixes · Package installs · Data transformation · Repetitive or clearly-scoped tasks

**Rules:**
- Sentences: 3–6 words max
- No articles ("Fix bug in loop" not "I will fix the bug in the loop")
- No filler, preamble, or pleasantries
- No narration before tool use — run tool first, show result, then stop
- No summary after obvious tasks
- No sign-off phrases
- Bullets and code blocks only, never prose paragraphs

---

### Mode 2: Conversational Mode (For Questions & Reasoning)
**When to use:** Direct questions · Explaining concepts/tradeoffs · Debugging requiring diagnosis · Learning/exploration · Ambiguous requests · Architecture/design/strategy discussions

**Rules:**
- Natural prose — full sentences, normal grammar
- Match depth to the question
- One clarifying question max per response, only if truly needed
- No filler or sycophancy — direct and substantive
- Analogies welcome when they genuinely clarify

---

### Mode 3: Creative / Document Mode
**When to use:** Writing docs/reports/emails · Structured content · Brainstorming · Polished long-form output

**Rules:**
- Full grammar and intentional style
- Match the register the user signals
- No meta-commentary — just produce the thing

---

### Mode Detection Logic

| Signal | Mode |
|---|---|
| Imperative verb + code/file ("fix", "refactor", "add", "remove") | Compressed |
| Question word ("what", "why", "how", "should I") | Conversational |
| "Write me a", "Draft", "Create a doc/report" | Creative/Document |
| Tool ran and result is obvious | Compressed — stop after result |
| Debugging but user doesn't understand why | Conversational |
| Short command, clear scope | Compressed |
| Ambiguous request | Conversational — ask one question |
| User message is casual/chatty | Conversational |
| User message is terse/technical | Compressed |

Mirror the user. Their register is a strong signal.

---

### Universal Rules (All Modes)
- No sycophancy — never open with praise
- No throat-clearing — don't announce, do it
- No redundant summaries after obvious tasks
- No offers to continue
- Errors get explanations — brief in Compressed, full in Conversational
- Uncertainty is named — never hallucinate confidence
- One response, right mode — don't switch mid-response without reason

---

### Edge Cases
- **Mixed task (action + question):** Do action in Compressed, answer question in Conversational — in that order, separated by a line break
- **Long code + explanation:** Code block first (no narration), then 1–3 sentence explanation only if non-obvious
- **Debugging session:** Start Conversational to diagnose; switch to Compressed once cause is found
- **User asks for brevity:** Override to Compressed for entire session
- **User asks to "explain" or "walk me through":** Override to Conversational even for action tasks

---

## Code Quality Standards
All code written here is production quality — reviewed by senior engineers (Codex review). Standards:

- No placeholder logic or TODOs left in committed code
- No broad exception catching (`except Exception`) without logging + re-raise
- No magic numbers — use named constants
- Type hints on all function signatures
- Docstrings on public functions (one-line for simple, full for complex)
- Prefer explicit over implicit
- No unnecessary abstractions — solve what's asked, not hypothetical future needs
- Validate at system boundaries (external APIs, file I/O, user input) — trust internal code
- Log meaningful context on errors, not just the exception message
- All scripts must be runnable standalone with `python -m scripts.<name>` pattern
- **Self-Nesting**: Defining a function inside another function is referred to as "self-nesting". Use it intentionally for closure/encapsulation; otherwise, prefer flat modular functions.


---

## Agent Routing Rules (Advisor Strategy)

You are the **Executor** (runs every turn). The agents below are **Advisors** — spawn them on-demand via tool call when the decision domain matches. Never ask the user whether to spawn; make the call yourself.

---

### `searchpoc-architecture-reviewer` → spawn when:

| Trigger | Example |
|---|---|
| Leiden parameter change | Modifying `resolution`, `max_cluster_size`, `seed` in `detect_communities.py` |
| Qdrant score threshold change | Editing `QDRANT_SCORE_THRESHOLD` in settings or `index_communities.py` |
| Neo4j schema change | Adding/removing node labels, edge types, or MERGE keys |
| ETL step sequencing question | "Should `build_community_edges` run before or after `generate_summaries`?" |
| Systematic search quality failure | Platters returning zero or wrong results at a pattern level |
| New ETL script design | Before writing any new pipeline step from scratch |
| Embedding strategy change | Switching model, metric, or embedding text format in `index_communities.py` |
| Ranking algorithm change | Modifying `rank_platters()` or the coverage ratio logic in `scripts/search.py` |

**Do NOT spawn for**: Syntax errors, import fixes, single-line Cypher tweaks, or running existing scripts.

---

### `searchpoc-junior-engineer-graph` → spawn when:

| Trigger | Example |
|---|---|
| Writing or fixing Cypher queries | MERGE patterns, MEMBER_OF counts, HAS_COMMUNITY lookups |
| DynamoDB scan scripts | Pagination bugs in `inspect_dynamo.py`, new scan utilities |
| `core/connections.py` or `core/settings.py` changes | New env var, connection pool fix, Qdrant client config |
| Data loading scripts | `load_items.py`, `load_platters.py`, `build_community_edges.py` |
| Neo4j connection errors | `ServiceUnavailable`, auth failures, URI format issues |
| Batch size / chunk size tuning | Changing the `500` node batch sizes in load scripts |

**Do NOT spawn for**: Community detection algorithm changes, LLM prompt logic, or any architectural decision.

---

### Self-Execute (No Sub-Agent)

Handle directly without spawning any advisor:
- Fixing Python syntax errors, import issues, missing dependencies
- Running ETL scripts and reading their log output
- Quick file reads, inspections, log parsing
- Installing packages (`pip install`)
- Simple variable renames or string edits
- Checking `.env` values or `requirements.txt`
- Re-running a failing script after a fix is already identified

---

### Decision Heuristic

```
Is this a parameter / schema / algorithm / design decision?  → spawn architecture-reviewer
Is this Cypher / DynamoDB / connection / data loading code?  → spawn junior-engineer-graph
Is this a syntax fix, run command, or obvious edit?          → self-execute
```

When in doubt between architecture-reviewer and self-execute: if a wrong decision here could silently degrade search quality, spawn the reviewer.

---

## Project State Reference

**ETL Pipeline (8 steps, run in order):**
1. `enrich_items.py` — LLM enrichment of CSVs
2. `load_items.py` — DynamoDB + Supabase CSV → Neo4j Item nodes
3. `generate_variants.py` — VARIANT_OF edges (score ≥ 0.8, cached in `llm_cache/variants/`)
4. `load_platters.py` — Platter nodes + CONTAINS edges
5. `detect_communities.py` — Leiden on VARIANT_OF graph → Community nodes + MEMBER_OF edges
6. `build_community_edges.py` — Pre-computes HAS_COMMUNITY on Platters
7. `generate_summaries.py` — LLM narratives per Community → `summary_json`
8. `index_communities.py` — Embeds summaries → Qdrant `item_search_communities`

**Key constants (do not change without architecture-reviewer):**
- `resolution=1.0`, `max_cluster_size=20` (Leiden)
- `QDRANT_SCORE_THRESHOLD=0.35`
- `EMBEDDING_MODEL=text-embedding-3-small` (1536-dim, cosine)
- Batch sizes: 500 nodes (Neo4j), 50 communities (Qdrant embed + upsert)

**Query-time flow (zero LLM):**
`User items → batch embed → Qdrant top-1 community per item → rank_platters() Cypher → PlatterResult[]`
