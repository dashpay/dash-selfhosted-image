import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from image_contract import canonical, differences, fingerprint, read_json, render, validate_manifest


class ImageContractTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {
            "schema": 1, "recipe_revision": "a" * 40,
            "requirements": json.loads((ROOT / "image.lock.json").read_text()),
        }

    def test_baseline_and_generated_dockerfile(self):
        validate_manifest(self.manifest)
        self.assertEqual(render(self.manifest["requirements"],
                                (ROOT / "Dockerfile.template").read_text()),
                         (ROOT / "Dockerfile").read_text())

    def test_hash_is_key_order_independent_but_binds_recipe_and_requirements(self):
        reordered = dict(reversed(list(self.manifest.items())))
        self.assertEqual(fingerprint(reordered), fingerprint(self.manifest))
        changed = copy.deepcopy(self.manifest)
        changed["requirements"]["versions"]["nextest"] = "0.9.145"
        self.assertNotEqual(fingerprint(changed), fingerprint(self.manifest))
        changed = copy.deepcopy(self.manifest)
        changed["recipe_revision"] = "b" * 40
        self.assertNotEqual(fingerprint(changed), fingerprint(self.manifest))

    def test_mutable_recipe_rejected(self):
        self.manifest["recipe_revision"] = "main"
        with self.assertRaisesRegex(ValueError, "full commit SHA"):
            validate_manifest(self.manifest)

    def test_package_shell_injection_and_privilege_tools_rejected(self):
        for package in ["curl;id", "$(id)", "foo\nRUN id", "--allow-unauthenticated",
                        "sudo", "docker-ce-cli"]:
            with self.subTest(package=package):
                changed = copy.deepcopy(self.manifest)
                changed["requirements"]["apt_packages"].append(package)
                with self.assertRaises(ValueError):
                    validate_manifest(changed)

    def test_download_credentials_internal_hosts_and_path_escape_rejected(self):
        for url in ["http://github.com/a", "https://user:pass@github.com/a",
                    "https://169.254.169.254/latest/meta-data/",
                    "https://github.com.evil.invalid/a", "https://github.com/a\nRUN id"]:
            changed = copy.deepcopy(self.manifest)
            changed["requirements"]["artifacts"][0]["url"] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_manifest(changed)
        for field, value in [("destination", "/etc/sudoers"), ("archive_root", "../escape")]:
            changed = copy.deepcopy(self.manifest)
            changed["requirements"]["artifacts"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_manifest(changed)

    def test_missing_checksum_unknown_field_and_duplicate_json_rejected(self):
        for field, value in [("sha256", ""), ("executable", "sh")]:
            changed = copy.deepcopy(self.manifest)
            changed["requirements"]["artifacts"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_manifest(changed)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text('{"schema": 1, "schema": 2}')
            with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
                read_json(path)

    def test_android_api_and_package_metadata_must_agree(self):
        changed = copy.deepcopy(self.manifest)
        changed["requirements"]["android"]["api"] = 36
        with self.assertRaisesRegex(ValueError, "System image"):
            validate_manifest(changed)
        changed = copy.deepcopy(self.manifest)
        artifact = next(a for a in changed["requirements"]["artifacts"] if "package_xml" in a)
        artifact["package_xml"] = artifact["package_xml"].replace(
            'path="' + artifact["name"] + '"', 'path="platforms;android-99"')
        with self.assertRaisesRegex(ValueError, "metadata"):
            validate_manifest(changed)

    def test_mismatch_names_fields_instead_of_dumping_configuration(self):
        changed = copy.deepcopy(self.manifest)
        changed["requirements"]["versions"]["protoc"] = "33.0"
        self.assertEqual(differences(self.manifest, changed),
                         ["requirements.versions.protoc"])


if __name__ == "__main__":
    unittest.main()
