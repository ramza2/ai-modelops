#!/usr/bin/env python3
"""REC-04A: read-only Backend image / PostgreSQL revision gate.

The local candidate is inspected by immutable image ID, without networking
or DB credentials. No build, tag, migration, restart or deployment.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
REV = re.compile(r"[A-Za-z0-9_]{1,64}\Z")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
CID = re.compile(r"[0-9a-f]{12,64}\Z")
GRAPH_PROBE = """
import json
import warnings
warnings.filterwarnings("error")
from alembic.config import Config
from alembic.script import ScriptDirectory
graph = ScriptDirectory.from_config(Config('/app/alembic.ini'))
heads = graph.get_heads()
revisions = [r.revision for r in graph.walk_revisions()]
print(json.dumps({'heads': heads, 'revisions': revisions}))
"""


class GateError(Exception):
    """Only fixed diagnostic codes may be shown to the operator."""


def require(condition, reason):
    if not condition:
        raise GateError(reason)


def run(args, *, code, stdin=None, timeout=30):
    try:
        result = subprocess.run(
            args, input=stdin, text=True, capture_output=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise GateError(code) from None
    require(result.returncode == 0, code)
    return result.stdout


def compose_args(env_file, project):
    args = ["docker", "compose"]
    if project:
        args += ["-p", project]
    return args + [
        "--env-file", str(env_file),
        "-f", str(ROOT / "deploy/compose/docker-compose.yml"),
        "-f", str(ROOT / "deploy/compose/docker-compose.server.yml"),
    ]


def load_config(compose):
    return json.loads(run(
        compose + ["config", "--format", "json"], code="COMPOSE_CONFIG_FAILED",
    ))


def target(compose, config):
    project = config["name"]
    volume = config["volumes"]["modelops_pgdata"]["name"]
    require(isinstance(project, str) and bool(project), "PROJECT_UNKNOWN")
    require(isinstance(volume, str) and bool(volume), "VOLUME_UNKNOWN")
    ids = run(compose + ["ps", "-a", "-q", "postgres"],
              code="POSTGRES_LOOKUP_FAILED").split()
    volumes = run(["docker", "volume", "ls", "--format", "{{.Name}}"],
                  code="VOLUME_LOOKUP_FAILED").splitlines()
    if not ids:
        require(volume not in volumes, "EXISTING_VOLUME_WITHOUT_POSTGRES")
        return None
    require(len(ids) == 1 and CID.fullmatch(ids[0]), "POSTGRES_ID_AMBIGUOUS")
    require(volume in volumes, "POSTGRES_VOLUME_NOT_FOUND")
    info = json.loads(run(["docker", "inspect", ids[0]],
                          code="POSTGRES_INSPECT_FAILED"))
    require(isinstance(info, list) and len(info) == 1, "POSTGRES_INSPECT_AMBIGUOUS")
    info = info[0]
    labels = info["Config"]["Labels"]
    require(labels.get("com.docker.compose.project") == project and
            labels.get("com.docker.compose.service") == "postgres",
            "POSTGRES_PROJECT_MISMATCH")
    require(info["State"]["Running"] is True and
            info["State"].get("Health", {}).get("Status") == "healthy",
            "POSTGRES_NOT_HEALTHY")
    mounts = [m for m in info["Mounts"]
              if m.get("Destination") == "/var/lib/postgresql/data"]
    require(len(mounts) == 1 and mounts[0].get("Type") == "volume" and
            mounts[0].get("Name") == volume, "POSTGRES_VOLUME_MISMATCH")
    return ids[0], info


def verify_connection(config, info):
    # Validate the same standard DB target without printing secrets.
    live = dict(item.split("=", 1) for item in info["Config"]["Env"] if "=" in item)
    expected = config["services"]["postgres"]["environment"]
    for key in ("POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD"):
        require(bool(live.get(key)) and live[key] == expected.get(key),
                "POSTGRES_ENV_MISMATCH")
    backend = config["services"]["backend"]["environment"]
    url = urlsplit(backend["MODELOPS_DATABASE_URL"])
    require(url.scheme == "postgresql+asyncpg" and url.hostname == "postgres" and
            url.port == 5432 and not url.query and not url.fragment and
            unquote(url.path) == "/" + live["POSTGRES_DB"] and
            unquote(url.username or "") == live["POSTGRES_USER"] and
            unquote(url.password or "") == live["POSTGRES_PASSWORD"],
            "BACKEND_DATABASE_TARGET_MISMATCH")


def read_db_revision(cid):
    sql = "SELECT version_num FROM alembic_version ORDER BY version_num;"
    output = run(
        ["docker", "exec", "-i", cid, "sh", "-ceu", '''
test -n "$POSTGRES_PASSWORD"
test -n "$POSTGRES_USER"
test -n "$POSTGRES_DB"
export PGPASSWORD="$POSTGRES_PASSWORD"
export PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=3000"
exec psql -h 127.0.0.1 -X -qAt -v ON_ERROR_STOP=1 \
  -U "$POSTGRES_USER" -d "$POSTGRES_DB"
'''], code="DB_REVISION_QUERY_FAILED", stdin=sql + "\n",
    )
    rows = output.splitlines()
    require(len(rows) == 1 and REV.fullmatch(rows[0]), "DB_REVISION_NOT_SINGLE")
    return rows[0]


def resolve_image(image):
    require(isinstance(image, str) and bool(image) and not image.startswith("-"),
            "CANDIDATE_IMAGE_REFERENCE_INVALID")
    image_id = run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                   code="CANDIDATE_IMAGE_NOT_LOCAL").strip()
    require(IMAGE_ID.fullmatch(image_id), "CANDIDATE_IMAGE_ID_INVALID")
    return image_id


def read_graph(image_id):
    output = run(
        ["docker", "run", "--rm", "-i", "--pull", "never", "--network", "none",
         "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
         "--entrypoint", "python", "--workdir", "/app", image_id, "-B", "-"],
        code="CANDIDATE_GRAPH_PROBE_FAILED", stdin=GRAPH_PROBE, timeout=60,
    )
    return json.loads(output)


def evaluate(db_revision, graph):
    heads, revisions = graph["heads"], graph["revisions"]
    require(isinstance(heads, list) and len(heads) == 1 and
            isinstance(heads[0], str) and REV.fullmatch(heads[0]),
            "CANDIDATE_HEAD_NOT_SINGLE")
    require(isinstance(revisions, list) and bool(revisions) and
            all(isinstance(r, str) and REV.fullmatch(r) for r in revisions) and
            len(set(revisions)) == len(revisions) and heads[0] in revisions,
            "CANDIDATE_GRAPH_INVALID")
    require(db_revision in revisions, "DB_REVISION_MISSING_FROM_IMAGE")
    require(db_revision == heads[0], "FORWARD_MIGRATION_REQUIRES_SEPARATE_APPROVAL")
    return heads[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--project-name")
    parser.add_argument("--image", help="Local Backend tag or ID; defaults to Compose Backend image")
    parser.add_argument("--classify-target", action="store_true",
                        help="Classify fresh/healthy existing target; no compatibility PASS")
    args = parser.parse_args(argv)
    try:
        require(args.env_file.is_file(), "ENV_FILE_NOT_FOUND")
        compose = compose_args(args.env_file.resolve(), args.project_name)
        config = load_config(compose)
        existing = target(compose, config)
        if args.classify_target:
            require(args.image is None, "CLASSIFY_WITH_IMAGE_NOT_ALLOWED")
            if existing:
                verify_connection(config, existing[1])
            print("EXISTING" if existing else "FRESH")
            return 0
        require(existing is not None, "NO_EXISTING_DATABASE")
        cid, info = existing
        verify_connection(config, info)
        db_revision = read_db_revision(cid)
        image = args.image or config["services"]["backend"].get("image") or (
            config["name"] + "-backend"
        )
        image_id = resolve_image(image)
        head = evaluate(db_revision, read_graph(image_id))
        require(read_db_revision(cid) == db_revision, "DB_REVISION_CHANGED")
        require(resolve_image(image) == image_id, "CANDIDATE_TAG_CHANGED")
        print("DB_REVISION=" + db_revision)
        print("IMAGE_HEAD=" + head)
        print("IMAGE_ID=" + image_id)
        print("MODELOPS_BACKEND_SCHEMA_PREFLIGHT_PASS")
        return 0
    except GateError as exc:
        print("MODELOPS_BACKEND_SCHEMA_PREFLIGHT_BLOCKED=" + str(exc), file=sys.stderr)
    except (KeyError, ValueError, TypeError, AttributeError, IndexError):
        print("MODELOPS_BACKEND_SCHEMA_PREFLIGHT_BLOCKED=INVALID_METADATA", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
