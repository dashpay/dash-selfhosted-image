"""Pinned legacy recipe compatibility; see fixtures/legacy-e49e8bc/README.md."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from image_contract import fingerprint, read_json, render, validate_manifest

FIXTURE = ROOT / "tests/fixtures/legacy-e49e8bc"
RECIPE = "e49e8bc9977f5f961a76ba1d1f7673c72173679f"
RENDERED_SHA256 = "93089083c5a3b6433ed4c91492b9f5f513e6d4293a4e20518517b22c6bf56961"


class LegacyTemplateTests(unittest.TestCase):
    def setUp(self):
        self.manifest = read_json(FIXTURE / "manifest.json")
        self.template = (FIXTURE / "Dockerfile.template").read_text()

    def test_captured_inputs_are_pinned(self):
        self.assertEqual(self.manifest["recipe_revision"], RECIPE)
        validate_manifest(self.manifest)
        for name, expected in {
            "Dockerfile.template": "22ec09d69cf5212a67115ccc8983647238434c17f1c21f0603b2b3917f3a01c1",
            "manifest.json": "7471834917bd9e0e39edb4e43ce3d877d03c92f7a9880e5e93df84cc28425399",
        }.items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest(), expected)

    def test_legacy_template_matches_old_renderer(self):
        lock = self.manifest["requirements"]
        # Old manifests omit profile; explicit full must work identically.
        for requirements in (lock, {**lock, "profile": "full"}):
            with self.subTest(profile=requirements.get("profile", "implicit full")):
                rendered = render(requirements, self.template)
                self.assertEqual(hashlib.sha256(rendered.encode()).hexdigest(), RENDERED_SHA256)
                self.assertNotIn("@@", rendered)
                self.assertIn("ANDROID_NDK_HOME=/opt/android-sdk/ndk/" + lock["android"]["ndk"], rendered)
                self.assertIn("/cmdline-tools/" + lock["android"]["cmdline_tools"] + "/bin:", rendered)

    def test_materialize_actual_request_and_template(self):
        with tempfile.TemporaryDirectory() as directory:
            context = Path(directory)
            (context / "Dockerfile.template").write_text(self.template)
            result = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts/image_contract.py"), "materialize",
                 str(FIXTURE / "manifest.json"), str(context)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(read_json(context / "image.lock.json"), self.manifest["requirements"])
            self.assertEqual(hashlib.sha256((context / "Dockerfile").read_bytes()).hexdigest(),
                             RENDERED_SHA256)
            self.assertIn(fingerprint(self.manifest), result.stdout)
            self.assertIn(RECIPE, result.stdout)

    def test_rust_profile_rejects_legacy_template_and_each_android_alias(self):
        rust = read_json(ROOT / "image.arm64.lock.json")
        for platform in ("linux/amd64", "linux/arm64"):
            lock = {**rust, "platform": platform}
            for template in (self.template, "@@NDK_VERSION@@", "@@CMDLINE_VERSION@@"):
                with self.subTest(platform=platform, template=template[:40]):
                    with self.assertRaisesRegex(ValueError, "Unknown Dockerfile template parameter"):
                        render(lock, template)

    def test_full_profile_on_arm64_rejects_legacy_aliases(self):
        lock = {**self.manifest["requirements"], "platform": "linux/arm64"}
        with self.assertRaisesRegex(ValueError, "Android/KVM"):
            render(lock, self.template)

    def test_unknown_placeholders_still_fail_closed(self):
        full = self.manifest["requirements"]
        rust = read_json(ROOT / "image.arm64.lock.json")
        current = (ROOT / "Dockerfile.template").read_text()
        for lock, template in ((full, self.template), (full, current), (rust, current)):
            for marker in ("@@UNKNOWN@@", "@@NDK_VERSOIN@@", "@@"):
                with self.subTest(platform=lock["platform"], legacy=template == self.template,
                                  marker=marker):
                    with self.assertRaisesRegex(ValueError, "Unknown Dockerfile template parameter"):
                        render(lock, template + "\n" + marker)


if __name__ == "__main__":
    unittest.main()
