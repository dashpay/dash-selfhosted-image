#!/usr/bin/env python3
"""Permanent Platform candidate allocator, run once per fresh Gateway context.

The protected credential is used only for api.github.com. Hosts receive one-job
JIT material via SSH stdin, never the credential. No PR or branch is hardcoded.
"""
import argparse
from collections import Counter
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

from candidate_pool_common import (GROUP_ID, GROUP_NAME, PLATFORM, WORKFLOWS, atomic_json,
                                   labels, require, validate_spec)
from candidate_runner import allowed_pr, items, parse_job
from platform_request import GitHub, candidate_digest, make_request

TRUST = {"trusted_fork_owners": ["thepastaclaw"], "approved_heads": {}}
REMOTE = "/opt/dash-ci-candidate-pool/scripts/candidate_pool_host.py"


class Ineligible(ValueError):
    """Expected stale/untrusted work is recorded, not treated as a pool outage."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class ProtectedGitHub(GitHub):
    def __init__(self):
        self.token = os.environ.get("GITHUB_MAC_CI_PAT", "")
        require(self.token.startswith("oc-sent-"), "Protected Gateway credential unavailable; no plaintext fallback")
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, data=None, method="GET"):
        require(isinstance(path, str) and not any(part in path for part in ("://", "\\", "..", "#")), "Invalid API path")
        require(path.startswith("repos/" + PLATFORM + "/")
                or path.startswith("orgs/dashpay/actions/"), "API outside Platform runner scope")
        request = urllib.request.Request("https://api.github.com/" + path,
            data=json.dumps(data).encode() if data is not None else None, method=method,
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
        # Never replay an ambiguous POST. The durable ledger owns recovery.
        for attempt in range(3 if method == "GET" else 1):
            try:
                with self.opener.open(request, timeout=35) as response:
                    raw = response.read(8 * 1024 * 1024 + 1)
                require(len(raw) <= 8 * 1024 * 1024, "Oversized GitHub response")
                return json.loads(raw) if raw else None
            except urllib.error.HTTPError as error:
                if method != "GET" or attempt == 2 or error.code not in (429, 500, 502, 503, 504):
                    raise
                delay = float(error.headers.get("Retry-After") or 2**attempt)
                require(delay <= 30, "API backoff exceeds this reconciliation")
                time.sleep(max(1, delay))
            except (urllib.error.URLError, TimeoutError):
                if method != "GET" or attempt == 2:
                    raise
                time.sleep(2**attempt)

    def call(self, path, data=None, method=None):
        require(data is None and method in (None, "GET"), "Validation API is read-only")
        return self.request(path)

    def register(self, spec):
        validate_spec(spec)
        return self.request("orgs/dashpay/actions/runners/generate-jitconfig",
                            {"name": spec["name"], "runner_group_id": GROUP_ID,
                             "labels": labels(spec), "work_folder": "/work"}, "POST")

    def remove(self, runner, record):
        spec = validate_spec(record["spec"])
        require(runner["name"] == spec["name"] and not runner["busy"], "Cleanup identity or busy lease changed")
        require(not record.get("runner_id") or record["runner_id"] == runner["id"], "Cleanup runner ID changed")
        live = self.call(f"orgs/dashpay/actions/runners/{runner['id']}")
        require(live["name"] == spec["name"] and not live["busy"], "Cleanup runner lease changed")
        return self.request(f"orgs/dashpay/actions/runners/{runner['id']}", method="DELETE")


def validate_job(run, job, queued=True):
    pr, head, digest, kind = parse_job(job)
    require(run["event"] == "pull_request" and WORKFLOWS.get(run["path"]) == kind,
            "Candidate requested by the wrong event/workflow")
    require(run.get("repository", {}).get("full_name") == PLATFORM, "Unexpected workflow repository")
    require(type(job.get("id")) is int and job["id"] > 0 and job["run_id"] == run["id"]
            and job["head_sha"] == head == run["head_sha"]
            and job.get("run_attempt", run["run_attempt"]) == run["run_attempt"], "Candidate revision/attempt mismatch")
    require(len(job["labels"]) == 4 and set(job["labels"]) == {"self-hosted", "Linux", "X64",
            f"platform-image-pr-{pr}-{head}-{digest[7:]}-{kind}"}, "Unexpected runner labels")
    require(not queued or job["status"] == "queued", "Job is no longer queued")
    name = job["name"]
    require((kind == "rust" and (name == "Tests" or name.endswith("/ Tests")))
            or (kind == "kotlin" and name.startswith("Kotlin SDK build + tests"))
            or (kind == "npm" and name == "NPM release build validation"), "Unexpected candidate job name")
    return pr, head, digest, kind


def discover(api):
    jobs, rejected, seen = [], [], set()
    for status in ("queued", "in_progress"):
        for run in items(api, f"repos/{PLATFORM}/actions/runs?status={status}", "workflow_runs"):
            if run["id"] in seen or run["event"] != "pull_request" or run["path"] not in WORKFLOWS:
                continue
            seen.add(run["id"])
            path = f"repos/{PLATFORM}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs"
            for job in items(api, path, "jobs"):
                if job["status"] not in ("queued", "in_progress") or not any(
                        label.startswith("platform-image-pr-") for label in job.get("labels", [])):
                    continue
                try:
                    validate_job(run, job, queued=False)
                    jobs.append((run, job))
                except ValueError as error:
                    rejected.append({"job": job["id"], "reason": str(error)})
    return sorted(jobs, key=lambda pair: pair[1]["id"]), rejected


def prove(api, run, job):
    number, head, digest, kind = validate_job(run, job)
    pr = api.call(f"repos/{PLATFORM}/pulls/{number}")
    if pr["state"] != "open" or pr["head"]["sha"] != head:
        raise Ineligible("Superseded or closed PR head")
    if not allowed_pr(pr, TRUST):
        raise Ineligible("PR is outside the existing trusted-fork policy")
    record = make_request(pr, api.manifest(head), api.manifest(pr["base"]["sha"], missing_ok=True))
    require(api.changes_manifest(pr), "Candidate requested without changed requirements")
    require(candidate_digest(api, record) == digest, "Candidate digest has been superseded")
    return {"name": f"platform-pr-{number}-{kind}-{job['id']}-{uuid.uuid4().hex[:8]}",
            "job_id": job["id"], "run_id": run["id"], "attempt": run["run_attempt"],
            "pr": number, "head": head, "digest": digest, "kind": kind,
            "manifest_sha256": record["manifest_sha256"], "recipe_revision": record["recipe_revision"],
            "label": next(label for label in job["labels"] if label.startswith("platform-image-pr-")),
            "created": int(time.time())}


def group_runners(api):
    base = f"orgs/dashpay/actions/runner-groups/{GROUP_ID}"
    group = api.call(base)
    repos = list(items(api, base + "/repositories", "repositories"))
    require(group["name"] == GROUP_NAME and group["visibility"] == "selected" and not group["default"]
            and group["allows_public_repositories"] and {repo["full_name"] for repo in repos} == {PLATFORM},
            "Candidate group must remain Platform-only")
    return list(items(api, base + "/runners", "runners"))


def host_call(target, request):
    # Config is protected operator data. Work orders never influence SSH argv.
    command = ["ssh", "-i", target["key"], "-p", str(target["port"]),
               "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15",
               "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", target["destination"],
               "sudo", "-n", "timeout", "420", "python3", REMOTE]
    result = subprocess.run(command, input=json.dumps(request) + "\n", text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=450)
    require(len(result.stdout) < 8 * 1024 * 1024, "Oversized host response")
    if result.returncode:
        # Host emits only sanitized error fields. Never echo SSH input/stderr.
        try:
            error = json.loads(result.stdout)
        except ValueError:
            error = {"message": "SSH/host operation failed"}
        raise RuntimeError(str(error.get("message", "Host operation failed")))
    return json.loads(result.stdout)


def reconcile(api, config, state, journal, plan=False):
    result = {"checked_at": int(time.time()), "hosts": {}, "queued": [], "started": [], "blocked": [], "ignored": []}
    # API failure must not prevent local cleanup. Hosts also have independent timers.
    live = {}
    for host, target in config["hosts"].items():
        try:
            live[host] = host_call(target, {"operation": "status"})
            result["hosts"][host] = live[host]["capacity"]
        except Exception as error:
            result["blocked"].append({"host": host, "reason": str(error)})
    runners = group_runners(api)
    for key, record in journal.items():
        spec, host = record["spec"], record["host"]
        if record["phase"] == "done" or host not in live:
            continue
        local = live[host]["allocations"].get(spec["name"])
        job = {}
        try:
            job = api.call(f"repos/{PLATFORM}/actions/jobs/{spec['job_id']}")
            terminal = job["status"] == "completed" or (job.get("runner_name") and job["runner_name"] != spec["name"])
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            terminal = True
        if terminal and local and local["phase"] != "cleaned":
            live[host] = host_call(config["hosts"][host], {"operation": "cleanup", "terminal": [spec["job_id"]]})
            result["hosts"][host] = live[host]["capacity"]
            local = live[host]["allocations"].get(spec["name"])
        matching = [runner for runner in runners if runner["name"] == spec["name"]]
        require(len(matching) <= 1, "Duplicate owned runner name")
        if matching:
            require(not record.get("runner_id") or record["runner_id"] == matching[0]["id"], "Runner ID changed")
            record["runner_id"] = matching[0]["id"]
        if not local or local["phase"] == "cleaned":
            if matching and matching[0]["busy"]:
                continue
            if matching and not plan:
                api.remove(matching[0], record)
                runners.remove(matching[0])
            record["phase"] = "done"
            record["outcome"] = job.get("conclusion") if terminal else "admission-interrupted"
            if record["outcome"] == "admission-interrupted":
                result["blocked"].append({"job": spec["job_id"], "reason": "Interrupted admission; rerun or operator recovery required"})
            if local:
                record["exit"] = local.get("exit")
        elif local["phase"] == "running":
            record["phase"] = "running"
        if not plan:
            atomic_json(state / "journal.json", journal)
    queued, rejected = discover(api)
    result["blocked"].extend(rejected)
    counts = Counter(next(label for label in job["labels"] if label.startswith("platform-image-pr-"))
                     for _, job in queued)
    for run, job in queued:
        if job["status"] != "queued":
            continue
        result["queued"].append(job["id"])
        if str(job["id"]) in journal:
            if journal[str(job["id"])]["phase"] == "done":
                result["blocked"].append({"job": job["id"], "reason": "One-shot admission consumed; operator attention required"})
            continue
        available = [host for host in live if live[host]["capacity"]["available"]]
        if not available:
            continue
        host = None
        try:
            spec = prove(api, run, job)
            require(counts[spec["label"]] == 1, "Multiple active jobs request the same candidate label")
            require(not any(spec["label"] in [label["name"] for label in runner["labels"]]
                            for runner in runners), "Matching runner already exists")
            # Stable host order distributes work; each host has one independent slot.
            host = available[0]
            if plan:
                result["started"].append({"job": job["id"], "host": host, "action": "would-start"})
                live[host]["capacity"]["available"] = False
                continue
            host_call(config["hosts"][host], {"operation": "prepare", "spec": spec})
            current_run = api.call(f"repos/{PLATFORM}/actions/runs/{run['id']}")
            current_job = api.call(f"repos/{PLATFORM}/actions/jobs/{job['id']}")
            fresh = prove(api, current_run, current_job)
            require(all(spec[field] == fresh[field] for field in spec if field not in ("name", "created")),
                    "Work order changed during image preparation")
            competing, _ = discover(api)
            require([j["id"] for _, j in competing if spec["label"] in j["labels"]] == [job["id"]],
                    "Candidate label became ambiguous during image preparation")
            runners = group_runners(api)
            require(not any(spec["label"] in [label["name"] for label in runner["labels"]]
                            for runner in runners), "A matching runner appeared during image preparation")
            record = {"spec": spec, "host": host, "phase": "registering"}
            journal[str(job["id"])] = record
            atomic_json(state / "journal.json", journal)  # durable before POST
            jit = api.register(spec)
            require(jit["runner"]["name"] == spec["name"] and type(jit["runner"]["id"]) is int,
                    "Unexpected single-use registration identity")
            record.update(runner_id=jit["runner"]["id"], phase="registered")
            atomic_json(state / "journal.json", journal)
            launched = host_call(config["hosts"][host], {"operation": "launch", "spec": spec, "jit": jit["encoded_jit_config"]})
            require(launched["name"] == spec["name"] and launched["phase"] == "running", "Worker launch not confirmed")
            record["phase"] = "running"
            atomic_json(state / "journal.json", journal)
            live[host]["capacity"]["available"] = False
            result["started"].append({"job": job["id"], "host": host, "runner": spec["name"]})
        except Ineligible as error:
            result["ignored"].append({"job": job["id"], "reason": str(error)})
        except Exception as error:
            result["blocked"].append({"job": job["id"], "reason": str(error)
                                      if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__})
            # Do not use an uncertain host again in this pass.
            if host in live:
                live[host]["capacity"]["available"] = False
    result["active"] = [{"job": int(key), "host": rec["host"], "runner": rec["spec"]["name"]}
                        for key, rec in journal.items() if rec["phase"] != "done"]
    atomic_json(state / "status.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args()
    config_path = Path(args.config)
    require(config_path.is_file() and not config_path.is_symlink() and config_path.stat().st_mode & 0o077 == 0,
            "Gateway config must be private")
    config = json.loads(config_path.read_text())
    require(set(config) == {"state_dir", "hosts"} and set(config["hosts"]) == {"runner1", "server2"}, "Unexpected pool configuration")
    state = Path(config["state_dir"])
    require(state.is_absolute() and state.is_dir() and not state.is_symlink()
            and state.stat().st_mode & 0o077 == 0, "Gateway state must be private")
    os.umask(0o077)
    with (state / "controller.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"locked": True}))
            return 0
        path = state / "journal.json"
        journal = json.loads(path.read_text()) if path.exists() else {}
        result = reconcile(ProtectedGitHub(), config, state, journal, args.plan)
        print(json.dumps(result), flush=True)
        return int(bool(result["blocked"]))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)
                          if isinstance(error, (ValueError, RuntimeError)) else "Controller unavailable"}), flush=True)
        sys.exit(1)
