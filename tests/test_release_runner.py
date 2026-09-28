"""Release scheduler, cleanup/crash recovery and isolation contract tests."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_runner as release

IMAGE = "sha256:" + "a" * 64
HEAD = "b" * 40


class ReleaseRunnerTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(app_id=1, installation_id=2, runner_group_id=3,
                           state_dir="/state", private_key_file="/key", max_runners=1,
                           cpus_per_runner=8, memory_gib_per_runner=32, min_free_gib=100,
                           max_age_seconds=14400, images=dict(npm=IMAGE, kotlin=IMAGE))
        self.run = dict(id=123, run_attempt=2, event="release", head_sha=HEAD,
                        repository=dict(full_name="dashpay/platform"),
                        head_repository=dict(full_name="dashpay/platform"),
                        path=".github/workflows/release.yml")
        self.job = dict(id=456, run_id=123, status="queued", head_sha=HEAD,
                        labels=["self-hosted", "Linux", "X64", "platform-release-123-2-npm"])
        self.record = dict(name="platform-release-456-abcdefabcdef", run_id=123,
                           job_id=456, attempt=2, kind="npm", created=100,
                           phase="running", runner_id=9, image=IMAGE)

    def test_should_accept_only_operator_pinned_images(self):
        release.validate_config(self.config)
        for reference in ["dashpay/dash-selfhosted-image:main", "ubuntu:latest", "$(bad)"]:
            with self.subTest(reference=reference):
                with self.assertRaisesRegex(ValueError, "immutable"):
                    release.validate_config(dict(self.config, images=dict(npm=reference, kotlin=IMAGE)))
        for key, value in [("max_runners", 5), ("max_age_seconds", 14401), ("runner_group_id", 0)]:
            with self.assertRaises(ValueError):
                release.validate_config(dict(self.config, **{key: value}))

    def test_should_use_the_host_cpu_pool_instead_of_an_eight_cpu_default(self):
        config = dict(self.config, cpus_per_runner="auto", reserved_cpus=12)
        release.validate_config(config)
        for host_cpus, slots, expected in [(32, 1, 20), (64, 1, 52), (16, 1, 4),
                                           (32, 2, 10), (33, 2, 10)]:
            with self.subTest(host_cpus=host_cpus, slots=slots), \
                 patch.object(release.os, "sched_getaffinity", return_value=set(range(host_cpus))):
                self.assertEqual(release.cpu_budget(dict(config, max_runners=slots)), expected)

    def test_should_reject_invalid_or_exhausted_cpu_reservations(self):
        for value in (0, -1, True, 1.5, "20", "maximum"):
            with self.subTest(cpus=value), self.assertRaises(ValueError):
                release.validate_config(dict(self.config, cpus_per_runner=value))
        for reserve in (None, -1, True, 1.5, "12"):
            with self.subTest(reserve=reserve), self.assertRaises(ValueError):
                release.validate_config(dict(self.config, cpus_per_runner="auto", reserved_cpus=reserve))
        with patch.object(release.os, "sched_getaffinity", return_value=set(range(8))):
            for reserve, slots in [(8, 1), (12, 1), (7, 2)]:
                config = dict(self.config, cpus_per_runner="auto", reserved_cpus=reserve, max_runners=slots)
                with self.subTest(reserve=reserve, slots=slots), self.assertRaisesRegex(ValueError, "Insufficient CPUs"):
                    release.cpu_budget(config)

    def test_should_keep_explicit_cpu_settings_and_align_optimizer_threads(self):
        for cpus in (8, 20):
            config = dict(self.config, cpus_per_runner=cpus)
            release.validate_config(config)
            args = release.docker_arguments(config, self.record, IMAGE, "/state/job.jit")
            self.assertEqual(args[args.index("--cpus") + 1], str(cpus))
            self.assertIn("CARGO_BUILD_JOBS=" + str(cpus), args)
            self.assertIn("BINARYEN_CORES=" + str(cpus), args)

    def test_should_honor_the_recorded_cpu_budget_for_a_launch(self):
        config = dict(self.config, cpus_per_runner="auto", reserved_cpus=12)
        with patch.object(release.os, "sched_getaffinity", side_effect=AssertionError("already resolved")):
            args = release.docker_arguments(config, dict(self.record, cpus=20), IMAGE, "/state/job.jit")
        self.assertEqual(args[args.index("--cpus") + 1], "20")
        self.assertIn("BINARYEN_CORES=20", args)

    def test_should_bind_job_to_run_attempt_commit_and_kind(self):
        self.assertEqual(release.parse_job(self.run, self.job), ("npm", "platform-release-123-2-npm"))
        for key, value in [("run_id", 124), ("head_sha", "c" * 40), ("status", "completed")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                release.parse_job(self.run, dict(self.job, **{key: value}))
        for label in ["platform-release-123-1-npm", "platform-release-124-2-npm", "npm-build"]:
            with self.subTest(label=label), self.assertRaises(ValueError):
                release.parse_job(self.run, dict(self.job, labels=["self-hosted", "Linux", "X64", label]))
        with self.assertRaisesRegex(ValueError, "extra labels"):
            release.parse_job(self.run, dict(self.job, labels=self.job["labels"] + ["kotlin-ci"]))

    def test_should_reject_fork_prs_even_when_they_copy_a_real_release_label(self):
        for event in ("pull_request", "pull_request_target", "push", "workflow_run"):
            with self.subTest(event=event), self.assertRaises(ValueError):
                release.parse_job(dict(self.run, event=event), self.job)
        for field in ("repository", "head_repository"):
            with self.assertRaises(ValueError):
                release.parse_job(dict(self.run, **{field: dict(full_name="fork/platform")}), self.job)

    def test_should_accept_dispatch_on_new_release_lines_without_host_branch_rules(self):
        for branch in ("v4.2-dev", "v4.3-dev"):
            run = dict(self.run, event="workflow_dispatch", head_branch=branch)
            self.assertEqual(release.parse_job(run, self.job)[0], "npm")
        with self.assertRaises(ValueError):
            release.parse_job(dict(self.run, path=".github/workflows/tests.yml"), self.job)

    def test_should_never_mount_shared_caches_or_host_authority(self):
        args = release.docker_arguments(self.config, self.record, IMAGE, "/state/job.jit")
        joined = " ".join(args)
        self.assertIn("--restart no --pull never --user 1001:1001", joined)
        self.assertIn("--cap-drop ALL", joined)
        self.assertIn("no-new-privileges=true", joined)
        self.assertEqual(args[-2:], [IMAGE, "jit"])
        for forbidden in ("docker.sock", "--privileged", "--network host", "--pid host",
                          "--device", "--group-add", "--env-file", "github-app.pem", "/home/runner,"):
            self.assertNotIn(forbidden, joined)
        mounts = [args[i + 1] for i, arg in enumerate(args) if arg == "--mount"]
        self.assertEqual(len(mounts), 3)
        self.assertTrue(all(mount.startswith("type=volume,src=" + self.record["name"])
                            for mount in mounts[:2]))
        self.assertTrue(mounts[2].endswith("dst=/run/secrets/runner-jit,readonly"))
        next_record = dict(self.record, name="platform-release-456-123456123456")
        next_args = release.docker_arguments(self.config, next_record, IMAGE, "/state/next.jit")
        self.assertNotIn(mounts[0], next_args)
        self.assertNotIn(mounts[1], next_args)

    def test_should_require_a_platform_only_nondefault_group(self):
        api = Mock()
        group = dict(name=release.GROUP, visibility="selected", allows_public_repositories=True, default=False)
        api.call.return_value = group
        with patch.object(release, "items", return_value=[dict(full_name="dashpay/platform")]):
            release.verify_group(api, self.config)
            for key, value in [("name", "Default"), ("default", True), ("visibility", "all"),
                               ("allows_public_repositories", False)]:
                api.call.return_value = dict(group, **{key: value})
                with self.assertRaises(ValueError):
                    release.verify_group(api, self.config)
        api.call.return_value = group
        with patch.object(release, "items", return_value=[dict(full_name="dashpay/platform"),
                                                         dict(full_name="dashpay/dash")]):
            with self.assertRaises(ValueError):
                release.verify_group(api, self.config)

    def test_should_fail_closed_if_a_container_is_missing_from_the_journal(self):
        with patch.object(release.subprocess, "check_output", return_value=self.record["name"]):
            with self.assertRaisesRegex(ValueError, "Unjournaled"):
                release.verify_no_orphans(dict(allocations={}))

    def test_should_refuse_cleanup_of_unrelated_docker_resources(self):
        with patch.object(release, "docker_exists", return_value=True), \
             patch.object(release, "docker_json", return_value=[dict(Config=dict(Labels={}))]):
            with self.assertRaisesRegex(ValueError, "outside"):
                release.owned_resource("container", self.record["name"], self.record["name"])

    def test_should_journal_before_registering_and_keep_jit_until_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            config = dict(self.config, cpus_per_runner="auto", reserved_cpus=12)
            state = Path(directory)
            journal = dict(allocations={}, attempts={})
            commands = []
            api = Mock()
            def call(path, data=None):
                if path.endswith("generate-jitconfig"):
                    saved = json.loads((state / "journal.json").read_bytes())
                    self.assertIn(data["name"], saved["allocations"])
                    self.assertEqual(len(commands), 2)  # Both fresh volumes exist first.
                    return dict(runner=dict(id=9), encoded_jit_config="test-only-placeholder")
                return self.job if "/jobs/" in path else self.run
            api.call.side_effect = call
            image = dict(Id=IMAGE, Os="linux", Architecture="amd64",
                         Config=dict(User="1001:1001", Entrypoint=["/opt/ci/bin/runner-entrypoint"]))
            with patch.object(release, "docker_json", return_value=[image]), \
                 patch.object(release.os, "sched_getaffinity", return_value=set(range(32))), \
                 patch.object(release, "docker_exists", return_value=False), \
                 patch.object(release.subprocess, "check_output", return_value="/docker\n"), \
                 patch.object(release.subprocess, "run", side_effect=lambda args, **kwargs: commands.append(args)), \
                 patch.object(release.shutil, "disk_usage", return_value=SimpleNamespace(free=1024**4)), \
                 patch.object(release.Path, "read_text", return_value="MemAvailable: 999999999 kB\n"), \
                 patch.object(release.os, "chown"):
                release.launch(api, config, state, journal, self.run, self.job)
            record = next(iter(journal["allocations"].values()))
            self.assertEqual(record["runner_id"], 9)
            self.assertEqual(record["phase"], "running")
            self.assertEqual(record["cpus"], 20)
            self.assertEqual(commands[-1][commands[-1].index("--cpus") + 1], "20")
            self.assertIn("BINARYEN_CORES=20", commands[-1])
            jit = state / (record["name"] + ".jit")
            self.assertEqual(jit.stat().st_mode & 0o777, 0o400)
            self.assertEqual(commands[-1][-2:], [IMAGE, "jit"])

    def test_should_remove_entire_container_then_volumes_and_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            api, commands = Mock(), []
            runner = dict(name=self.record["name"], id=9, busy=False)
            with patch.object(release, "owned_resource", return_value={"exists": True}), \
                 patch.object(release, "items", return_value=[runner]), \
                 patch.object(release.subprocess, "run", side_effect=lambda args, **kw: commands.append(args)):
                self.assertTrue(release.cleanup(api, self.config, Path(directory), self.record))
            self.assertEqual(commands[0][:2], ["docker", "kill"])
            self.assertEqual(commands[2][:3], ["docker", "rm", "-f"])
            self.assertEqual(commands[3][-1], self.record["name"] + "-registration")
            self.assertEqual(commands[4][-1], self.record["name"] + "-work")
            api.call.assert_called_once_with("orgs/dashpay/actions/runners/9", method="DELETE")

    def test_should_recover_a_lost_jit_response_by_exact_recorded_name(self):
        with tempfile.TemporaryDirectory() as directory:
            record = dict(self.record)
            del record["runner_id"]
            api = Mock()
            runners = [dict(id=9, name=record["name"], busy=False),
                       dict(id=10, name="ubuntu-server-2", busy=False)]
            with patch.object(release, "owned_resource", return_value=None), \
                 patch.object(release, "items", return_value=runners):
                self.assertTrue(release.cleanup(api, self.config, Path(directory), record))
            api.call.assert_called_once_with("orgs/dashpay/actions/runners/9", method="DELETE")

    def test_should_keep_cleanup_record_while_github_retains_a_busy_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            api = Mock()
            with patch.object(release, "owned_resource", return_value=None), \
                 patch.object(release, "items", return_value=[dict(id=9, name=self.record["name"], busy=True)]):
                self.assertFalse(release.cleanup(api, self.config, Path(directory), self.record))
            api.call.assert_not_called()

    def reconcile_record(self, status, now=101, running=True, api_error=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = Path(directory.name)
        journal = dict(allocations={self.record["name"]: self.record}, attempts={})
        release.save(state / "journal.json", journal)
        api = Mock()
        if api_error:
            api.call.side_effect = api_error
        else:
            api.call.return_value = dict(status=status)
        with patch.object(release, "owned_resource", return_value=dict(State=dict(Running=running))), \
             patch.object(release, "cleanup", return_value=True) as cleanup, \
             patch.object(release.time, "time", return_value=now):
            release.reconcile(api, dict(self.config, state_dir=str(state)), apply=True, drain=True)
        return cleanup.call_count, json.loads((state / "journal.json").read_text())

    def test_should_preserve_a_live_job_but_remove_a_completed_or_cancelled_job(self):
        self.assertEqual(self.reconcile_record("in_progress")[0], 0)
        count, journal = self.reconcile_record("completed")
        self.assertEqual(count, 1)
        self.assertFalse(journal["allocations"])
        self.assertEqual(self.reconcile_record("in_progress", running=False)[0], 1)

    def test_should_enforce_lifetime_even_without_github(self):
        error = urllib.error.URLError("offline")
        self.assertEqual(self.reconcile_record(None, api_error=error)[0], 0)
        self.assertEqual(self.reconcile_record(None, now=14500, api_error=error)[0], 1)

    def test_should_not_mutate_anything_in_dry_run(self):
        with patch.object(release, "queued_jobs", return_value=[(self.run, self.job)]), \
             patch.object(release, "launch") as launch, \
             patch.object(release, "save") as save:
            release.reconcile(Mock(), self.config)
        launch.assert_not_called()
        save.assert_not_called()

    def test_should_bound_retries_across_controller_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            config = dict(self.config, state_dir=directory)
            api = Mock()
            with patch.object(release, "verify_group"), patch.object(release, "verify_no_orphans"), \
                 patch.object(release, "items", return_value=[]), \
                 patch.object(release, "queued_jobs", return_value=[(self.run, self.job)]), \
                 patch.object(release, "launch", side_effect=RuntimeError("image unavailable")) as launch:
                for now in (1000, 1301, 1602, 1903):
                    with patch.object(release.time, "time", return_value=now):
                        release.reconcile(api, config, apply=True)
            self.assertEqual(launch.call_count, 3)
            journal = json.loads((Path(directory) / "journal.json").read_text())
            self.assertEqual(journal["attempts"]["456"]["count"], 3)


if __name__ == "__main__":
    unittest.main()
