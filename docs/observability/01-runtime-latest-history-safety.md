# Runtime Latest: current view vs persisted history (P1-10)

The Management API `GET /api/v1/observability/runtime/latest` reads **persisted DB snapshots**, not current Node Agent metrics. Its `availability` is the observation at `sampled_at`, **not** the present lifecycle or Gateway availability.

## API

- Legacy request (no filter) continues to return the latest snapshot for each Deployment, including STOPPED/retired ones.
- `?active_only=true` returns only Deployments whose Management DB currently has `runtime_status=RUNNING` and `retired_at IS NULL`. This does **not** guarantee readiness. The UI still checks sample freshness.
- The latest list and per-ID responses include `runtime_status` and `retired_at` so consumers can distinguish historical availability. A scoped `?deployment_id=<uuid>` remains accessible for retired Deployments regardless of `active_only`.
- History, analytics and capacity-profile endpoints are unchanged; existing DB snapshots are never removed or rewritten.

## Admin UI

The main Observability **Runtime Latest (persisted)** section requests `active_only=true` by default. Select **종료·중지된 Deployment 포함 (과거 데이터)** to show the old rows; the opt-in is URL-scoped with `include_inactive=true` and does not change the Invocation Summary.

The Availability badge favors lifecycle truth: **RETIRED** (if retired), **STOPPED/other non-running state**, then **STALE** (sample missing, invalid, >120 seconds old or far in the future). Only a running, non-retired Deployment with a fresh sample displays its observed `AVAILABLE`/`PARTIAL`/`UNAVAILABLE`. When overridden, the original observed value remains shown as **수집 당시**. This is a display classification, not a live health scrape.

Models / Versions **필터 초기화** clears query, provider, type, active and page in one action. Archived Model Registry records intentionally remain visible with Active=ALL.

## Acceptance

- Default UI does not list retired MiniLM or stopped legacy BGE, while current Qwen3/VLM/BGE snapshots remain visible.
- Historical opt-in displays retired MiniLM as **RETIRED**, with original `AVAILABLE` indicated only as past data.
- Stale running snapshots show **STALE** even if the previous observation said `AVAILABLE`.
- Direct per-deployment latest and history queries still expose retained snapshots.
- Clear button resets all four model-list filters together; no Docker/Node Agent mutations.
