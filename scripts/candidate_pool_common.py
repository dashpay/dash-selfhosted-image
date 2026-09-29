"""Credential-free contracts for the permanent, split candidate pool."""
import json
import os
from pathlib import Path
import re
import tempfile

from image_contract import require
from platform_request import IMAGE, PLATFORM, candidate_label

MANAGED = "org.dash.ci.candidate-pool"
OWNER = "org.dash.ci.candidate-pool-owner"
GROUP_ID = 7
GROUP_NAME = "platform-image-candidates"
STATE = "/var/lib/dash-ci-candidate-pool"
CONFIG = "/etc/dash-ci-candidate-pool/config.json"
WORKFLOWS = {
    ".github/workflows/tests.yml": "rust",
    ".github/workflows/kotlin-sdk-build.yml": "kotlin",
    ".github/workflows/npm-runner-validation.yml": "npm",
}
SPEC_KEYS = {"name", "job_id", "run_id", "attempt", "pr", "head", "digest",
             "manifest_sha256", "recipe_revision", "kind", "label", "created"}


def atomic_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_spec(spec):
    require(isinstance(spec, dict) and set(spec) == SPEC_KEYS, "Invalid work order fields")
    for key in ("job_id", "run_id", "attempt", "pr", "created"):
        require(type(spec[key]) is int and spec[key] > 0, "Invalid " + key)
    require(spec["kind"] in WORKFLOWS.values(), "Invalid candidate kind")
    require(re.fullmatch(r"platform-pr-" + str(spec["pr"]) + "-" + spec["kind"]
                         + "-" + str(spec["job_id"]) + r"-[0-9a-f]{8}", spec["name"]),
            "Invalid allocation name")
    for key in ("head", "recipe_revision"):
        require(re.fullmatch(r"[0-9a-f]{40}", spec[key]), "Invalid " + key)
    require(re.fullmatch(r"[0-9a-f]{64}", spec["manifest_sha256"]), "Invalid manifest hash")
    require(spec["label"] == candidate_label(spec["pr"], spec["head"], spec["digest"], spec["kind"]),
            "Work order label mismatch")
    return spec


def validate_host_config(config):
    require(set(config) == {"host", "state_dir", "cpus", "memory_gib", "binaryen_cores",
                           "min_free_gib", "max_age_seconds", "max_runners"}, "Invalid host config")
    require(config["host"] in ("runner1", "server2") and config["state_dir"] == STATE,
            "Unexpected host or state directory")
    for key in ("cpus", "memory_gib", "binaryen_cores", "min_free_gib", "max_age_seconds", "max_runners"):
        require(type(config[key]) is int and config[key] > 0, "Invalid " + key)
    require(config["max_runners"] == 1 and config["cpus"] <= 20 and config["memory_gib"] <= 32
            and config["min_free_gib"] >= 100 and config["max_age_seconds"] <= 10800
            and config["binaryen_cores"] <= config["cpus"], "Resource bounds exceeded")
    return config


def labels(spec):
    return ["self-hosted", "Linux", "X64", spec["label"]]


def image_reference(spec):
    return IMAGE + "@" + spec["digest"]
