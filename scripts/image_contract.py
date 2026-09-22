#!/usr/bin/env python3
"""Validate data-only Platform requirements and compare them to a built image."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import re
import shlex
import sys
import urllib.parse
import xml.etree.ElementTree as ET


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    data = Path(path).read_bytes()
    require(len(data) <= 256 * 1024, "Requirements exceed 256 KiB")
    return json.loads(data, object_pairs_hook=unique_object)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


VERSION = r"[0-9]+(?:[.][0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?"
SHA256 = r"[0-9a-f]{64}"
SHA1 = r"[0-9a-f]{40}"
DOWNLOAD_HOSTS = {
    "github.com", "static.rust-lang.org", "dl.google.com",
    "snapshot.ubuntu.com",
}


def validate_url(value):
    require(isinstance(value, str), "Download URL must be a string")
    parsed = urllib.parse.urlsplit(value)
    require(parsed.scheme == "https" and parsed.hostname in DOWNLOAD_HOSTS,
            "Download URL must use a supported upstream HTTPS host")
    require(not parsed.username and not parsed.password and parsed.port in (None, 443),
            "Credentials/custom ports are forbidden in download URLs")
    require(not parsed.query and not parsed.fragment and "\\" not in value,
            "Download URLs cannot contain queries, fragments or backslashes")
    require(not any(ord(c) < 33 for c in value), "Control/space in download URL")


def validate_lock(lock):
    required = {
        "schema", "contract_version", "platform", "ubuntu_image", "apt_snapshot",
        "rust_version", "rust_manifest_sha256", "artifacts", "bootstrap_ca",
        "apt_packages", "java_major", "versions", "android",
    }
    require(isinstance(lock, dict) and set(lock) == required, "Unknown/missing lock fields")
    require(lock["schema"] == 2 and type(lock["schema"]) is int, "Expected lock schema 2")
    require(lock["contract_version"] == "1", "This recipe supports image contract 1")
    require(lock["platform"] == "linux/amd64", "Only linux/amd64 is supported")
    require(matches(r"ubuntu:24[.]04@sha256:[0-9a-f]{64}", lock["ubuntu_image"]),
            "Ubuntu base must be digest-pinned")
    require(matches(r"[0-9]{8}T[0-9]{6}Z", lock["apt_snapshot"]), "Invalid apt snapshot")
    datetime.datetime.strptime(lock["apt_snapshot"], "%Y%m%dT%H%M%SZ")
    require(matches(VERSION, lock["rust_version"]), "Rust must be version-pinned")
    require(matches(SHA256, lock["rust_manifest_sha256"]), "Missing Rust manifest checksum")
    versions = lock["versions"]
    require(isinstance(versions, dict) and set(versions) == {
        "runner", "llvm_cov", "nextest", "machete", "cargo_ndk", "protoc", "rustup",
    }, "Unexpected version keys")
    require(all(matches(VERSION, v) for v in versions.values()), "Invalid tool version")
    require(type(lock["java_major"]) is int and lock["java_major"] in (17, 21),
            "Supported JDK major versions: 17, 21")
    packages = lock["apt_packages"]
    require(isinstance(packages, list) and 1 <= len(packages) <= 128, "Invalid apt package list")
    require(all(matches(r"[a-z0-9][a-z0-9+.-]*(?:=[A-Za-z0-9.+:~_-]+)?", p)
                for p in packages), "Invalid apt package name/version")
    require(len(packages) == len(set(packages)), "Duplicate apt packages")
    package_names = {p.split("=")[0] for p in packages}
    require(not package_names.intersection({"sudo", "docker.io", "docker-ce", "docker-ce-cli",
                                           "moby-engine", "moby-cli", "podman"}),
            "Runner image cannot include privilege/container-management tools")
    require({
        "ca-certificates", "python3", "git", "curl", "build-essential", "clang", "llvm",
        "libgmp-dev", "libssl-dev", "libsnappy-dev", "pkg-config",
        f"openjdk-{lock['java_major']}-jdk-headless",
    } <= package_names, "Missing mandatory image packages")
    ca = lock["bootstrap_ca"]
    require(isinstance(ca, dict) and set(ca) == {"url", "sha256", "version"}, "Invalid CA bootstrap")
    validate_url(ca["url"])
    require(ca["url"].startswith("https://snapshot.ubuntu.com/ubuntu/" + lock["apt_snapshot"] + "/"),
            "CA bootstrap must come from the selected apt snapshot")
    require(matches(SHA256, ca["sha256"]), "Invalid CA checksum")
    require(matches(r"[A-Za-z0-9.+:~_-]+", ca["version"]), "Invalid CA package version")
    android = lock["android"]
    require(isinstance(android, dict) and set(android) == {
        "api", "build_tools", "ndk", "cmdline_tools", "system_image", "abi",
    }, "Unknown/missing Android requirements")
    require(type(android["api"]) is int and 30 <= android["api"] <= 99, "Invalid Android API")
    require(android["abi"] == "x86_64", "Only x86_64 Android is supported")
    require(all(matches(VERSION, android[k]) for k in ("build_tools", "ndk", "cmdline_tools")),
            "Invalid Android component version")
    require(android["system_image"] == f"system-images;android-{android['api']};default;x86_64",
            "System image must match the requested API/ABI and default target")
    destinations = {
        "runner": "/opt/actions-runner",
        "cargo-llvm-cov": "/opt/ci/bin/cargo-llvm-cov",
        "cargo-nextest": "/opt/ci/bin/cargo-nextest",
        "cargo-machete": "/opt/ci/bin/cargo-machete",
        "cargo-ndk": "/opt/ci/bin/cargo-ndk",
        "protoc": "/opt/protoc",
        "rustup-init": "/opt/ci/rustup-init",
        f"platforms;android-{android['api']}": f"/opt/android-sdk/platforms/android-{android['api']}",
        f"ndk;{android['ndk']}": f"/opt/android-sdk/ndk/{android['ndk']}",
        f"build-tools;{android['build_tools']}": f"/opt/android-sdk/build-tools/{android['build_tools']}",
        f"cmdline-tools;{android['cmdline_tools']}": f"/opt/android-sdk/cmdline-tools/{android['cmdline_tools']}",
        "platform-tools": "/opt/android-sdk/platform-tools",
        "emulator": "/opt/android-sdk/emulator",
        android["system_image"]: "/opt/android-sdk/" + android["system_image"].replace(";", "/"),
    }
    artifacts = lock["artifacts"]
    require(isinstance(artifacts, list) and len(artifacts) == len(destinations),
            "Exactly the supported artifact set is required")
    seen = set()
    for artifact in artifacts:
        require(isinstance(artifact, dict), "Invalid artifact")
        mandatory = {"name", "url", "sha256", "format", "destination"}
        optional = {"upstream_sha1", "archive_root", "package_xml", "binary", "build_only"}
        require(mandatory <= set(artifact) and set(artifact) <= mandatory | optional,
                "Unknown/missing artifact fields")
        name = artifact["name"]
        require(isinstance(name, str) and name in destinations and name not in seen,
                "Unknown/duplicate artifact")
        seen.add(name)
        require(artifact["destination"] == destinations[name], "Artifact destination is not permitted")
        validate_url(artifact["url"])
        require(matches(SHA256, artifact["sha256"]), "Invalid artifact checksum")
        require(artifact["format"] in ("tar", "zip", "file"), "Unsupported archive format")
        if "upstream_sha1" in artifact:
            require(matches(SHA1, artifact["upstream_sha1"]), "Invalid upstream SHA-1")
        if "archive_root" in artifact:
            require(matches(r"[A-Za-z0-9][A-Za-z0-9_.-]*", artifact["archive_root"]),
                    "Unsafe archive root")
        if "binary" in artifact:
            require(name.startswith("cargo-") and artifact["binary"] == name,
                    "Invalid executable selection")
        require(artifact.get("build_only", False) == (name == "rustup-init"),
                "Only rustup-init may be build-only")
        if artifact["destination"].startswith("/opt/android-sdk/"):
            xml = artifact.get("package_xml")
            require(isinstance(xml, str) and len(xml) < 32768 and "<!" not in xml,
                    "Missing or unsafe Android package metadata")
            package = ET.fromstring(xml)
            require(package.tag == "{http://schemas.android.com/repository/android/common/02}repository",
                    "Invalid Android repository namespace")
            local = package.find("localPackage")
            require(local is not None and local.attrib.get("path") == name,
                    "Android package metadata does not match the artifact")
    return lock


def validate_manifest(manifest):
    require(isinstance(manifest, dict) and set(manifest) == {"schema", "recipe_revision", "requirements"},
            "Expected schema, recipe_revision and requirements")
    require(manifest["schema"] == 1 and type(manifest["schema"]) is int, "Expected manifest schema 1")
    require(matches(SHA1, manifest["recipe_revision"]), "Recipe must be pinned to a full commit SHA")
    validate_lock(manifest["requirements"])
    return manifest


def render(lock, template):
    validate_lock(lock)
    epoch = int(datetime.datetime.strptime(lock["apt_snapshot"], "%Y%m%dT%H%M%SZ")
                .replace(tzinfo=datetime.timezone.utc).timestamp())
    values = {
        "BASE_IMAGE": lock["ubuntu_image"], "CA_SHA256": lock["bootstrap_ca"]["sha256"],
        "CA_URL": lock["bootstrap_ca"]["url"], "APT_SNAPSHOT": lock["apt_snapshot"],
        "SOURCE_DATE_EPOCH": str(epoch), "APT_PACKAGES": shlex.join(lock["apt_packages"]),
        "CONTRACT_VERSION": lock["contract_version"], "JAVA_MAJOR": str(lock["java_major"]),
        "NDK_VERSION": lock["android"]["ndk"], "CMDLINE_VERSION": lock["android"]["cmdline_tools"],
    }
    for key, value in values.items():
        template = template.replace("@@" + key + "@@", value)
    require("@@" not in template, "Unknown Dockerfile template parameter")
    return template


def differences(expected, actual, prefix=""):
    if isinstance(expected, dict) and isinstance(actual, dict):
        return [path for key in sorted(set(expected) | set(actual))
                for path in differences(expected.get(key), actual.get(key), prefix + "." + key)]
    return [] if expected == actual else [prefix.lstrip(".")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("manifest")
    materialize = commands.add_parser("materialize")
    materialize.add_argument("manifest")
    materialize.add_argument("context")
    compare = commands.add_parser("compare")
    compare.add_argument("base")
    compare.add_argument("head")
    compare.add_argument("--github-output")
    verify = commands.add_parser("verify")
    verify.add_argument("manifest")
    verify.add_argument("--installed-root", default="/opt/ci")
    args = parser.parse_args()
    if args.command == "compare":
        head = validate_manifest(read_json(args.head))
        base_path = Path(args.base)
        base = validate_manifest(read_json(base_path)) if base_path.exists() else None
        result = {
            "changed": head != base, "manifest_sha256": fingerprint(head),
            "recipe_revision": head["recipe_revision"],
            "changed_fields": differences(base, head),
        }
        if args.github_output:
            with open(args.github_output, "a") as handle:
                handle.write(f"changed={str(result['changed']).lower()}\n")
                handle.write(f"manifest_sha256={result['manifest_sha256']}\n")
                handle.write(f"recipe_revision={result['recipe_revision']}\n")
        print(json.dumps(result))
        return
    manifest = validate_manifest(read_json(args.manifest))
    if args.command == "materialize":
        context = Path(args.context)
        template = (context / "Dockerfile.template").read_text()
        (context / "image.lock.json").write_text(json.dumps(manifest["requirements"], indent=2) + "\n")
        (context / "Dockerfile").write_text(render(manifest["requirements"], template))
    elif args.command == "verify":
        installed = Path(args.installed_root)
        actual = {
            "schema": 1, "recipe_revision": (installed / "recipe-revision").read_text().strip(),
            "requirements": validate_lock(read_json(installed / "image.lock.json")),
        }
        delta = differences(manifest, actual)
        require(not delta, "Runner image mismatch: " + ", ".join(delta)
                + ". Build/use the candidate for this exact PR; do not install system packages in the job.")
    print(json.dumps({"manifest_sha256": fingerprint(manifest),
                      "recipe_revision": manifest["recipe_revision"], "status": "ok"}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, ET.ParseError) as error:
        print(f"Image contract error: {error}", file=sys.stderr)
        sys.exit(1)
