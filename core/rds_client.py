"""MySQL RDS client for item_similarity_resolution lookups.

Reads RDS_HOST, RDS_PORT, RDS_DB, RDS_USER, RDS_PASSWORD from env.
Provides a lazy singleton connection and a batch-fetch function with
the same interface as ddb_resolution.get_many().
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

import pymysql
import pymysql.cursors

log = logging.getLogger(__name__)

_lock = threading.Lock()
_conn: pymysql.Connection | None = None


def _get_connection() -> pymysql.Connection:
    global _conn
    with _lock:
        if _conn is None or not _conn.open:
            _conn = pymysql.connect(
                host=os.environ["RDS_HOST"],
                port=int(os.getenv("RDS_PORT", "3306")),
                db=os.environ["RDS_DB"],
                user=os.environ["RDS_USER"],
                password=os.environ["RDS_PASSWORD"],
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
                connect_timeout=5,
            )
        return _conn


def get_alias_names() -> list[str]:
    """Return all distinct alias names from item_similarity_resolution, sorted."""
    conn = _get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT alias_name FROM item_similarity_resolution ORDER BY alias_name")
        return [row["alias_name"] for row in cur.fetchall()]


def get_resolutions_batch(alias_item_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch resolution records by alias_item_id. Returns {alias_item_id: record}."""
    if not alias_item_ids:
        return {}

    placeholders = ",".join(["%s"] * len(alias_item_ids))
    sql = f"SELECT * FROM item_similarity_resolution WHERE alias_item_id IN ({placeholders})"

    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, alias_item_ids)
            rows = cur.fetchall()
    except pymysql.OperationalError:
        # Reconnect on stale connection
        global _conn
        with _lock:
            _conn = None
        conn = _get_connection()
        with conn.cursor() as cur:
            cur.execute(sql, alias_item_ids)
            rows = cur.fetchall()

    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        pk = row["alias_item_id"]
        # Deserialize top_k JSON string → list
        if isinstance(row.get("top_k"), str):
            try:
                row["top_k"] = json.loads(row["top_k"])
            except (json.JSONDecodeError, TypeError):
                row["top_k"] = []
        # Normalize field names to match ddb_resolution output
        row["alias_veg"] = row.get("veg_type")
        out[pk] = dict(row)
    return out
