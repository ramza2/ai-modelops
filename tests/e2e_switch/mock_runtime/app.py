"""CPU-only OpenAI-compatible Mock Runtime for P1-8K Switch E2E.

No GPU, no model weights, no Docker Engine access. Records inference calls so
Gateway routing cutover can be asserted against a specific instance.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

INSTANCE_ID = os.environ.get("MOCK_INSTANCE_ID", "mock-runtime")
SERVED_MODEL = os.environ.get("MOCK_SERVED_MODEL_NAME", "mock-model")
FAIL_HEALTH = os.environ.get("MOCK_FAIL_HEALTH", "").lower() in {"1", "true", "yes"}
FAIL_INFERENCE = os.environ.get("MOCK_FAIL_INFERENCE", "").lower() in {
    "1",
    "true",
    "yes",
}

app = FastAPI(title=f"ModelOps Mock Runtime ({INSTANCE_ID})")
_calls: list[dict[str, Any]] = []


@app.get("/health")
async def health() -> dict[str, str] | JSONResponse:
    if FAIL_HEALTH:
        return JSONResponse(status_code=503, content={"status": "UNHEALTHY"})
    return {"status": "ok", "instance_id": INSTANCE_ID}


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": SERVED_MODEL, "object": "model", "owned_by": INSTANCE_ID}],
    }


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> dict[str, Any] | JSONResponse:
    body = await request.json()
    _calls.append(
        {
            "path": "/v1/embeddings",
            "instance_id": INSTANCE_ID,
            "model": body.get("model"),
            "ts": time.time(),
        }
    )
    if FAIL_INFERENCE:
        return JSONResponse(
            status_code=503,
            content={"error": {"message": "mock inference failure", "type": "server"}},
        )
    inputs = body.get("input", [])
    if isinstance(inputs, str):
        inputs = [inputs]
    data = [
        {
            "object": "embedding",
            "index": i,
            "embedding": [float((i + 1) % 7), float((i + 2) % 11), float(hash(INSTANCE_ID) % 13)],
        }
        for i, _ in enumerate(inputs or ["x"])
    ]
    return {
        "object": "list",
        "data": data,
        "model": body.get("model") or SERVED_MODEL,
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
        # Non-standard diagnostic field; Gateway passes JSON through.
        "instance_id": INSTANCE_ID,
    }


@app.post("/v1/chat/completions")
async def chat(request: Request) -> dict[str, Any] | JSONResponse:
    body = await request.json()
    _calls.append(
        {
            "path": "/v1/chat/completions",
            "instance_id": INSTANCE_ID,
            "model": body.get("model"),
            "ts": time.time(),
        }
    )
    if FAIL_INFERENCE:
        return JSONResponse(
            status_code=503,
            content={"error": {"message": "mock inference failure", "type": "server"}},
        )
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "model": body.get("model") or SERVED_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": f"hello from {INSTANCE_ID}",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        "instance_id": INSTANCE_ID,
    }


@app.get("/harness/calls")
async def harness_calls() -> dict[str, Any]:
    return {"instance_id": INSTANCE_ID, "calls": list(_calls)}


@app.post("/harness/reset")
async def harness_reset() -> dict[str, str]:
    _calls.clear()
    return {"status": "ok"}
