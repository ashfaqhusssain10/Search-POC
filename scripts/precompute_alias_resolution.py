"""Precompute alias → best_canonical resolution using Bedrock Haiku.

For each Supabase alias, runs the same veg+form-family filter as v4, keeps
every canonical above the per-form threshold, and asks Claude Haiku to pick
the best semantic match (LLM rerank, not just cosine top-1).

Output: diagnostics/alias_resolution.json
  { alias_name: { best_canonical, confidence, reason, top_k, decision_source } }

LLM responses are cached in llm_cache/alias_resolution/ by sha256 of
(alias_name, sorted_candidate_names, prompt_version). Re-runs only call
Bedrock for new aliases or changed candidate sets.

CLI:
    python -m scripts.precompute_alias_resolution
    python -m scripts.precompute_alias_resolution --limit 50           # smoke test
    python -m scripts.precompute_alias_resolution --aliases "Kaju Paneer Curry,Pulihora"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from core.bedrock_client import BEDROCK_MODEL_ID, invoke_claude, parse_json_response
from core.connections import close_connections, get_qdrant_client, neo4j_session
from scripts.search_v4 import (
    ALIAS_COLLECTION,
    CANONICAL_COLLECTION,
    FETCH_CANONICAL_DESCRIPTIONS_QUERY,
)

FETCH_ALIAS_DESCRIPTIONS_QUERY = """
MATCH (i:Item {source: 'supabase'})
WHERE i.name IN $names
RETURN i.name AS name,
       i.llm_description AS llm_description,
       i.category_name AS category_name,
       i.typecode_name AS typecode_name
"""
from scripts.search_v5 import (
    FALLBACK_THRESHOLD_GLOBAL,
    FORM_THRESHOLDS,
    _veg_compatible,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

OUTPUT_PATH = Path("diagnostics/alias_resolution.json")
LLM_CACHE_DIR = Path("llm_cache/alias_resolution")
PROMPT_VERSION = "v1"
SCROLL_BATCH = 256


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ItemRecord:
    name: str
    item_id: str | None
    form: str | None
    veg: str | None
    vector: np.ndarray
    llm_description: str | None = None
    category_name: str | None = None
    typecode_name: str | None = None


@dataclass
class ResolutionRecord:
    alias: str
    alias_item_id: str | None
    alias_form: str | None
    alias_veg: str | None
    alias_category_name: str | None
    alias_typecode_name: str | None
    best_canonical: str | None
    best_canonical_item_id: str | None
    best_canonical_score: float
    confidence: float
    reason: str
    decision_source: str  # "llm", "single_candidate", "no_candidates"
    top_k: list[dict[str, Any]]
    llm_model: str
    prompt_version: str
    computed_at: str

    def to_jsonable(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Qdrant loaders
# ---------------------------------------------------------------------------

def _scroll_all(qdrant, collection: str) -> list[ItemRecord]:
    out: list[ItemRecord] = []
    next_offset = None
    while True:
        points, next_offset = qdrant.scroll(
            collection_name=collection,
            offset=next_offset,
            limit=SCROLL_BATCH,
            with_payload=True,
            with_vectors=True,
        )
        for p in points:
            payload = p.payload or {}
            name = payload.get("name")
            if not name:
                continue
            out.append(ItemRecord(
                name=name,
                item_id=payload.get("item_id"),
                form=payload.get("form"),
                veg=payload.get("veg_type"),
                vector=np.asarray(p.vector, dtype=np.float32),
            ))
        if next_offset is None:
            break
    return out


def _attach_neo4j_descriptions(
    items: list[ItemRecord],
    cypher: str,
) -> None:
    """Mutates items in place, adding llm_description + category/typecode from Neo4j."""
    names = [i.name for i in items]
    by_name = {i.name: i for i in items}
    with neo4j_session() as session:
        for row in session.run(cypher, names=names):
            rec = by_name.get(row["name"])
            if rec is not None:
                rec.llm_description = row["llm_description"]
                # category_name / typecode_name only present on alias query
                if "category_name" in row.keys():
                    rec.category_name = row["category_name"] or None
                if "typecode_name" in row.keys():
                    rec.typecode_name = row["typecode_name"] or None


# ---------------------------------------------------------------------------
# Candidate selection (mirrors v4 filter logic, computed in-process)
# ---------------------------------------------------------------------------

def _cosine_against_all(query_vec: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Vectorized cosine of query against every row of matrix."""
    qn = np.linalg.norm(query_vec)
    if qn == 0.0:
        return np.zeros(matrix.shape[0], dtype=np.float32)
    mn = np.linalg.norm(matrix, axis=1)
    mn[mn == 0.0] = 1.0  # avoid div-by-zero; those rows will score 0 anyway
    return (matrix @ query_vec) / (mn * qn)


def _form_floor(form: str | None) -> float:
    if not form:
        return FALLBACK_THRESHOLD_GLOBAL
    return FORM_THRESHOLDS.get(form.strip().lower(), FALLBACK_THRESHOLD_GLOBAL)


def _select_candidates(
    alias: ItemRecord,
    canonicals: list[ItemRecord],
    canonical_matrix: np.ndarray,
) -> list[tuple[ItemRecord, float]]:
    """Return [(canonical, score)] above the per-form floor, sorted by score desc."""
    floor = _form_floor(alias.form)
    alias_form_norm = alias.form.strip().lower() if alias.form else None
    sims = _cosine_against_all(alias.vector, canonical_matrix)

    results: list[tuple[ItemRecord, float]] = []
    for can, score in zip(canonicals, sims):
        if not _veg_compatible(alias.veg, can.veg):
            continue
        if alias_form_norm is not None:
            if (can.form or "").strip().lower() != alias_form_norm:
                continue
        if score < floor:
            continue
        results.append((can, float(score)))
    results.sort(key=lambda x: -x[1])
    return results


# ---------------------------------------------------------------------------
# LLM rerank
# ---------------------------------------------------------------------------

JUDGE_SYSTEM = (
    "You map customer-facing dish names to their best semantic match in a "
    "catering catalog. A high cosine score is informative but not decisive — "
    "prefer the candidate whose ingredients, form, and cuisine align with the "
    "query dish, even if cosine is lower. Be honest: if none truly match, "
    "pick the closest and use low confidence."
)


def _format_description(desc: str | None) -> str:
    """Compact one-line summary of llm_description for prompt readability."""
    if not desc:
        return ""
    try:
        parsed = json.loads(desc) if isinstance(desc, str) else desc
    except (json.JSONDecodeError, TypeError):
        return str(desc)[:200]
    if not isinstance(parsed, dict):
        return str(desc)[:200]
    bits = []
    for key in ("cuisine", "category", "sub_category", "cooking_method", "flavor_profile"):
        v = parsed.get(key)
        if v:
            bits.append(f"{key}={v}")
    ing = parsed.get("primary_ingredients")
    if isinstance(ing, list) and ing:
        bits.append(f"ingredients={', '.join(str(x) for x in ing[:6])}")
    return " | ".join(bits)


def _build_prompt(
    alias: ItemRecord,
    candidates: list[tuple[ItemRecord, float]],
) -> str:
    lines = [
        f"Query dish: {alias.name}",
        f"Query metadata: {_format_description(alias.llm_description) or '(none)'}",
        f"Query form: {alias.form or '(unknown)'} | veg: {alias.veg or '(unknown)'}",
        "",
        f"Candidates (all passed veg+form filter, all above similarity floor):",
    ]
    for idx, (can, score) in enumerate(candidates, 1):
        lines.append(
            f"  {idx}. {can.name} — {_format_description(can.llm_description) or '(no metadata)'} "
            f"(cosine={score:.3f})"
        )
    lines.extend([
        "",
        "Pick the best semantic match. Return STRICT JSON, no markdown fences:",
        '{"best_idx": <1-based int>, "confidence": <0.0-1.0 float>, "reason": "<one sentence>"}',
    ])
    return "\n".join(lines)


def _cache_key(alias: str, candidates: list[tuple[ItemRecord, float]]) -> str:
    canon_names = sorted(c.name for c, _ in candidates)
    blob = f"{PROMPT_VERSION}|{alias}|{'||'.join(canon_names)}"
    return hashlib.sha256(blob.encode()).hexdigest()


def _cache_get(key: str) -> dict[str, Any] | None:
    path = LLM_CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        log.warning("Corrupt cache entry %s; discarding", key)
        return None


def _cache_put(key: str, value: dict[str, Any]) -> None:
    LLM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (LLM_CACHE_DIR / f"{key}.json").write_text(json.dumps(value, indent=2))


def _llm_pick(
    alias: ItemRecord,
    candidates: list[tuple[ItemRecord, float]],
) -> tuple[int, float, str]:
    """Returns (best_idx_1based, confidence, reason). Falls back to top-1 by
    cosine on any LLM failure, with confidence=0.5 to flag the case.
    """
    key = _cache_key(alias.name, candidates)
    cached = _cache_get(key)
    if cached is not None:
        return cached["best_idx"], cached["confidence"], cached["reason"]

    prompt = _build_prompt(alias, candidates)
    try:
        raw = invoke_claude(prompt, system=JUDGE_SYSTEM)
        parsed = parse_json_response(raw)
    except Exception as exc:  # noqa: BLE001 — log and fall back
        log.warning("Bedrock call failed for %r: %s", alias.name, exc)
        return 1, 0.5, f"llm_call_failed: {exc!s}"

    if not isinstance(parsed, dict):
        log.warning("LLM returned non-dict for %r: %r", alias.name, raw[:200])
        return 1, 0.5, "llm_response_unparseable"

    idx = parsed.get("best_idx")
    if not isinstance(idx, int) or not (1 <= idx <= len(candidates)):
        log.warning("LLM returned bad best_idx for %r: %r", alias.name, idx)
        return 1, 0.5, f"llm_bad_idx:{idx}"

    confidence = float(parsed.get("confidence", 0.0))
    reason = str(parsed.get("reason", ""))[:300]

    _cache_put(key, {"best_idx": idx, "confidence": confidence, "reason": reason})
    return idx, confidence, reason


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _resolve_one(alias: ItemRecord, candidates: list[tuple[ItemRecord, float]]) -> ResolutionRecord:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    top_k_payload = [
        {
            "name": c.name,
            "item_id": c.item_id,
            "score": round(s, 4),
            "form": c.form,
            "veg": c.veg,
        }
        for c, s in candidates
    ]

    common = dict(
        alias=alias.name,
        alias_item_id=alias.item_id,
        alias_form=alias.form,
        alias_veg=alias.veg,
        alias_category_name=alias.category_name,
        alias_typecode_name=alias.typecode_name,
        top_k=top_k_payload,
        llm_model=BEDROCK_MODEL_ID,
        prompt_version=PROMPT_VERSION,
        computed_at=now,
    )

    if not candidates:
        return ResolutionRecord(
            **common,
            best_canonical=None,
            best_canonical_item_id=None,
            best_canonical_score=0.0,
            confidence=0.0,
            reason="no candidates above per-form floor",
            decision_source="no_candidates",
        )

    if len(candidates) == 1:
        can, score = candidates[0]
        return ResolutionRecord(
            **common,
            best_canonical=can.name,
            best_canonical_item_id=can.item_id,
            best_canonical_score=score,
            confidence=1.0,
            reason="only one candidate above floor",
            decision_source="single_candidate",
        )

    idx, conf, reason = _llm_pick(alias, candidates)
    chosen_can, chosen_score = candidates[idx - 1]
    return ResolutionRecord(
        **common,
        best_canonical=chosen_can.name,
        best_canonical_item_id=chosen_can.item_id,
        best_canonical_score=chosen_score,
        confidence=conf,
        reason=reason,
        decision_source="llm",
    )


def run(
    limit: int | None = None,
    only: list[str] | None = None,
    workers: int = 1,
) -> None:
    qdrant = get_qdrant_client()
    log.info("Scrolling Qdrant collections…")
    aliases = _scroll_all(qdrant, ALIAS_COLLECTION)
    canonicals = _scroll_all(qdrant, CANONICAL_COLLECTION)
    log.info("  %d aliases, %d canonicals", len(aliases), len(canonicals))

    log.info("Fetching llm_descriptions from Neo4j…")
    _attach_neo4j_descriptions(aliases, FETCH_ALIAS_DESCRIPTIONS_QUERY)
    _attach_neo4j_descriptions(canonicals, FETCH_CANONICAL_DESCRIPTIONS_QUERY)

    if only:
        wanted = {n.strip() for n in only}
        aliases = [a for a in aliases if a.name in wanted]
        log.info("Filtered to %d aliases by --aliases", len(aliases))
    if limit:
        aliases = aliases[:limit]
        log.info("Limited to first %d aliases", len(aliases))

    canonical_matrix = np.stack([c.vector for c in canonicals])
    log.info("Canonical matrix: shape=%s", canonical_matrix.shape)

    # Build all candidate sets up front — pure numpy, fast, no API calls.
    log.info("Computing candidate shortlists…")
    candidate_sets: list[list[tuple[ItemRecord, float]]] = [
        _select_candidates(a, canonicals, canonical_matrix) for a in aliases
    ]

    out: list[dict[str, Any] | None] = [None] * len(aliases)
    by_source = {"llm": 0, "single_candidate": 0, "no_candidates": 0}
    start = time.time()
    completed = 0

    def _do_one(idx: int) -> tuple[int, ResolutionRecord]:
        return idx, _resolve_one(aliases[idx], candidate_sets[idx])

    if workers <= 1:
        log.info("Resolving aliases serially…")
        for i in range(len(aliases)):
            _, record = _do_one(i)
            out[i] = record.to_jsonable()
            by_source[record.decision_source] += 1
            completed += 1
            if completed % 50 == 0 or completed == len(aliases):
                log.info(
                    "  [%d/%d] %.1fs | llm=%d single=%d none=%d",
                    completed, len(aliases), time.time() - start,
                    by_source["llm"], by_source["single_candidate"], by_source["no_candidates"],
                )
    else:
        log.info("Resolving aliases with %d workers…", workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_do_one, i) for i in range(len(aliases))]
            for fut in as_completed(futures):
                idx, record = fut.result()
                out[idx] = record.to_jsonable()
                by_source[record.decision_source] += 1
                completed += 1
                if completed % 50 == 0 or completed == len(aliases):
                    log.info(
                        "  [%d/%d] %.1fs | llm=%d single=%d none=%d",
                        completed, len(aliases), time.time() - start,
                        by_source["llm"], by_source["single_candidate"], by_source["no_candidates"],
                    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps({"records": out}, indent=2))
    log.info("Wrote %d records to %s in %.1fs", len(out), OUTPUT_PATH, time.time() - start)
    log.info("Decision sources: %s", by_source)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N aliases")
    parser.add_argument("--aliases", type=str, default=None,
                        help="Comma-separated alias names to process (overrides --limit)")
    parser.add_argument("--workers", type=int, default=6,
                        help="Concurrent Bedrock workers. 1 = serial. Default 6.")
    args = parser.parse_args()

    only = [s for s in args.aliases.split(",")] if args.aliases else None
    try:
        run(limit=args.limit, only=only, workers=args.workers)
    finally:
        close_connections()
