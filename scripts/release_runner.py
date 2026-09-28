#!/usr/bin/env python3
"""Host-side single-job Platform release runners. Read-only unless --apply.

The image is operator-pinned, not supplied by a workflow. No persistent CI
container, registration, HOME, workspace or cache is reused. GitHub JIT config
admits one job; the host also kills completed/cancelled/expired containers,
including descendants a job might leave behind.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import uuid

from candidate_runner import app_token, docker_json, items
from image_contract import read_json, require
from platform_request import GitHub, IMAGE, PLATFORM

MANAGED = "org.dash.ci.release"
OWNER = "org.dash.ci.release-owner"
GROUP = "platform-release-builds"
LABEL = re.compile(r"platform-release-([1-9][0-9]*)-([1-9][0-9]*)-(npm|kotlin)")
NAME = re.compile(r"platform-release-[1-9][0-9]*-[0-9a-f]{12}")
WORKFLOWS = {
    ".github/workflows/release.yml": {"npm", "kotlin"},
    ".github/workflows/release-kotlin-sdk.yml": {"kotlin"},
}


def validate_config(config):
    for key in ("app_id", "installation_id", "runner_group_id", "max_runners",
                "memory_gib_per_runner", "min_free_gib",
                "max_age_seconds"):
        require(type(config.get(key)) is int and config[key] > 0, "Configure a positive " + key)
    cpus = config.get("cpus_per_runner")
    require(cpus == "auto" or (type(cpus) is int and cpus > 0),
            "Configure cpus_per_runner as a positive integer or auto")
    if cpus == "auto" or "reserved_cpus" in config:
        require(type(config.get("reserved_cpus")) is int and config["reserved_cpus"] >= 0,
                "Configure nonnegative reserved_cpus for the host and other workloads")
    if "binaryen_cores" in config:
        require(type(config["binaryen_cores"]) is int and config["binaryen_cores"] > 0,
                "Configure binaryen_cores as a positive integer")
    require(config["max_runners"] <= 4 and config["max_age_seconds"] <= 14400,
            "Release capacity and lifetime must stay bounded")
    for key in ("state_dir", "private_key_file"):
        require(isinstance(config.get(key), str) and Path(config[key]).is_absolute(),
                "Use an absolute " + key)
    images = config.get("images")
    require(isinstance(images, dict) and set(images) == {"npm", "kotlin"},
            "Pin an image for each release kind")
    for reference in images.values():
        require(isinstance(reference, str) and re.fullmatch(
            r"(?:" + re.escape(IMAGE) + r"@)?sha256:[0-9a-f]{64}", reference),
            "Use a local image ID or an immutable Dash image digest, never a tag")
    return config


def cpu_budget(config):
    """Split the configured release CPU pool across its maximum concurrent jobs."""
    if config["cpus_per_runner"] != "auto":
        return config["cpus_per_runner"]
    available = len(os.sched_getaffinity(0)) - config["reserved_cpus"]
    require(available >= config["max_runners"],
            "Insufficient CPUs after reservations for all configured release slots")
    return available // config["max_runners"]


def binaryen_budget(config, cpus):
    """Allow independent optimizer tuning within the resolved runner CPU budget."""
    cores = config.get("binaryen_cores", cpus)
    require(type(cores) is int and 0 < cores <= cpus,
            "binaryen_cores must be a positive integer no greater than the runner CPU budget")
    return cores


def parse_job(run, job):
    matches = [LABEL.fullmatch(label) for label in job.get("labels", [])]
    matches = [match for match in matches if match]
    require(len(matches) == 1, "Expected one release label")
    match = matches[0]
    run_id, attempt, kind = int(match[1]), int(match[2]), match[3]
    require(type(job.get("id")) is int and job["id"] > 0, "Invalid job ID")
    require(run_id == run["id"] == job["run_id"] and attempt == run["run_attempt"],
            "Release label must bind the current run and attempt")
    require(job["head_sha"] == run["head_sha"], "Job and run commits disagree")
    require(kind in WORKFLOWS.get(run["path"], set()), "Unexpected release workflow/kind")
    require(run.get("event") in ("release", "workflow_dispatch")
            and run.get("repository", {}).get("full_name") == PLATFORM
            and run.get("head_repository", {}).get("full_name") == PLATFORM,
            "Only Platform release/dispatch runs can request a release runner")
    require(set(job["labels"]) == {"self-hosted", "Linux", "X64", match[0]},
            "Release runners cannot carry persistent-pool or extra labels")
    require(job.get("status") == "queued", "Job is no longer queued")
    return kind, match[0]


def queued_jobs(api):
    seen = set()
    for status in ("queued", "in_progress"):
        for run in items(api, f"repos/{PLATFORM}/actions/runs?status={status}", "workflow_runs"):
            if run["id"] in seen or run.get("event") not in ("release", "workflow_dispatch"):
                continue
            seen.add(run["id"])
            if run.get("path") not in WORKFLOWS:
                continue
            path = (f"repos/{PLATFORM}/actions/runs/{run['id']}/attempts/"
                    f"{run['run_attempt']}/jobs")
            for job in items(api, path, "jobs"):
                if job.get("status") != "queued" or not any(
                        LABEL.fullmatch(label) for label in job.get("labels", [])):
                    continue
                try:
                    parse_job(run, job)
                except ValueError as error:
                    print(f"Release job {job['id']} rejected: {error}", file=sys.stderr, flush=True)
                    continue
                yield run, job


def save(path, value):
    """Atomic controller journal; never read anything written by job containers."""
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as file:
        os.chmod(temporary, 0o600)
        json.dump(value, file)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def docker_exists(kind, name):
    args = ["docker", "ps", "-aq", "--filter", f"name=^/{name}$"] if kind == "container" else [
        "docker", "volume", "ls", "-q", "--filter", f"name=^{name}$"]
    return bool(subprocess.check_output(args, text=True).strip())


def owned_resource(kind, name, owner):
    if not docker_exists(kind, name):
        return None
    value = docker_json("inspect", name)[0] if kind == "container" else docker_json("volume", "inspect", name)[0]
    labels = value["Config"].get("Labels", {}) if kind == "container" else value.get("Labels", {})
    require(labels.get(MANAGED) == "1" and labels.get(OWNER) == owner,
            "Refusing to touch a resource outside this release allocation")
    return value


def docker_arguments(config, record, image_id, jit_path):
    """All writable state is fresh; HOME lives in the disposable image layer."""
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", image_id), "Expected an immutable local image ID")
    cpus = record["cpus"] if "cpus" in record else cpu_budget(config)
    # Keep journaled settings stable across config changes. Older records with
    # only cpus used the same budget for Binaryen; do not retune them implicitly.
    binaryen_cores = binaryen_budget(record if "cpus" in record else config, cpus)
    args = [
        "docker", "run", "--detach", "--init", "--name", record["name"],
        "--restart", "no", "--pull", "never", "--user", "1001:1001",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
        "--cpus", str(cpus),
        "--memory", f"{config['memory_gib_per_runner']}g", "--pids-limit", "4096",
        "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
        "--label", MANAGED + "=1", "--label", OWNER + "=" + record["name"],
        "--mount", f"type=volume,src={record['name']}-registration,dst=/runner",
        "--mount", f"type=volume,src={record['name']}-work,dst=/work",
        "--mount", f"type=bind,src={jit_path},dst=/run/secrets/runner-jit,readonly",
        "--env", "RUNNER_MANUALLY_TRAP_SIG=1",
        "--env", "DASH_RELEASE_RUNNER=1",
        "--env", "DASH_RELEASE_RUN_ID=" + str(record["run_id"]),
        "--env", "DASH_RELEASE_RUN_ATTEMPT=" + str(record["attempt"]),
        "--env", "DASH_RELEASE_KIND=" + record["kind"],
        "--env", "CARGO_BUILD_JOBS=" + str(cpus),
        # Binaryen otherwise sees every host CPU, ignoring the Docker quota.
        "--env", "BINARYEN_CORES=" + str(binaryen_cores),
    ]
    # NPM and Kotlin RELEASE builds need no emulator, KVM, Docker or host mounts.
    return args + [image_id, "jit"]


def verify_group(api, config):
    group = api.call(f"orgs/dashpay/actions/runner-groups/{config['runner_group_id']}")
    repositories = list(items(api, f"orgs/dashpay/actions/runner-groups/"
                              f"{config['runner_group_id']}/repositories", "repositories"))
    require(group["name"] == GROUP and group["visibility"] == "selected"
            and not group.get("default", False)
            and group.get("allows_public_repositories") is True
            and {repo["full_name"] for repo in repositories} == {PLATFORM},
            "Use the dedicated platform-release-builds group selected for Platform only")


def verify_no_orphans(journal):
    names = subprocess.check_output([
        "docker", "ps", "-a", "--filter", "label=" + MANAGED + "=1",
        "--format", "{{.Names}}"], text=True).split()
    require(set(names) <= set(journal["allocations"]),
            "Unjournaled release container exists; operator review required")


def cleanup(api, config, state, record):
    name = record["name"]
    require(NAME.fullmatch(name), "Invalid allocation name")
    container = owned_resource("container", name, name)
    if container:
        # Stop the entire container first, not just Runner.Listener; no process
        # from a completed job may survive to a later release.
        subprocess.run(["docker", "kill", name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
        with (state / (name + ".log")).open("wb") as log:
            subprocess.run(["docker", "logs", "--tail", "2000", name], stdout=log,
                           stderr=subprocess.STDOUT, check=True)
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, check=True)
    for suffix in ("registration", "work"):
        volume = name + "-" + suffix
        if owned_resource("volume", volume, name):
            subprocess.run(["docker", "volume", "rm", volume], stdout=subprocess.DEVNULL, check=True)
    (state / (name + ".jit")).unlink(missing_ok=True)
    # Recover even if generate-jitconfig succeeded but its response was lost.
    # Names are journaled BEFORE that request, and never reused for a retry.
    for runner in items(api, f"orgs/dashpay/actions/runner-groups/"
                        f"{config['runner_group_id']}/runners", "runners"):
        if runner["name"] != name:
            continue
        require(not record.get("runner_id") or record["runner_id"] == runner["id"],
                "Cleanup registration identity changed")
        if runner.get("busy"):
            return False  # Keep the journal and retry after the dead lease expires.
        try:
            api.call(f"orgs/dashpay/actions/runners/{runner['id']}", method="DELETE")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
    return True


def launch(api, config, state, journal, run, job):
    kind, label = parse_job(run, job)
    cpus = cpu_budget(config)
    binaryen_cores = binaryen_budget(config, cpus)
    reference = config["images"][kind]
    image = docker_json("image", "inspect", reference)[0]
    require(image.get("Os") == "linux" and image.get("Architecture") == "amd64"
            and image["Config"].get("User") == "1001:1001"
            and image["Config"].get("Entrypoint") == ["/opt/ci/bin/runner-entrypoint"]
            and not image["Config"].get("Volumes"), "Unrecognized release image runtime contract")
    docker_root = subprocess.check_output(["docker", "info", "--format", "{{.DockerRootDir}}"], text=True).strip()
    require(shutil.disk_usage(docker_root).free >= config["min_free_gib"] * 1024**3,
            "Insufficient disk; no existing CI storage will be pruned")
    available = next(int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()
                     if line.startswith("MemAvailable:"))
    require(available >= (config["memory_gib_per_runner"] + 16) * 1024**2,
            "Insufficient memory for a release plus the 16 GiB host reserve")
    # Recheck after resource/image inspection; do not launch cancelled or old attempts.
    current_run = api.call(f"repos/{PLATFORM}/actions/runs/{run['id']}")
    current_job = api.call(f"repos/{PLATFORM}/actions/jobs/{job['id']}")
    require(current_run["run_attempt"] == run["run_attempt"], "Run attempt changed")
    parse_job(current_run, current_job)
    name = f"platform-release-{job['id']}-{uuid.uuid4().hex[:12]}"
    record = {"name": name, "job_id": job["id"], "run_id": run["id"],
              "attempt": run["run_attempt"], "kind": kind, "created": int(time.time()),
              "image": image["Id"], "phase": "starting", "cpus": cpus,
              "binaryen_cores": binaryen_cores}
    journal["allocations"][name] = record
    save(state / "journal.json", journal)
    for suffix in ("registration", "work"):
        volume = name + "-" + suffix
        require(not docker_exists("volume", volume), "Refusing to reuse any existing volume")
        subprocess.run(["docker", "volume", "create", "--label", MANAGED + "=1",
                        "--label", OWNER + "=" + name, volume], check=True, stdout=subprocess.DEVNULL)
    jit = api.call("orgs/dashpay/actions/runners/generate-jitconfig", {
        "name": name, "runner_group_id": config["runner_group_id"],
        "labels": ["self-hosted", "Linux", "X64", label], "work_folder": "/work",
    })
    record["runner_id"] = jit["runner"]["id"]
    save(state / "journal.json", journal)
    jit_path = state / (name + ".jit")
    with jit_path.open("x") as file:
        file.write(jit["encoded_jit_config"])
    os.chown(jit_path, 1001, 1001)
    jit_path.chmod(0o400)
    subprocess.run(docker_arguments(config, record, image["Id"], jit_path), check=True,
                   stdout=subprocess.DEVNULL)
    record["phase"] = "running"
    save(state / "journal.json", journal)
    print(json.dumps({"job": job["id"], "runner": name, "image": image["Id"],
                      "cpus": cpus, "binaryen_cores": binaryen_cores}), flush=True)


def reconcile(api, config, apply=False, drain=False):
    if not apply:
        for run, job in queued_jobs(api):
            kind, _ = parse_job(run, job)
            cpus = cpu_budget(config)
            print(json.dumps({"job": job["id"], "action": "would-start",
                              "image": config["images"][kind], "cpus": cpus,
                              "binaryen_cores": binaryen_budget(config, cpus)}))
        return
    state = Path(config["state_dir"])
    path = state / "journal.json"
    journal = read_json(path) if path.exists() else {"allocations": {}, "attempts": {}}
    now = int(time.time())
    # Local cleanup still runs if GitHub or group policy is unavailable. Active
    # containers without a readable job status remain bounded by max_age_seconds.
    for name, record in list(journal["allocations"].items()):
        container = owned_resource("container", name, name)
        expired = now - record["created"] >= config["max_age_seconds"]
        finished = not container or not container["State"]["Running"] or expired
        if not finished:
            try:
                job = api.call(f"repos/{PLATFORM}/actions/jobs/{record['job_id']}")
                finished = job["status"] == "completed"
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    finished = True
                else:
                    print(f"Job status unavailable for {record['job_id']}; lifetime limit still applies", flush=True)
            except (urllib.error.URLError, TimeoutError):
                print(f"Job status unavailable for {record['job_id']}; lifetime limit still applies", flush=True)
        if finished:
            try:
                if cleanup(api, config, state, record):
                    del journal["allocations"][name]
                    print(f"Removed release allocation {name}", flush=True)
            except Exception as error:
                print(f"Release cleanup pending for {name}: {error}", file=sys.stderr, flush=True)
            save(path, journal)
    if drain:
        return
    verify_no_orphans(journal)
    verify_group(api, config)
    # Never adopt or reuse another runner, including an old persistent release runner.
    known = set(journal["allocations"])
    runners = list(items(api, f"orgs/dashpay/actions/runner-groups/"
                        f"{config['runner_group_id']}/runners", "runners"))
    require(all(runner["name"] in known for runner in runners),
            "Release group contains an unmanaged registration; operator review required")
    active = {record["job_id"] for record in journal["allocations"].values()}
    journal["attempts"] = {key: retry for key, retry in journal["attempts"].items()
                           if int(key) in active or retry["after"] >= now - 7 * 86400}
    save(path, journal)
    for run, job in queued_jobs(api):
        if job["id"] in active:
            continue
        if len(journal["allocations"]) >= config["max_runners"]:
            break
        key = str(job["id"])
        retry = journal["attempts"].get(key, {"count": 0, "after": 0})
        if retry["count"] >= 3 or now < retry["after"]:
            continue
        journal["attempts"][key] = {"count": retry["count"] + 1, "after": now + 300}
        save(path, journal)
        try:
            launch(api, config, state, journal, run, job)
        except Exception as error:
            print(f"Release job {job['id']} launch blocked: {error}", file=sys.stderr, flush=True)
        if journal["attempts"][key]["count"] == 3:
            print(f"Release job {job['id']}: final automatic allocation attempt; inspect if it stays queued", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--drain", action="store_true", help="Cleanup only; do not allocate new runners")
    args = parser.parse_args()
    config = validate_config(read_json(args.config))
    lock = None
    if args.apply:
        require(os.geteuid() == 0, "Apply runs only in the host operator service")
        config_path = Path(args.config)
        info = config_path.stat()
        require(not config_path.is_symlink() and info.st_uid == 0 and info.st_mode & 0o077 == 0,
                "Controller config must be a root-owned, owner-only file")
        state = Path(config["state_dir"])
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(state.resolve() == state and state.stat().st_uid == 0
                and state.stat().st_mode & 0o077 == 0, "State must be a private root-owned directory")
        os.umask(0o077)
        lock = (state / "controller.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    token, refreshed = None, 0
    while True:
        failed = False
        try:
            if args.apply and time.time() - refreshed > 2400:
                # Keep cleanup running with the last token if renewal fails.
                try:
                    token, refreshed = app_token(config), time.time()
                except Exception as error:
                    print(f"App token renewal failed: {error}", file=sys.stderr, flush=True)
            reconcile(GitHub(token), config, apply=args.apply, drain=args.drain)
        except Exception as error:
            failed = True
            print(f"Release controller blocked: {error}", file=sys.stderr, flush=True)
        if args.once:
            return int(failed)
        time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
