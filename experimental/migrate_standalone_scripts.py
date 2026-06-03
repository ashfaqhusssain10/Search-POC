"""Standalone RDS migration — zero non-stdlib dependencies beyond boto3.

Uses the system `mysql` CLI (pre-installed in CloudShell) instead of pymysql.
Generates SQL from alias_resolution.json + DDB category fields and pipes it
directly to mysql.

Usage in CloudShell VPC:
    aws s3 cp s3://search-item-item-poc/migrate/alias_resolution.json .
    aws s3 cp s3://search-item-item-poc/migrate/migrate_standalone.py .

    RDS_HOST=... RDS_DB=catalog RDS_USER=admin RDS_PASSWORD=... python migrate_standalone.py
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

import boto3

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

RESOLUTION_JSON = os.getenv("RESOLUTION_JSON", "alias_resolution.json")
DDB_TABLE       = os.getenv("DDB_TABLE", "Item-Item-Similarity-Search")
AWS_REGION      = os.getenv("AWS_REGION", "ap-south-1")

RDS_HOST     = os.environ["RDS_HOST"]
RDS_PORT     = os.getenv("RDS_PORT", "3306")
RDS_DB       = os.environ["RDS_DB"]
RDS_USER     = os.environ["RDS_USER"]
RDS_PASSWORD = os.environ["RDS_PASSWORD"]

CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS item_similarity_resolution (
    alias_item_id         VARCHAR(50)   PRIMARY KEY,
    alias_name            VARCHAR(255)  NOT NULL,
    alias_category_name   VARCHAR(100),
    alias_typecode_name   VARCHAR(100),
    veg_type              VARCHAR(10),
    alias_form            VARCHAR(50),
    best_canonical        VARCHAR(255),
    best_canonical_score  DECIMAL(6,4),
    confidence            DECIMAL(4,3),
    reason                TEXT,
    decision_source       VARCHAR(30),
    top_k                 JSON,
    llm_model             VARCHAR(100),
    prompt_version        VARCHAR(10),
    computed_at           DATETIME DEFAULT CURRENT_TIMESTAMP,
    catalog_id            VARCHAR(36),
    catalog_item_active   TINYINT(1),
    INDEX idx_alias_name  (alias_name),
    INDEX idx_catalog_id  (catalog_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def _esc(v: object) -> str:
    """Escape a value for MySQL string literal."""
    if v is None:
        return "NULL"
    s = str(v).replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")
    return f"'{s}'"


def fetch_ddb_category_fields() -> dict[str, dict]:
    ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = ddb.Table(DDB_TABLE)
    out: dict[str, dict] = {}
    kwargs: dict = {
        "ProjectionExpression": "alias_item_id, alias_category_name, alias_typecode_name",
    }
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            pk = item.get("alias_item_id")
            if pk:
                out[pk] = {
                    "alias_category_name": item.get("alias_category_name") or "",
                    "alias_typecode_name": item.get("alias_typecode_name") or "",
                }
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    log.info("Fetched category fields for %d aliases from DDB", len(out))
    return out


def build_sql(records: list[dict], ddb_fields: dict[str, dict]) -> str:
    lines = ["SET NAMES utf8mb4;", CREATE_TABLE_SQL]
    for rec in records:
        pk = rec.get("alias_item_id")
        if not pk:
            continue
        cat = ddb_fields.get(pk, {})
        top_k_json = json.dumps(rec.get("top_k") or [])
        cols = (
            f"{_esc(pk)}, "
            f"{_esc(rec.get('alias'))}, "
            f"{_esc(cat.get('alias_category_name') or None)}, "
            f"{_esc(cat.get('alias_typecode_name') or None)}, "
            f"{_esc(rec.get('alias_veg'))}, "
            f"{_esc(rec.get('alias_form'))}, "
            f"{_esc(rec.get('best_canonical'))}, "
            f"{_esc(rec.get('best_canonical_score'))}, "
            f"{_esc(rec.get('confidence'))}, "
            f"{_esc(rec.get('reason'))}, "
            f"{_esc(rec.get('decision_source'))}, "
            f"{_esc(top_k_json)}, "
            f"{_esc(rec.get('llm_model'))}, "
            f"{_esc(rec.get('prompt_version'))}, "
            f"{_esc(rec.get('computed_at'))}"
        )
        lines.append(
            f"INSERT INTO item_similarity_resolution "
            f"(alias_item_id,alias_name,alias_category_name,alias_typecode_name,"
            f"veg_type,alias_form,best_canonical,best_canonical_score,"
            f"confidence,reason,decision_source,top_k,llm_model,prompt_version,computed_at) "
            f"VALUES ({cols}) "
            f"ON DUPLICATE KEY UPDATE "
            f"alias_name=VALUES(alias_name), alias_category_name=VALUES(alias_category_name), "
            f"alias_typecode_name=VALUES(alias_typecode_name), veg_type=VALUES(veg_type), "
            f"alias_form=VALUES(alias_form), best_canonical=VALUES(best_canonical), "
            f"best_canonical_score=VALUES(best_canonical_score), confidence=VALUES(confidence), "
            f"reason=VALUES(reason), decision_source=VALUES(decision_source), "
            f"top_k=VALUES(top_k), llm_model=VALUES(llm_model), "
            f"prompt_version=VALUES(prompt_version), computed_at=VALUES(computed_at);"
        )
    lines.append("SELECT COUNT(*) AS total_rows FROM item_similarity_resolution;")
    return "\n".join(lines)


def run_mysql(sql: str) -> None:
    cmd = [
        "mysql",
        f"-h{RDS_HOST}",
        f"-P{RDS_PORT}",
        f"-u{RDS_USER}",
        f"-p{RDS_PASSWORD}",
        "--ssl",
        RDS_DB,
    ]

    result = subprocess.run(cmd, input=sql, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        # mysql prints warnings to stderr even on success
        print(result.stderr, file=sys.stderr)
    if result.returncode != 0:
        log.error("mysql exited with code %d", result.returncode)
        sys.exit(result.returncode)


def main() -> None:
    if not os.path.exists(RESOLUTION_JSON):
        log.error("File not found: %s", RESOLUTION_JSON)
        sys.exit(1)

    log.info("Loading %s", RESOLUTION_JSON)
    with open(RESOLUTION_JSON) as f:
        data = json.load(f)
    records = data["records"] if isinstance(data, dict) else data
    log.info("Loaded %d records", len(records))

    # Skip DDB fetch — VPC has no DDB endpoint. Category fields inserted as NULL.
    ddb_fields: dict[str, dict] = {}

    log.info("Building SQL…")
    sql = build_sql(records, ddb_fields)

    log.info("Running migration via mysql CLI…")
    run_mysql(sql)
    log.info("Done.")


if __name__ == "__main__":
    main()
