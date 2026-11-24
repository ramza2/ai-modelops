#!/usr/bin/env python3
"""REC-04C: read-only Control Plane evidence and image/rollback readiness.

No tag, build, restart, deployment, migration, prune or automatic rollback.
REC-04A graph checks use isolated ephemeral containers without DB credentials.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "schema_gate", ROOT / "scripts/backend-schema-preflight.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

SERVICES = ("postgres", "backend", "worker", "gateway", "frontend")
REPLACEABLE = ("backend", "frontend")
PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
FULL_CID = re.compile(r"[0-9a-f]{64}\Z")
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z\Z")
REV_LABEL = "org.opencontainers.image.revision"
SERVICE_LABEL = "ai.modelops.control-plane.service"
STARTUP = ["python", "-m", "app.core.server_startup"]


def require(condition, reason):
    gate.require(condition, reason)


def run(args, code="DOCKER_QUERY_FAILED"):
    return gate.run(args, code=code)


def image_metadata(image):
    require(isinstance(image, str) and gate.IMAGE_ID.fullmatch(image),
            "IMAGE_ID_INVALID")
    result = json.loads(run(["docker", "image", "inspect", image]))
    require(isinstance(result, list) and len(result) == 1 and
            result[0]["Id"] == image, "IMAGE_ID_MISMATCH")
    labels = result[0]["Config"].get("Labels") or {}
    rev = labels.get(REV_LABEL)
    svc = labels.get(SERVICE_LABEL)
    return {
        "image_id": image,
        "source_revision": rev if isinstance(rev, str) and SHA.fullmatch(rev) else None,
        "source_service": svc if svc in REPLACEABLE else None,
    }


def service_state(project, service):
    ids = run(["docker", "ps", "-a", "-q", "--no-trunc",
               "--filter", "label=com.docker.compose.project=" + project,
               "--filter", "label=com.docker.compose.service=" + service]).split()
    require(len(ids) == 1 and FULL_CID.fullmatch(ids[0]),
            "SERVICE_ID_NOT_SINGLE_" + service.upper())
    records = json.loads(run(["docker", "inspect", ids[0]]))
    require(isinstance(records, list) and len(records) == 1, "INSPECT_NOT_SINGLE")
    c = records[0]
    labels = c["Config"]["Labels"]
    require(c["Id"] == ids[0] and
            labels.get("com.docker.compose.project") == project and
            labels.get("com.docker.compose.service") == service and
            labels.get("com.docker.compose.oneoff", "False").lower() == "false",
            "SERVICE_IDENTITY_MISMATCH_" + service.upper())
    state = c["State"]
    policy = c["HostConfig"]["RestartPolicy"]["Name"]
    health = (state.get("Health") or {}).get("Status")
    record = {
        "container_id": ids[0],
        **image_metadata(c["Image"]),
        "status": state["Status"],
        "health": health,
        "restart_count": c["RestartCount"],
        "started_at": state["StartedAt"],
        "restart_policy": policy or "no",
        "server_startup_gate": service == "backend" and c["Config"]["Cmd"] == STARTUP,
    }
    validate_record(record)
    require(record["source_service"] in (None, service),
            "IMAGE_SERVICE_MISMATCH_" + service.upper())
    return record


def validate_record(r):
    require(set(r) == {"container_id", "image_id", "source_revision",
                       "source_service", "status", "health", "restart_count",
                       "started_at", "restart_policy", "server_startup_gate"},
            "INVALID_SERVICE_RECORD")
    require(FULL_CID.fullmatch(r["container_id"]) and
            gate.IMAGE_ID.fullmatch(r["image_id"]) and
            STAMP.fullmatch(r["started_at"]) and
            (r["source_revision"] is None or SHA.fullmatch(r["source_revision"])) and
            r["source_service"] in (None, *REPLACEABLE) and
            r["status"] in ("created", "running", "paused", "restarting",
                            "removing", "exited", "dead") and
            r["health"] in (None, "starting", "healthy", "unhealthy", "none") and
            type(r["restart_count"]) is int and r["restart_count"] >= 0 and
            r["restart_policy"] in ("no", "always", "unless-stopped", "on-failure") and
            type(r["server_startup_gate"]) is bool,
            "INVALID_SERVICE_RECORD")


def snapshot(project, env_file):
    require(PROJECT.fullmatch(project), "INVALID_PROJECT")
    require(env_file.is_file(), "ENV_FILE_NOT_FOUND")
    compose = gate.compose_args(env_file.resolve(), project)
    cfg = gate.load_config(compose)
    require(cfg["name"] == project, "PROJECT_MISMATCH")
    states = {s: service_state(project, s) for s in SERVICES}
    existing = gate.target(compose, cfg)
    require(existing is not None, "NO_EXISTING_DATABASE")
    cid, info = existing
    gate.verify_connection(cfg, info)
    require(states["postgres"]["container_id"] == info["Id"] == cid,
            "POSTGRES_ID_CHANGED")
    backend = json.loads(run(["docker", "inspect", states["backend"]["container_id"]]))
    require(len(backend) == 1 and backend[0]["Id"] == states["backend"]["container_id"],
            "BACKEND_ID_CHANGED")
    backend_env = dict(item.split("=", 1) for item in backend[0]["Config"]["Env"] if "=" in item)
    require(backend_env.get("MODELOPS_DATABASE_URL") ==
            cfg["services"]["backend"]["environment"]["MODELOPS_DATABASE_URL"],
            "LIVE_BACKEND_DATABASE_TARGET_MISMATCH")
    revision = gate.read_db_revision(cid)
    require(all(states[s]["status"] == "running" for s in SERVICES),
            "CONTROL_PLANE_NOT_RUNNING")
    require(all(states[s]["health"] in (None, "healthy", "none") for s in SERVICES),
            "CONTROL_PLANE_NOT_HEALTHY")
    require(states["postgres"]["health"] == "healthy", "POSTGRES_NOT_HEALTHY")
    # A snapshot is not an atomic observation; detect ordinary concurrent changes.
    require({s: service_state(project, s) for s in SERVICES} == states and
            gate.read_db_revision(cid) == revision, "BASELINE_CHANGED_DURING_CAPTURE")
    return {"version": 1, "project": project,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "db_revision": revision,
            "postgres_volume": cfg["volumes"]["modelops_pgdata"]["name"],
            "services": states}


def validate_snapshot(s):
    require(set(s) == {"version", "project", "captured_at", "db_revision",
                       "postgres_volume", "services"} and s["version"] == 1 and
            PROJECT.fullmatch(s["project"]) and gate.REV.fullmatch(s["db_revision"]) and
            isinstance(s["postgres_volume"], str) and
            re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,254}", s["postgres_volume"]) and
            set(s["services"]) == set(SERVICES), "INVALID_BASELINE")
    stamp = datetime.fromisoformat(s["captured_at"])
    require(stamp.tzinfo is not None, "INVALID_BASELINE_TIME")
    for r in s["services"].values():
        validate_record(r)
        require(r["status"] == "running", "INVALID_BASELINE_STATUS")
    require(s["services"]["postgres"]["health"] == "healthy", "INVALID_BASELINE_HEALTH")


def same_target(before, after):
    validate_snapshot(before)
    validate_snapshot(after)
    for field in ("project", "db_revision", "postgres_volume"):
        require(before[field] == after[field], "TARGET_CHANGED_" + field.upper())


def verify(before, after, replacements, postgres_policy=False):
    same_target(before, after)
    for svc in SERVICES:
        old, new = before["services"][svc], after["services"][svc]
        if svc in replacements:
            require(svc in REPLACEABLE, "REPLACEMENT_SERVICE_INVALID")
            require(new["image_id"] == replacements[svc],
                    "IMAGE_MISMATCH_" + svc.upper())
            require(new["image_id"] != old["image_id"] and
                    new["container_id"] != old["container_id"],
                    "REPLACEMENT_NOT_OBSERVED_" + svc.upper())
            require(new["restart_policy"] == "unless-stopped",
                    "RESTART_POLICY_MISMATCH_" + svc.upper())
            if svc == "backend":
                require(new["server_startup_gate"], "BACKEND_STARTUP_GATE_MISSING")
            continue
        if svc == "postgres" and postgres_policy:
            require(new["restart_policy"] == "unless-stopped", "POSTGRES_POLICY_MISMATCH")
            new = {**new, "restart_policy": old["restart_policy"]}
        require(old == new, "UNAPPROVED_CHANGE_" + svc.upper())


def rollback_tag(project, service, image_id):
    return "modelops-rec04-rollback-" + project + "-" + service + ":" + image_id[7:]


def plan(project, env_file, baseline, service, candidate, revision):
    require(service in REPLACEABLE and gate.IMAGE_ID.fullmatch(candidate)
            and SHA.fullmatch(revision), "INVALID_CANDIDATE")
    live = snapshot(project, env_file)
    verify(baseline, live, {})
    old = live["services"][service]["image_id"]
    require(candidate != old, "CANDIDATE_EQUALS_CURRENT")
    meta = image_metadata(candidate)
    require(meta["source_revision"] == revision and meta["source_service"] == service,
            "CANDIDATE_PROVENANCE_MISMATCH")
    tag = rollback_tag(project, service, old)
    require(gate.resolve_image(tag) == old, "ROLLBACK_TAG_MISMATCH")
    # Both images must be compatible with the same actual DB, not merely
    # with each other. For frontend changes, check the unchanged backend.
    if service == "backend":
        for image in (old, candidate):
            gate.evaluate(live["db_revision"], gate.read_graph(image))
    else:
        gate.evaluate(live["db_revision"],
                      gate.read_graph(live["services"]["backend"]["image_id"]))
    verify(live, snapshot(project, env_file), {})
    require(gate.resolve_image(tag) == old, "ROLLBACK_TAG_CHANGED")
    return {"project": project, "service": service, "candidate": meta,
            "rollback_image": old, "rollback_tag": tag,
            "rollback_backend_gate_observed": live["services"][service]["server_startup_gate"]
                if service == "backend" else None,
            "db_revision": live["db_revision"],
            "status": "PLAN_VERIFIED_NOT_DEPLOYED"}


def parse_replacements(values):
    result = {}
    for v in values:
        svc, sep, image = v.partition("=")
        require(sep and svc in REPLACEABLE and svc not in result and
                gate.IMAGE_ID.fullmatch(image), "INVALID_REPLACEMENT")
        result[svc] = image
    return result


def read_baseline(path):
    require(path.is_file() and path.stat().st_size <= 65536, "INVALID_BASELINE_FILE")
    value = json.loads(path.read_text())
    validate_snapshot(value)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("capture", help="JSON evidence for healthy/running Control Plane")
    audit = sub.add_parser("audit", help="Report service states without DB access")
    check = sub.add_parser("verify", help="Compare after state to captured baseline")
    check.add_argument("--baseline", type=Path, required=True)
    check.add_argument("--replace", action="append", default=[])
    check.add_argument("--postgres-policy", action="store_true")
    prep = sub.add_parser("plan", help="Read-only candidate/rollback gate")
    prep.add_argument("--baseline", type=Path, required=True)
    prep.add_argument("--service", choices=REPLACEABLE, required=True)
    prep.add_argument("--candidate", required=True)
    prep.add_argument("--revision", required=True)
    args = parser.parse_args(argv)
    try:
        require(PROJECT.fullmatch(args.project), "INVALID_PROJECT")
        if args.action == "audit":
            states, errors = {}, {}
            for svc in SERVICES:
                try:
                    states[svc] = service_state(args.project, svc)
                except gate.GateError as exc:
                    errors[svc] = str(exc)
                except (KeyError, ValueError, TypeError, AttributeError, IndexError):
                    errors[svc] = "INVALID_METADATA"
            print(json.dumps({"project": args.project, "services": states,
                              "errors": errors}, indent=2))
            return 1 if errors or any(
                r["status"] != "running" or r["health"] not in (None, "healthy", "none")
                for r in states.values()) or states.get("postgres", {}).get("health") != "healthy" else 0
        if args.action == "capture":
            result = snapshot(args.project, args.env_file)
        elif args.action == "verify":
            result = snapshot(args.project, args.env_file)
            verify(read_baseline(args.baseline), result, parse_replacements(args.replace),
                   args.postgres_policy)
        else:
            result = plan(args.project, args.env_file, read_baseline(args.baseline),
                          args.service, args.candidate, args.revision)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except gate.GateError as exc:
        print("MODELOPS_REC04C_BLOCKED=" + str(exc), file=sys.stderr)
    except (OSError, KeyError, ValueError, TypeError, AttributeError, IndexError):
        print("MODELOPS_REC04C_BLOCKED=INVALID_METADATA", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
