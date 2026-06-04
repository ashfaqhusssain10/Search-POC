"""FastAPI HTTP server for ECS deployment.

Wraps handler.handler in a real HTTP server (uvicorn) so the same container
image works for both Lambda (CMD handler.handler) and ECS (CMD uvicorn server:app ...).

API key validation is done here (matches the API Gateway key injected as env var
API_KEY). The body is forwarded to handler.handler in Lambda event format.

Run locally:
    API_KEY=dev uvicorn lambda.server:app --port 8080
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from core import runtime_index
from scripts.search_v7 import search_platters_v7

log = logging.getLogger(__name__)

_API_KEY = os.environ.get("API_KEY", "")

app = FastAPI(title="SearchPOC v7", version="1.0.0")


@app.post("/search")
async def search(request: Request, x_api_key: str = Header(default="")) -> JSONResponse:
    if _API_KEY and x_api_key != _API_KEY:
        raise HTTPException(status_code=403, detail="forbidden")

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    dishes = body.get("dishes")
    if not isinstance(dishes, list) or not all(isinstance(d, str) for d in dishes):
        raise HTTPException(status_code=400, detail="`dishes` must be a list of strings")
    if not dishes:
        raise HTTPException(status_code=400, detail="`dishes` cannot be empty")

    top_n = body.get("top_n", 10)
    if not isinstance(top_n, int) or top_n <= 0:
        raise HTTPException(status_code=400, detail="`top_n` must be a positive integer")

    service_types = body.get("service_types")
    if service_types is not None and (
        not isinstance(service_types, list)
        or not all(isinstance(s, str) for s in service_types)
    ):
        raise HTTPException(status_code=400, detail="`service_types` must be a list of strings")

    try:
        index = runtime_index.load()
        # Resolve sub_N IDs to alias names if the caller sent IDs instead of names
        resolved_dishes = [
            index.alias_id_to_name.get(d, d) for d in dishes
        ]
        results = search_platters_v7(resolved_dishes, top_n=top_n, service_types=service_types)
        version = index.version
    except Exception as exc:
        log.exception("search failed")
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "detail": str(exc)},
        )

    return JSONResponse(content={
        "version": version,
        "count": len(results),
        "results": [asdict(r) for r in results],
    })


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
