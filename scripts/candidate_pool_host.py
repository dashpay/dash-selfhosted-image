#!/usr/bin/env python3
"""Root-owned worker lifecycle. No GitHub credential or API access is needed."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time

from candidate_pool_common import (CONFIG, MANAGED, OWNER, STATE, atomic_json,
                                   image_reference, require, validate_host_config, validate_spec)


def docker(*args, timeout=60):
    return subprocess.check_output(["docker", *args], text=True, stderr=subprocess.DEVNULL, timeout=timeout)


def inspect(kind, name):
    result = subprocess.run(["docker", kind, "inspect", name], capture_output=True, text=True, timeout=30)
    if result.returncode:
        require(any(message in result.stderr.casefold() for message in
                    ("no such object", "no such container", "no such volume", "no such image")), "Docker inspection unavailable")
        return None
    return json.loads(result.stdout)[0]


def owned(kind, name, owner):
    item = inspect(kind, name)
    if item is not None:
        tags = (item.get("Config", {}) if kind == "container" else item).get("Labels", {}) or {}
        require(tags.get(MANAGED) == "1" and tags.get(OWNER) == owner, "Resource ownership mismatch")
    return item


def save(config, journal):
    atomic_json(Path(config["state_dir"]) / "journal.json", journal)


def cleanup(config, journal, terminal=()):
    """Offline age/exit cleanup works even if the Gateway or GitHub is down."""
    for name, record in journal.items():
        spec = validate_spec(record["spec"])
        require(name == spec["name"], "Journal identity mismatch")
        if record["phase"] == "cleaned":
            continue
        container = owned("container", name, name)
        if container and container["State"]["Running"] and spec["job_id"] not in terminal \
                and time.time() - spec["created"] < config["max_age_seconds"]:
            continue
        if container:
            record["exit"] = {key: container["State"].get(key) for key in
                              ("Status", "ExitCode", "OOMKilled", "StartedAt", "FinishedAt")}
            record["restarts"] = container["RestartCount"]
            record["phase"] = "removing"
            save(config, journal)
            docker("rm", "--force", name, timeout=90)
        for suffix in ("registration", "work"):
            volume = name + "-" + suffix
            if owned("volume", volume, name):
                docker("volume", "rm", volume)
        (Path(config["state_dir"]) / (name + ".jit")).unlink(missing_ok=True)
        record["phase"] = "cleaned"
        save(config, journal)


def capacity(config, journal):
    names = docker("ps", "-a", "--filter", "label=" + MANAGED + "=1", "--format", "{{.Names}}").split()
    require(set(names) <= set(journal), "Unjournaled pool worker; operator attention required")
    legacy = docker("ps", "-a", "--filter", "label=org.dash.ci.candidate=1", "--format", "{{.Names}}").split()
    releases = docker("ps", "-a", "--filter", "label=org.dash.ci.release=1", "--format", "{{.Names}}").split()
    root = docker("info", "--format", "{{.DockerRootDir}}").strip()
    disk_gib = shutil.disk_usage(root).free // 1024**3
    memory_gib = next(int(line.split()[1]) // 1024**2 for line in Path("/proc/meminfo").read_text().splitlines()
                      if line.startswith("MemAvailable:"))
    return {"available": not names and not legacy and not releases and disk_gib >= config["min_free_gib"]
            and memory_gib >= config["memory_gib"] + 16,
            "workers": len(names), "legacy_workers": len(legacy), "release_workers": len(releases), "free_disk_gib": disk_gib,
            "available_memory_gib": memory_gib}


def verify_image(spec):
    image = inspect("image", image_reference(spec))
    require(image is not None, "Candidate image is not prepared")
    runtime = image["Config"]
    require(image["Os"] == "linux" and image["Architecture"] == "amd64"
            and runtime.get("User") == "1001:1001"
            and runtime.get("Entrypoint") == ["/opt/ci/bin/runner-entrypoint"]
            and not runtime.get("Volumes"), "Invalid image runtime contract")
    tags = runtime.get("Labels", {})
    expected = {"org.dash.platform.pr": str(spec["pr"]), "org.dash.platform.head": spec["head"],
                "org.dash.ci.manifest-sha256": spec["manifest_sha256"],
                "org.opencontainers.image.revision": spec["recipe_revision"]}
    require(all(tags.get(key) == value for key, value in expected.items()), "Candidate provenance mismatch")
    return image["Id"]


def arguments(config, spec, image_id, jit_path):
    name = spec["name"]
    args = ["run", "--detach", "--init", "--name", name, "--restart", "no", "--pull", "never",
            "--user", "1001:1001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
            "--cpus", str(config["cpus"]), "--memory", str(config["memory_gib"]) + "g", "--pids-limit", "4096",
            "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
            "--label", MANAGED + "=1", "--label", OWNER + "=" + name,
            "--label", "org.dash.ci.job=" + str(spec["job_id"]),
            "--mount", f"type=volume,src={name}-registration,dst=/runner",
            "--mount", f"type=volume,src={name}-work,dst=/work",
            "--mount", f"type=bind,src={jit_path},dst=/run/secrets/runner-jit,readonly",
            "--env", "CARGO_BUILD_JOBS=" + str(config["cpus"]),
            "--env", "BINARYEN_CORES=" + str(config["binaryen_cores"])]
    if spec["kind"] == "kotlin":
        info = os.stat("/dev/kvm")
        require(stat.S_ISCHR(info.st_mode), "KVM is unavailable")
        args += ["--device", "/dev/kvm:/dev/kvm:rw", "--group-add", str(info.st_gid)]
    return args + [image_id, "jit"]


def launch(config, journal, spec, jit):
    validate_spec(spec)
    require(capacity(config, journal)["available"], "Host resource floor or single-worker limit")
    name = spec["name"]
    require(name not in journal and not any(r["spec"]["job_id"] == spec["job_id"] for r in journal.values()),
            "Job already admitted; no duplicate allocation")
    require(isinstance(jit, str) and 0 < len(jit) <= 256 * 1024, "Invalid single-use runner configuration")
    require(abs(time.time() - spec["created"]) < 900, "Expired work order")
    image_id = verify_image(spec)
    # Validate KVM and all arguments before creating resources.
    jit_path = Path(config["state_dir"]) / (name + ".jit")
    args = arguments(config, spec, image_id, jit_path)
    record = {"spec": spec, "image_id": image_id, "phase": "starting"}
    journal[name] = record
    save(config, journal)  # before any external side effect; volume names never change
    for suffix in ("registration", "work"):
        volume = name + "-" + suffix
        require(inspect("volume", volume) is None, "Refusing to reuse an existing volume")
        docker("volume", "create", "--label", MANAGED + "=1", "--label", OWNER + "=" + name, volume)
    with jit_path.open("x") as stream:
        stream.write(jit)
    os.chown(jit_path, 1001, 1001)
    jit_path.chmod(0o400)
    docker(*args, timeout=90)
    record["phase"] = "running"
    save(config, journal)
    return {"name": name, "phase": "running", "image_id": image_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    require(os.geteuid() == 0, "Root-owned operator only")
    config_path = Path(CONFIG)
    info = config_path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_mode & 0o077 == 0,
            "Host configuration must be private and root-owned")
    config = validate_host_config(json.loads(config_path.read_text()))
    state = Path(STATE)
    info = state.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and info.st_mode & 0o077 == 0,
            "Host journal must be private and root-owned")
    os.umask(0o077)
    with (state / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = state / "journal.json"
        journal = json.loads(path.read_text()) if path.exists() else {}
        request = {"operation": "status"} if args.cleanup else json.loads(sys.stdin.readline(1024 * 1024))
        operation = request["operation"]
        require(operation in ("status", "prepare", "launch", "cleanup"), "Unexpected operation")
        terminal = request.get("terminal", [])
        require(isinstance(terminal, list) and all(type(i) is int for i in terminal), "Invalid terminal job list")
        cleanup(config, journal, terminal)
        if operation == "prepare":
            spec = validate_spec(request["spec"])
            require(capacity(config, journal)["available"], "Host is full")
            # immutable digest only; no credential-bearing Docker login
            docker("pull", image_reference(spec), timeout=300)
            result = {"image_id": verify_image(spec)}
        elif operation == "launch":
            result = launch(config, journal, request["spec"], request["jit"])
        else:
            result = {"capacity": capacity(config, journal), "allocations": journal}
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # No command, stdin, JIT, API reply, or credential is printed on failure.
        print(json.dumps({"error": type(error).__name__, "message": str(error)
                          if isinstance(error, ValueError) else "Host operation failed"}), flush=True)
        sys.exit(1)
