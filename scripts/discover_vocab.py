"""Pass 1: Discover and canonicalize the vocabulary used in llm_description.

Reads the existing llm_description JSON from every Item in Neo4j, collects
unique values per closed-vocab field, then asks Gemini to cluster the raw
strings into a canonical vocabulary (canonical label → synonyms).

Output: vocab/{field}.json — one file per field, structured as:
    {
      "canonical_label_1": ["raw value", "another raw value", ...],
      "canonical_label_2": [...],
      ...
    }

Pass 2 (separate script) will use these to constrain re-enrichment so both
alias and canonical pick from the same closed list.

Usage:
    python -m scripts.discover_vocab
    python -m scripts.discover_vocab --fields cuisine,sub_category   # subset
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from pathlib import Path

from google import genai
from google.genai import types

from core.connections import close_connections, neo4j_session
from core.settings import GEMINI_API_KEY

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

MODEL = "gemini-2.5-flash"
OUT_DIR = Path("vocab")

# Fields where a closed vocabulary makes sense. Free-form fields like
# `texture`, `flavor_profile`, `primary_ingredients` are deliberately excluded.
DEFAULT_FIELDS = [
    "cuisine",
    "category",
    "sub_category",
    "cooking_method",
    "regional_variant",
]

CLUSTER_PROMPT_TEMPLATE = """You are normalizing a vocabulary used to tag Indian dishes.

Below is the full list of raw values an LLM produced for the field "{field}",
along with how many times each value appeared.

Your job: cluster these raw values into a SMALL set of canonical labels
(typically 10-25 for sub_category, 8-15 for cuisine, etc.). Each canonical
label should:
  - be Title Case and short (1-3 words)
  - represent a real, meaningful grouping (not a catch-all)
  - cover semantically equivalent or near-equivalent raw values

Rules:
  - Merge spelling variants ("Chocolatey" + "Chocolaty"), plurals
    ("Noodles" + "Noodle Dish"), and synonyms ("Pan fried" + "Stir-fried"
    + "Sauteed") into ONE canonical label.
  - Pick the clearest, most standard term as the canonical.
  - DO NOT lose information by over-merging unrelated concepts. E.g. "Dal"
    and "Stew" are related but distinct — keep them separate.
  - Every raw value must end up under exactly one canonical label.
  - Return a JSON object: {{"canonical_label": ["raw1", "raw2", ...], ...}}

Field being normalized: {field}

Raw values (with counts):
{values}

Respond with ONLY the JSON object. No commentary, no markdown fences."""


def parse_desc(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def collect_raw_values(field: str) -> Counter[str]:
    """Scan Neo4j Item.llm_description JSON and tally distinct values for `field`."""
    counts: Counter[str] = Counter()
    with neo4j_session() as session:
        rows = session.run(
            "MATCH (i:Item) WHERE i.llm_description IS NOT NULL "
            "RETURN i.llm_description AS desc"
        )
        for r in rows:
            desc = parse_desc(r["desc"])
            value = desc.get(field)
            if not value:
                continue
            if isinstance(value, list):
                for v in value:
                    if v:
                        counts[str(v).strip()] += 1
            else:
                counts[str(value).strip()] += 1
    return counts


def cluster_with_gemini(field: str, counts: Counter[str]) -> dict[str, list[str]]:
    """Ask Gemini to cluster raw values into canonical labels."""
    if not counts:
        return {}
    lines = [f"  {v!r:<50} ({n})" for v, n in counts.most_common()]
    prompt = CLUSTER_PROMPT_TEMPLATE.format(field=field, values="\n".join(lines))

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0,
        ),
    )
    parsed = json.loads(response.text)
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected dict, got {type(parsed).__name__}")
    return parsed


def verify_coverage(raw_values: set[str], clusters: dict[str, list[str]]) -> tuple[set[str], set[str]]:
    """Return (missing, extra) — raw values not covered, and synonyms invented by LLM."""
    covered: set[str] = set()
    for syns in clusters.values():
        covered.update(s.strip() for s in syns)
    missing = raw_values - covered
    extra = covered - raw_values
    return missing, extra


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fields", type=str, default=None,
                        help=f"Comma-separated subset of fields (default: {','.join(DEFAULT_FIELDS)})")
    args = parser.parse_args()

    fields = (
        [f.strip() for f in args.fields.split(",") if f.strip()]
        if args.fields else DEFAULT_FIELDS
    )

    OUT_DIR.mkdir(exist_ok=True)

    for field in fields:
        log.info("─" * 80)
        log.info("Field: %s", field)

        counts = collect_raw_values(field)
        if not counts:
            log.warning("  No values found for %s — skipping.", field)
            continue
        log.info("  Collected %d distinct raw values (%d total occurrences).",
                 len(counts), sum(counts.values()))

        if len(counts) <= 1:
            clusters = {next(iter(counts)).title(): list(counts.keys())}
        else:
            log.info("  Calling Gemini to cluster...")
            clusters = cluster_with_gemini(field, counts)

        # Sort canonical labels by total frequency descending, for readability.
        scored = sorted(
            clusters.items(),
            key=lambda kv: sum(counts.get(s, 0) for s in kv[1]),
            reverse=True,
        )
        clusters_sorted = {label: syns for label, syns in scored}

        missing, extra = verify_coverage(set(counts.keys()), clusters_sorted)

        out_path = OUT_DIR / f"{field}.json"
        out_path.write_text(json.dumps(clusters_sorted, indent=2))
        log.info("  Wrote %d canonical labels → %s", len(clusters_sorted), out_path)

        for label, syns in scored[:10]:
            freq = sum(counts.get(s, 0) for s in syns)
            log.info("    %-25s [%3d items, %d synonyms]  e.g. %s",
                     label, freq, len(syns), ", ".join(syns[:3]))
        if len(scored) > 10:
            log.info("    ... and %d more", len(scored) - 10)

        if missing:
            log.warning("  %d raw values NOT covered by any cluster: %s",
                        len(missing), sorted(missing)[:5])
        if extra:
            log.warning("  %d values appear in clusters but not in raw data (LLM hallucinated): %s",
                        len(extra), sorted(extra)[:5])

    close_connections()
    log.info("─" * 80)
    log.info("Done. Vocab files in: %s/", OUT_DIR)


if __name__ == "__main__":
    main()
