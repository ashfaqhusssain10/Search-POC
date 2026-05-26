"""v6: Platter search with LLM-judge precision rerank.

Builds on v5's coverage-based ranking. The change is in how substitutes are
decided:

  v5 (arithmetic):
      Cosine ≥ per-form threshold + veg + form guard → accept as substitute.
      Brittle: thresholds had to be calibrated per form and still missed
      domain-specific judgments (e.g. "any flatbread fills the bread slot").

  v6 (LLM judge):
      1. For each top-N platter, find every uncovered query dish.
      2. For each uncovered dish, propose the top-K closest items already in
         the platter via cosine (veg guard only, no form filter).
      3. Send the user dish + candidates to Claude Haiku 4.5 (Bedrock).
         The model picks the best substitute with a reason, or rejects.
      4. Apply the model's decisions; rescued matches get is_substitute=True
         and a `substitute_reason` string for the UI.

Why this is sound:
  - Dense retrieval still does recall (cheap, fast).
  - LLM only runs on the SHORTLIST (top-N platters × uncovered dishes).
  - One Claude call per platter, batching all its uncovered-dish judgments.
  - Total cost per query: ~$0.001, ~500-1500ms.

Toggleable via `enable_llm_judge=True` so v5 stays as the safe fallback path.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

import boto3
import numpy as np
from botocore.config import Config

from core.connections import close_connections, get_qdrant_client
from core.platter_cache import get_cache as get_platter_cache
from scripts.search_v4 import search_items_v4
from scripts.search_v5 import (
    COVERAGE_WEIGHT,
    DEFAULT_RANKER,
    DEFAULT_TOP_K_PER_ITEM,
    DEFAULT_TOP_N_PLATTERS,
    QUALITY_WEIGHT,
    SPECIFICITY_WEIGHT,
    SERVICE_TYPE_LABELS,
    SkeletonSlot,
    V4_CANDIDATE_FLOOR,
    _cosine,
    _scroll_meta,
    _threshold_for_form,
    _veg_compatible,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ALIAS_COLLECTION = "searchpoc_aliases"
CANONICAL_COLLECTION = "searchpoc_canonicals"

# Bedrock — Claude Haiku 4.5 via the global inference profile.
# Region uses AWS_REGION env var (set to ap-south-1 in this project's .env).
BEDROCK_MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
BEDROCK_REGION = os.getenv("AWS_REGION", "ap-south-1")
LLM_MAX_TOKENS = 1024
LLM_TIMEOUT_SECONDS = 20

# Per-platter candidate selection: how many items per uncovered dish do we
# send to the LLM? Keeping it small bounds cost + keeps the prompt readable.
TOP_K_CANDIDATES_PER_DISH = 5
# Parallel LLM calls — one per platter; Bedrock handles concurrency fine.
LLM_MAX_WORKERS = 6
# Only judge the top-K platters by arithmetic score. Platters far down the
# ranking won't be shown to the user even if rescued, so judging them is
# wasted Bedrock calls. K is bounded so latency stays predictable.
LLM_JUDGE_TOP_K_PLATTERS = 10

# Re-export so app.py can import everything from one module.
__all__ = [
    "search_platters_v6",
    "PlatterResultV6",
    "DishMatchV6",
    "SkeletonSlot",
    "SERVICE_TYPE_LABELS",
]


# ---------------------------------------------------------------------------
# Result types — extend v5's with an LLM reason string
# ---------------------------------------------------------------------------

@dataclass
class DishMatchV6:
    query_item: str
    matched_canonical: str | None
    score: float
    is_substitute: bool = False
    substitute_reason: str | None = None  # LLM-provided "why this counts as a sub"


@dataclass
class PlatterResultV6:
    platter_id: str
    name: str
    platter_type: str | None
    meal_type: str | None
    veg: bool | None
    min_price: float | None
    coverage: float
    quality: float
    specificity: float
    intended_slot_count: int
    final_score: float
    matched_count: int
    total_query_dishes: int
    dish_matches: list[DishMatchV6]
    ranker_used: str = DEFAULT_RANKER
    skeleton: list[SkeletonSlot] = field(default_factory=list)
    all_items: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Bedrock client (lazy, singleton)
# ---------------------------------------------------------------------------

_bedrock_client = None


def _get_bedrock():
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=BEDROCK_REGION,
            config=Config(
                read_timeout=LLM_TIMEOUT_SECONDS,
                connect_timeout=5,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )
    return _bedrock_client


# ---------------------------------------------------------------------------
# LLM judge — one call per platter, judges all uncovered dishes at once
# ---------------------------------------------------------------------------

JUDGE_SYSTEM = (
    "You are a catering menu expert helping a customer find platters that "
    "match their selected dishes. The customer picked some dishes; a platter "
    "may not contain an exact match, but might contain a close substitute. "
    "Your job is to decide whether each candidate substitute is a TRUE "
    "substitute in catering context (same meal slot, same dietary type, "
    "same role in the meal), not just lexically similar.\n\n"
    "Rules:\n"
    "- VEG dishes can ONLY be substituted by VEG items.\n"
    "- NONVEG dishes can ONLY be substituted by NONVEG items of the same protein family "
    "(chicken/mutton/fish/prawn). Different proteins are different dishes.\n"
    "- A bread (flatbread) is a fine substitute for another bread in the same meal.\n"
    "- A rice variant is fine for another rice variant.\n"
    "- A regional curry variant is fine for the same protein in another regional style.\n"
    "- Continental items (pasta, pizza, tres leches) are NOT substitutes for Indian items "
    "even if the form matches.\n"
    "- Snacks/starters of the same type are substitutes.\n"
    "- If no candidate is a true substitute, return null. Be honest."
)


def _build_judge_prompt(
    platter_name: str,
    judgments: list[dict],   # [{user_dish, user_veg, user_form, candidates: [{name, form, score}]}]
) -> str:
    """Build a compact, structured prompt for one platter's worth of judgments."""
    lines = [
        f"Platter being evaluated: {platter_name}",
        "",
        "For each user dish below, the candidates list contains the items "
        "already in this platter that are closest to it in embedding space. "
        "Pick the BEST one as a real catering substitute, or return null if "
        "none of them genuinely substitute.",
        "",
        "Return STRICT JSON with this shape (one key per user_dish):",
        "{",
        '  "<user_dish>": {"substitute": "<candidate_name or null>", '
        '"confidence": "high|medium|low", "reason": "<one short sentence>"}',
        "}",
        "",
        "User dishes to judge:",
    ]
    for j in judgments:
        lines.append("")
        lines.append(f"- USER DISH: {j['user_dish']}  (veg={j['user_veg']}, form={j['user_form']})")
        lines.append("  candidates in this platter:")
        for c in j["candidates"]:
            lines.append(f"    * {c['name']}  (form={c['form']}, cosine={c['score']:.3f})")
    lines.append("")
    lines.append("Respond with JSON only, no markdown fences.")
    return "\n".join(lines)


def _invoke_bedrock(prompt: str) -> str:
    """One Bedrock call. Returns raw text; caller parses JSON."""
    client = _get_bedrock()
    resp = client.invoke_model(
        modelId=BEDROCK_MODEL_ID,
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": LLM_MAX_TOKENS,
            "system": JUDGE_SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
        }),
    )
    payload = json.loads(resp["body"].read())
    return payload["content"][0]["text"]


def _parse_judge_response(text: str) -> dict[str, dict[str, Any]]:
    """Tolerate stray code fences or whitespace around the JSON."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        # Drop a "json" hint line if present
        if "\n" in t:
            head, _, body = t.partition("\n")
            if head.strip().lower() in ("json", ""):
                t = body
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        # Last-ditch: find the outermost JSON object
        start = t.find("{")
        end = t.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(t[start:end + 1])
            except json.JSONDecodeError:
                pass
        log.warning("LLM judge returned unparseable JSON: %r", text[:300])
        return {}


# ---------------------------------------------------------------------------
# Per-platter judge: builds candidates + dispatches one Bedrock call
# ---------------------------------------------------------------------------

def _judge_platter(
    platter_name: str,
    uncovered_dishes: list[tuple[str, str | None, str | None, np.ndarray]],
    platter_items_meta: dict[str, tuple[np.ndarray, str | None, str | None]],
) -> dict[str, tuple[str | None, str | None]]:
    """Return {user_dish: (chosen_canonical_or_None, reason_or_None)}.
    The LLM gets the top-K cosine-closest veg-compatible candidates per dish.
    """
    if not uncovered_dishes or not platter_items_meta:
        return {}

    judgments: list[dict] = []
    for dish_name, dish_veg, dish_form, dish_vec in uncovered_dishes:
        # Rank platter items by cosine, with veg guard (hard rule, mirrors the
        # LLM's own instructions — saves it from arguing with us about veg).
        scored: list[tuple[float, str, str | None]] = []
        for cname, (cv, cveg, cform) in platter_items_meta.items():
            if not _veg_compatible(dish_veg, cveg):
                continue
            s = _cosine(dish_vec, cv)
            scored.append((s, cname, cform))
        scored.sort(key=lambda x: -x[0])
        candidates = [
            {"name": n, "form": (f or ""), "score": s}
            for s, n, f in scored[:TOP_K_CANDIDATES_PER_DISH]
        ]
        if not candidates:
            continue
        judgments.append({
            "user_dish": dish_name,
            "user_veg": dish_veg or "",
            "user_form": dish_form or "",
            "candidates": candidates,
        })

    if not judgments:
        return {}

    prompt = _build_judge_prompt(platter_name, judgments)
    try:
        text = _invoke_bedrock(prompt)
    except Exception as exc:
        log.warning("Bedrock call failed for platter %r: %s", platter_name, exc)
        return {}

    parsed = _parse_judge_response(text)
    out: dict[str, tuple[str | None, str | None]] = {}
    for j in judgments:
        dish = j["user_dish"]
        entry = parsed.get(dish, {})
        sub = entry.get("substitute")
        reason = entry.get("reason")
        # Defensive: only accept a substitute that was actually in the candidate list
        candidate_names = {c["name"] for c in j["candidates"]}
        if sub and sub in candidate_names:
            out[dish] = (sub, reason)
        else:
            out[dish] = (None, reason if isinstance(reason, str) else None)
    return out


# ---------------------------------------------------------------------------
# Core: search_platters_v6
# ---------------------------------------------------------------------------

def search_platters_v6(
    items: list[str],
    top_k_per_item: int = DEFAULT_TOP_K_PER_ITEM,
    top_n: int = DEFAULT_TOP_N_PLATTERS,
    service_types: list[str] | None = None,
    ranker: str = DEFAULT_RANKER,  # kept for back-compat; ignored
    enable_llm_judge: bool = False,
) -> list[PlatterResultV6]:
    """Same shape as v5 but uses Claude Haiku 4.5 to judge substitutes.

    Scoring is fixed (no ranker selection); per-form thresholds gate the
    item-level matches, identical to v5. `ranker` accepted but ignored.
    """
    items = [i.strip() for i in items if i and i.strip()]
    if not items:
        return []

    if ranker != DEFAULT_RANKER:
        log.info("Ignoring legacy ranker=%r — fixed weights in effect", ranker)
    coverage_w = COVERAGE_WEIGHT
    quality_w = QUALITY_WEIGHT
    specificity_w = SPECIFICITY_WEIGHT

    # ── 1. v4 with a permissive global floor; per-form floor applied below ─
    import scripts.search_v4 as _v4
    original_floor = _v4.ITEM_SCORE_THRESHOLD
    _v4.ITEM_SCORE_THRESHOLD = V4_CANDIDATE_FLOOR
    try:
        item_results = search_items_v4(items, top_k=top_k_per_item)
    finally:
        _v4.ITEM_SCORE_THRESHOLD = original_floor

    canonical_to_dish_scores: dict[str, dict[str, float]] = {}
    for r in item_results:
        floor = _threshold_for_form(r.query_form)
        for h in r.hits:
            if h.score < floor:
                continue
            slot = canonical_to_dish_scores.setdefault(h.name, {})
            prev = slot.get(r.query_item, 0.0)
            if h.score > prev:
                slot[r.query_item] = h.score

    candidate_canonicals = list(canonical_to_dish_scores.keys())
    if not candidate_canonicals:
        log.info("v4 produced no candidates — no platters to rank.")
        return []

    # ── 2. Candidate platters ────────────────────────────────────────────
    # Platter data now comes from a DynamoDB-backed in-memory cache instead
    # of Neo4j. No graph features were in use — just three relational joins,
    # which the cache handles in-process with always-fresh data.
    rows = get_platter_cache().fetch_for_canonicals(
        candidate_canonicals,
        service_types=service_types if service_types else None,
    )
    log.info("Found %d candidate platters", len(rows))

    # ── 3. Pre-fetch vectors needed for the LLM-judge step ──────────────
    query_meta: dict[str, tuple[np.ndarray, str | None, str | None]] = {}
    canon_meta: dict[str, tuple[np.ndarray, str | None, str | None]] = {}
    if enable_llm_judge:
        qdrant = get_qdrant_client()
        query_meta = _scroll_meta(qdrant, ALIAS_COLLECTION, set(items))
        all_platter_items: set[str] = set()
        for row in rows:
            all_platter_items |= set(row["all_items"])
        canon_meta = _scroll_meta(qdrant, CANONICAL_COLLECTION, all_platter_items)
        log.info("LLM judge enabled: %d query vectors, %d canonical vectors",
                 len(query_meta), len(canon_meta))

    # ── 4. First pass: compute direct matches + collect uncovered dishes ─
    n_dishes = len(items)

    @dataclass
    class _Pending:
        row: Any
        dish_matches: list[DishMatchV6]
        uncovered: list[tuple[str, str | None, str | None, np.ndarray]]  # (name, veg, form, vec)

    pending: list[_Pending] = []
    for row in rows:
        platter_items_in_row: list[str] = row["matched_items"]
        dish_matches: list[DishMatchV6] = []
        uncovered: list[tuple[str, str | None, str | None, np.ndarray]] = []
        for query_dish in items:
            best_canonical: str | None = None
            best_score = 0.0
            for canonical in platter_items_in_row:
                score = canonical_to_dish_scores.get(canonical, {}).get(query_dish, 0.0)
                if score > best_score:
                    best_score = score
                    best_canonical = canonical
            dish_matches.append(DishMatchV6(query_dish, best_canonical, best_score))
            if best_canonical is None and enable_llm_judge and query_dish in query_meta:
                qv, q_veg, q_form = query_meta[query_dish]
                uncovered.append((query_dish, q_veg, q_form, qv))
        pending.append(_Pending(row=row, dish_matches=dish_matches, uncovered=uncovered))

    # ── 5. Run LLM judge in parallel — ONLY on top-K platters by pre-judge
    #       arithmetic score. Platters far down the rank won't surface to the
    #       user even if rescued, so judging them wastes Bedrock calls.
    if enable_llm_judge:
        def _prejudge_score(p: _Pending) -> float:
            ms = [m.score for m in p.dish_matches if m.matched_canonical is not None]
            cov = len(ms) / n_dishes
            qual = sum(ms) / len(ms) if ms else 0.0
            return coverage_w * cov + quality_w * qual  # specificity adds tie-breaking only
        pending_sorted = sorted(pending, key=_prejudge_score, reverse=True)
        candidates = pending_sorted[:LLM_JUDGE_TOP_K_PLATTERS]
        platters_to_judge = [p for p in candidates if p.uncovered]
        log.info("Judging %d of top-%d platters with uncovered dishes",
                 len(platters_to_judge), LLM_JUDGE_TOP_K_PLATTERS)

        def _judge_one(p: _Pending) -> tuple[_Pending, dict[str, tuple[str | None, str | None]]]:
            items_meta = {
                n: canon_meta[n] for n in p.row["all_items"] if n in canon_meta
            }
            decisions = _judge_platter(p.row["name"], p.uncovered, items_meta)
            return p, decisions

        with ThreadPoolExecutor(max_workers=LLM_MAX_WORKERS) as pool:
            futures = [pool.submit(_judge_one, p) for p in platters_to_judge]
            for fut in as_completed(futures):
                p, decisions = fut.result()
                # Apply: update the DishMatch entries for uncovered dishes
                for m in p.dish_matches:
                    if m.matched_canonical is not None:
                        continue
                    if m.query_item not in decisions:
                        continue
                    sub_name, reason = decisions[m.query_item]
                    if sub_name is None:
                        continue
                    # Score: use the cosine we already have between user dish
                    # and the chosen substitute. Look it up.
                    sub_meta = canon_meta.get(sub_name)
                    qv_meta = query_meta.get(m.query_item)
                    score = 0.0
                    if sub_meta and qv_meta:
                        score = _cosine(qv_meta[0], sub_meta[0])
                    m.matched_canonical = sub_name
                    m.score = score
                    m.is_substitute = True
                    m.substitute_reason = reason

    # ── 6. Finalize scoring per platter ─────────────────────────────────
    results: list[PlatterResultV6] = []
    for p in pending:
        row = p.row
        match_scores: list[float] = [
            m.score for m in p.dish_matches if m.matched_canonical is not None
        ]
        matched_count = len(match_scores)
        coverage = matched_count / n_dishes
        quality = sum(match_scores) / matched_count if match_scores else 0.0

        family_totals: dict[str, tuple[int, int]] = {}
        for entry in row["skeleton_raw"] or []:
            family = entry.get("family") or "Other"
            slot_count = int(entry.get("slot_count") or 0)
            order = int(entry.get("order") or 999)
            existing_count, existing_order = family_totals.get(family, (0, 999))
            family_totals[family] = (existing_count + slot_count, min(existing_order, order))
        skeleton = [
            SkeletonSlot(family=fam, slot_count=count, order=order)
            for fam, (count, order) in sorted(family_totals.items(), key=lambda kv: kv[1][1])
            if count > 0
        ]
        intended_slot_count = sum(s.slot_count for s in skeleton)
        denominator = intended_slot_count or (len(row["all_items"]) or 1)
        specificity = min(matched_count / denominator, 1.0)
        final_score = (
            coverage_w * coverage
            + quality_w * quality
            + specificity_w * specificity
        )

        results.append(PlatterResultV6(
            platter_id=row["id"],
            name=row["name"],
            platter_type=row["platter_type"],
            meal_type=row["meal_type"],
            veg=row["veg"],
            min_price=row["min_price"],
            coverage=coverage,
            quality=quality,
            specificity=specificity,
            intended_slot_count=intended_slot_count,
            final_score=final_score,
            matched_count=matched_count,
            total_query_dishes=n_dishes,
            dish_matches=p.dish_matches,
            ranker_used=ranker,
            skeleton=skeleton,
            all_items=row["all_items"],
        ))

    results.sort(
        key=lambda r: (r.final_score, r.matched_count, r.quality, r.specificity),
        reverse=True,
    )
    return results[:top_n]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    raw = " ".join(sys.argv[1:]) or (
        "Achari Chicken Curry, Chicken Biryani, Apricot Delight, Rumali Roti"
    )
    selected = [i.strip() for i in raw.split(",") if i.strip()]
    log.info("Query dishes: %s", selected)

    results = search_platters_v6(
        selected, top_k_per_item=5, top_n=5,
        ranker="coverage_dominant", enable_llm_judge=True,
    )
    if not results:
        log.info("No platters matched.")
        sys.exit(0)

    for i, r in enumerate(results, 1):
        log.info("")
        log.info("#%d  %s  coverage=%d/%d  score=%.2f",
                 i, r.name, r.matched_count, r.total_query_dishes, r.final_score)
        for m in r.dish_matches:
            if m.is_substitute:
                log.info("    SUB  %-30s → %s  (%.3f) — %s",
                         m.query_item, m.matched_canonical, m.score, m.substitute_reason)
            elif m.matched_canonical:
                log.info("    OK   %-30s → %s  (%.3f)",
                         m.query_item, m.matched_canonical, m.score)
            else:
                log.info("    MISS %-30s (no substitute)", m.query_item)

    close_connections()
