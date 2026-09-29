#!/usr/bin/env python3
"""Reuse only previously gated, immutable, exact-contract Platform candidates.

This is not a shared BuildKit cache: ordinary PR jobs cannot write a reusable
source. The new head still gets a new image configuration, the complete smoke /
contract / KVM gates, OCI attestations and a separate current-head publication.
"""
import argparse
import json
from pathlib import Path
import sys

from image_contract import fingerprint, matches, read_json, require, validate_manifest
from platform_request import (
    GitHub, IMAGE, PLATFORM, candidate_digest, check_current, inspect_published,
    validate_request,
)

# Reviewed pre-reuse controller whose successful publication included every gate.
LEGACY_CONTROLLER = "7d901150bd3d0789d50c46f365b058f2f5f1f52d"
WORKFLOW = ".github/workflows/runner-image-candidate.yml"
RUNS = (f"repos/{PLATFORM}/actions/workflows/runner-image-candidate.yml/runs"
        "?event=pull_request_target&status=success&per_page=30")


def validate_source_run(run, record, control_revision):
    require(matches(r"[0-9a-f]{40}", control_revision), "Controller must be immutable")
    require(run.get("path") == WORKFLOW and run.get("event") == "pull_request_target"
            and run.get("status") == "completed" and run.get("conclusion") == "success",
            "Reuse source is not a completed trusted publication")
    require(run.get("repository", {}).get("full_name") == PLATFORM
            and run.get("head_repository", {}).get("full_name") == PLATFORM,
            "Reuse is restricted to same-repository publications")
    require(type(run.get("id")) is int and run["id"] > 0
            and run.get("head_sha") == record["head_sha"], "Reuse source head/run mismatch")
    require(any(pr.get("number") == record["pr"] for pr in run.get("pull_requests", [])),
            "Reuse source belongs to another PR")
    refs = run.get("referenced_workflows", [])
    require(len(refs) == 1, "Reuse source must identify its reviewed controller")
    revision = refs[0].get("sha")
    require(revision in (LEGACY_CONTROLLER, control_revision)
            and refs[0].get("path") == f"{IMAGE}/.github/workflows/platform-candidate.yml@{revision}",
            "Reuse source controller is not approved")


def find_source(api, record, manifest, control_revision):
    validate_request(record)
    validate_manifest(manifest)
    require(fingerprint(manifest) == record["manifest_sha256"]
            and manifest["recipe_revision"] == record["recipe_revision"],
            "Request and manifest differ")
    require(matches(r"[0-9a-f]{40}", control_revision), "Controller must be immutable")
    if record["head_repository"] != PLATFORM or manifest["requirements"]["platform"] != "linux/amd64":
        return None
    check_current(api, record)
    # Bounded discovery is an optimization, not a reason to relax any gate.
    for listed in api.call(RUNS)["workflow_runs"]:
        head = listed.get("head_sha")
        if not matches(r"[0-9a-f]{40}", head) or head == record["head_sha"]:
            continue
        if not any(pr.get("number") == record["pr"] for pr in listed.get("pull_requests", [])):
            continue
        old = dict(record, head_sha=head,
                   candidate_tag=f"pr-{record['pr']}-{head}-{record['manifest_sha256'][:12]}")
        try:
            run = api.call(f"repos/{PLATFORM}/actions/runs/{listed['id']}")
            validate_source_run(run, old, control_revision)
            source_manifest = api.manifest(head)
            require(fingerprint(source_manifest) == record["manifest_sha256"],
                    "Reuse source has different requirements/recipe")
            # Latest full status only: failures, spoofed creators and case variants
            # shadow an older success. The status must name this exact run.
            digest = candidate_digest(api, old, expected_run_id=run["id"])
            reference = IMAGE + "@" + digest
            config = inspect_published(reference, old)
            require(not config["config"].get("OnBuild"), "Reuse source contains ONBUILD triggers")
        except ValueError as error:
            print(f"Not reusing run {listed['id']}: {error}", file=sys.stderr)
            continue
        return {"reference": reference, "source_run": run["id"], "source_head": head,
                "manifest_sha256": record["manifest_sha256"],
                "recipe_revision": record["recipe_revision"], "platform": "linux/amd64"}
    return None


def prepare_context(source, destination):
    reference = source["reference"]
    require(matches(r"dashpay/dash-selfhosted-image@sha256:[0-9a-f]{64}", reference),
            "Reuse must use an immutable image reference")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    # No PR files, build arguments, RUN commands or copied data enter this context.
    # The workflow supplies this head's labels to BOTH tested/exported builds.
    (destination / "Dockerfile").write_text(f"FROM {reference}\n")
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--control-revision", required=True)
    parser.add_argument("--recipe", required=True)
    parser.add_argument("--reuse-context", required=True)
    parser.add_argument("--github-output", required=True)
    args = parser.parse_args()
    source = find_source(GitHub(), read_json(args.request), read_json(args.manifest), args.control_revision)
    context = prepare_context(source, args.reuse_context) if source else Path(args.recipe)
    with open(args.github_output, "a") as handle:
        require(not any(c in str(context) for c in "\r\n"), "Invalid context path")
        handle.write(f"context={context}\nreused={str(source is not None).lower()}\n")
    print(json.dumps(source or {"reused": False, "reason": "No eligible exact-contract publication"}))


if __name__ == "__main__":
    main()
