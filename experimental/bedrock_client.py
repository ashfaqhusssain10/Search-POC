"""Shared Bedrock Claude client.

Used by:
  - scripts/search_v6.py (runtime LLM-as-judge for substitutes)
  - scripts/precompute_alias_resolution.py (offline alias-to-canonical rerank)

Reads `AWS_REGION` (defaults to ap-south-1, where this project's account is set up).
The model id uses the global inference profile so the call works from any region.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from botocore.config import Config

log = logging.getLogger(__name__)

BEDROCK_MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
BEDROCK_REGION = os.getenv("AWS_REGION", "ap-south-1")
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TIMEOUT_SECONDS = 20

_client = None


def get_bedrock_client():
    """Lazy singleton. Reuses one connection pool across the process."""
    global _client
    if _client is None:
        _client = boto3.client(
            "bedrock-runtime",
            region_name=BEDROCK_REGION,
            config=Config(
                read_timeout=DEFAULT_TIMEOUT_SECONDS,
                connect_timeout=5,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
    return _client


def invoke_claude(
    prompt: str,
    system: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """Single Claude call via Bedrock. Returns the raw text response.

    Callers parse JSON / structured output themselves — keeps this module
    free of prompt-specific logic.
    """
    body: dict[str, Any] = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system

    resp = get_bedrock_client().invoke_model(
        modelId=BEDROCK_MODEL_ID,
        body=json.dumps(body),
    )
    payload = json.loads(resp["body"].read())
    return payload["content"][0]["text"]


def parse_json_response(text: str) -> dict[str, Any] | list[Any] | None:
    """Tolerant JSON parser. Strips markdown fences and extracts the outermost
    JSON object/array if the model added extra prose.
    """
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if "\n" in t:
            head, _, body = t.partition("\n")
            if head.strip().lower() in ("json", ""):
                t = body
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        for open_ch, close_ch in (("{", "}"), ("[", "]")):
            start = t.find(open_ch)
            end = t.rfind(close_ch)
            if start >= 0 and end > start:
                try:
                    return json.loads(t[start:end + 1])
                except json.JSONDecodeError:
                    continue
        log.warning("Claude returned unparseable JSON: %r", text[:300])
        return None
