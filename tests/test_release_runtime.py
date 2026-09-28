"""Opt-in real Docker isolation smoke test; no GitHub registration or secrets.

Run against an already built image with DASH_RELEASE_TEST_IMAGE=<reference>.
Only uniquely named/labelled test containers and volumes are created/removed.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_runner as release


@unittest.skipUnless(os.environ.get("DASH_RELEASE_TEST_IMAGE"), "set DASH_RELEASE_TEST_IMAGE for real Docker proof")
class ReleaseRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.image = release.docker_json("image", "inspect", os.environ["DASH_RELEASE_TEST_IMAGE"])[0]
        self.assertEqual(self.image["Config"]["User"], "1001:1001")
        self.config = dict(cpus_per_runner=1, memory_gib_per_runner=1, runner_group_id=1)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)

    @contextmanager
    def allocation(self):
        name = "platform-release-1-" + uuid.uuid4().hex[:12]
        record = dict(name=name, run_id=1, attempt=1, kind="npm")
        jit = self.state / (name + ".jit")
        jit.write_text("unused-test-configuration")
        try:
            for suffix in ("registration", "work"):
                volume = name + "-" + suffix
                self.assertFalse(release.docker_exists("volume", volume))
                subprocess.run(["docker", "volume", "create", "--label", release.MANAGED + "=1",
                                "--label", release.OWNER + "=" + name, volume],
                               check=True, stdout=subprocess.DEVNULL)
            args = release.docker_arguments(self.config, record, self.image["Id"], jit)
            yield record, args
        finally:
            # Real production cleanup, with only the GitHub call replaced.
            with patch.object(release, "items", return_value=[]):
                release.cleanup(Mock(), self.config, self.state, record)
            self.assertFalse(release.docker_exists("container", name))
            self.assertFalse(release.docker_exists("volume", name + "-registration"))
            self.assertFalse(release.docker_exists("volume", name + "-work"))

    def test_should_destroy_canaries_and_background_processes_before_the_next_job(self):
        with self.allocation() as (first, args):
            script = '''set -eu
                test "$(id -u)" = 1001
                test ! -e /var/run/docker.sock
                mkdir -p "$HOME/.gradle" "$HOME/.cache/dash-platform"
                touch /runner/poison /work/poison "$HOME/.gradle/poison" "$HOME/.cache/dash-platform/poison"
                sleep 300 &
                wait
            '''
            # Exercise the production mounts/confinement, without registering a runner.
            subprocess.run(args[:-1] + ["bash", "-c", script], check=True, stdout=subprocess.DEVNULL)
            for _ in range(50):
                result = subprocess.run(["docker", "exec", first["name"], "test", "-e", "/work/poison"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if result.returncode == 0:
                    break
                time.sleep(0.1)
            self.assertEqual(result.returncode, 0, "first container did not create the canary")
            runtime = release.docker_json("inspect", first["name"])[0]
            self.assertTrue(runtime["State"]["Running"])
            self.assertEqual(runtime["HostConfig"]["RestartPolicy"]["Name"], "no")
            self.assertFalse(runtime["HostConfig"]["Privileged"])
            self.assertFalse(runtime["HostConfig"]["Devices"])
            self.assertEqual({mount["Destination"] for mount in runtime["Mounts"]},
                             {"/runner", "/work", "/run/secrets/runner-jit"})
        with self.allocation() as (second, args):
            self.assertNotEqual(first["name"], second["name"])
            script = '''set -eu
                test ! -e /runner/poison
                test ! -e /work/poison
                test ! -e "$HOME/.gradle/poison"
                test ! -e "$HOME/.cache/dash-platform/poison"
                test "$DASH_RELEASE_RUNNER" = 1
            '''
            subprocess.run(args[:-1] + ["bash", "-c", script], check=True, stdout=subprocess.DEVNULL)
            result = subprocess.check_output(["docker", "wait", second["name"]], text=True).strip()
            self.assertEqual(result, "0", "state from the prior allocation survived")


if __name__ == "__main__":
    unittest.main()
