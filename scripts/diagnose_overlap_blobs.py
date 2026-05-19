"""For each MISS/EMPTY in the overlap diagnostic, print the embedding blob
on both sides (Supabase alias vs DynamoDB canonical) so we can see exactly
what differs between two items that share the same name.

Reads diagnostics/overlap_quality.csv, picks rows where status != TOP1_OK,
fetches both items from Neo4j, and writes a side-by-side dump.

Usage:
    python -m scripts.diagnose_overlap_blobs
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from core.connections import close_connections, neo4j_session
from core.embedding_text import build_item_embedding_text

IN_PATH = Path("diagnostics/overlap_quality.csv")
OUT_PATH = Path("diagnostics/overlap_blob_diff.txt")

FETCH = """
MATCH (i:Item)
WHERE toLower(i.name) = toLower($name)
RETURN i.source AS source, i.name AS name,
       i.itemType AS item_type,
       coalesce(i.category_name, i.itemCategory) AS category,
       i.llm_description AS llm_description
"""


def parse_desc(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def main() -> None:
    if not IN_PATH.exists():
        raise SystemExit(f"Missing {IN_PATH} — run diagnose_overlap_items first.")

    misses: list[tuple[str, str]] = []
    with IN_PATH.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["status"] in ("MISS", "EMPTY"):
                misses.append((row["supabase_name"], row["status"]))

    print(f"Inspecting {len(misses)} non-TOP1 overlap pairs.\n")

    OUT_PATH.parent.mkdir(exist_ok=True)
    out = OUT_PATH.open("w")

    def emit(s: str = "") -> None:
        print(s)
        out.write(s + "\n")

    with neo4j_session() as session:
        for name, status in misses:
            rows = list(session.run(FETCH, name=name))
            by_source = {r["source"]: r for r in rows}
            sup = by_source.get("supabase")
            dyn = by_source.get("dynamodb")
            if not sup or not dyn:
                continue

            sup_desc = parse_desc(sup["llm_description"])
            dyn_desc = parse_desc(dyn["llm_description"])

            emit("=" * 92)
            emit(f"[{status}]  {name}")
            emit("-" * 92)
            emit(f"  {'field':<22} {'SUPABASE (alias)':<32} {'DYNAMODB (canonical)':<32}")
            emit(f"  {'-'*22} {'-'*32} {'-'*32}")
            for field in ("cuisine", "category", "sub_category", "cooking_method",
                          "flavor_profile", "texture", "regional_variant", "veg_type"):
                a = str(sup_desc.get(field, "") or "")[:32]
                b = str(dyn_desc.get(field, "") or "")[:32]
                mark = " ≠" if a.strip().lower() != b.strip().lower() else "  "
                emit(f"{mark}{field:<22} {a:<32} {b:<32}")
            sa = ", ".join(sup_desc.get("primary_ingredients", []) or [])[:32]
            sb = ", ".join(dyn_desc.get("primary_ingredients", []) or [])[:32]
            mark = " ≠" if sa.lower() != sb.lower() else "  "
            emit(f"{mark}{'primary_ingredients':<22} {sa:<32} {sb:<32}")

            emit("")
            emit("  Supabase blob :  " + build_item_embedding_text(sup["name"], sup["llm_description"]))
            emit("  DynamoDB blob :  " + build_item_embedding_text(dyn["name"], dyn["llm_description"]))
            emit("")

    close_connections()
    out.close()
    print(f"\nFull dump written to {OUT_PATH}")


if __name__ == "__main__":
    main()
