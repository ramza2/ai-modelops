"""Fake Node Agent for P1-8K multi-service Switch E2E.

Simulates managed container lifecycle over HTTP without Docker Engine, NVIDIA
runtime, privileged mode, host network, or host model mounts. Health/probe
may optionally call bound Mock Runtime URLs over the compose network.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

TOKEN = os.environ.get("NODE_AGENT_TOKEN", "").strip()
GPU_UUID = os.environ.get("FAKE_GPU_UUID", "GPU-P18K-FAKE-0")
VRAM_TOTAL_MB = int(os.environ.get("FAKE_VRAM_TOTAL_MB", "16000"))
VRAM_FREE_MB = int(os.environ.get("FAKE_VRAM_FREE_MB", "14000"))

app = FastAPI(title="ModelOps Fake Node Agent (P1-8K)")

# deployment_id -> container state
_containers: dict[str, dict[str, Any]] = {}
# deployment_id -> upstream base URL for health/probe
_upstreams: dict[str, str] = {}
# Queued failures: "start"|"health"|"probe" -> remaining fail counts or lists
_fail_next: dict[str, list[str]] = {}
_calls: list[dict[str, Any]] = []
_vram_free_mb = VRAM_FREE_MB
_source_used_vram_mb = 0
_source_deployment_id: str | None = None


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "error": {
                "code": "UNAUTHORIZED",
                "message": "Invalid or missing agent token.",
            }
        },
    )


def _require_token(authorization: str | None) -> JSONResponse | None:
    if not TOKEN:
        return None
    if not authorization or not authorization.lower().startswith("bearer "):
        return _unauthorized()
    if authorization.split(" ", 1)[1].strip() != TOKEN:
        return _unauthorized()
    return None


def _error(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body)


def _record(method: str, path: str, body: Any) -> None:
    _calls.append({"method": method, "path": path, "body": body, "ts": time.time()})


class BindRequest(BaseModel):
    deployment_id: str = Field(min_length=1)
    upstream_base_url: str = Field(min_length=1)
    runtime_status: str = "CREATED"
    container_id: str | None = None
    container_name: str | None = None
    source_used_vram_mb: int | None = None


class FailNextRequest(BaseModel):
    action: str = Field(min_length=1)  # start|health|probe|prepare
    deployment_id: str | None = None
    times: int = Field(default=1, ge=1, le=20)


class VramRequest(BaseModel):
    free_mb: int = Field(ge=0)
    source_used_vram_mb: int | None = None
    source_deployment_id: str | None = None


@app.middleware("http")
async def auth_middleware(request: Request, call_next):  # noqa: ANN001
    path = request.url.path
    if path in {"/health", "/ready"} or path.startswith("/harness/"):
        return await call_next(request)
    if path.startswith("/internal/v1/"):
        denied = _require_token(request.headers.get("authorization"))
        if denied is not None:
            return denied
    return await call_next(request)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "UP"}


@app.get("/ready")
async def ready() -> dict[str, object]:
    # Fake agent is always READY for Switch E2E (no Docker/NVML).
    return {
        "status": "READY",
        "docker": "AVAILABLE",
        "nvml": "AVAILABLE",
        "mode": "fake-p18k",
    }


@app.get("/internal/v1/health")
async def internal_health() -> dict[str, str]:
    return {"status": "UP"}


@app.get("/internal/v1/ready")
async def internal_ready() -> dict[str, object]:
    return await ready()


@app.get("/internal/v1/node")
async def node_info() -> dict[str, object]:
    return {
        "hostname": "fake-node-p18k",
        "agent_version": "p18k-fake",
        "gpu_count": 1,
    }


@app.get("/internal/v1/resources")
async def resources() -> dict[str, object]:
    processes: list[dict[str, Any]] = []
    if _source_deployment_id and _source_used_vram_mb > 0:
        processes.append(
            {
                "pid": 4242,
                "deployment_id": _source_deployment_id,
                "used_vram_mb": _source_used_vram_mb,
            }
        )
    return {
        "gpus": [
            {
                "gpu_uuid": GPU_UUID,
                "device_index": 0,
                "vram_total_mb": VRAM_TOTAL_MB,
                "vram_free_mb": _vram_free_mb,
                "vram_used_mb": max(0, VRAM_TOTAL_MB - _vram_free_mb),
                "processes": processes,
            }
        ]
    }


@app.post("/internal/v1/resources/wait-vram-release")
async def wait_vram_release(body: dict[str, Any]) -> dict[str, Any]:
    _ = body
    # After stop, free VRAM is already raised; report success immediately.
    return {
        "released": True,
        "gpus": [
            {
                "device_index": 0,
                "gpu_uuid": GPU_UUID,
                "free_vram_mb": _vram_free_mb,
            }
        ],
    }


def _pop_fail(action: str, deployment_id: str) -> bool:
    queued = _fail_next.get(action) or []
    if not queued:
        return False
    # Match specific deployment or wildcard "*"
    for i, item in enumerate(list(queued)):
        if item in {deployment_id, "*"}:
            queued.pop(i)
            _fail_next[action] = queued
            return True
    return False


@app.get("/internal/v1/deployments/{deployment_id}")
async def get_deployment(deployment_id: str) -> dict[str, Any] | JSONResponse:
    ctr = _containers.get(deployment_id)
    if ctr is None:
        return _error(404, "CONTAINER_NOT_FOUND", "not found")
    return ctr


@app.post("/internal/v1/deployments/{deployment_id}/prepare")
async def prepare(deployment_id: str, request: Request) -> dict[str, Any] | JSONResponse:
    body = await request.json()
    _record("POST", request.url.path, body)
    if _pop_fail("prepare", deployment_id):
        return _error(409, "IMAGE_NOT_READY", "fake prepare failure")
    artifacts = body.get("artifacts") or []
    return {
        "status": "READY",
        "image_ready": True,
        "artifacts_ready": True,
        "artifacts": [
            {
                "artifact_id": a.get("artifact_id"),
                "target_path": a.get("target_path"),
                "ready": True,
                "verified_checksum": "p18k-fake",
                "error": None,
            }
            for a in artifacts
        ],
    }


@app.post("/internal/v1/deployments/{deployment_id}/create", status_code=201)
async def create(deployment_id: str, request: Request) -> dict[str, Any] | JSONResponse:
    body = await request.json()
    _record("POST", request.url.path, body)
    existing = _containers.get(deployment_id)
    if existing is not None:
        return JSONResponse(status_code=200, content=existing)
    upstream = _upstreams.get(deployment_id)
    if not upstream:
        # Prefer environment hint from Worker create payload when present.
        env = body.get("environment") or {}
        upstream = env.get("MOCK_UPSTREAM_URL") or env.get("UPSTREAM_BASE_URL")
    ctr = {
        "deployment_id": deployment_id,
        "container_id": f"ctr-{uuid.uuid4().hex[:12]}",
        "container_name": body.get("container_name"),
        "runtime_status": "CREATED",
        "command": body.get("command"),
        "runtime_image": body.get("runtime_image"),
        "runtime_port": body.get("runtime_port"),
        "upstream_base_url": upstream,
    }
    _containers[deployment_id] = ctr
    if upstream:
        _upstreams[deployment_id] = str(upstream)
    return ctr


@app.post("/internal/v1/deployments/{deployment_id}/start")
async def start(deployment_id: str, request: Request) -> dict[str, Any] | JSONResponse:
    body = None
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = None
    _record("POST", request.url.path, body)
    if _pop_fail("start", deployment_id):
        return _error(500, "CONTAINER_START_FAILED", "fake start failure")
    ctr = _containers.get(deployment_id)
    if ctr is None:
        # Idempotent bootstrap for seeded Running Source without prior create.
        upstream = _upstreams.get(deployment_id)
        ctr = {
            "deployment_id": deployment_id,
            "container_id": f"ctr-{uuid.uuid4().hex[:12]}",
            "container_name": f"dep-{deployment_id[:8]}",
            "runtime_status": "RUNNING",
            "upstream_base_url": upstream,
        }
        _containers[deployment_id] = ctr
    else:
        ctr["runtime_status"] = "RUNNING"
    return ctr


@app.post("/internal/v1/deployments/{deployment_id}/stop")
async def stop(deployment_id: str, request: Request) -> dict[str, Any] | JSONResponse:
    body = None
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    _record("POST", request.url.path, body)
    ctr = _containers.get(deployment_id)
    if ctr is None:
        return _error(404, "CONTAINER_NOT_FOUND", "missing")
    ctr["runtime_status"] = "STOPPED"
    global _vram_free_mb, _source_used_vram_mb
    if _source_deployment_id == deployment_id:
        _source_used_vram_mb = 0
        _vram_free_mb = max(_vram_free_mb, 14000)
    return ctr


@app.post("/internal/v1/deployments/{deployment_id}/restart")
async def restart(deployment_id: str, request: Request) -> dict[str, Any] | JSONResponse:
    _ = await start(deployment_id, request)
    return _containers[deployment_id]


@app.delete("/internal/v1/deployments/{deployment_id}")
async def remove(deployment_id: str) -> Response | JSONResponse:
    ctr = _containers.get(deployment_id)
    if ctr is None:
        return _error(404, "CONTAINER_NOT_FOUND", "missing")
    if ctr.get("runtime_status") == "RUNNING":
        return _error(409, "CONTAINER_CONFLICT", "running")
    del _containers[deployment_id]
    return Response(status_code=204)


@app.get("/internal/v1/deployments/{deployment_id}/health")
async def deployment_health(
    deployment_id: str,
    health_path: str = "/health",
    timeout_seconds: float = 5.0,
) -> dict[str, Any] | JSONResponse:
    _record("GET", f"/internal/v1/deployments/{deployment_id}/health", None)
    ctr = _containers.get(deployment_id)
    if ctr is None:
        return _error(404, "CONTAINER_NOT_FOUND", "missing")
    if _pop_fail("health", deployment_id):
        return {
            "deployment_id": deployment_id,
            "runtime_status": ctr.get("runtime_status"),
            "health_status": "UNHEALTHY",
            "http_status": 503,
            "latency_ms": 1,
            "checked_at": _now(),
            "message": "fake health failure",
        }
    if ctr.get("runtime_status") != "RUNNING":
        return {
            "deployment_id": deployment_id,
            "runtime_status": ctr.get("runtime_status"),
            "health_status": "UNKNOWN",
            "http_status": None,
            "latency_ms": 0,
            "checked_at": _now(),
            "message": "not running",
        }
    upstream = _upstreams.get(deployment_id) or ctr.get("upstream_base_url")
    if upstream:
        url = f"{str(upstream).rstrip('/')}{health_path}"
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                resp = await client.get(url)
            healthy = resp.status_code == 200
            return {
                "deployment_id": deployment_id,
                "runtime_status": "RUNNING",
                "health_status": "HEALTHY" if healthy else "UNHEALTHY",
                "http_status": resp.status_code,
                "latency_ms": 5,
                "checked_at": _now(),
                "message": None if healthy else "upstream unhealthy",
            }
        except httpx.HTTPError as exc:
            return {
                "deployment_id": deployment_id,
                "runtime_status": "RUNNING",
                "health_status": "UNHEALTHY",
                "http_status": None,
                "latency_ms": 5,
                "checked_at": _now(),
                "message": str(exc),
            }
    return {
        "deployment_id": deployment_id,
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
        "http_status": 200,
        "latency_ms": 1,
        "checked_at": _now(),
        "message": None,
    }


@app.post("/internal/v1/deployments/{deployment_id}/probe")
async def probe(deployment_id: str, request: Request) -> dict[str, Any] | JSONResponse:
    body = await request.json()
    _record("POST", request.url.path, body)
    ctr = _containers.get(deployment_id)
    if ctr is None:
        return _error(404, "CONTAINER_NOT_FOUND", "missing")
    if _pop_fail("probe", deployment_id):
        return {
            "success": False,
            "latency_ms": 2,
            "checked_at": _now(),
            "error_code": "PROBE_HTTP_ERROR",
            "error_message": "fake probe failure",
        }
    upstream = _upstreams.get(deployment_id) or ctr.get("upstream_base_url")
    probe_type = str(body.get("probe_type") or "CHAT").upper()
    served = str(body.get("served_model_name") or "mock")
    timeout = float(body.get("timeout_seconds") or 30)
    if not upstream:
        return {
            "success": True,
            "latency_ms": 1,
            "checked_at": _now(),
            "error_code": None,
            "error_message": None,
        }
    path = "/v1/embeddings" if probe_type == "EMBEDDING" else "/v1/chat/completions"
    url = f"{str(upstream).rstrip('/')}{path}"
    payload: dict[str, Any]
    if probe_type == "EMBEDDING":
        payload = {"model": served, "input": ["p18k-probe"]}
    else:
        payload = {
            "model": served,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
        }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
        ok = resp.status_code == 200
        return {
            "success": ok,
            "latency_ms": 10,
            "checked_at": _now(),
            "error_code": None if ok else "PROBE_HTTP_ERROR",
            "error_message": None if ok else f"Probe HTTP {resp.status_code}",
        }
    except httpx.HTTPError as exc:
        return {
            "success": False,
            "latency_ms": 3,
            "checked_at": _now(),
            "error_code": "PROBE_TRANSPORT_ERROR",
            "error_message": str(exc),
        }


# --- Harness control API (test-only; not a production Node Agent surface) ---


@app.post("/harness/bind")
async def harness_bind(body: BindRequest) -> dict[str, Any]:
    _upstreams[body.deployment_id] = body.upstream_base_url.rstrip("/")
    ctr = {
        "deployment_id": body.deployment_id,
        "container_id": body.container_id or f"ctr-{uuid.uuid4().hex[:12]}",
        "container_name": body.container_name or f"dep-{body.deployment_id[:8]}",
        "runtime_status": body.runtime_status,
        "upstream_base_url": body.upstream_base_url.rstrip("/"),
    }
    _containers[body.deployment_id] = ctr
    global _source_deployment_id, _source_used_vram_mb
    if body.source_used_vram_mb is not None:
        _source_deployment_id = body.deployment_id
        _source_used_vram_mb = int(body.source_used_vram_mb)
    return ctr


@app.post("/harness/fail-next")
async def harness_fail_next(body: FailNextRequest) -> dict[str, Any]:
    key = body.action.strip().lower()
    target = body.deployment_id or "*"
    bucket = _fail_next.setdefault(key, [])
    bucket.extend([target] * int(body.times))
    return {"action": key, "queued": list(bucket)}


@app.post("/harness/vram")
async def harness_vram(body: VramRequest) -> dict[str, Any]:
    global _vram_free_mb, _source_used_vram_mb, _source_deployment_id
    _vram_free_mb = int(body.free_mb)
    if body.source_used_vram_mb is not None:
        _source_used_vram_mb = int(body.source_used_vram_mb)
    if body.source_deployment_id is not None:
        _source_deployment_id = body.source_deployment_id
    return {
        "free_mb": _vram_free_mb,
        "source_used_vram_mb": _source_used_vram_mb,
        "source_deployment_id": _source_deployment_id,
    }


@app.post("/harness/reset")
async def harness_reset() -> dict[str, str]:
    _containers.clear()
    _upstreams.clear()
    _fail_next.clear()
    _calls.clear()
    global _vram_free_mb, _source_used_vram_mb, _source_deployment_id
    _vram_free_mb = VRAM_FREE_MB
    _source_used_vram_mb = 0
    _source_deployment_id = None
    return {"status": "ok"}


@app.get("/harness/calls")
async def harness_calls() -> dict[str, Any]:
    return {"calls": list(_calls), "containers": dict(_containers)}
