"""Candidate resolution against captured API shapes and synthetic status history."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, call
import urllib.error

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from platform_request import PLATFORM, candidate_digest

FIXTURES = ROOT / "tests/fixtures/candidate-status-pr5151"
HEAD = "a02b1460736e18b6345bb4722c622e55e787371d"
DIGEST = "sha256:e5ebd957d28d15976320023b76cffa8b982e1d131c572d92eb9c91814dbf807b"
RUN_PATH = f"repos/{PLATFORM}/actions/runs/36474975257"
MISSING = object()


class CandidateStatusTests(unittest.TestCase):
    def setUp(self):
        self.combined = json.loads((FIXTURES / "combined.json").read_text())
        self.statuses = json.loads((FIXTURES / "statuses.json").read_text())
        self.run = json.loads((FIXTURES / "publisher-run.json").read_text())
        # Synthetic request identity; status and publisher evidence remain unchanged.
        self.record = {
            "schema": 1, "repository": PLATFORM, "pr": 5151, "head_sha": HEAD,
            "recipe_revision": "b" * 40, "manifest_sha256": "c" * 64,
            "candidate_tag": f"pr-5151-{HEAD}-{'c' * 12}",
        }
        self.responses = {self.page(1): self.statuses, RUN_PATH: self.run}
        self.api = Mock()
        self.api.call.side_effect = self.respond

    def page(self, number, head=HEAD):
        return f"repos/{PLATFORM}/commits/{head}/statuses?per_page=100&page={number}"

    def respond(self, path):
        # Unexpected endpoints/pages fail the test, never return a permissive default.
        self.assertIn(path, self.responses, f"Unexpected API request: {path}")
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        return response

    def unrelated(self, count):
        return [{"context": f"unrelated / {index}", "state": "success"}
                for index in range(count)]

    def test_captured_shapes_and_trusted_success_without_creator_fabrication(self):
        self.assertIsInstance(self.combined, dict)
        self.assertIsInstance(self.statuses, list)
        combined = self.combined["statuses"][0]
        candidate = self.statuses[0]
        self.assertNotIn("creator", combined)
        self.assertEqual(combined, {key: value for key, value in candidate.items()
                                    if key != "creator"})
        self.assertEqual(candidate["id"], 55116170875)
        self.assertEqual(candidate["creator"]["login"], "github-actions[bot]")
        self.assertEqual(candidate["creator"]["id"], 41898282)
        self.assertEqual(candidate_digest(self.api, self.record), DIGEST)
        self.assertEqual(self.api.call.call_args_list, [call(self.page(1)), call(RUN_PATH)])

    def test_combined_status_cannot_supply_creator_provenance(self):
        self.responses[self.page(1)] = self.combined["statuses"]
        with self.assertRaisesRegex(ValueError, "GitHub Actions"):
            candidate_digest(self.api, self.record)
        self.api.call.assert_called_once_with(self.page(1))

    def test_missing_null_wrong_and_malformed_creators_are_value_errors(self):
        for creator in (MISSING, None, {}, {"login": "unknown"}, {"login": None},
                        {"login": ["github-actions[bot]"]}, "github-actions[bot]", [], 7, True):
            with self.subTest(creator=creator):
                candidate = copy.deepcopy(self.statuses[0])
                if creator is MISSING:
                    candidate.pop("creator")
                else:
                    candidate["creator"] = creator
                self.responses[self.page(1)] = [candidate]
                self.api.reset_mock()
                with self.assertRaisesRegex(ValueError, "GitHub Actions"):
                    candidate_digest(self.api, self.record)
                self.api.call.assert_called_once_with(self.page(1))

    def test_invalid_newest_shadows_older_success_on_same_or_next_page(self):
        cases = [
            ({"state": state}, {}, "successfully published")
            for state in ("pending", "failure", "error")
        ] + [
            ({"creator": creator}, {}, "GitHub Actions")
            for creator in (MISSING, None, {"login": "unknown"})
        ] + [
            ({"description": "sha256:not-a-digest"}, {}, "immutable digest"),
            ({"target_url": "https://github.com/other/platform/actions/runs/1"}, {}, "publishing run"),
            ({}, {"path": ".github/workflows/untrusted.yml"}, "trusted workflow"),
            ({}, {"event": "pull_request"}, "trusted workflow"),
            ({}, {"conclusion": "failure"}, "trusted workflow"),
        ]
        for changes, run_changes, error in cases:
            for boundary in (False, True):
                with self.subTest(changes=changes, run=run_changes, boundary=boundary):
                    newest = copy.deepcopy(self.statuses[0])
                    for key, value in changes.items():
                        if value is MISSING:
                            newest.pop(key)
                        else:
                            newest[key] = value
                    old = copy.deepcopy(self.statuses[0])
                    old["target_url"] = "https://github.com/dashpay/platform/actions/runs/1"
                    self.responses = {RUN_PATH: dict(self.run, **run_changes)}
                    if boundary:
                        self.responses[self.page(1)] = self.unrelated(99) + [newest]
                        self.responses[self.page(2)] = [old]
                    else:
                        self.responses[self.page(1)] = [newest, old]
                    self.api.reset_mock()
                    with self.assertRaisesRegex(ValueError, error):
                        candidate_digest(self.api, self.record)
                    self.assertNotIn(call(self.page(2)), self.api.call.call_args_list)
                    self.assertNotIn(call(f"repos/{PLATFORM}/actions/runs/1"),
                                     self.api.call.call_args_list)

    def test_repeated_successes_select_first_digest_without_sorting(self):
        newest = copy.deepcopy(self.statuses[0])
        newest["description"] = "sha256:" + "f" * 64
        # API ordering is authoritative, not locally sorted IDs or timestamps.
        self.responses[self.page(1)] = [newest, self.statuses[0]]
        self.assertEqual(candidate_digest(self.api, self.record), newest["description"])
        self.assertEqual(self.api.call.call_args_list, [call(self.page(1)), call(RUN_PATH)])

    def test_exact_context_and_head_isolation(self):
        context = self.statuses[0]["context"]
        near_matches = [dict(self.statuses[0], context=value) for value in (
            context + "0", context + " ", "Runner image candidate / PR 5152",
        )]
        self.responses[self.page(1)] = near_matches
        other_head = "d" * 40
        self.responses[self.page(1, other_head)] = self.statuses
        with self.assertRaisesRegex(ValueError, "successfully published"):
            candidate_digest(self.api, self.record)
        self.api.call.assert_called_once_with(self.page(1))
        self.api.reset_mock()
        self.responses[self.page(1)] = near_matches + self.statuses
        self.assertEqual(candidate_digest(self.api, self.record), DIGEST)
        self.assertEqual(self.api.call.call_args_list, [call(self.page(1)), call(RUN_PATH)])

    def test_newer_case_variant_shadows_canonical_success(self):
        for changes in ({"state": "failure"}, {"creator": {"login": "unknown"}}, {}):
            for boundary in (False, True):
                with self.subTest(changes=changes, boundary=boundary):
                    newest = dict(self.statuses[0], **changes)
                    newest["context"] = newest["context"].lower()
                    if boundary:
                        self.responses[self.page(1)] = self.unrelated(99) + [newest]
                        self.responses[self.page(2)] = self.statuses
                    else:
                        self.responses[self.page(1)] = [newest] + self.statuses
                    self.api.reset_mock()
                    with self.assertRaisesRegex(ValueError, "successfully published"):
                        candidate_digest(self.api, self.record)
                    self.api.call.assert_called_once_with(self.page(1))

    def test_later_page_candidate_stops_even_when_page_is_full(self):
        self.responses[self.page(1)] = self.unrelated(100)
        self.responses[self.page(2)] = self.unrelated(100)
        self.responses[self.page(3)] = self.unrelated(99) + self.statuses
        self.assertEqual(candidate_digest(self.api, self.record), DIGEST)
        self.assertEqual(self.api.call.call_args_list, [
            call(self.page(1)), call(self.page(2)), call(self.page(3)), call(RUN_PATH),
        ])

    def test_first_page_candidate_does_not_fetch_another_full_page(self):
        self.responses[self.page(1)] = self.statuses + self.unrelated(99)
        self.assertEqual(candidate_digest(self.api, self.record), DIGEST)
        self.assertEqual(self.api.call.call_args_list, [call(self.page(1)), call(RUN_PATH)])

    def test_missing_candidate_and_full_page_exhaustion_fail_closed(self):
        for sizes in ((0,), (99,), (100, 0), (100, 100, 0), (100, 99)):
            with self.subTest(sizes=sizes):
                self.responses = {self.page(page): self.unrelated(size)
                                  for page, size in enumerate(sizes, 1)}
                self.api.reset_mock()
                with self.assertRaisesRegex(ValueError, "successfully published"):
                    candidate_digest(self.api, self.record)
                self.assertEqual(self.api.call.call_args_list,
                                 [call(self.page(page)) for page in range(1, len(sizes) + 1)])

    def test_api_errors_propagate_without_fallback(self):
        for target in (self.page(1), self.page(2), RUN_PATH):
            for error in (urllib.error.HTTPError(target, 403, "Forbidden", {}, None),
                          urllib.error.URLError("connection failed"),
                          json.JSONDecodeError("invalid response", "", 0)):
                with self.subTest(target=target, error=type(error).__name__):
                    self.responses = {self.page(1): self.statuses, RUN_PATH: self.run}
                    expected = [call(self.page(1))]
                    if target == self.page(2):
                        self.responses[self.page(1)] = self.unrelated(100)
                        expected.append(call(self.page(2)))
                    elif target == RUN_PATH:
                        expected.append(call(RUN_PATH))
                    self.responses[target] = error
                    self.api.reset_mock()
                    with self.assertRaises(type(error)) as raised:
                        candidate_digest(self.api, self.record)
                    self.assertIs(raised.exception, error)
                    self.assertEqual(self.api.call.call_args_list, expected)

    def test_url_and_digest_gates(self):
        cases = [("target_url", value, "publishing run") for value in (
            "", "http://github.com/dashpay/platform/actions/runs/36474975257",
            "https://github.com.evil/dashpay/platform/actions/runs/36474975257",
            "https://github.com/dashpay/other/actions/runs/36474975257",
            "https://github.com/dashpay/platform/actions/runs/36474975257?x=1",
            "https://github.com/dashpay/platform/actions/runs/not-a-run",
        )] + [("description", value, "immutable digest") for value in (
            "", None, "main", "sha256:" + "a" * 63, "sha256:" + "a" * 65,
            "sha256:" + "G" * 64, DIGEST + "\n",
        )]
        for field, value, message in cases:
            with self.subTest(field=field, value=value):
                self.responses[self.page(1)] = [dict(self.statuses[0], **{field: value})]
                with self.assertRaisesRegex(ValueError, message):
                    candidate_digest(self.api, self.record)

    def test_publisher_workflow_event_and_conclusion_gates(self):
        for field, values in {
            "path": (".github/workflows/tests.yml", "", None),
            "event": ("pull_request", "push", "workflow_dispatch", None),
            "conclusion": ("failure", "cancelled", "skipped", None),
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.responses[RUN_PATH] = dict(self.run, **{field: value})
                    with self.assertRaisesRegex(ValueError, "trusted workflow"):
                        candidate_digest(self.api, self.record)


if __name__ == "__main__":
    unittest.main()
