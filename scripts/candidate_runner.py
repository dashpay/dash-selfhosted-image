#!/usr/bin/env python3
"""Operator-side, bounded launcher for uniquely labelled Platform PR jobs.

This belongs on the Docker HOST, never in a job container. Default is dry-run.
GitHub App and Docker authority are not passed into candidate runners.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error

from image_contract import matches, read_json, require
from platform_request import (
    GitHub, IMAGE, PLATFORM, candidate_digest, candidate_label, check_current, make_request,
)

MANAGED = "org.dash.ci.candidate"
LABEL = re.compile(r"platform-image-pr-([1-9][0-9]*)-([0-9a-f]{40})-([0-9a-f]{64})-(rust|kotlin)")


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def app_token(config):
    key = Path(config["private_key_file"])
    info = key.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_mode & 0o077 == 0,
            "App key must be a root-owned, owner-only regular file")
    now = int(time.time())
    unsigned = b64(b'{"alg":"RS256","typ":"JWT"}') + "." + b64(json.dumps({
        "iat": now - 30, "exp": now + 540, "iss": str(config["app_id"]),
    }).encode())
    signature = subprocess.check_output(
        ["openssl", "dgst", "-sha256", "-sign", str(key)],
        input=unsigned.encode(), stderr=subprocess.DEVNULL)
    api = GitHub(unsigned + "." + b64(signature))
    response = api.call(f"app/installations/{config['installation_id']}/access_tokens", {})
    return response["token"]


def allowed_pr(pr, config):
    # Keep the existing Platform fork boundary; no default wildcard trust.
    return (pr["head"]["repo"]["full_name"] == PLATFORM
            or pr["head"]["repo"]["owner"]["login"] in config["trusted_fork_owners"]
            or config.get("approved_heads", {}).get(str(pr["number"])) == pr["head"]["sha"])


def parse_job(job):
    labels = [LABEL.fullmatch(label) for label in job.get("labels", [])]
    labels = [match for match in labels if match]
    require(len(labels) == 1, "Job needs exactly one candidate label")
    match = labels[0]
    return int(match.group(1)), match.group(2), "sha256:" + match.group(3), match.group(4)


def docker_arguments(config, job, record, digest, jit_path, kvm_gid=None, runner_id=None):
    pr, head, requested_digest, kind = parse_job(job)
    require(pr == record["pr"] and head == record["head_sha"], "Job label and current PR disagree")
    require(matches(r"sha256:[0-9a-f]{64}", digest) and digest == requested_digest, "Candidate digest differs from requested job")
    require(type(job["id"]) is int and job["id"] > 0, "Invalid GitHub job ID")
    name = f"platform-pr-{pr}-{head[:8]}-{digest[7:15]}-{kind}-{job['id']}"
    args = [
        "docker", "run", "--detach", "--init", "--name", name,
        "--user", "1001:1001", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges=true",
        "--cpus", str(config["cpus_per_runner"]),
        "--memory", f"{config['memory_gib_per_runner']}g", "--pids-limit", "4096",
        "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
        "--label", MANAGED + "=1", "--label", "org.dash.ci.job=" + str(job["id"]),
        "--label", "org.dash.ci.created=" + str(int(time.time())),
        "--mount", "type=volume,destination=/runner",
        "--mount", "type=volume,destination=/work",
        "--mount", f"type=bind,src={jit_path},dst=/run/secrets/runner-jit,readonly",
        "--env", "CARGO_BUILD_JOBS=" + str(config["cpus_per_runner"]),
    ]
    if kind == "kotlin":
        require(type(kvm_gid) is int and kvm_gid >= 0, "KVM group is required")
        args += ["--device", "/dev/kvm:/dev/kvm:rw", "--group-add", str(kvm_gid)]
    if runner_id is not None:
        require(type(runner_id) is int and runner_id > 0, "Invalid runner registration ID")
        args += ["--label", "org.dash.ci.runner-id=" + str(runner_id)]
    return args + [IMAGE + "@" + digest, "jit"]


def docker_json(*args):
    return json.loads(subprocess.check_output(["docker", *args], text=True))


def managed_containers():
    ids = subprocess.check_output(
        ["docker", "ps", "-aq", "--filter", "label=" + MANAGED + "=1"], text=True).split()
    return docker_json("inspect", *ids) if ids else []


def validate_config(config):
    for key in ("app_id", "installation_id", "runner_group_id", "max_runners",
                "cpus_per_runner", "memory_gib_per_runner", "min_free_gib", "max_age_seconds"):
        require(type(config.get(key)) is int and config[key] > 0, "Configure a positive " + key)
    require(config["max_runners"] <= 4 and config["max_age_seconds"] <= 14400,
            "Candidate pool and lifetime must stay bounded")
    require(config.get("trusted_fork_owners") == ["thepastaclaw"],
            "Keep the existing Platform trusted-fork policy; use approved_heads for explicit exceptions")
    require(Path(config["state_dir"]).is_absolute() and Path(config["private_key_file"]).is_absolute(),
            "Use absolute protected state/key paths")
    for number, head in config.get("approved_heads", {}).items():
        require(matches(r"[1-9][0-9]*", number) and matches(r"[0-9a-f]{40}", head),
                "Explicit approvals must bind a PR number to one exact head SHA")
    return config


def items(api, path, key):
    separator = "&" if "?" in path else "?"
    for page in range(1, 101):
        result = api.call(path + separator + f"per_page=100&page={page}")[key]
        yield from result
        if len(result) < 100:
            return
    raise RuntimeError("Pagination limit reached; refusing to silently drop queued work")


def queued_jobs(api):
    seen = set()
    for status in ("queued", "in_progress"):
        for run in items(api, f"repos/{PLATFORM}/actions/runs?status={status}", "workflow_runs"):
            if run["id"] in seen or run["event"] != "pull_request":
                continue
            seen.add(run["id"])
            if run["path"] not in (".github/workflows/tests.yml", ".github/workflows/kotlin-sdk-build.yml"):
                continue
            for job in items(api, f"repos/{PLATFORM}/actions/runs/{run['id']}/jobs", "jobs"):
                if job["status"] == "queued" and any(LABEL.fullmatch(label) for label in job.get("labels", [])):
                    try:
                        require(parse_job(job)[1] == run["head_sha"], "Queued workflow head and runner label disagree")
                    except ValueError as error:
                        print(f"Skipped invalid candidate job {job['id']}: {error}", file=sys.stderr, flush=True)
                        continue
                    yield job


def reconcile(api, config, apply=False):
    state = Path(config["state_dir"])
    if apply:
        require(os.geteuid() == 0, "Apply mode must run as the dedicated host operator service")
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(state.stat().st_mode & 0o077 == 0, "Controller state directory must be owner-only")
    if apply:
        repositories = list(items(api, f"orgs/dashpay/actions/runner-groups/{config['runner_group_id']}/repositories",
                                  "repositories"))
        group = api.call(f"orgs/dashpay/actions/runner-groups/{config['runner_group_id']}")
        require(group["visibility"] == "selected" and {r["full_name"] for r in repositories} == {PLATFORM},
                "Use a dedicated candidate group selected for dashpay/platform only")
        require(group.get("allows_public_repositories") is True,
                "The candidate group must explicitly permit the selected public Platform repository")
    containers = managed_containers() if apply else []
    tombstone_path = state / "registrations-to-clean.json"
    tombstones = read_json(tombstone_path) if apply and tombstone_path.exists() else {}
    active_jobs = set()
    active_registrations = set()
    now = int(time.time())
    for container in containers:
        labels = container["Config"]["Labels"]
        require(labels.get(MANAGED) == "1" and container["Name"].startswith("/platform-pr-"),
                "Refusing to manage a container outside the dedicated candidate pool")
        age = now - int(labels["org.dash.ci.created"])
        if not container["State"]["Running"] or age > config["max_age_seconds"]:
            if labels.get("org.dash.ci.runner-id"):
                tombstones[labels["org.dash.ci.runner-id"]] = container["Name"].lstrip("/")
                tombstone_path.write_text(json.dumps(tombstones) + "\n")
            # Only this controller's disposable registration/work volumes.
            subprocess.run(["docker", "rm", "-f", "-v", container["Id"]], check=True,
                           stdout=subprocess.DEVNULL)
            print(f"Removed completed/expired candidate container {container['Name']}", flush=True)
        else:
            active_jobs.add(int(labels["org.dash.ci.job"]))
            if labels.get("org.dash.ci.runner-id"):
                active_registrations.add(labels["org.dash.ci.runner-id"])
    for runner_id, expected_name in list(tombstones.items()):
        require(runner_id.isdigit() and expected_name.startswith("platform-pr-"), "Invalid cleanup registration")
        if runner_id in active_registrations:
            continue
        try:
            registration = api.call(f"orgs/dashpay/actions/runners/{runner_id}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        else:
            require(registration["name"] == expected_name, "Runner cleanup identity changed")
            if registration.get("busy"):
                continue
            api.call(f"orgs/dashpay/actions/runners/{runner_id}", method="DELETE")
        del tombstones[runner_id]
    if apply:
        tombstone_path.write_text(json.dumps(tombstones) + "\n")
    retries_path = state / "launch-retries.json"
    retries = read_json(retries_path) if apply and retries_path.exists() else {}
    for job in queued_jobs(api):
        if job["id"] in active_jobs:
            continue
        if len(active_jobs) >= config["max_runners"]:
            break
        retry = retries.get(str(job["id"]), {"attempts": 0, "after": 0})
        if retry["attempts"] >= 3 or now < retry["after"]:
            continue
        try:
            if launch_candidate(api, config, job, state, apply):
                active_jobs.add(job["id"])
                retries.pop(str(job["id"]), None)
        except Exception as error:
            # One rejected/stale PR must not starve independent approved jobs.
            print(f"Candidate job {job['id']} blocked: {type(error).__name__}: {error}",
                  file=sys.stderr, flush=True)
            retry = {"attempts": retry["attempts"] + 1, "after": now + 60 * 5}
            retries[str(job["id"])] = retry
            if retry["attempts"] >= 3:
                print(f"Candidate job {job['id']} needs operator attention; retry budget exhausted", flush=True)
        if apply:
            retries_path.write_text(json.dumps(retries) + "\n")


def launch_candidate(api, config, job, state, apply):
    number, head, requested_digest, kind = parse_job(job)
    pr = api.call(f"repos/{PLATFORM}/pulls/{number}")
    require(allowed_pr(pr, config), f"PR {number} is outside the persistent-runner fork policy")
    require(pr["state"] == "open" and pr["head"]["sha"] == head, "Queued candidate request is stale")
    manifest = api.manifest(head)
    record = make_request(pr, manifest, api.manifest(pr["base"]["sha"], missing_ok=True))
    record["changed"] = api.changes_manifest(pr)
    require(record["changed"], "Candidate label used without a requirements change")
    digest = candidate_digest(api, record)
    require(digest == requested_digest, "Queued job references a superseded candidate digest; rerun its selector")
    if not apply:
        print(json.dumps({"job": job["id"], "pr": number, "kind": kind,
                          "reference": IMAGE + "@" + digest, "action": "would-start"}))
        return True
    docker_root = subprocess.check_output(["docker", "info", "--format", "{{.DockerRootDir}}"], text=True).strip()
    require(shutil.disk_usage(docker_root).free >= config["min_free_gib"] * 1024**3,
            "Insufficient free disk for a candidate; no existing volumes will be pruned")
    available_kib = next(int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()
                         if line.startswith("MemAvailable:"))
    require(available_kib >= (config["memory_gib_per_runner"] + 16) * 1024**2,
            "Insufficient available RAM for the candidate plus the host reserve")
    subprocess.run(["docker", "pull", IMAGE + "@" + digest], check=True,
                   stdout=subprocess.DEVNULL)
    image = docker_json("image", "inspect", IMAGE + "@" + digest)[0]
    require(image["Config"].get("User") == "1001:1001", "Image user is not 1001:1001")
    labels = image["Config"].get("Labels", {})
    require(labels.get("org.dash.platform.head") == head
            and labels.get("org.dash.ci.manifest-sha256") == record["manifest_sha256"]
            and labels.get("org.dash.platform.pr") == str(number)
            and labels.get("org.opencontainers.image.revision") == record["recipe_revision"],
            "Published candidate identity does not match the queued PR")
    check_current(api, record)
    runner_name = f"platform-pr-{number}-{head[:8]}-{digest[7:15]}-{kind}-{job['id']}"
    jit = api.call("orgs/dashpay/actions/runners/generate-jitconfig", {
        "name": runner_name, "runner_group_id": config["runner_group_id"],
        "labels": ["self-hosted", "Linux", "X64", candidate_label(number, head, digest, kind)],
        "work_folder": "/work",
    })
    # Persist ownership before starting Docker so a crash/failed start does not
    # leave an untracked GitHub registration. Reconciliation skips live runners
    # and waits for any busy lease before deleting this exact name/id.
    tombstone_path = state / "registrations-to-clean.json"
    tombstones = read_json(tombstone_path) if tombstone_path.exists() else {}
    tombstones[str(jit["runner"]["id"])] = runner_name
    tombstone_path.write_text(json.dumps(tombstones) + "\n")
    # GitHub App credentials never enter a job. Only this single-use runner
    # configuration is mounted; no host keys, host workspaces or socket.
    with tempfile.TemporaryDirectory(prefix="jit-", dir=state) as directory:
        os.chmod(directory, 0o711)
        jit_path = Path(directory) / "config"
        jit_path.write_text(jit["encoded_jit_config"])
        os.chown(jit_path, 1001, 1001)
        jit_path.chmod(0o400)
        gid = os.stat("/dev/kvm").st_gid if kind == "kotlin" else None
        if kind == "kotlin":
            require(stat.S_ISCHR(os.stat("/dev/kvm").st_mode), "KVM is not a character device")
        command = docker_arguments(config, job, record, digest, jit_path, gid, jit["runner"]["id"])
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
    print(json.dumps({"job": job["id"], "runner": runner_name, "reference": IMAGE + "@" + digest}), flush=True)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = validate_config(read_json(args.config))
    token, refreshed = None, 0
    while True:
        try:
            if args.apply and time.time() - refreshed > 2400:
                token, refreshed = app_token(config), time.time()
            reconcile(GitHub(token), config, apply=args.apply)
        except Exception as error:
            print(f"Candidate launcher blocked: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
