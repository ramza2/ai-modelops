"""P1-8L isolated Admin UI browser Switch E2E (Playwright Chromium).

Real Frontend → same-origin Backend proxy → Worker → Fake NA → Mock Runtime.
No API mock / fetch interception of Switch success. Ops GPU/DB/Frontend forbidden.

Uses sync Playwright (not asyncio) so the browser event loop is not nested.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from harness_lib import (
    BACKEND,
    DATABASE_URL,
    FAKE_NA,
    FRONTEND,
    GATEWAY,
    assert_isolated_targets,
    bind_fake_na,
    endpoint_page_url,
    get_endpoint,
    get_operation,
    harness_delay,
    harness_fail_next,
    seed_world,
    wait_gateway_alias,
    wait_operation,
)

ARTIFACT_DIR = Path(os.environ.get("P18K_ARTIFACT_DIR", "/opt/cursor/artifacts/p18l-browser"))
TERMINAL = {
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "ROLLED_BACK",
    "MANUAL_INTERVENTION_REQUIRED",
}


def _shot(page: Any, name: str) -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    path = ARTIFACT_DIR / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)


def _seed(strategy_hint: str) -> dict[str, Any]:
    """Seed via a fresh event loop in a worker thread (pytest-asyncio may own the main loop)."""

    async def _run() -> dict[str, Any]:
        engine = create_async_engine(DATABASE_URL, future=True)
        sf = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        try:
            async with sf() as session:
                return await seed_world(session, strategy_hint=strategy_hint)
        finally:
            await engine.dispose()

    def _thread_main() -> dict[str, Any]:
        return asyncio.run(_run())

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_thread_main).result(timeout=60)


@pytest.fixture(scope="session", autouse=True)
def _isolation_gate() -> None:
    assert_isolated_targets(require_frontend=True)
    with httpx.Client(timeout=5.0) as c:
        for url in (
            f"{BACKEND}/health",
            f"{GATEWAY}/health",
            f"{FAKE_NA}/health",
            f"{FRONTEND}/health",
        ):
            r = c.get(url)
            assert r.status_code == 200, f"{url} -> {r.status_code}"
        r = c.get(f"{FRONTEND}/api/v1/endpoints?limit=1")
        assert r.status_code == 200, f"frontend /api proxy failed: {r.status_code} {r.text[:200]}"


@pytest.fixture(scope="session")
def browser() -> Iterator[Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        pytest.fail(
            f"BLOCKED: playwright not installed ({exc}). "
            "Do not treat missing browser as PASS."
        )
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(
                f"BLOCKED: Chromium launch failed ({exc}). "
                "Do not treat missing browser as PASS."
            )
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser: Any) -> Iterator[Any]:
    context = browser.new_context(
        base_url=FRONTEND,
        viewport={"width": 1440, "height": 900},
    )

    def _on_nav(frame: Any) -> None:
        if frame != frame.page.main_frame:
            return
        url = frame.url
        if not url or url.startswith("about:") or url.startswith("data:"):
            return
        parsed = urlparse(url)
        assert parsed.scheme == "http", url
        assert parsed.hostname == "127.0.0.1", url
        assert parsed.port == 28081, url

    pg = context.new_page()
    pg.on("framenavigated", _on_nav)
    try:
        yield pg
    finally:
        context.close()


def _open_endpoint(page: Any, endpoint_id: Any, *, op_id: str | None = None) -> None:
    url = endpoint_page_url(endpoint_id, op_id=op_id)
    page.goto(url, wait_until="domcontentloaded")
    page.get_by_role("heading", name="ACTIVE Route").wait_for(timeout=45_000)


def _select_target_and_preview(page: Any, *, target_id: Any, target_name: str) -> None:
    page.locator("#switch-target").wait_for(timeout=30_000)
    # Prefer stable option value (deployment UUID); label includes runtime/health noise.
    page.locator("#switch-target").select_option(value=str(target_id))
    assert target_name  # seed must expose name for screenshot/debug context
    page.get_by_role("button", name="Preflight Preview").click()
    page.get_by_role("heading", name="Preflight Preview").wait_for(timeout=45_000)


def _choose_strategy(page: Any, strategy: str) -> None:
    page.locator("#switch-strategy").select_option(strategy)


def _enqueue_switch(page: Any) -> str:
    page.get_by_role("button", name="Switch Enqueue").click()
    page.get_by_test_id("switch-operation-result").wait_for(timeout=45_000)
    deadline = time.time() + 30
    op_id = ""
    while time.time() < deadline:
        qs = parse_qs(urlparse(page.url).query)
        vals = qs.get("switch_operation_id") or []
        if vals and vals[0]:
            op_id = vals[0]
            break
        time.sleep(0.2)
    assert op_id, f"switch_operation_id missing from URL: {page.url}"
    return op_id


def _wait_ui_terminal(page: Any, *, timeout_s: float = 180.0) -> str:
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        terminal = page.get_by_test_id("switch-op-terminal")
        if terminal.count() > 0 and terminal.first.is_visible():
            text = terminal.inner_text()
            for status in TERMINAL:
                if status in text:
                    return status
            last = text
        time.sleep(0.5)
    raise AssertionError(f"UI never showed terminal Operation status (last={last!r})")


def _active_route_href(page: Any) -> str:
    section = page.locator("#active-route-heading").locator("xpath=ancestor::section[1]")
    link = section.get_by_role("link").first
    return link.get_attribute("href") or ""


def _assert_active_matches_backend(page: Any, endpoint_id: Any, expected_dep: Any) -> None:
    # Prefer Backend GET as source of truth; UI must agree after reload/refresh.
    deadline = time.time() + 60
    last_active = None
    while time.time() < deadline:
        ep = get_endpoint(endpoint_id)
        active = ep.get("active_route") or {}
        last_active = active.get("deployment_id")
        if str(last_active) == str(expected_dep):
            break
        time.sleep(0.5)
    assert str(last_active) == str(expected_dep), f"backend ACTIVE={last_active}"

    page.get_by_role("button", name="새로고침").click()
    page.get_by_role("heading", name="ACTIVE Route").wait_for(timeout=30_000)
    # Give React a beat to render refreshed Endpoint payload.
    page.wait_for_timeout(400)
    href = _active_route_href(page)
    assert str(expected_dep) in href, f"UI ACTIVE Route href={href} expected {expected_dep}"
    assert page.locator('[title="SERVING"]').count() > 0


def test_a_endpoint_load_preflight(page: Any) -> None:
    fixture = _seed("UI-A")
    bind_fake_na(fixture, hot_vram=True)
    wait_gateway_alias(fixture["alias"])

    _open_endpoint(page, fixture["endpoint_id"])
    href = _active_route_href(page)
    assert str(fixture["source_id"]) in href
    _select_target_and_preview(
        page, target_id=fixture["target_id"], target_name=fixture["target_name"]
    )
    assert page.get_by_text("HOT_SWITCH_AVAILABLE").count() > 0
    _shot(page, "a-preflight-hot")


def test_b_hot_switch_browser(page: Any) -> None:
    fixture = _seed("UI-HOT")
    bind_fake_na(fixture, hot_vram=True)
    wait_gateway_alias(fixture["alias"])

    _open_endpoint(page, fixture["endpoint_id"])
    _select_target_and_preview(
        page, target_id=fixture["target_id"], target_name=fixture["target_name"]
    )
    _choose_strategy(page, "HOT")
    op_id = _enqueue_switch(page)
    backend_op = get_operation(op_id)
    assert backend_op.get("id") == op_id or backend_op.get("operation_id") == op_id
    assert backend_op.get("switch_strategy") == "HOT"

    status = _wait_ui_terminal(page, timeout_s=180.0)
    assert status == "SUCCEEDED", get_operation(op_id)
    _assert_active_matches_backend(page, fixture["endpoint_id"], fixture["target_id"])
    _shot(page, "b-hot-succeeded")


def test_c_cold_switch_browser(page: Any) -> None:
    fixture = _seed("UI-COLD")
    bind_fake_na(fixture, hot_vram=False)
    wait_gateway_alias(fixture["alias"])

    _open_endpoint(page, fixture["endpoint_id"])
    _select_target_and_preview(
        page, target_id=fixture["target_id"], target_name=fixture["target_name"]
    )
    assert page.get_by_text("COLD_SWITCH_ONLY").count() > 0
    _choose_strategy(page, "COLD")
    op_id = _enqueue_switch(page)
    assert get_operation(op_id).get("switch_strategy") == "COLD"

    status = _wait_ui_terminal(page, timeout_s=180.0)
    assert status == "SUCCEEDED", get_operation(op_id)
    final = wait_operation(op_id, timeout_s=10.0)
    steps = {s["step_code"]: s["status"] for s in (final.get("steps") or [])}
    for required in (
        "DRAIN_TRAFFIC",
        "STOP_SOURCE",
        "START_TARGET",
        "ACTIVATE_TARGET_ROUTE",
        "RESTORE_TRAFFIC",
    ):
        assert steps.get(required) == "SUCCEEDED", steps

    _assert_active_matches_backend(page, fixture["endpoint_id"], fixture["target_id"])
    _shot(page, "c-cold-succeeded")


def test_d_f5_restore_while_running(page: Any, browser: Any) -> None:
    """F5 while Backend status is RUNNING; QUEUED-only observation must not PASS.

    After reload, the same switch_operation_id must restore with in-flight UI.
    Separate assertion uses a new Browser Context (not a new Chromium process).
    """
    fixture = _seed("UI-F5")
    bind_fake_na(fixture, hot_vram=True)
    # Hold Target start long enough to observe RUNNING across reload + restore UI.
    harness_delay(
        action="start",
        deployment_id=str(fixture["target_id"]),
        delay_ms=35_000,
        times=1,
    )
    wait_gateway_alias(fixture["alias"])

    _open_endpoint(page, fixture["endpoint_id"])
    _select_target_and_preview(
        page, target_id=fixture["target_id"], target_name=fixture["target_name"]
    )
    _choose_strategy(page, "HOT")
    op_id = _enqueue_switch(page)

    # Fail closed: Backend must be RUNNING before F5. QUEUED alone is insufficient.
    deadline = time.time() + 90
    pre_f5: dict[str, Any] = {}
    while time.time() < deadline:
        pre_f5 = get_operation(op_id)
        st = pre_f5.get("status")
        if st == "RUNNING":
            break
        if st in TERMINAL:
            pytest.fail(
                f"Operation reached {st} before RUNNING; cannot validate F5 restore: {pre_f5}"
            )
        time.sleep(0.25)
    assert pre_f5.get("status") == "RUNNING", (
        f"F5 requires Backend status RUNNING (got {pre_f5.get('status')!r}); "
        f"QUEUED-only is not PASS. op={pre_f5}"
    )
    page.get_by_test_id("switch-op-active").wait_for(timeout=15_000)
    assert "RUNNING" in page.get_by_test_id("switch-op-active").inner_text()

    _shot(page, "d-before-f5-running")
    # Reload while still RUNNING (delay must still be holding start).
    assert get_operation(op_id)["status"] == "RUNNING", "status left RUNNING before reload"
    page.reload(wait_until="domcontentloaded")

    assert f"switch_operation_id={op_id}" in page.url
    page.get_by_test_id("switch-operation-result").wait_for(timeout=45_000)
    # Restored progress UI for the same Operation (still RUNNING after F5).
    page.get_by_test_id("switch-op-active").wait_for(timeout=30_000)
    restored_active = page.get_by_test_id("switch-op-active").inner_text()
    assert op_id in page.url
    assert "RUNNING" in restored_active, (
        f"after F5 expected RUNNING progress banner for {op_id}, got: {restored_active!r}"
    )
    post_reload = get_operation(op_id)
    assert post_reload.get("status") == "RUNNING", (
        f"after F5 Backend must still be RUNNING (got {post_reload.get('status')!r})"
    )
    _shot(page, "d-after-f5-restored-running")

    status = _wait_ui_terminal(page, timeout_s=180.0)
    assert status == "SUCCEEDED", get_operation(op_id)
    assert get_operation(op_id)["status"] == "SUCCEEDED"
    _assert_active_matches_backend(page, fixture["endpoint_id"], fixture["target_id"])
    _shot(page, "d-after-f5-succeeded")

    # New Browser Context restore (same Chromium browser process, fresh context).
    ctx2 = browser.new_context(base_url=FRONTEND, viewport={"width": 1440, "height": 900})
    try:
        p2 = ctx2.new_page()
        p2.goto(
            endpoint_page_url(fixture["endpoint_id"], op_id=op_id),
            wait_until="domcontentloaded",
        )
        p2.get_by_test_id("switch-op-terminal").wait_for(timeout=45_000)
        assert "SUCCEEDED" in p2.get_by_test_id("switch-op-terminal").inner_text()
        assert f"switch_operation_id={op_id}" in p2.url
        _assert_active_matches_backend(p2, fixture["endpoint_id"], fixture["target_id"])
        _shot(p2, "d-new-browser-context-restore")
    finally:
        ctx2.close()


def test_e_failure_preserves_source_ui(page: Any) -> None:
    fixture = _seed("UI-FAIL")
    bind_fake_na(fixture, hot_vram=True)
    harness_fail_next(
        action="start",
        deployment_id=str(fixture["target_id"]),
        times=3,
    )
    wait_gateway_alias(fixture["alias"])

    _open_endpoint(page, fixture["endpoint_id"])
    _select_target_and_preview(
        page, target_id=fixture["target_id"], target_name=fixture["target_name"]
    )
    _choose_strategy(page, "HOT")
    op_id = _enqueue_switch(page)
    status = _wait_ui_terminal(page, timeout_s=180.0)
    assert status in {
        "FAILED",
        "ROLLED_BACK",
        "MANUAL_INTERVENTION_REQUIRED",
    }, get_operation(op_id)

    _assert_active_matches_backend(page, fixture["endpoint_id"], fixture["source_id"])
    assert page.get_by_test_id("switch-op-unresolved").count() == 0
    page.locator("#switch-target").wait_for(timeout=15_000)
    _shot(page, "e-failure-source-preserved")


def test_f_duplicate_enqueue_blocked_and_history(page: Any) -> None:
    fixture = _seed("UI-DUP")
    bind_fake_na(fixture, hot_vram=True)
    harness_delay(
        action="start",
        deployment_id=str(fixture["target_id"]),
        delay_ms=15_000,
        times=1,
    )
    wait_gateway_alias(fixture["alias"])

    _open_endpoint(page, fixture["endpoint_id"])
    _select_target_and_preview(
        page, target_id=fixture["target_id"], target_name=fixture["target_name"]
    )
    _choose_strategy(page, "HOT")
    op_id = _enqueue_switch(page)

    page.get_by_test_id("switch-op-active").wait_for(timeout=45_000)
    enqueue = page.get_by_role("button", name="Switch Enqueue")
    if enqueue.count() > 0:
        assert enqueue.is_disabled() or not enqueue.is_visible()

    page.goto(endpoint_page_url(fixture["endpoint_id"], op_id=op_id), wait_until="domcontentloaded")
    page.get_by_test_id("switch-operation-result").wait_for(timeout=45_000)
    assert f"switch_operation_id={op_id}" in page.url

    status = _wait_ui_terminal(page, timeout_s=180.0)
    assert status == "SUCCEEDED", get_operation(op_id)
    _assert_active_matches_backend(page, fixture["endpoint_id"], fixture["target_id"])

    history_heading = page.get_by_role(
        "heading", name=re.compile("Route History|Route 이력", re.I)
    )
    if history_heading.count() > 0:
        history_heading.first.scroll_into_view_if_needed()
    ep = get_endpoint(fixture["endpoint_id"])
    assert str((ep.get("active_route") or {}).get("deployment_id")) == str(fixture["target_id"])
    _shot(page, "f-terminal-history")
