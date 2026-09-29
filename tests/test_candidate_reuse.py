"""Positive and adversarial immutable candidate-reuse policy tests."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import candidate_reuse as reuse
from image_contract import fingerprint
from platform_request import IMAGE, PLATFORM, candidate_digest, make_request


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {"schema": 1, "recipe_revision": "b" * 40,
                         "requirements": json.loads((ROOT / "image.lock.json").read_text())}
        self.pr = {"number": 5151, "state": "open", "head": {
            "sha": "a" * 40, "repo": {"full_name": PLATFORM}},
            "base": {"ref": "v4.3-dev"}}
        self.record = make_request(self.pr, self.manifest, None)
        self.old_head = "c" * 40
        self.digest = "sha256:" + "d" * 64
        self.control = "e" * 40
        self.run = {"id": 7, "head_sha": self.old_head, "path": reuse.WORKFLOW,
                    "event": "pull_request_target", "status": "completed", "conclusion": "success",
                    "repository": {"full_name": PLATFORM}, "head_repository": {"full_name": PLATFORM},
                    "pull_requests": [{"number": 5151}], "referenced_workflows": [{
                        "path": f"{IMAGE}/.github/workflows/platform-candidate.yml@{reuse.LEGACY_CONTROLLER}",
                        "sha": reuse.LEGACY_CONTROLLER}]}
        self.status = {"context": "Runner image candidate / PR 5151", "state": "success",
                       "description": self.digest, "creator": {"login": "github-actions[bot]"},
                       "target_url": "https://github.com/dashpay/platform/actions/runs/7"}
        self.responses = {
            f"repos/{PLATFORM}/pulls/5151": self.pr,
            reuse.RUNS: {"workflow_runs": [self.run]},
            f"repos/{PLATFORM}/actions/runs/7": self.run,
            f"repos/{PLATFORM}/commits/{self.old_head}/statuses?per_page=100&page=1": [self.status],
        }
        self.api = Mock()
        self.api.call.side_effect = lambda path: self.responses[path]
        self.api.manifest.return_value = self.manifest
        self.inspect = patch.object(reuse, "inspect_published", return_value={"config": {}}).start()
        self.addCleanup(patch.stopall)

    def find(self):
        return reuse.find_source(self.api, self.record, self.manifest, self.control)

    def test_identical_contract_resolves_immutable_previously_gated_source(self):
        source = self.find()
        self.assertEqual(source["reference"], IMAGE + "@" + self.digest)
        self.assertEqual(source["manifest_sha256"], fingerprint(self.manifest))
        self.assertEqual(source["source_head"], self.old_head)
        old = self.inspect.call_args.args[1]
        self.assertEqual(old["head_sha"], self.old_head)
        self.assertEqual(old["recipe_revision"], self.record["recipe_revision"])
        with tempfile.TemporaryDirectory() as tmp:
            context = reuse.prepare_context(source, Path(tmp) / "context")
            self.assertEqual((context / "Dockerfile").read_text(), f"FROM {IMAGE}@{self.digest}\n")
            self.assertEqual([p.name for p in context.iterdir()], ["Dockerfile"])

    def test_current_reviewed_controller_can_supply_later_reuse(self):
        self.run["referenced_workflows"][0] = {
            "path": f"{IMAGE}/.github/workflows/platform-candidate.yml@{self.control}", "sha": self.control}
        self.assertIsNotNone(self.find())

    def test_incompatible_manifest_or_recipe_is_cache_miss(self):
        for field in ("recipe_revision", "requirements"):
            with self.subTest(field=field):
                other = copy.deepcopy(self.manifest)
                if field == "recipe_revision":
                    other[field] = "f" * 40
                else:
                    other[field]["versions"]["protoc"] = "99.0"
                self.api.manifest.side_effect = lambda head: self.manifest if head == "a" * 40 else other
                self.assertIsNone(self.find())
                self.inspect.assert_not_called()

    def test_other_pr_reuse_preserves_source_status_and_label_identity(self):
        self.run["pull_requests"] = [{"number": 5000}]
        self.status["context"] = "Runner image candidate / PR 5000"
        source = self.find()
        self.assertIsNotNone(source)
        old = self.inspect.call_args.args[1]
        self.assertEqual(old["pr"], 5000)
        self.assertEqual(old["candidate_tag"], f"pr-5000-{self.old_head}-{self.record['manifest_sha256'][:12]}")
        # A status for the destination PR must never stand in for source proof.
        self.status["context"] = "Runner image candidate / PR 5151"
        self.inspect.reset_mock()
        self.assertIsNone(self.find())
        self.inspect.assert_not_called()

    def test_ambiguous_pr_same_head_forks_and_unapproved_controllers_are_not_reused(self):
        original = copy.deepcopy(self.run)
        cases = [("pull_requests", []), ("pull_requests", [{"number": 5151}, {"number": 5152}]),
                 ("pull_requests", [{"number": "5151"}]), ("head_sha", "a" * 40),
                 ("event", "pull_request"), ("conclusion", "failure"), ("status", "in_progress"),
                 ("path", ".github/workflows/tests.yml"),
                 ("repository", {"full_name": "evil/platform"}),
                 ("head_repository", {"full_name": "evil/platform"}),
                 ("referenced_workflows", []), ("referenced_workflows", [{"sha": "f" * 40,
                  "path": f"{IMAGE}/.github/workflows/platform-candidate.yml@{'f' * 40}"}])]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.run.clear(); self.run.update(copy.deepcopy(original)); self.run[field] = value
                self.assertIsNone(self.find())
                self.inspect.assert_not_called()

    def test_fork_request_stays_on_cold_build_without_expanding_trust(self):
        self.record["head_repository"] = "untrusted/platform"
        self.assertIsNone(self.find())
        self.api.call.assert_not_called()

    def test_latest_bad_status_cannot_fall_back_to_older_success(self):
        path = f"repos/{PLATFORM}/commits/{self.old_head}/statuses?per_page=100&page=1"
        for changes in ({"state": "failure"}, {"state": "pending"}, {"creator": {"login": "unknown"}},
                        {"context": self.status["context"].lower()},
                        {"target_url": "https://github.com/dashpay/platform/actions/runs/8"},
                        {"description": "latest"}):
            with self.subTest(changes=changes):
                self.responses[path] = [dict(self.status, **changes), self.status]
                self.assertIsNone(self.find())
                self.inspect.assert_not_called()

    def test_wrong_arch_user_labels_and_onbuild_cannot_supply_source(self):
        # inspect_published independently enforces platform/user/identity labels.
        for error in ("Wrong image platform", "Image must default to uid/gid 1001", "Published image mismatch"):
            self.inspect.side_effect = ValueError(error)
            self.assertIsNone(self.find())
        self.inspect.side_effect = None
        self.inspect.return_value = {"config": {"OnBuild": ["RUN evil"]}}
        self.assertIsNone(self.find())

    def test_api_error_does_not_turn_into_unverified_source(self):
        self.api.call.side_effect = URLError("unavailable")
        with self.assertRaises(URLError):
            self.find()
        self.inspect.assert_not_called()

    def test_stale_head_and_request_manifest_mismatch_fail_closed(self):
        self.pr["head"]["sha"] = "f" * 40
        with self.assertRaisesRegex(ValueError, "PR changed"):
            self.find()
        self.record["manifest_sha256"] = "f" * 64
        self.record["candidate_tag"] = f"pr-5151-{'a' * 40}-{'f' * 12}"
        with self.assertRaisesRegex(ValueError, "Request and manifest differ"):
            self.find()

    def test_context_rejects_tags_and_injection(self):
        for ref in (IMAGE + ":main", IMAGE + "@" + self.digest + "\nRUN evil", "evil/image@" + self.digest):
            with self.assertRaisesRegex(ValueError, "immutable image"):
                reuse.prepare_context({"reference": ref}, "/unused")

    def test_workflow_keeps_tests_and_export_after_reuse_and_separates_publisher(self):
        workflow = (ROOT / ".github/workflows/platform-candidate.yml").read_text()
        build = workflow.split("\n  build:", 1)[1].split("\n  publish:", 1)[0]
        self.assertEqual(build.count("context: ${{ steps.inputs.outputs.context }}"), 2)
        self.assertEqual(build.count("org.dash.platform.head=${{ needs.request.outputs.head_sha }}"), 2)
        self.assertEqual(build.count("if:"), 2)  # Job gate + cold materialization, NOT testing gates.
        for gate in ("smoke-test.sh dash-selfhosted-image:candidate", "ci-image-contract verify",
                     "--kvm", "provenance: mode=max", "sbom: true"):
            self.assertIn(gate, build)
        self.assertNotIn("secrets.", build)
        self.assertNotIn("cache-from:", build)
        self.assertNotIn("cache-to:", build)
        self.assertLess(build.index("--kvm"), build.index("provenance: mode=max"))


if __name__ == "__main__":
    unittest.main()
