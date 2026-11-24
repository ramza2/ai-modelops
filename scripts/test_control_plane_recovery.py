"""REC-04C unit tests: no Docker, DB, or server access."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "recovery", Path(__file__).with_name("control-plane-recovery.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

REV = "a" * 40
DB = "b8c9d0e1f2a3"
NEW = "sha256:" + "f" * 64
PROJECT = "modelops-ci"


def baseline():
    return {
        "version": 1, "project": PROJECT, "captured_at": "2026-11-24T00:00:00+00:00",
        "db_revision": DB, "postgres_volume": PROJECT + "_modelops_pgdata",
        "services": {
            svc: {
                "container_id": str(i + 1) * 64, "image_id": "sha256:" + str(i + 1) * 64,
                "source_revision": None, "source_service": None, "status": "running",
                "health": "healthy" if svc == "postgres" else None,
                "restart_count": 0, "started_at": "2026-11-24T00:00:00.123456789Z",
                "restart_policy": "no", "server_startup_gate": False,
            } for i, svc in enumerate(r.SERVICES)
        },
    }


class RecoveryTests(unittest.TestCase):
    def test_unchanged_baseline(self):
        r.verify(baseline(), baseline(), {})

    def test_allowed_backend_change(self):
        before = baseline()
        after = copy.deepcopy(before)
        after["services"]["backend"].update(image_id=NEW, container_id="a"*64,
                                            restart_policy="unless-stopped",
                                            server_startup_gate=True)
        r.verify(before, after, {"backend": NEW})

    def test_unapproved_worker_restart(self):
        before = baseline()
        after = copy.deepcopy(before)
        after["services"]["worker"]["restart_count"] = 1
        with self.assertRaisesRegex(r.gate.GateError, "UNAPPROVED_CHANGE_WORKER"):
            r.verify(before, after, {})

    def test_db_and_volume_mismatch(self):
        for field, value in (("db_revision", "other"), ("postgres_volume", "other"),
                             ("project", "other")):
            with self.subTest(field=field):
                before = baseline()
                after = copy.deepcopy(before)
                after[field] = value
                with self.assertRaisesRegex(r.gate.GateError, "TARGET_CHANGED"):
                    r.verify(before, after, {})

    def test_postgres_policy_only(self):
        before = baseline()
        after = copy.deepcopy(before)
        after["services"]["postgres"]["restart_policy"] = "unless-stopped"
        r.verify(before, after, {}, True)
        after["services"]["postgres"]["container_id"] = "a"*64
        with self.assertRaisesRegex(r.gate.GateError, "UNAPPROVED_CHANGE_POSTGRES"):
            r.verify(before, after, {}, True)

    def test_backend_gate_required(self):
        before = baseline()
        after = copy.deepcopy(before)
        after["services"]["backend"].update(image_id=NEW, container_id="a"*64,
                                            restart_policy="unless-stopped")
        with self.assertRaisesRegex(r.gate.GateError, "BACKEND_STARTUP_GATE_MISSING"):
            r.verify(before, after, {"backend": NEW})

    def test_replacement_validation(self):
        self.assertEqual(r.parse_replacements(["frontend=" + NEW]), {"frontend": NEW})
        for values in (["worker=" + NEW], ["backend=tag"],
                       ["backend=" + NEW, "backend=" + NEW]):
            with self.subTest(values=values), self.assertRaises(r.gate.GateError):
                r.parse_replacements(values)

    def test_invalid_baseline(self):
        value = baseline()
        value["services"]["backend"]["source_revision"] = "sensitive arbitrary string"
        with self.assertRaises(r.gate.GateError):
            r.validate_snapshot(value)

    def test_metadata_allowlist(self):
        payload = [{"Id": NEW, "Config": {"Env": ["PASSWORD=secret"],
                    "Labels": {r.REV_LABEL: "secret", r.SERVICE_LABEL: "other",
                               "other": "secret"}}}]
        with patch.object(r, "run", return_value=json.dumps(payload)):
            self.assertEqual(r.image_metadata(NEW), {
                "image_id": NEW, "source_revision": None, "source_service": None})

    def test_service_identity_ambiguity(self):
        with patch.object(r, "run", return_value="a"*64 + "\n" + "b"*64):
            with self.assertRaisesRegex(r.gate.GateError, "SERVICE_ID_NOT_SINGLE"):
                r.service_state(PROJECT, "backend")

    def test_plan_checks_both_backend_images(self):
        value = baseline()
        old = value["services"]["backend"]["image_id"]
        graph = {"heads": [DB], "revisions": [DB]}
        with patch.object(r, "snapshot", return_value=value), \
             patch.object(r, "image_metadata", return_value={
                 "image_id": NEW, "source_revision": REV, "source_service": "backend"}), \
             patch.object(r.gate, "resolve_image", return_value=old), \
             patch.object(r.gate, "read_graph", return_value=graph) as probe:
            plan = r.plan(PROJECT, Path("/unused"), value, "backend", NEW, REV)
            self.assertEqual([c.args[0] for c in probe.call_args_list], [old, NEW])
            self.assertEqual(plan["rollback_image"], old)
            self.assertFalse(plan["rollback_backend_gate_observed"])
            self.assertEqual(plan["status"], "PLAN_VERIFIED_NOT_DEPLOYED")

    def test_rollback_tag_must_match(self):
        value = baseline()
        with patch.object(r, "snapshot", return_value=value), \
             patch.object(r, "image_metadata", return_value={
                 "image_id": NEW, "source_revision": REV, "source_service": "backend"}), \
             patch.object(r.gate, "resolve_image", return_value=NEW):
            with self.assertRaisesRegex(r.gate.GateError, "ROLLBACK_TAG_MISMATCH"):
                r.plan(PROJECT, Path("/unused"), value, "backend", NEW, REV)

    def test_provenance_mismatch(self):
        value = baseline()
        with patch.object(r, "snapshot", return_value=value), \
             patch.object(r, "image_metadata", return_value={
                 "image_id": NEW, "source_revision": REV, "source_service": "frontend"}):
            with self.assertRaisesRegex(r.gate.GateError, "CANDIDATE_PROVENANCE_MISMATCH"):
                r.plan(PROJECT, Path("/unused"), value, "backend", NEW, REV)

    def test_stale_rollback_revision_blocked(self):
        value = baseline()
        old = value["services"]["backend"]["image_id"]
        with patch.object(r, "snapshot", return_value=value), \
             patch.object(r, "image_metadata", return_value={
                 "image_id": NEW, "source_revision": REV, "source_service": "backend"}), \
             patch.object(r.gate, "resolve_image", return_value=old), \
             patch.object(r.gate, "read_graph", return_value={
                 "heads": ["old"], "revisions": ["old"]}):
            with self.assertRaisesRegex(r.gate.GateError, "DB_REVISION_MISSING"):
                r.plan(PROJECT, Path("/unused"), value, "backend", NEW, REV)

    def test_snapshot_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "evidence.json"
            p.write_text(json.dumps(baseline()))
            self.assertEqual(r.read_baseline(p), baseline())


if __name__ == "__main__":
    unittest.main()
