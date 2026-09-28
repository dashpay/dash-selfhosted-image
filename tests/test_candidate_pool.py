import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import candidate_pool as pool
import candidate_pool_host as host
from candidate_pool_common import labels, validate_spec, validate_host_config


def spec(job=101, pr=5151, kind="npm"):
    return {"name": f"platform-pr-{pr}-{kind}-{job}-1234abcd", "job_id": job, "run_id": job + 1000,
            "attempt": 1, "pr": pr, "head": "a" * 40, "digest": "sha256:" + "b" * 64,
            "manifest_sha256": "c" * 64, "recipe_revision": "d" * 40, "kind": kind,
            "label": f"platform-image-pr-{pr}-{'a' * 40}-{'b' * 64}-{kind}", "created": int(time.time())}


def pair(value):
    kind = value["kind"]
    run = {"id": value["run_id"], "run_attempt": value["attempt"], "head_sha": value["head"],
           "event": "pull_request", "path": next(k for k, v in pool.WORKFLOWS.items() if v == kind),
           "repository": {"full_name": "dashpay/platform"}}
    job = {"id": value["job_id"], "run_id": run["id"], "run_attempt": 1, "head_sha": value["head"],
           "name": {"npm": "NPM release build validation", "rust": "Tests",
                    "kotlin": "Kotlin SDK build + tests (x86_64)"}[kind], "labels": labels(value),
           "status": "queued", "conclusion": None, "runner_name": ""}
    return run, job


def host_config():
    return {"host": "runner1", "state_dir": "/var/lib/dash-ci-candidate-pool", "cpus": 20,
            "memory_gib": 32, "binaryen_cores": 4, "min_free_gib": 100,
            "max_age_seconds": 10800, "max_runners": 1}


class PolicyTests(unittest.TestCase):
    def test_all_kinds_and_arbitrary_prs(self):
        for pr in (5151, 5152, 6000):
            for kind in ("rust", "kotlin", "npm"):
                value = spec(pr=pr, kind=kind)
                self.assertEqual(validate_spec(value), value)
                self.assertEqual(pool.validate_job(*pair(value)), (pr, value["head"], value["digest"], kind))

    def test_wrong_event_workflow_head_attempt_repo_or_labels_rejected(self):
        for target, key, value in [(0, "event", "pull_request_target"), (0, "path", ".github/workflows/release.yml"),
                (0, "head_sha", "e" * 40), (0, "run_attempt", 2),
                (0, "repository", {"full_name": "outside/repo"}), (1, "labels", labels(spec()) + ["npm-pr"]),
                (1, "status", "completed"), (1, "name", "Unrelated task")]:
            run, job = pair(spec()); [run, job][target][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                pool.validate_job(run, job)

    def test_fork_boundary_retained(self):
        for repo, owner, allowed in [("dashpay/platform", "dashpay", True),
                                     ("thepastaclaw/platform", "thepastaclaw", True),
                                     ("arbitrary/platform", "arbitrary", False)]:
            pr = {"head": {"repo": {"full_name": repo, "owner": {"login": owner}}, "sha": "a" * 40}, "number": 5151}
            self.assertEqual(pool.allowed_pr(pr, pool.TRUST), allowed)

    def test_names_cannot_escape_state(self):
        value = spec(); value["name"] = "../../unrelated"
        with self.assertRaises(ValueError):
            validate_spec(value)

    def test_resource_caps(self):
        self.assertEqual(validate_host_config(host_config())["max_runners"], 1)
        for key, value in [("cpus", 100), ("max_runners", 2), ("max_age_seconds", 10801), ("min_free_gib", 1)]:
            config = host_config(); config[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_host_config(config)

    def test_confinement_and_kvm_kind(self):
        for kind in ("rust", "npm", "kotlin"):
            with patch.object(host.os, "stat", return_value=Mock(st_mode=0o20666, st_gid=108)):
                args = host.arguments(host_config(), spec(kind=kind), "sha256:" + "e" * 64, Path("/private/jit"))
            self.assertEqual("--device" in args, kind == "kotlin")
            self.assertIn("no-new-privileges=true", args)
            self.assertIn("ALL", args)
            self.assertIn("BINARYEN_CORES=4", args)
            self.assertNotIn("--privileged", args)
            self.assertNotIn("docker.sock", " ".join(args))
            self.assertEqual(args[-1], "jit")

    def test_docker_absence_case_variants(self):
        for message in ("Error: No such object: missing", "error: no such object: missing", "Error: No such volume: missing"):
            with patch.object(host.subprocess, "run", return_value=Mock(returncode=1, stderr=message)):
                self.assertIsNone(host.inspect("container", "missing"))
        with patch.object(host.subprocess, "run", return_value=Mock(returncode=1, stderr="daemon unavailable")):
            with self.assertRaises(ValueError):
                host.inspect("container", "missing")

    def test_cleanup_never_removes_unowned_resources(self):
        with patch.object(host, "inspect", return_value={"Config": {"Labels": {}}}):
            with self.assertRaises(ValueError):
                host.owned("container", spec()["name"], spec()["name"])

    def test_offline_cleanup_live_expired_and_terminal(self):
        for expired, terminal, removed in [(False, [], False), (True, [], True), (False, [101], True)]:
            value = spec(); value["created"] -= 11000 if expired else 0
            records = {value["name"]: {"spec": value, "phase": "running"}}
            container = {"State": {"Running": True, "Status": "running"}, "RestartCount": 0}
            with tempfile.TemporaryDirectory() as folder:
                config = host_config(); config["state_dir"] = folder
                with patch.object(host, "owned", side_effect=lambda kind, *a: container if kind == "container" else {}), \
                     patch.object(host, "docker") as docker, patch.object(host, "save"):
                    host.cleanup(config, records, terminal)
                self.assertEqual(docker.called, removed)
                self.assertEqual(records[value["name"]]["phase"], "cleaned" if removed else "running")

    def test_partial_start_cleanup_reclaims_volumes_without_container(self):
        value = spec(); records = {value["name"]: {"spec": value, "phase": "starting"}}
        with tempfile.TemporaryDirectory() as folder:
            config = host_config(); config["state_dir"] = folder
            with patch.object(host, "owned", side_effect=lambda kind, *a: None if kind == "container" else {"Name": "owned"}), \
                 patch.object(host, "docker") as docker, patch.object(host, "save"):
                host.cleanup(config, records)
            self.assertEqual(docker.call_count, 2)
            self.assertEqual(records[value["name"]]["phase"], "cleaned")


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.state = Path(self.tmp.name)
        self.config = {"hosts": {"runner1": {}, "server2": {}}}
        self.specs = [spec(), spec(job=102, pr=5152, kind="rust")]
        self.pairs = [pair(s) for s in self.specs]
        self.journal = {}
        self.api = Mock()
        self.api.call.side_effect = self.call
        self.api.register.side_effect = self.register
        self.fail_register = False

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, path):
        for run, job in self.pairs:
            if path == f"repos/dashpay/platform/actions/runs/{run['id']}": return run
            if path == f"repos/dashpay/platform/actions/jobs/{job['id']}": return job
        raise AssertionError("Unexpected path " + path)

    def register(self, value):
        saved = json.loads((self.state / "journal.json").read_text())
        self.assertEqual(saved[str(value["job_id"])]["phase"], "registering")
        if self.fail_register: raise TimeoutError("lost response")
        return {"runner": {"id": value["job_id"] + 9000, "name": value["name"]}, "encoded_jit_config": "one-job-test-only"}

    def host_call(self, target, request):
        if request["operation"] == "launch":
            return {"name": request["spec"]["name"], "phase": "running"}
        return {"capacity": {"available": True}, "allocations": {}}

    def run_pass(self):
        def prove(api, run, job): return next(s for s in self.specs if s["job_id"] == job["id"])
        with patch.object(pool, "host_call", side_effect=self.host_call), \
             patch.object(pool, "group_runners", return_value=[]), \
             patch.object(pool, "discover", side_effect=lambda api: (copy.deepcopy(self.pairs), [])), \
             patch.object(pool, "prove", side_effect=prove):
            return pool.reconcile(self.api, self.config, self.state, self.journal)

    def test_two_prs_get_independent_host_slots(self):
        result = self.run_pass()
        self.assertEqual({r["job"] for r in result["started"]}, {101, 102})
        self.assertEqual({r["host"] for r in result["started"]}, {"runner1", "server2"})
        self.assertEqual(result["blocked"], [])

    def test_lost_registration_response_never_blindly_retried(self):
        self.pairs = self.pairs[:1]; self.fail_register = True
        self.run_pass(); self.run_pass()
        self.assertEqual(self.api.register.call_count, 1)
        self.assertEqual(self.journal["101"]["phase"], "done")

    def test_competing_same_label_is_not_admitted(self):
        other = copy.deepcopy(self.pairs[0]); other[1]["id"] = 103
        self.pairs = [self.pairs[0], other]
        result = self.run_pass()
        self.api.register.assert_not_called()
        self.assertTrue(result["blocked"])


if __name__ == "__main__":
    unittest.main()
