# REC-04B1 — Admin Frontend startup resilience

Refs #115 / REC-04A. Scope: Frontend Nginx and server-only Frontend restart
policy. The recovery incident exposed a fatal Nginx startup DNS lookup when
Backend was absent. The original static proxy also cached Backend IP at
configuration load. Compose depends_on is not a Docker-daemon reboot ordering
or health guarantee.

## Contract

- Keep Nginx SPA and same-origin proxy on the Compose user-defined network.
  Use Docker embedded DNS 127.0.0.11, IPv4, 5s cache validity, 3s resolver
  timeout, fixed service name backend:8000. Variable proxy_pass without a URI
  preserves the original path and query, method, body and existing headers.
  No caller-provided destination; no extra ingress, host network or routes.
- SPA remains available while Backend is absent. /api/, /health and /ready
  must not return a synthetic success or SPA instead of a Backend response.
  DNS/connect failure normally produces 502; Backend's own 503 remains 503.
  Backend reappearance or IP replacement permits later requests to recover
  without Nginx reload or restart. A short failure interval is expected.
- Server overlay declares Frontend restart: unless-stopped. An explicitly
  stopped container is not forcibly started. Docker health is not a restart
  trigger; no watchdog or auto-remediation service is added.
- Base local Compose startup dependencies and port/network boundaries stay
  unchanged. Backend, PostgreSQL, Gateway, Worker policies and commands are
  unchanged. In particular, Backend still runs alembic upgrade head on start:
  do not extend automatic restart policy or claim full boot safety here.
  REC-04B2 will address Backend startup/migration separation and audit other
  service policies after reviewing existing deployment and runtime contracts.
- No Backend migration, build/recreate on production, Docker-wide restart,
  model/GPU operation, inference, or change to collector/retention flags.
  REC-04A remains mandatory for existing DB Backend image changes.

## Validation

1. Inspect branch diff; Nginx syntax check and simulated DNS tests can be run
   on a disposable host. They do not substitute for actual candidate Docker.
2. Build a candidate Frontend image from tracked source in an isolated Docker
   context (no production env). Then run:

   ```sh
   python3 -B scripts/test_frontend_startup_docker.py \
     --isolated-docker --image <local-candidate-frontend-image>
   ```

   The script uses a random internal network, no host ports, no pulls, no DB,
   no GPU, no production Compose. It starts the candidate before a mock
   Backend; verifies SPA, 502 for all three proxy paths, exact escaped URI
   and query, /health and true /ready status; changes mock Backend IP and
   checks recovery with same Frontend identity/start time/restart count.
   Cleanup targets only this invocation's uniquely labelled resources.
   Run only on an isolated Docker host/context; not while production OS is
   being updated.
3. Verify merged server Compose config has only Frontend policy change
   (do not print secrets); verify local config and existing CI as applicable.
4. Report local mock and real Docker evidence separately. No production
   fault injection, forced reboot, or deliberate Backend stop.

## Later production gate (separate approval)

Wait for Ubuntu update/reboot and baseline stability. Inspect running service
identities, live restart policies, exact Frontend image and rollback tag.
Build/validate candidate from tracked source in isolation. Deploy only
Frontend, using no-deps/no-build/pull-never with the approved immutable
candidate and explicit rollback. Verify same-origin GETs, Backend/Gateway
health, existing model state, collector continuity, other services unchanged.
Do not use deploy-server.sh normal up on an existing DB. Do not force reboot
to demonstrate recovery. The policy/config changes do not apply to already
running containers merely by merging this PR.
