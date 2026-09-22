# Platform PR image lifecycle

Platform owns its desired Linux runner environment in
.github/runner-requirements.json. The wrapper pins this repository's recipe
commit and embeds the complete dependency lock: package snapshot, versions,
download URLs and checksums. This repository's image.lock.json is the standalone
build fixture; Platform builds override it with their reviewed requirements.
It is not a second independently maintained Platform requirement.

~~~mermaid
flowchart LR
  PR[PR changes locked requirements] --> Build[Disposable build and smoke / KVM tests]
  Build --> Publish[Separate publisher: immutable PR digest]
  Publish --> Queue[PR jobs request digest-specific labels]
  Queue --> Runner[Host controller: one-job candidate runners]
  Runner --> Tests[Real Rust and Kotlin jobs]
  Tests --> Merge[PR merged]
  Merge --> Promote[Promote the same tested digest]
~~~

## What is automatic

1. The trusted Platform runner-image-candidate.yml workflow reacts to PRs changing
   the manifest. It calls the commit-pinned reusable platform-candidate.yml
   workflow here. A formatting-only manifest edit also rebuilds; a stale PR that
   did not edit the manifest does not.
2. Requirements are parsed as data. The build runs on a disposable hosted VM
   with a read-only GitHub token and **no Docker Hub credentials**. It materializes
   the requested lock/Dockerfile, then proves compiler versions, exact manifest
   compatibility, non-root confinement, KVM and a real emulator boot.
3. A **different hosted VM** publishes the tested OCI archive with provenance and
   SBOM. It never executes the PR recipe, image or application code. Its tag binds
   the PR number, head SHA and manifest hash; jobs use the immutable digest.
4. Normal Rust/Kotlin workflows select labels containing the PR, complete head
   SHA, **complete image digest** and job kind. The host controller creates a JIT
   one-job runner for each eligible queued job. A rebuild of the same head cannot
   borrow tests from a different digest.
5. On merge, promotion checks the head, merged/current branch requirements,
   publisher provenance and successful real Rust/Kotlin jobs on that digest.
   Skipped jobs, ordinary runners, old digests and a newer failed run do not count.
   A superseding requirements change prevents rollback.
6. Promotion copies the same OCI digest to a platform-<base-branch> channel
   (slashes become hyphens). It updates **main only for Platform's GitHub default
   branch**, so development/maintenance branches cannot overwrite one another's
   primary image. Production containers still select a reviewed immutable digest;
   promotion does not restart them.

Ordinary project dependencies and user-local, repository-pinned Rust toolchains
remain ordinary job work. Update the manifest when changing prebaked/native
requirements. Include exact upstream checksums and matching Android metadata;
changing a version string without its artifact should fail verification.
Changes to recipe semantics/schema need reviewed recipe/control revision updates.
Native macOS provisioning and hosted-only Kotlin release tool setup stay separate.

## One-time activation and bootstrap order

These files are implementation, **not an installed controller or an authority
grant**. Activation requires organization runner administration.

1. Review and land the image-repository implementation.
2. Land the small Platform bootstrap PR containing **only** the trusted
   pull_request_target caller, pinned to that reviewed control revision. It must
   exist on the target branch before serving a PR. Do not bootstrap by executing
   PR workflow code with registry/admin credentials.
3. Create a dedicated organization runner group with selected repository
   **only dashpay/platform**, explicitly allowing that public repository. Do not
   change the existing production runner group's repository selection.
4. Install a dedicated GitHub App with organization **self-hosted runners: write**
   and Platform repository **Actions: read / Contents: read / Pull requests: read /
   Commit statuses: read**. Keep its
   installation limited to Platform. The private key belongs only to the host
   operator service. Existing Docker Hub credentials go only to the trusted
   publisher through the caller's named secrets.
5. Install a reviewed checkout at /opt/dash-selfhosted-image. Fill
   /etc/dash-ci-candidates/config.json from
   [the example](../deploy/candidate-controller.example.json). Zero IDs
   deliberately fail closed. Keep the App key root-owned, owner-only, at the
   configured path. Create /var/lib/dash-ci-candidates as root, mode 0700.
6. Inspect one pass using candidate_runner.py with --config and --once (default
   dry-run; supply read-only API authentication through the operator's protected
   environment). Install/enable [the service](../deploy/dash-ci-candidates.service)
   only after approving the concrete configuration. Docker access belongs to the
   **host operator**, never to a CI job.
7. Land the consuming manifest/workflow changes only after their candidate runs
   real Rust and Kotlin jobs. Existing persistent-runner fork guards remain.
   A controller approved_heads entry is bound to one PR/head; it does **not**
   override the workflow-side guard. Arrange the corresponding reviewed test
   authorization rather than broadening trust to every fork or counting skips.

The initial workflow cannot bootstrap itself from an unmerged PR. Selectors time
out explicitly when the publisher is absent. If the controller is not active,
candidate jobs remain queued: installing only the workflow is not a rollout.

## Host-side boundary and lifecycle

The example allows one candidate, 8 CPUs, 32 GiB RAM, a three-hour lifetime and a
100 GiB pre-launch disk floor. The launcher also reserves 16 GiB available host
memory. It never prunes shared Docker storage to make room.

Each candidate uses uid/gid 1001, all capabilities dropped, no-new-privileges,
default seccomp/AppArmor and fresh anonymous registration/work volumes. Only
Kotlin receives /dev/kvm:rw and its numeric group. It receives a one-job JIT
configuration, **not** the App key/token, Docker socket, host workspace, host
network/PID namespace or registry credentials.

Only queued pull-request jobs in the normal Platform Rust/Kotlin workflow paths
are eligible, and their current head, label, digest and manifest must agree.
Existing trusted-fork policy is preserved. Containers are not a new isolation
guarantee for arbitrary mutually untrusted code; workflow approval still matters.

Finished/expired candidate containers and anonymous volumes are removed.
Registration tombstones retain unresolved cleanup until GitHub clears a busy
lease. Cleanup does not delete other runners/containers. A blocked/stale job is
logged without starving independent approved work. Launch failures retry at most
three times, five minutes apart. After repairing the cause, rerun the GitHub job
(new job ID) or explicitly clear its entry in launch-retries.json.

Monitor the service journal and queued candidate jobs. App/API failures,
fork-policy rejection and resource floors fail closed. Published tags remain as
evidence/rollback references; OCI transfer artifacts expire after three days.
No source commit, merge or tag restarts the production runner or OpenClaw.
