"""REC-04A stdlib unit tests; no Docker daemon or database required."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "schema_gate", Path(__file__).with_name("backend-schema-preflight.py")
)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

OLD = "a7c8d9e0f1a2"
HEAD = "b8c9d0e1f2a3"
IMAGE = "sha256:" + "a" * 64
CID = "b" * 64
GRAPH = {"heads": [HEAD], "revisions": [HEAD, OLD]}


class GateTests(unittest.TestCase):
    def test_exact_head(self):
        self.assertEqual(gate.evaluate(HEAD, GRAPH), HEAD)

    def test_stale_image(self):
        with self.assertRaisesRegex(gate.GateError, "DB_REVISION_MISSING"):
            gate.evaluate(HEAD, {"heads": [OLD], "revisions": [OLD]})

    def test_forward_migration_requires_approval(self):
        with self.assertRaisesRegex(gate.GateError, "FORWARD_MIGRATION"):
            gate.evaluate(OLD, GRAPH)

    def test_ambiguous_heads(self):
        for heads in ([], [OLD, HEAD]):
            with self.subTest(heads=heads), self.assertRaisesRegex(
                gate.GateError, "HEAD_NOT_SINGLE"
            ):
                gate.evaluate(HEAD, {"heads": heads, "revisions": [OLD, HEAD]})

    def test_bad_graph(self):
        for revisions in ([], [OLD], [HEAD, HEAD], [HEAD, "invalid secret!"]):
            with self.subTest(revisions=revisions), self.assertRaisesRegex(
                gate.GateError, "GRAPH_INVALID"
            ):
                gate.evaluate(HEAD, {"heads": [HEAD], "revisions": revisions})

    def test_db_revision_ambiguous(self):
        for rows in ("", HEAD + "\n" + OLD + "\n", "not a revision\n"):
            with self.subTest(rows=rows), patch.object(gate, "run", return_value=rows):
                with self.assertRaisesRegex(gate.GateError, "DB_REVISION_NOT_SINGLE"):
                    gate.read_db_revision(CID)

    def test_query_read_only(self):
        with patch.object(gate, "run", return_value=HEAD + "\n") as run:
            self.assertEqual(gate.read_db_revision(CID), HEAD)
        args, kwargs = run.call_args
        self.assertIn("default_transaction_read_only=on", args[0][-1])
        self.assertEqual(kwargs["stdin"],
                         "SELECT version_num FROM alembic_version ORDER BY version_num;\n")

    def test_offline_probe(self):
        with patch.object(gate, "run", return_value=json.dumps(GRAPH)) as run:
            self.assertEqual(gate.read_graph(IMAGE), GRAPH)
        cmd = run.call_args.args[0]
        self.assertIn(IMAGE, cmd)
        self.assertIn("--read-only", cmd)
        self.assertEqual(cmd[cmd.index("--network") + 1], "none")
        self.assertEqual(cmd[cmd.index("--pull") + 1], "never")
        self.assertNotIn("--env-file", cmd)
        self.assertIn("walk_revisions", run.call_args.kwargs["stdin"])

    def test_fresh_and_orphan_volume(self):
        cfg = {"name": "compose", "volumes": {"modelops_pgdata": {"name": "compose_pg"}}}
        with patch.object(gate, "run", side_effect=["", "other\n"]):
            self.assertIsNone(gate.target(["docker", "compose"], cfg))
        with patch.object(gate, "run", side_effect=["", "compose_pg\n"]):
            with self.assertRaisesRegex(gate.GateError, "EXISTING_VOLUME"):
                gate.target(["docker", "compose"], cfg)

    def test_target_identity_and_health(self):
        cfg = {"name": "compose", "volumes": {"modelops_pgdata": {"name": "compose_pg"}}}
        info = {"Config": {"Labels": {"com.docker.compose.project": "compose",
                                      "com.docker.compose.service": "postgres"}},
                "State": {"Running": True, "Health": {"Status": "healthy"}},
                "Mounts": [{"Destination": "/var/lib/postgresql/data",
                            "Type": "volume", "Name": "compose_pg"}]}
        with patch.object(gate, "run", side_effect=[CID, "compose_pg\n", json.dumps([info])]):
            self.assertEqual(gate.target([], cfg)[0], CID)
        info["State"]["Running"] = False
        with patch.object(gate, "run", side_effect=[CID, "compose_pg\n", json.dumps([info])]):
            with self.assertRaisesRegex(gate.GateError, "POSTGRES_NOT_HEALTHY"):
                gate.target([], cfg)

    def test_connection_mismatch(self):
        env = {"POSTGRES_USER": "u", "POSTGRES_DB": "db", "POSTGRES_PASSWORD": "dummy"}
        cfg = {"services": {"postgres": {"environment": env},
                            "backend": {"environment": {
                                "MODELOPS_DATABASE_URL": "postgresql+asyncpg://u:dummy@postgres:5432/db"}}}}
        info = {"Config": {"Env": [f"{k}={v}" for k, v in env.items()]}}
        gate.verify_connection(cfg, info)
        cfg["services"]["backend"]["environment"]["MODELOPS_DATABASE_URL"] = (
            "postgresql+asyncpg://u:dummy@other:5432/db"
        )
        with self.assertRaisesRegex(gate.GateError, "BACKEND_DATABASE_TARGET_MISMATCH"):
            gate.verify_connection(cfg, info)

    def test_main_pass_and_races(self):
        cfg = {"name": "compose", "services": {"backend": {}}}
        for dbs, images, expected in [
            ([HEAD, HEAD], [IMAGE, IMAGE], "MODELOPS_BACKEND_SCHEMA_PREFLIGHT_PASS"),
            ([HEAD, OLD], [IMAGE], "DB_REVISION_CHANGED"),
            ([HEAD, HEAD], [IMAGE, "sha256:" + "c" * 64], "CANDIDATE_TAG_CHANGED"),
        ]:
            with self.subTest(expected=expected), tempfile.NamedTemporaryFile() as env, \
                 patch.object(gate, "load_config", return_value=cfg), \
                 patch.object(gate, "target", return_value=(CID, {})), \
                 patch.object(gate, "verify_connection"), \
                 patch.object(gate, "read_db_revision", side_effect=dbs), \
                 patch.object(gate, "resolve_image", side_effect=images), \
                 patch.object(gate, "read_graph", return_value=GRAPH), \
                 contextlib.redirect_stdout(io.StringIO()) as stdout, \
                 contextlib.redirect_stderr(io.StringIO()) as stderr:
                rc = gate.main(["--env-file", env.name])
                self.assertIn(expected, stdout.getvalue() + stderr.getvalue())
                self.assertEqual(rc, 0 if expected.endswith("_PASS") else 1)

    def test_subprocess_error_does_not_disclose_credentials(self):
        from subprocess import CompletedProcess
        with patch.object(gate.subprocess, "run", return_value=CompletedProcess(
            [], 1, stdout="secret-output", stderr="secret-password"
        )):
            with self.assertRaisesRegex(gate.GateError, "^TEST_FAILED$"):
                gate.run(["dummy"], code="TEST_FAILED")


if __name__ == "__main__":
    unittest.main()
