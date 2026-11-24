# REC-04B2 — Server Backend startup and migration separation

Refs #115, REC-04A, REC-04B1. Server overlay only. Base local Compose keeps its
existing development startup contract. No new migration revision.

## Ordinary server start

- Server Backend command is python -m app.core.server_startup. PostgreSQL
  and Backend server restart policy is unless-stopped; explicit stops stay
  stopped. Gateway/Worker policy and startup command unchanged.
- Validate actual image Alembic graph (single valid head, known revisions).
  Connect to the configured DB with a dedicated short-lived read-only
  connection (default_transaction_read_only and SET TRANSACTION READ ONLY).
  Require one version row, known revision, exact head. Never call upgrade,
  downgrade, stamp or create missing schema on normal startup.
- Retry DB connection failures up to 12 attempts, 5s delay, connection
  timeout 3s, statement timeout 3s, lock timeout 1s. DNS/driver delays can
  extend wall clock. Missing/unknown/older/ambiguous revision and other
  query errors fail closed. Emit fixed reason codes, no DSN or raw error.
- On success dispose probe engine and exec Uvicorn as the container process.
  On failure exit nonzero; Docker may retry with its restart backoff (not
  a migration or automatic rollback). A permanently blocked image must be
  diagnosed and corrected by an operator. This is a startup check, not a
  continuous schema drift monitor; /ready retains existing DB reachability
  semantics. Never migrate against concurrently active application services.
- Compose depends_on is not a daemon reboot ordering guarantee.
  REC-04A host-side candidate/target check remains required before every
  approved existing Backend deployment or rollback.

## Fresh installation only

The existing fresh-only deploy-server.sh now refuses any existing project
service and repeats the REC-04A fresh target check after building images.
It starts only PostgreSQL first, then runs a one-shot Backend command
python -m app.core.server_startup --bootstrap-empty. This is the explicit
schema initialization step of a fresh installation, not a normal restart.
It obtains a transaction-scoped advisory lock, requires an empty standard
public schema/database (no user schemas/public relations, functions/types),
uses the same connection and transaction for Alembic upgrade, verifies the
single resulting head before commit. The ordinary server start follows.
Do not invoke against an existing installation; existing schema is rejected.
The lock coordinates cooperating bootstrap commands, not arbitrary external
DDL; serialize deployment and exclude all other writers. A failed partial
bootstrap is not retried automatically: investigate, do not delete volumes.
No existing-schema migration automation is added.

## Existing DB migration (separate explicit operator approval)

Prepare and pin approved image, take and verify backup and isolated restore,
review migration and recovery plan, quiesce incompatible writers in an
approved maintenance window. Verify target identity and DB revision, then
run only the explicitly approved one-shot Alembic command in the pinned
image using the correct DB environment. Do not use --bootstrap-empty,
stamp, downgrade or all-service deploy helper to repair an existing DB.
Re-run REC-04A exact-head gate before service-scoped startup. Rollback must
be schema-compatible; no automatic rollback or DDL. No command against
production is authorized by this PR.

## Verification

GitHub hosted isolated Docker only: actual Backend image and disposable
PostgreSQL 16 network/volume, no published ports, no model/GPU or production
environment. Assert empty normal start blocked without creating version
table; explicit bootstrap initializes exact head; re-bootstrap refused;
synthetic older, unknown, multiple and empty version rows blocked unchanged;
delayed PostgreSQL start and Backend restart reach /health and /ready with
unchanged DB revision/schema hash. Synthetic revision edits are not a real
downgrade test. REC-04A regression, shell syntax, Frontend Compose contract
and existing CI remain required. CI success does not prove production
reboot recovery. Production rollout separately approved, with exact image
and rollback compatibility and no unrelated project changes.
