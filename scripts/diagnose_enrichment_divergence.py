"""Diagnostic: find same-name alias↔canonical pairs with divergent LLM enrichments.

When an item name exists in both Supabase (alias) and DynamoDB (canonical),
independent Gemini enrichment runs produce different `llm_description` metadata.
Under no-name embeddings (where the dish name is stripped), these divergent
descriptions produce divergent vectors — so "Curd" ↔ "Curd" scores 0.75
instead of ~1.0.

This script:
  1. Reads both CSVs directly (no Neo4j needed)
  2. Finds exact-name overlaps
  3. Compares their `llm_description` field by field
  4. Reports divergences

Usage:
    python -m scripts.diagnose_enrichment_divergence
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pandas as pd

from core.embedding_text import build_item_embedding_text
from core.settings import DYNAMODB_CSV, SUPABASE_CSV

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Fields that matter for embedding divergence (the ones build_item_embedding_text uses)
EMBEDDING_FIELDS = [
    "cuisine", "category", "sub_category", "primary_ingredients",
    "cooking_method", "flavor_profile", "texture", "regional_variant",
]


def _parse(raw: str | None) -> dict:
    if not raw or not isinstance(raw, str) or raw.strip() == "":
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}


def main() -> None:
    root = Path(__file__).parent.parent
    ddb_path = root / DYNAMODB_CSV
    sub_path = root / SUPABASE_CSV

    ddb = pd.read_csv(ddb_path, dtype=str)
    sub = pd.read_csv(sub_path, dtype=str)

    # Build name → llm_description lookups
    canon_lookup: dict[str, str] = {}
    for _, row in ddb.iterrows():
        name = str(row.get("itemName", "")).strip()
        desc = str(row.get("llm_description", "")).strip()
        if name and desc:
            canon_lookup[name.lower()] = desc

    alias_lookup: dict[str, tuple[str, str]] = {}  # lower_name → (display_name, desc)
    for _, row in sub.iterrows():
        name = str(row.get("item_name", "")).strip()
        desc = str(row.get("llm_description", "")).strip()
        if name and desc:
            alias_lookup[name.lower()] = (name, desc)

    # Find overlaps
    overlaps = set(canon_lookup.keys()) & set(alias_lookup.keys())
    log.info("Found %d exact-name overlaps between Supabase and DynamoDB", len(overlaps))

    if not overlaps:
        log.info("No overlaps found — nothing to diagnose.")
        return

    divergent = []
    identical = []

    for key in sorted(overlaps):
        display_name = alias_lookup[key][0]
        canon_desc = _parse(canon_lookup[key])
        alias_desc = _parse(alias_lookup[key][1])

        # Build embedding text for each side
        canon_text = build_item_embedding_text("", json.dumps(canon_desc), include_name=False)
        alias_text = build_item_embedding_text("", json.dumps(alias_desc), include_name=False)

        if canon_text.strip() == alias_text.strip():
            identical.append(display_name)
            continue

        # Find which fields diverge
        diffs = []
        for field in EMBEDDING_FIELDS:
            cv = canon_desc.get(field, "")
            av = alias_desc.get(field, "")
            # Normalize for comparison
            if isinstance(cv, list):
                cv = sorted(str(x).lower().strip() for x in cv)
            else:
                cv = str(cv).lower().strip()
            if isinstance(av, list):
                av = sorted(str(x).lower().strip() for x in av)
            else:
                av = str(av).lower().strip()
            if cv != av:
                diffs.append((field, canon_desc.get(field), alias_desc.get(field)))

        divergent.append({
            "name": display_name,
            "diffs": diffs,
            "canon_text": canon_text,
            "alias_text": alias_text,
        })

    # Report
    log.info("")
    log.info("═══ RESULTS ═══")
    log.info("Identical embedding text: %d items (no action needed)", len(identical))
    log.info("Divergent embedding text: %d items (these cause low same-name scores)", len(divergent))

    if divergent:
        log.info("")
        log.info("─── DIVERGENT ITEMS ───")
        for item in divergent:
            log.info("")
            log.info("  %s  (%d field(s) differ)", item["name"], len(item["diffs"]))
            for field, canon_val, alias_val in item["diffs"]:
                log.info("    %-22s  canonical: %-40s  alias: %s", field, canon_val, alias_val)
            log.info("    CANONICAL blob: %s", item["canon_text"][:120])
            log.info("    ALIAS    blob: %s", item["alias_text"][:120])

    # Summary
    log.info("")
    log.info("═══ SUMMARY ═══")
    log.info("Total overlaps:    %d", len(overlaps))
    log.info("Already identical: %d  (%.0f%%)", len(identical), 100 * len(identical) / len(overlaps) if overlaps else 0)
    log.info("Need fixing:       %d  (%.0f%%)", len(divergent), 100 * len(divergent) / len(overlaps) if overlaps else 0)
    log.info("")
    log.info("Fix: run `python -m scripts.embed_items_noname` — it now auto-propagates")
    log.info("     canonical descriptions to same-name aliases before embedding.")


if __name__ == "__main__":
    main()
