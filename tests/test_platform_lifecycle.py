import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from candidate_runner import allowed_pr, docker_arguments, parse_job, reconcile, MANAGED
from platform_request import (
    GitHub, PLATFORM, candidate_digest, candidate_label, check_current, make_request,
    tested_candidate_jobs, validate_request, verify_promotion,
)

HEAD = "a" * 40
DIGEST = "sha256:" + "d" * 64


class FakeAPI:
    def __init__(self, pr, manifest):
        self.pr, self.wanted = pr, manifest
        self.responses = {}

    def call(self, path, *args, **kwargs):
        if path == f"repos/{PLATFORM}/pulls/4702":
            return self.pr
        return self.responses[path]

    def manifest(self, ref, **kwargs):
        return self.responses.get("manifest:" + ref, self.wanted)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {
            "schema": 1, "recipe_revision": "b" * 40,
            "requirements": json.loads((ROOT / "image.lock.json").read_text()),
        }
        self.pr = {"number": 4702, "state": "open", "merged": False, "changed_files": 1,
                   "head": {"sha": HEAD, "repo": {"full_name": PLATFORM, "owner": {"login": "dashpay"}}},
                   "base": {"sha": "c" * 40, "ref": "v4.3-dev"}, "merge_commit_sha": "e" * 40}
        self.record = make_request(self.pr, self.manifest, None)
        self.api = FakeAPI(self.pr, self.manifest)
        self.config = {"cpus_per_runner": 8, "memory_gib_per_runner": 32,
                       "trusted_fork_owners": ["thepastaclaw"], "approved_heads": {}}

    def job(self, kind, digest=DIGEST):
        return {"id": 12345, "labels": [candidate_label(4702, HEAD, digest, kind), "Linux", "self-hosted"],
                "name": "Tests" if kind == "rust" else "Kotlin SDK build + tests (x86_64 emulator)",
                "runner_name": "platform-pr-4702-test", "conclusion": "success", "html_url": "job:" + kind}

    def test_candidate_label_binds_kind_head_and_exact_digest(self):
        job = self.job("kotlin")
        self.assertEqual(parse_job(job), (4702, HEAD, DIGEST, "kotlin"))
        self.assertNotEqual(job["labels"][0], candidate_label(4702, HEAD, "sha256:" + "e" * 64, "kotlin"))
        bad = copy.deepcopy(self.record)
        bad["candidate_tag"] = "main"
        with self.assertRaisesRegex(ValueError, "bound"):
            validate_request(bad)

    def test_pr_advancing_or_retargeting_invalidates_candidate(self):
        check_current(self.api, self.record)
        for field, value in [("sha", "e" * 40)]:
            self.pr["head"][field] = value
            with self.assertRaisesRegex(ValueError, "changed"):
                check_current(self.api, self.record)
        self.pr["head"]["sha"] = HEAD
        self.pr["base"]["ref"] = "v4.2-dev"
        with self.assertRaisesRegex(ValueError, "target branch"):
            check_current(self.api, self.record)

    def test_promotion_rejects_unmerged_and_superseded_requirements(self):
        with self.assertRaisesRegex(ValueError, "merged"):
            verify_promotion(self.api, self.record, DIGEST)
        self.pr["merged"] = True
        changed = copy.deepcopy(self.manifest)
        changed["requirements"]["versions"]["protoc"] = "33.0"
        self.api.responses["manifest:v4.3-dev"] = changed
        with self.assertRaisesRegex(ValueError, "supersede"):
            verify_promotion(self.api, self.record, DIGEST)

    def setup_jobs(self, digest=DIGEST):
        query = f"repos/{PLATFORM}/actions/runs?event=pull_request&head_sha={HEAD}&per_page=100"
        self.api.responses[query] = {"workflow_runs": [
            {"id": 2, "path": ".github/workflows/kotlin-sdk-build.yml", "conclusion": "success"},
            {"id": 1, "path": ".github/workflows/tests.yml", "conclusion": "success"},
        ]}
        self.api.responses[f"repos/{PLATFORM}/actions/runs/1/jobs?per_page=100"] = {"jobs": [self.job("rust", digest)]}
        self.api.responses[f"repos/{PLATFORM}/actions/runs/2/jobs?per_page=100"] = {"jobs": [self.job("kotlin", digest)]}
        return query

    def test_real_jobs_must_both_pass_on_this_exact_digest(self):
        self.setup_jobs()
        self.assertEqual(set(tested_candidate_jobs(self.api, self.record, DIGEST)), {"rust", "kotlin"})
        with self.assertRaisesRegex(ValueError, "Both real"):
            tested_candidate_jobs(self.api, self.record, "sha256:" + "f" * 64)
        self.api.responses[f"repos/{PLATFORM}/actions/runs/2/jobs?per_page=100"]["jobs"][0]["conclusion"] = "skipped"
        with self.assertRaisesRegex(ValueError, "Both real"):
            tested_candidate_jobs(self.api, self.record, DIGEST)

    def test_newer_failed_run_cannot_reuse_an_older_success(self):
        query = self.setup_jobs()
        self.api.responses[query]["workflow_runs"].append({
            "id": 3, "path": ".github/workflows/tests.yml", "conclusion": "failure",
        })
        with self.assertRaisesRegex(ValueError, "Both real"):
            tested_candidate_jobs(self.api, self.record, DIGEST)

    def test_fork_trust_is_not_expanded_and_explicit_approval_is_head_bound(self):
        self.assertTrue(allowed_pr(self.pr, self.config))
        self.pr["head"]["repo"] = {"full_name": "unknown/platform", "owner": {"login": "unknown"}}
        self.assertFalse(allowed_pr(self.pr, self.config))
        self.config["approved_heads"] = {"4702": HEAD}
        self.assertTrue(allowed_pr(self.pr, self.config))
        self.pr["head"]["sha"] = "e" * 40
        self.assertFalse(allowed_pr(self.pr, self.config))

    def test_candidate_runtime_never_gets_host_control_authority(self):
        args = docker_arguments(self.config, self.job("rust"), self.record, DIGEST, "/safe/jit", runner_id=9)
        joined = " ".join(args)
        self.assertIn("--user 1001:1001", joined)
        self.assertIn("--cap-drop ALL", joined)
        self.assertIn("no-new-privileges=true", joined)
        for forbidden in ("/var/run/docker.sock", "--privileged", "--network host", "--pid host", "--device"):
            self.assertNotIn(forbidden, joined)
        self.assertIn("type=volume,destination=/work", args)
        self.assertNotIn("github-app.pem", joined)
        with self.assertRaisesRegex(ValueError, "differs"):
            docker_arguments(self.config, self.job("rust"), self.record, "sha256:" + "f" * 64, "/safe/jit")

    def test_kotlin_receives_only_kvm(self):
        args = docker_arguments(self.config, self.job("kotlin"), self.record, DIGEST, "/safe/jit", kvm_gid=108)
        self.assertEqual(args.count("--device"), 1)
        self.assertIn("/dev/kvm:/dev/kvm:rw", args)
        self.assertEqual(args[args.index("--group-add") + 1], "108")

    def test_publisher_status_must_be_from_successful_trusted_workflow(self):
        status_path = f"repos/{PLATFORM}/commits/{HEAD}/status"
        candidate = {"context": "Runner image candidate", "state": "success", "description": DIGEST,
                     "creator": {"login": "github-actions[bot]"},
                     "target_url": "https://github.com/dashpay/platform/actions/runs/7"}
        self.api.responses[status_path] = {"statuses": [candidate]}
        self.api.responses[f"repos/{PLATFORM}/actions/runs/7"] = {
            "path": ".github/workflows/runner-image-candidate.yml",
            "event": "pull_request_target", "conclusion": "success",
        }
        self.assertEqual(candidate_digest(self.api, self.record), DIGEST)
        candidate["creator"]["login"] = "unknown"
        with self.assertRaisesRegex(ValueError, "GitHub Actions"):
            candidate_digest(self.api, self.record)

    def test_no_requirement_change_when_only_other_files_changed(self):
        api = GitHub("test-only-not-a-credential")
        with patch.object(api, "call", return_value=[{"filename": "Cargo.lock"}]):
            self.assertFalse(api.changes_manifest(self.pr))
        with patch.object(api, "call", return_value=[{"filename": ".github/runner-requirements.json"}]):
            self.assertTrue(api.changes_manifest(self.pr))

    def test_cleanup_keeps_active_and_busy_registrations(self):
        with tempfile.TemporaryDirectory() as directory:
            config = dict(self.config, state_dir=directory, runner_group_id=1,
                          max_runners=1, max_age_seconds=1000)
            state = Path(directory) / "registrations-to-clean.json"
            state.write_text(json.dumps({"9": "platform-pr-active", "10": "platform-pr-busy"}))
            self.api.responses["orgs/dashpay/actions/runner-groups/1"] = {
                "visibility": "selected", "allows_public_repositories": True}
            self.api.responses["orgs/dashpay/actions/runners/10"] = {
                "name": "platform-pr-busy", "busy": True}
            container = {"Name": "/platform-pr-active", "State": {"Running": True},
                         "Config": {"Labels": {MANAGED: "1", "org.dash.ci.created": "100",
                                              "org.dash.ci.job": "12345", "org.dash.ci.runner-id": "9"}}}
            with patch("candidate_runner.os.geteuid", return_value=0), \
                 patch("candidate_runner.time.time", return_value=101), \
                 patch("candidate_runner.items", return_value=[{"full_name": PLATFORM}]), \
                 patch("candidate_runner.managed_containers", return_value=[container]), \
                 patch("candidate_runner.queued_jobs", return_value=[]), \
                 patch.object(self.api, "call", wraps=self.api.call) as call:
                reconcile(self.api, config, apply=True)
            self.assertEqual(set(json.loads(state.read_text())), {"9", "10"})
            self.assertNotIn("orgs/dashpay/actions/runners/9", [c.args[0] for c in call.call_args_list])
            self.assertFalse(any(c.kwargs.get("method") == "DELETE" for c in call.call_args_list))

    def test_one_rejected_candidate_does_not_starve_independent_job(self):
        with tempfile.TemporaryDirectory() as directory:
            config = dict(self.config, state_dir=directory, max_runners=1)
            jobs = [{"id": 1}, {"id": 2}]
            with patch("candidate_runner.queued_jobs", return_value=jobs), \
                 patch("candidate_runner.launch_candidate", side_effect=[ValueError("stale"), True]) as launch:
                reconcile(self.api, config)
            self.assertEqual(launch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
