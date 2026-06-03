"""API Gateway handler for v7 platter search.

POST /search
Body:
    {
        "dishes": ["Garlic Naan", "Paneer Butter Masala"],
        "top_n": 10,                       (optional, default 10)
        "service_types": ["MEALBOX"]       (optional)
    }

Response:
    {
        "version": "v1",
        "count": N,
        "results": [PlatterResultV7 as dict, ...]
    }

The runtime index (S3 JSON) and platter cache (DDB scans) load lazily on
first call. Subsequent warm invocations reuse the in-memory state.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any

from core import runtime_index
from scripts.search_v7 import search_platters_v7

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _bad_request(message: str) -> dict[str, Any]:
    return {
        "statusCode": 400,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": message}),
    }


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    raw_body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw_body = base64.b64decode(raw_body).decode()
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError as e:
        return _bad_request(f"invalid JSON: {e}")

    dishes = body.get("dishes")
    if not isinstance(dishes, list) or not all(isinstance(d, str) for d in dishes):
        return _bad_request("`dishes` must be a list of strings")
    if not dishes:
        return _bad_request("`dishes` cannot be empty")

    top_n = body.get("top_n", 10)
    if not isinstance(top_n, int) or top_n <= 0:
        return _bad_request("`top_n` must be a positive integer")

    service_types = body.get("service_types")
    if service_types is not None:
        if not isinstance(service_types, list) or not all(isinstance(s, str) for s in service_types):
            return _bad_request("`service_types` must be a list of strings")

    try:
        results = search_platters_v7(
            dishes,
            top_n=top_n,
            service_types=service_types,
        )
        version = runtime_index.load().version
    except Exception as exc:  # noqa: BLE001 — surface as 500 with message
        log.exception("search failed")
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "internal_error", "detail": str(exc)}),
        }

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "version": version,
            "count": len(results),
            "results": [asdict(r) for r in results],
        }),
    }
