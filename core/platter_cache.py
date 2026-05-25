"""In-memory cache of platter data sourced live from DynamoDB.

Replaces Neo4j as the platter retrieval layer. Builds a single in-memory
snapshot of all three platter tables on first access; subsequent calls hit
the cache. Refresh manually via `refresh()` or restart the process.

Public API:
  - get_cache()                                  → PlatterCache singleton
  - PlatterCache.fetch_for_canonicals(names, …)  → list of platter dicts
                                                   with the exact shape v5/v6
                                                   previously expected from
                                                   the Neo4j Cypher

Why singleton + in-memory:
  - 5,606 platter-item rows + 1,021 categories + 211 platters is ~50KB. Tiny.
  - DynamoDB scan latency is ~1-3s once; query latency at scale is milliseconds.
  - Streamlit / FastAPI workers each load it once, then reuse it.

Mirrors the v5 Cypher behaviour exactly:
  - matched_items: canonical names in this platter that intersect query
  - all_items   : every item in the platter (deduped)
  - skeleton_raw: list of {family, slot_count, order} dicts
  - service_types filter applied at fetch time
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

import boto3
from boto3.dynamodb.types import Decimal

from core.categories import category_family, normalize_category

log = logging.getLogger(__name__)

AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
PLATTERS_TABLE = "DefaultPlattersTable"
PLATTER_CATEGORIES_TABLE = "DefaultPlattersCategoriesTable"
PLATTER_ITEMS_TABLE = "DefaultPlatterItemsTable"
MENU_ITEMS_TABLE = "MenuItemsTable"


# ---------------------------------------------------------------------------
# Lightweight parsing helpers (mirror load_platters.py)
# ---------------------------------------------------------------------------

def _str(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _float_or_none(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int_or_none(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _parse_meal_times(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, set)):
        return sorted({_str(x).upper() for x in v if x})
    raw = _str(v).upper()
    return [raw] if raw else []


# ---------------------------------------------------------------------------
# Cache data model
# ---------------------------------------------------------------------------

@dataclass
class PlatterRecord:
    id: str
    name: str
    platter_type: str | None
    meal_type: list[str]
    veg: bool | None
    min_price: float | None
    active: bool
    # Items in this platter (by canonical item name, deduped)
    all_items: list[str] = field(default_factory=list)
    # Skeleton: aggregated category slots
    skeleton_raw: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------

class PlatterCache:
    """Thread-safe lazy cache. Loads from DynamoDB on first use."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loaded = False
        self._platters: dict[str, PlatterRecord] = {}
        # Reverse index: item_name → set[platter_id]
        self._item_to_platters: dict[str, set[str]] = {}

    # ── Loading ────────────────────────────────────────────────────────────

    def _scan(self, table_name: str) -> list[dict[str, Any]]:
        table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(table_name)
        out: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {}
        while True:
            resp = table.scan(**kwargs)
            out.extend(resp.get("Items", []))
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        log.info("PlatterCache: scanned %d rows from %s", len(out), table_name)
        return out

    def _build(self) -> None:
        """One-time load: pull all four tables and stitch together.

        Activity policy:
          - Skip items where itemActive != 'ACTIVE'.
          - Skip platters where platterActive != 'ACTIVE' (enforced at
            fetch-time via only_active=True).
          - Skip platter-item references that point to inactive items.
        This mirrors what production actually serves; inactive items are
        legacy/test rows that would otherwise inflate the catalog.
        """
        # Item id → name (canonical) for joining, ACTIVE items only
        menu_rows = self._scan(MENU_ITEMS_TABLE)
        item_id_to_name: dict[str, str] = {}
        inactive_skipped = 0
        for r in menu_rows:
            iid = _str(r.get("itemId"))
            name = _str(r.get("itemName"))
            active = _str(r.get("itemActive")).upper() == "ACTIVE"
            if not iid or not name:
                continue
            if not active:
                inactive_skipped += 1
                continue
            item_id_to_name[iid] = name
        log.info(
            "PlatterCache: %d active items kept, %d inactive items skipped",
            len(item_id_to_name), inactive_skipped,
        )

        # Platters
        platter_rows = self._scan(PLATTERS_TABLE)
        platters: dict[str, PlatterRecord] = {}
        for r in platter_rows:
            pid = _str(r.get("platterId"))
            if not pid:
                continue
            active = _str(r.get("platterActive")).upper() == "ACTIVE"
            platters[pid] = PlatterRecord(
                id=pid,
                name=_str(r.get("platterName")),
                platter_type=_str(r.get("platterType")) or None,
                meal_type=_parse_meal_times(r.get("mealTimes")),
                veg=_str(r.get("menuType")).upper() == "VEG"
                    if _str(r.get("menuType")) else None,
                min_price=_float_or_none(r.get("minPrice")),
                active=active,
            )

        # Categories (skeleton)
        # Aggregate per platter: family → (slot_count, min_order)
        cat_rows = self._scan(PLATTER_CATEGORIES_TABLE)
        per_platter_family: dict[str, dict[str, tuple[int, int]]] = {}
        for r in cat_rows:
            pid = _str(r.get("platterId"))
            if pid not in platters:
                continue
            raw_name = _str(r.get("categoryName"))
            family = category_family(raw_name) or normalize_category(raw_name) or "Other"
            slots = _int_or_none(r.get("categoryItemsLimit")) or 0
            order = _int_or_none(r.get("category_order")) or 999
            if slots <= 0:
                continue
            cur = per_platter_family.setdefault(pid, {})
            existing_count, existing_order = cur.get(family, (0, 999))
            cur[family] = (existing_count + slots, min(existing_order, order))

        for pid, fams in per_platter_family.items():
            platters[pid].skeleton_raw = [
                {"family": fam, "slot_count": count, "order": order}
                for fam, (count, order) in sorted(fams.items(), key=lambda kv: kv[1][1])
            ]

        # Items per platter
        item_rows = self._scan(PLATTER_ITEMS_TABLE)
        platter_items_set: dict[str, set[str]] = {}
        item_to_platters: dict[str, set[str]] = {}
        unresolved = 0
        for r in item_rows:
            pid = _str(r.get("platterId"))
            iid = _str(r.get("itemId"))
            if not pid or not iid or pid not in platters:
                continue
            name = item_id_to_name.get(iid)
            if not name:
                unresolved += 1
                continue
            platter_items_set.setdefault(pid, set()).add(name)
            item_to_platters.setdefault(name, set()).add(pid)
        if unresolved:
            log.warning(
                "PlatterCache: %d platter-item rows referenced itemIds not in MenuItemsTable",
                unresolved,
            )

        for pid, names in platter_items_set.items():
            platters[pid].all_items = sorted(names)

        self._platters = platters
        self._item_to_platters = item_to_platters
        self._loaded = True
        log.info(
            "PlatterCache ready: %d platters, %d items, %d item→platter edges",
            len(platters),
            len(item_to_platters),
            sum(len(v) for v in item_to_platters.values()),
        )

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if not self._loaded:
                self._build()

    def refresh(self) -> None:
        """Force a re-scan. Caller should expect ~3s latency."""
        with self._lock:
            self._loaded = False
            self._build()

    # ── Public query ───────────────────────────────────────────────────────

    def fetch_for_canonicals(
        self,
        canonical_names: list[str],
        service_types: list[str] | None = None,
        only_active: bool = True,
    ) -> list[dict[str, Any]]:
        """Return platter dicts in the exact shape v5/v6 expect from the
        former Cypher query.

        Each dict has keys:
            id, name, platter_type, meal_type, veg, min_price,
            matched_items, all_items, skeleton_raw
        """
        self._ensure_loaded()
        wanted = set(canonical_names)

        # Reverse lookup: find every platter that contains at least one wanted name
        candidate_pids: set[str] = set()
        for name in wanted:
            candidate_pids |= self._item_to_platters.get(name, set())

        out: list[dict[str, Any]] = []
        for pid in candidate_pids:
            p = self._platters.get(pid)
            if p is None:
                continue
            if only_active and not p.active:
                continue
            if service_types and p.platter_type not in service_types:
                continue
            matched = [n for n in p.all_items if n in wanted]
            out.append({
                "id": p.id,
                "name": p.name,
                "platter_type": p.platter_type,
                "meal_type": p.meal_type,
                "veg": p.veg,
                "min_price": p.min_price,
                "matched_items": matched,
                "all_items": p.all_items,
                "skeleton_raw": p.skeleton_raw,
            })
        return out


# Module-level singleton — first import is cheap, first query triggers the scan.
_cache_singleton: PlatterCache | None = None


def get_cache() -> PlatterCache:
    global _cache_singleton
    if _cache_singleton is None:
        _cache_singleton = PlatterCache()
    return _cache_singleton
