"""Standalone RDS migration — no project imports, drop into CloudShell with alias_resolution.json.

Usage in CloudShell:
    pip install pymysql -q
    python migrate_standalone.py

Env vars (set before running):
    RDS_HOST, RDS_PORT, RDS_DB, RDS_USER, RDS_PASSWORD
"""

from __future__ import annotations

import json
import logging
import os
import sys

import boto3
import pymysql
import pymysql.cursors

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
RESOLUTION_JSON = os.getenv("RESOLUTION_JSON", "alias_resolution.json")
DDB_TABLE       = os.getenv("DDB_TABLE", "Item-Item-Similarity-Search")
AWS_REGION      = os.getenv("AWS_REGION", "ap-south-1")

RDS_HOST     = os.environ["RDS_HOST"]
RDS_PORT     = int(os.getenv("RDS_PORT", "3306"))
RDS_DB       = os.environ["RDS_DB"]
RDS_USER     = os.environ["RDS_USER"]
RDS_PASSWORD = os.environ["RDS_PASSWORD"]

# ── SQL ───────────────────────────────────────────────────────────────────────
CREATE_TABLE_SQL = """
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

UPSERT_SQL = """
INSERT INTO item_similarity_resolution
    (alias_item_id, alias_name, alias_category_name, alias_typecode_name,
     veg_type, alias_form, best_canonical, best_canonical_score,
     confidence, reason, decision_source, top_k, llm_model, prompt_version, computed_at)
VALUES
    (%(alias_item_id)s, %(alias_name)s, %(alias_category_name)s, %(alias_typecode_name)s,
     %(veg_type)s, %(alias_form)s, %(best_canonical)s, %(best_canonical_score)s,
     %(confidence)s, %(reason)s, %(decision_source)s, %(top_k)s, %(llm_model)s,
     %(prompt_version)s, %(computed_at)s)
ON DUPLICATE KEY UPDATE
    alias_name            = VALUES(alias_name),
    alias_category_name   = VALUES(alias_category_name),
    alias_typecode_name   = VALUES(alias_typecode_name),
    veg_type              = VALUES(veg_type),
    alias_form            = VALUES(alias_form),
    best_canonical        = VALUES(best_canonical),
    best_canonical_score  = VALUES(best_canonical_score),
    confidence            = VALUES(confidence),
    reason                = VALUES(reason),
    decision_source       = VALUES(decision_source),
    top_k                 = VALUES(top_k),
    llm_model             = VALUES(llm_model),
    prompt_version        = VALUES(prompt_version),
    computed_at           = VALUES(computed_at);
"""


def fetch_ddb_category_fields() -> dict[str, dict]:
    """Returns {alias_item_id: {alias_category_name, alias_typecode_name}}."""
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


def connect() -> pymysql.Connection:
    ssl_opts = None
    if os.path.exists("global-bundle.pem"):
        ssl_opts = {"ca": "global-bundle.pem"}
        log.info("Using SSL with global-bundle.pem")
    return pymysql.connect(
        host=RDS_HOST,
        port=RDS_PORT,
        database=RDS_DB,
        user=RDS_USER,
        password=RDS_PASSWORD,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        ssl=ssl_opts,
        connect_timeout=10,
    )


def main() -> None:
    if not os.path.exists(RESOLUTION_JSON):
        log.error("File not found: %s", RESOLUTION_JSON)
        sys.exit(1)

    log.info("Loading %s", RESOLUTION_JSON)
    with open(RESOLUTION_JSON) as f:
        data = json.load(f)
    # Handle both {records: [...]} and bare list formats
    records = data["records"] if isinstance(data, dict) else data
    log.info("Loaded %d records", len(records))

    log.info("Fetching category fields from DDB (%s)…", AWS_REGION)
    ddb_fields = fetch_ddb_category_fields()

    log.info("Connecting to RDS %s:%s/%s…", RDS_HOST, RDS_PORT, RDS_DB)
    conn = connect()
    log.info("Connected")

    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
        conn.commit()
        log.info("Table created/verified")

        rows = []
        for rec in records:
            pk = rec.get("alias_item_id")
            if not pk:
                continue
            cat = ddb_fields.get(pk, {})
            rows.append({
                "alias_item_id":       pk,
                "alias_name":          rec["alias"],
                "alias_category_name": cat.get("alias_category_name") or None,
                "alias_typecode_name": cat.get("alias_typecode_name") or None,
                "veg_type":            rec.get("alias_veg"),
                "alias_form":          rec.get("alias_form"),
                "best_canonical":      rec.get("best_canonical"),
                "best_canonical_score":rec.get("best_canonical_score"),
                "confidence":          rec.get("confidence"),
                "reason":              rec.get("reason"),
                "decision_source":     rec.get("decision_source"),
                "top_k":               json.dumps(rec.get("top_k") or []),
                "llm_model":           rec.get("llm_model"),
                "prompt_version":      rec.get("prompt_version"),
                "computed_at":         rec.get("computed_at"),
            })

        BATCH = 100
        for i in range(0, len(rows), BATCH):
            chunk = rows[i: i + BATCH]
            with conn.cursor() as cur:
                cur.executemany(UPSERT_SQL, chunk)
            conn.commit()
            log.info("  upserted %d / %d", min(i + BATCH, len(rows)), len(rows))

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM item_similarity_resolution")
            count = cur.fetchone()["cnt"]
        log.info("Done. Total rows in RDS: %d", count)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
