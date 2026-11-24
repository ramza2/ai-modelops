# REC-04C — Control Plane image provenance and recovery evidence

Refs #115, REC-04A/B1/B2. Scope: Backend and Frontend replacement preparation,
PostgreSQL restart-policy-only verification, read-only five-service audit.
No production deployment, DB migration, GPU/model lifecycle, Worker/Gateway
replacement, automatic rollback, image cleanup or scheduler is authorized.

## Source and image identity

Build from a pinned Git commit in an isolated Docker context:

```sh
bash scripts/build-control-plane-image.sh --isolated-build backend <40-char-commit>
bash scripts/build-control-plane-image.sh --isolated-build frontend <40-char-commit>
```

The helper archives tracked service files only, uses a unique candidate tag,
records Git revision and service as image labels and verifies the actual local
immutable image ID. It does not use the running checkout's modified/untracked
files or server environment. Base images/dependencies can change between
builds; a Git SHA is not an image digest or a signed supply-chain attestation.
Keep the tested exact image ID, not just the SHA or mutable tag. Legacy image
labels may be absent: record unknown, never assign a guessed source revision.
Labels are claims, not proof of authenticity; retain the trusted build log.
A candidate built in CI is not automatically present on a production host.

## Local evidence and baseline

Use the exact ModelOps Compose project and external environment file.
Store outputs in an operator-owned directory outside the Git repository
with mode 0700, files mode 0600. Do not publish raw inspect/config/log/env,
credentials or internal addresses to the public repository. Examples below
are placeholders; use the same explicit project and env file for all steps.

```sh
umask 077
python3 -B scripts/control-plane-recovery.py --project <project> \
  --env-file /path/to/server.env audit
python3 -B scripts/control-plane-recovery.py --project <project> \
  --env-file /path/to/server.env capture > /secure/local/before.json
```

Audit returns selected per-service container/image identities, state,
health status if available, restart policy/count, start time, allowlisted
revision/service labels and whether the Backend command matches the new
startup gate. It reports missing/ambiguous/stopped services without DB access.
Capture requires all five services uniquely running, healthy PostgreSQL,
standard Compose volume and actual DB target (including live Backend URL),
single DB revision and stable re-observation. These are process and revision
checks, not HTTP or inference readiness evidence. Also run separately
approved existing same-origin read-only GET probes and check Gateway/Node
Agent/model state without mutation. Do not infer system-wide health from
a capture PASS. Audit does not identify unrelated Docker workloads.

## Approved replacement preparation, one service at a time

1. Freeze concurrent deployment/cleanup, capture baseline, verify actual
   health, backup checksum/restore evidence and rollback prerequisites.
   Use an isolated worktree and exact approved main commit, never deploy a PR
   checkout or rebuild during replacement. No cross-project changes.
2. Build candidate and run CI-equivalent isolated tests, pin its immutable ID.
   Preserve the running service image by a **separately approved** local tag
   (do not overwrite a tag pointing elsewhere). Required tag convention:
   modelops-rec04-rollback-<project>-<service>:<full-64-hex-image-id>.
   Tagging prevents ordinary dangling-image cleanup, not aggressive prune
   or disk loss. Retain both images and records; no prune/rmi. For stronger
   recovery, separately approve export/checksum and off-host backup.
3. Run the read-only plan after tagging, using the current baseline and
   approved exact candidate image ID and commit:

```sh
python3 -B scripts/control-plane-recovery.py --project <project> \
  --env-file /path/to/server.env plan --baseline /secure/local/before.json \
  --service backend --candidate sha256:<candidate-id> --revision <commit> \
  > /secure/local/backend-plan.json
```

Plan requires baseline unchanged, candidate label/service/revision match,
retained rollback tag matching the exact old image, and REC-04A graph
compatibility for candidate AND previous Backend against the same actual
DB revision. Frontend plan checks the unchanged Backend graph. Rechecks
baseline and tag after probes. The probes create network-none temporary
containers from local images; no DB credentials passed. A verified plan
is not authorization, a deployment, or a guarantee of rollback readiness.
The checks are not atomic with actual Docker changes; serialize and
revalidate immediately before executing approved steps.

## Later production execution and verification (separate approval)

Use the standard base/server Compose and a private service-scoped override
with image: sha256:<approved-id>. Render and inspect selected nonsecret
fields, ensure correct project/network/bind/env and intended command,
then use only the approved target with --no-deps --no-build --pull never.
Never run deploy-server.sh normal up against an existing DB. Do not
use all-service up, down, build, recreate, Docker daemon restart or prune.
Backend first, verify, then Frontend, verify. PostgreSQL restart policy
can be updated in place separately by exact validated container ID, not
by recreation. Worker/Gateway/model containers and collection/retention
flags unchanged. Take a fresh baseline between phases.

After each replacement, verify only the approved service change:

```sh
python3 -B scripts/control-plane-recovery.py --project <project> \
  --env-file /path/to/server.env verify --baseline /secure/local/before.json \
  --replace backend=sha256:<candidate-id> > /secure/local/after.json
```

Verification checks DB revision/volume identity, new container/image,
required restart policy, Backend new command and exact non-target service
records (container/image, start time/restart count, state). Use
--postgres-policy only for a separately approved in-place policy change
to unless-stopped; no other postgres record field may change. Unexpected
changes block acceptance even if caused by another operator. Verify actual
HTTP health/ready, read-only Admin paths, model availability/collector
continuity separately. Keep before/plan/after and operator approval
references as deployment history; no automatic evidence deletion.

## Rollback and failure handling

Stop on mismatch, failed probe, ambiguous identity or unexpected change.
No automatic rollback. Preserve images, DB/volume, evidence and logs;
never stamp/downgrade the DB to fit an image. A Backend rollback requires
REC-04A on the pinned previous image AGAIN immediately before use. A legacy
Backend image may lack app.core.server_startup; a compatible revision alone
does not establish safe startup. Review previous image/command separately,
test an explicit no-migration Uvicorn command in isolation, and use a
service-only approved override with restart: "no" if the legacy image lacks
the read-only startup gate. Do not run the old automatic migration command.
If the safe override is unproven or schema mismatch exists, stop and require
manual recovery. Frontend legacy rollback requires Backend already healthy.
Use exact retained ID, no rebuild, no mutable tag selection, and verify all
non-target services, DB revision/volume and same-origin health after rollback.
The normal verify command expects REC-04B hardening on replaced services;
legacy rollback is a distinct operator acceptance path, not a false PASS.

No intentional production reboot or fault injection to demonstrate success.
If Gateway/Worker restart policies differ from expectations, report only;
they are out of this rollout scope.

## CI

Stdlib mock tests cover evidence validation, exact identities, target/volume
and non-target drift, restart-policy-only change, rollback tag/provenance
mismatch, dual Backend graph checks. Hosted Docker builds exact tracked
Backend/Frontend source and verifies labels, then runs existing REC-04B1/B2
isolated smoke against those images. This does not prove production image
provenance, backup/restore, actual reboot or end-to-end rollback.
