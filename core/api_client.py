"""Client for the deployed v7 search API.

Wraps the Lambda endpoint exposed by `infra/cdk/searchpoc_stack.py`. Returns
results with the same attribute shape as `PlatterResultV5`/`V7` so existing
Streamlit rendering code works without changes.

Configuration via env vars (typically in .env):
    SEARCH_API_URL   — full base URL ending in `/prod/` (no trailing /search)
    SEARCH_API_KEY   — value of the x-api-key header

If SEARCH_API_URL is unset, `is_configured()` returns False so the UI can
fall back to local v5/v6.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def api_url() -> str:
    return _env("SEARCH_API_URL")


def api_key() -> str:
    return _env("SEARCH_API_KEY")


def is_configured() -> bool:
    return bool(api_url() and api_key())


class _NS:
    """Lightweight attribute-access wrapper for dict payloads."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.__dict__.update(data)

    def __repr__(self) -> str:
        return f"_NS({self.__dict__!r})"


def _hydrate(value: Any) -> Any:
    if isinstance(value, dict):
        return _NS({k: _hydrate(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_hydrate(v) for v in value]
    return value


def search_platters_via_api(
    dishes: list[str],
    top_n: int = 10,
    service_types: list[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[list[Any], str]:
    """Returns (results, version). Raises RuntimeError on network/API failure."""
    base = api_url().rstrip("/")
    if not base:
        raise RuntimeError("SEARCH_API_URL is not set")
    key = api_key()
    if not key:
        raise RuntimeError("SEARCH_API_KEY is not set")

    body = json.dumps({
        "dishes": dishes,
        "top_n": top_n,
        "service_types": service_types,
    }).encode()
    req = urllib.request.Request(
        f"{base}/search",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"API {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"API network error: {e.reason}") from e

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"API returned non-JSON: {raw[:200]}") from e

    results = [_hydrate(r) for r in payload.get("results", [])]
    return results, payload.get("version", "?")
