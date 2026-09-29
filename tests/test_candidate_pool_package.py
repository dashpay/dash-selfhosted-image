"""Exercise the deployed layout, not imports satisfied by the full checkout."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from package_candidate_pool import RUNTIME_FILES, build_bundle


class RuntimePackagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in (*RUNTIME_FILES, "image.lock.json"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, path)

    def tearDown(self):
        self.tmp.cleanup()

    def extract(self, bundle, destination, scripts_only=False):
        with tarfile.open(fileobj=io.BytesIO(bundle)) as archive:
            for item in archive.getmembers():
                if scripts_only and not item.name.startswith("scripts/"):
                    continue
                self.assertTrue(item.isfile())
                self.assertIn(item.name, (*RUNTIME_FILES, "runtime-files.sha256.json"))
                path = destination / item.name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.extractfile(item).read())

    def validate_isolated(self, destination):
        # -I excludes the source checkout and PYTHONPATH. The imported controller
        # can only find support data in the packaged deployment layout.
        code = """
import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(sys.argv[1]) / 'scripts'))
import candidate_pool, candidate_pool_host
from image_contract import validate_manifest
validate_manifest(json.load(sys.stdin))
print('PACKAGED_CONTRACT_OK')
"""
        manifest = {"schema": 1, "recipe_revision": "1" * 40,
                    "requirements": json.loads((self.root / "image.lock.json").read_text())}
        return subprocess.run([sys.executable, "-I", "-c", code, str(destination)],
                              input=json.dumps(manifest), capture_output=True, text=True, timeout=20)

    def test_bundle_is_complete_and_deterministic_without_secrets(self):
        first = build_bundle(self.root)
        self.assertEqual(first, build_bundle(self.root))
        with tarfile.open(fileobj=io.BytesIO(first)) as archive:
            self.assertEqual(set(archive.getnames()), set(RUNTIME_FILES) | {"runtime-files.sha256.json"})
            hashes = json.load(archive.extractfile("runtime-files.sha256.json"))
            self.assertEqual(set(hashes), set(RUNTIME_FILES))
            for name, digest in hashes.items():
                self.assertEqual(hashlib.sha256(archive.extractfile(name).read()).hexdigest(), digest)
            self.assertTrue(all(m.mode == 0o600 and m.uid == m.gid == m.mtime == 0
                                for m in archive.getmembers()))
        destination = self.root / "complete"
        self.extract(first, destination)
        result = self.validate_isolated(destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "PACKAGED_CONTRACT_OK")

    def test_scripts_only_install_reproduces_missing_support_failure(self):
        destination = self.root / "scripts-only"
        self.extract(build_bundle(self.root), destination, scripts_only=True)
        result = self.validate_isolated(destination)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FileNotFoundError", result.stderr)
        self.assertIn("/opt/client-codegen-recipe/lock.json", result.stderr)

    def test_missing_support_is_rejected_before_artifact_creation(self):
        (self.root / "client-codegen/lock.json").unlink()
        with self.assertRaisesRegex(ValueError, "Missing or symlinked runtime input"):
            build_bundle(self.root)

    def test_changed_support_is_rejected(self):
        (self.root / "client-codegen/lock.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "differs from the image contract"):
            build_bundle(self.root)

    def test_symlinked_input_or_parent_is_rejected(self):
        for parent in (False, True):
            with self.subTest(parent=parent):
                original = self.root / "client-codegen"
                moved = self.root / "moved"
                if parent:
                    original.rename(moved)
                    original.symlink_to(moved, target_is_directory=True)
                else:
                    target = original / "lock.json"
                    target.rename(self.root / "outside.json")
                    target.symlink_to(self.root / "outside.json")
                with self.assertRaisesRegex(ValueError, "Missing or symlinked runtime input"):
                    build_bundle(self.root)
                if parent:
                    original.unlink(); moved.rename(original)
                else:
                    target.unlink(); (self.root / "outside.json").rename(target)


if __name__ == "__main__":
    unittest.main()
