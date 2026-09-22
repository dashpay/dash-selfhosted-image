#!/usr/bin/env python3
"""Trusted orchestration for data-only Platform image requests and promotion."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from image_contract import canonical, fingerprint, matches, read_json, require, validate_manifest

PLATFORM = "dashpay/platform"
IMAGE = "dashpay/dash-selfhosted-image"
MANIFEST_PATH = ".github/runner-requirements.json"
CONTEXT = "Runner image candidate"


class GitHub:
    def __init__(self, token=None):
        self.token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    def call(self, path, data=None, method=None):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "dash-runner-image",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(
            "https://api.github.com/" + path, headers=headers,
            data=json.dumps(data).encode() if data is not None else None, method=method)
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
        return json.loads(raw) if raw else None

    def manifest(self, ref, missing_ok=False):
        require(matches(r"[0-9a-f]{40}|[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", ref), "Invalid Git ref")
        query = urllib.parse.urlencode({"ref": ref})
        try:
            file = self.call(f"repos/{PLATFORM}/contents/{MANIFEST_PATH}?{query}")
        except urllib.error.HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            raise
        require(file.get("type") == "file" and file.get("encoding") == "base64"
                and file.get("size", 2**30) <= 256 * 1024, "Invalid requirements file")
        from image_contract import unique_object
        return validate_manifest(json.loads(base64.b64decode(file["content"]),
                                            object_pairs_hook=unique_object))

    def changes_manifest(self, pr):
        require(pr.get("changed_files", 0) <= 3000,
                "GitHub's 3000-file PR limit requires an explicit requirements review")
        for page in range(1, 31):
            files = self.call(f"repos/{PLATFORM}/pulls/{pr['number']}/files?per_page=100&page={page}")
            if any(item["filename"] == MANIFEST_PATH or item.get("previous_filename") == MANIFEST_PATH
                   for item in files):
                return True
            if len(files) < 100:
                return False
        return False


def candidate_label(pr, head, digest, kind):
    require(type(pr) is int and pr > 0 and matches(r"[0-9a-f]{40}", head), "Invalid candidate identity")
    require(kind in ("rust", "kotlin"), "Unsupported candidate job")
    require(matches(r"sha256:[0-9a-f]{64}", digest), "Invalid candidate digest")
    return f"platform-image-pr-{pr}-{head}-{digest[7:]}-{kind}"


def make_request(pr, manifest, base):
    validate_manifest(manifest)
    number, head = pr["number"], pr["head"]["sha"]
    require(type(number) is int and number > 0 and matches(r"[0-9a-f]{40}", head), "Invalid PR identity")
    branch = pr["base"]["ref"]
    require(matches(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", branch), "Unsupported base branch")
    digest = fingerprint(manifest)
    return {
        "schema": 1, "repository": PLATFORM, "pr": number, "head_sha": head,
        "head_repository": pr["head"]["repo"]["full_name"], "base_branch": branch,
        "manifest_sha256": digest, "recipe_revision": manifest["recipe_revision"],
        "candidate_tag": f"pr-{number}-{head}-{digest[:12]}",
        "changed": manifest != base,
    }


def validate_request(record):
    require(isinstance(record, dict) and record.get("schema") == 1
            and record.get("repository") == PLATFORM, "Wrong request repository/schema")
    require(type(record.get("pr")) is int and record["pr"] > 0, "Invalid PR number")
    for field, size in (("head_sha", 40), ("recipe_revision", 40), ("manifest_sha256", 64)):
        require(matches(r"[0-9a-f]{" + str(size) + "}", record.get(field)), f"Invalid {field}")
    expected = f"pr-{record['pr']}-{record['head_sha']}-{record['manifest_sha256'][:12]}"
    require(record.get("candidate_tag") == expected, "Candidate tag is not bound to the PR/head/requirements")
    return record


def check_current(api, record, merged=False):
    validate_request(record)
    pr = api.call(f"repos/{PLATFORM}/pulls/{record['pr']}")
    require(pr["head"]["sha"] == record["head_sha"], "PR changed since this candidate was requested")
    require(pr["head"]["repo"]["full_name"] == record["head_repository"], "PR source repository changed")
    require(pr["base"]["ref"] == record["base_branch"], "PR target branch changed")
    require(pr.get("merged", False) if merged else pr["state"] == "open",
            "Expected a merged PR" if merged else "PR is no longer open")
    manifest = api.manifest(pr["head"]["sha"])
    require(fingerprint(manifest) == record["manifest_sha256"], "PR requirements no longer match")
    return pr


def inspect_published(reference, record):
    require(matches(re.escape(IMAGE) + r"@sha256:[0-9a-f]{64}", reference), "Use the immutable candidate digest")
    config = json.loads(subprocess.check_output(
        ["skopeo", "inspect", "--config", "docker://" + reference], text=True))
    require(config.get("architecture") == "amd64" and config.get("os") == "linux", "Wrong image platform")
    require(config["config"].get("User") == "1001:1001", "Image must default to uid/gid 1001")
    labels = config["config"].get("Labels", {})
    for key, value in {
        "org.dash.platform.pr": str(record["pr"]),
        "org.dash.platform.head": record["head_sha"],
        "org.dash.ci.manifest-sha256": record["manifest_sha256"],
        "org.opencontainers.image.revision": record["recipe_revision"],
    }.items():
        require(labels.get(key) == value, "Published image mismatch: " + key)
    return config


def candidate_digest(api, record):
    validate_request(record)
    statuses = api.call(f"repos/{PLATFORM}/commits/{record['head_sha']}/status")["statuses"]
    candidates = [item for item in statuses if item["context"] == CONTEXT]
    require(len(candidates) == 1 and candidates[0]["state"] == "success",
            "No successfully published candidate for this PR head")
    candidate = candidates[0]
    require(candidate.get("creator", {}).get("login") == "github-actions[bot]",
            "Candidate status was not created by GitHub Actions")
    match = re.fullmatch(r"https://github[.]com/dashpay/platform/actions/runs/([0-9]+)",
                         candidate.get("target_url", ""))
    require(match is not None, "Candidate status must identify its trusted publishing run")
    run = api.call(f"repos/{PLATFORM}/actions/runs/{match.group(1)}")
    require(run["path"] == ".github/workflows/runner-image-candidate.yml"
            and run["event"] == "pull_request_target" and run["conclusion"] == "success",
            "Candidate publisher did not complete successfully in the trusted workflow")
    digest = candidate.get("description", "")
    require(matches(r"sha256:[0-9a-f]{64}", digest), "Candidate status has no immutable digest")
    return digest


def tested_candidate_jobs(api, record, digest):
    required = {kind: candidate_label(record["pr"], record["head_sha"], digest, kind) for kind in ("rust", "kotlin")}
    found = {}
    query = urllib.parse.urlencode({"event": "pull_request", "head_sha": record["head_sha"], "per_page": 100})
    runs = api.call(f"repos/{PLATFORM}/actions/runs?{query}")["workflow_runs"]
    seen_paths = set()
    for run in sorted(runs, key=lambda item: item["id"], reverse=True):
        if run["path"] not in (".github/workflows/tests.yml", ".github/workflows/kotlin-sdk-build.yml"):
            continue
        if run["path"] in seen_paths:
            continue
        seen_paths.add(run["path"])
        if run["conclusion"] != "success":
            continue
        jobs = api.call(f"repos/{PLATFORM}/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
        for job in jobs:
            if job["conclusion"] != "success":
                continue
            for kind, label in required.items():
                expected_job = (job["name"] == "Tests" or job["name"].endswith("/ Tests")) if kind == "rust" else job["name"].startswith("Kotlin SDK build + tests")
                if expected_job and label in job["labels"] and job.get("runner_name", "").startswith("platform-pr-"):
                    found[kind] = job["html_url"]
    require(set(found) == set(required), "Both real Rust and Kotlin jobs must pass on this PR's candidate runners")
    return found


def verify_promotion(api, record, digest):
    pr = check_current(api, record, merged=True)
    require(matches(r"[0-9a-f]{40}", pr.get("merge_commit_sha")), "Missing merge commit")
    merged = api.manifest(pr["merge_commit_sha"])
    require(fingerprint(merged) == record["manifest_sha256"], "Merged requirements differ from the tested candidate")
    current = api.manifest(pr["base"]["ref"])
    require(fingerprint(current) == record["manifest_sha256"],
            "Newer branch requirements supersede this candidate; refusing to roll the image back")
    return tested_candidate_jobs(api, record, digest)


def status(api, record, state, description):
    run_id = os.environ["GITHUB_RUN_ID"]
    run_repository = os.environ["GITHUB_REPOSITORY"]
    require(run_repository in (PLATFORM, IMAGE) and run_id.isdigit(), "Unexpected workflow context")
    api.call(f"repos/{PLATFORM}/statuses/{record['head_sha']}", {
        "state": state, "context": CONTEXT, "description": description[:140],
        "target_url": f"https://github.com/{run_repository}/actions/runs/{run_id}",
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--pr", type=int, required=True)
    plan.add_argument("--output", required=True)
    plan.add_argument("--github-output")
    for name in ("check-current", "resolve-candidate", "published", "promote"):
        command = commands.add_parser(name)
        command.add_argument("--request", required=True)
        if name in ("published", "promote"):
            command.add_argument("--digest", required=True)
    args = parser.parse_args()
    api = GitHub()
    if args.command == "plan":
        require(args.pr > 0, "Invalid PR number")
        pr = api.call(f"repos/{PLATFORM}/pulls/{args.pr}")
        manifest = api.manifest(pr["head"]["sha"])
        base = api.manifest(pr["base"]["sha"], missing_ok=True)
        record = make_request(pr, manifest, base)
        record["changed"] = api.changes_manifest(pr)
        destination = Path(args.output)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "request.json").write_text(json.dumps(record, indent=2) + "\n")
        (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        if args.github_output:
            with open(args.github_output, "a") as handle:
                for key in ("head_sha", "recipe_revision", "manifest_sha256", "candidate_tag"):
                    handle.write(f"{key}={record[key]}\n")
                handle.write(f"changed={str(record['changed']).lower()}\n")
        print(json.dumps(record))
        return
    record = validate_request(read_json(args.request))
    if args.command == "check-current":
        check_current(api, record)
        print("Current PR/head/requirements verified")
        return
    if args.command == "resolve-candidate":
        print(candidate_digest(api, record))
        return
    require(matches(r"sha256:[0-9a-f]{64}", args.digest), "Invalid published digest")
    reference = IMAGE + "@" + args.digest
    inspect_published(reference, record)
    if args.command == "published":
        check_current(api, record)
        status(api, record, "success", args.digest)
        print(reference)
        return
    proof = verify_promotion(api, record, args.digest)
    branch = record["base_branch"]
    channel = "platform-" + re.sub(r"[^A-Za-z0-9_.-]", "-", branch)
    require(len(channel) <= 128, "Image channel tag is too long")
    tags = [channel]
    default_branch = api.call(f"repos/{PLATFORM}")["default_branch"]
    if branch == default_branch:
        tags.append("main")
    authfile = str(Path(os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker"))) / "config.json")
    for tag in tags:
        subprocess.run(["skopeo", "copy", "--all", "--preserve-digests", "--dest-authfile", authfile,
                        "docker://" + reference, "docker://" + IMAGE + ":" + tag], check=True)
    print(json.dumps({"reference": reference, "promoted_tags": tags, "job_proof": proof}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, urllib.error.HTTPError) as error:
        print(f"Platform image request failed: {error}", file=sys.stderr)
        sys.exit(1)
