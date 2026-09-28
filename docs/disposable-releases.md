# Disposable NPM and Kotlin release builders

`scripts/release_runner.py` runs on a trusted Docker host, outside all CI jobs.
For each queued release build it registers a **one-job JIT runner**, starts a
fresh non-root container from an operator-pinned image, and destroys the whole
allocation afterward. It never launches Docker through the persistent CI runner.

## Cache and trust boundary

- Ordinary PR runners and their Cargo, Gradle and Yarn caches are unchanged.
- Release jobs get fresh registration and work volumes. HOME, Cargo/Gradle
  caches and all other writable image layers are new for each allocation.
- The existing prebaked image and Docker's read-only image layers are reused;
  there is no image rebuild per release. Release compilation is cold unless a
  separately reviewed trusted cache mechanism is introduced. Do not mount a PR
  cache, runner HOME, checkout or Docker socket into a release container.
- Platform must disable shared executable dependency-cache restores for these
  jobs, including Yarn build caches and Gradle caches. A fresh local filesystem
  is not sufficient if a workflow restores untrusted build outputs afterward.
- GitHub-hosted jobs retain publishing credentials/OIDC. The host controller's
  App credentials never enter a build container; only the one-job JIT config does.
- Neither NPM nor Kotlin release compilation needs KVM. No host devices, extra
  groups, capabilities or host namespaces are granted.

This closes the prior-job persistence route, assuming a trusted host and image.
It does not repair a previously compromised host, validate release source code,
or replace publishing authorization. Preserve evidence/audit the former native
and Docker-socket-enabled runners before treating their host as release-trusted.

## Operator installation

1. Use a reviewed checkout of this repository on the Docker host, conventionally
   `/opt/dash-selfhosted-image`. Keep source/configuration root-owned and not
   writable from job containers. No recipe/image rebuild is needed if the
   already-tested image supports the existing `jit` entrypoint and the consumer's
   image contract. Current native NPM builds need the client-codegen recipe, not
   the older main-branch image without those tools.
2. Create an organization runner group named **`platform-release-builds`**,
   selected for **`dashpay/platform` only**, explicitly allowing this public repo.
   Keep the group empty initially. Do not move persistent runners into it or
   add release labels to existing CI runners. Other registrations block startup.
3. Install a dedicated GitHub App on Platform with repository **Actions: read**
   and organization **Self-hosted runners: read/write**, plus the access needed
   to read the selected runner-group repository policy. It must be able to list
   that group's repositories/runners and generate/delete JIT registrations.
   No repository contents-write, release-write or publishing credentials are
   needed. Store the private key root-owned, mode 0600; never put it in a workflow.
4. Copy `deploy/release-controller.example.json` to
   `/etc/dash-ci-releases/config.json` (root-owned, mode 0600). Set the App and
   installation IDs, group ID and immutable image pins. A tested local image ID
   (`sha256:...`) is supported, as is
   `dashpay/dash-selfhosted-image@sha256:...`. Preload registry images outside the
   controller; it never pulls a job-supplied image or follows a moving tag.
   NPM and Kotlin may use the same image when their requirements agree.
5. Create `/var/lib/dash-ci-releases` root-owned, mode 0700. Review the CPU/RAM,
   minimum-disk and four-hour maximum lifetime limits against existing CI and
   other services. The controller reserves another 16 GiB of available host RAM.
6. Use **one active controller per group**. The local file lock prevents duplicate
   local processes; it is not a distributed scheduler across multiple hosts.
   Inspect `python3 scripts/release_runner.py --config <file> --once` first.
   This default mode makes no registrations or Docker changes. Install the
   supplied `deploy/dash-ci-releases.service` only after the configuration and
   host trust are reviewed. The service uses `--apply`.

The existing CI registrations/services/volumes are never touched. This repository
does not create the App/group or change fork-approval policy automatically.
Keep fork approvals and publishing controls appropriate for the public repos.

## CPU sizing

The example uses `cpus_per_runner: "auto"`. Each new worker receives
`floor((available logical CPUs - reserved_cpus) / max_runners)` CPUs, using the
controller's CPU affinity rather than assuming every host has eight CPUs.
`reserved_cpus` is the operator's capacity reservation for the OS and other
workloads, including other CI pools. Review it against the host inventory; it is
not a measurement of momentary idle CPU or a cross-controller scheduler.

For the current 32-logical-CPU host, reserve 12 (eight for NPM validation and four
for host services), with `max_runners: 1`: each release worker gets **20 CPUs**.
A 64-CPU host with the same reservation gets 52. Two release slots split the
release pool equally, including when one slot is idle, so later arrivals do not
overcommit the pool. Insufficient capacity after reservations blocks allocation.
Memory and disk admission remain independent limits; increasing CPU capacity does
not relax the configured memory limit or the 16 GiB host memory reserve.

The controller gives Docker, `CARGO_BUILD_JOBS`, and `BINARYEN_CORES` the same
budget. This avoids Binaryen starting a host-sized thread pool inside a smaller
container quota. No optimization passes, application sources, or cache isolation
are changed. The resolved CPU budget is saved in the allocation journal and
reported by plan/launch output.

Existing numeric settings, such as `cpus_per_runner: 20`, remain supported as
explicit operator overrides. Existing config files are not rewritten on upgrade:
set auto/reservations or a numeric value deliberately during installation.
Changes affect new workers only; active jobs are not restarted or retuned by the
controller. Keep the controller on the trusted host, not inside a CPU-limited
job container. Before adding another co-located worker pool, revise reservations
and the overall host resource budget.

## Consumer protocol and branches

NPM and Kotlin build jobs must request:

```yaml
runs-on:
  group: platform-release-builds
  labels: [self-hosted, Linux, X64, 'platform-release-${{ github.run_id }}-${{ github.run_attempt }}-npm']
```

Use `-kotlin` for Kotlin. No generic `npm-build`/`kotlin-ci` fallback is permitted.
The controller accepts only Platform-owned `release`/`workflow_dispatch` runs
from `release.yml` or `release-kotlin-sdk.yml`, checks the job's run, attempt and
commit, and rechecks before registration. It does not trust labels supplied by
a PR run. Workflow runtime markers detect accidental persistent-runner routing;
they are diagnostics, not cryptographic attestation.

The one-job lifecycle, not a branch allowlist, prevents a PR poisoning a later
release. Copying a queued release's unique label cannot turn its one-job runner
into a persistent runner. Labels are scheduling hints, not authorization: a
malicious job racing for that exact label could cause denial of service, but it
gets no cache/volume that a subsequent allocation reuses. Workflow allowlists
can be added as defense in depth if the organization wants them; they are not
required for this lifecycle's storage isolation.

Port the matching workflows and image requirements when moving from 4.2 to 4.3.
The host controller has no release-branch names to update. Protected-branch
manual dispatches and tag/publishing rules still apply in the consumer. If the
new branch needs different dependencies, test and update the operator image
pin; do not silently bypass a failing image-contract check. **Old release tags
retain their original workflows** and must not be rerun on persistent CI as a
shortcut around this migration.

## Cleanup, failures and rollback

The root-only atomic journal is written before JIT registration. Every allocation
has a unique name and separately labelled volumes; retrying a job never reuses
them. The JIT file remains mounted until cleanup to avoid a startup race.
Completion, cancellation, container exit or the lifetime ceiling triggers whole-
container termination, private log preservation, volume removal and exact-name
registration cleanup. Busy GitHub leases remain journaled for retry. A lost JIT
response is recovered by the recorded name. GitHub outages do not disable the
local lifetime ceiling. No broad prune or deletion of unrelated runners occurs.

Failed launches have a three-attempt budget with backoff. Missing images,
unmanaged registrations, orphaned containers, low resources and API errors
leave jobs queued/blocked; they never route to a persistent pool. Inspect the
service journal and `/var/lib/dash-ci-releases/journal.json`. JIT files and saved
container logs are private; never paste them into chat.

For maintenance, stop the service and run the same controller with `--apply
--drain` to let active jobs complete and remove their allocations without creating
new ones. Wait for an empty journal before editing the running controller or
changing image pins. To roll back, stop new release scheduling; **do not** restore
the old shared-CI release routing as an automatic fallback.

## Verification before enabling publication

```sh
python3 -m unittest discover -s tests -v
DASH_RELEASE_TEST_IMAGE=<tested-image> python3 -m unittest discover -s tests -p test_release_runtime.py -v
```

The opt-in Docker test uses the production mounts/confinement/cleanup but no
GitHub credentials. It leaves canaries in runner state, the workspace and HOME,
starts a background process, destroys the allocation, and proves the next
allocation has none of those files. It removes only its own labelled resources.

An operator must additionally prove a real JIT job accepts only one job, its
registration and volumes disappear, cancellation/restart cleanup works, and a
second job gets fresh state. Run Platform's NPM release dry-run (no publication)
and verify the Kotlin build/tag/publishing gates before the next live release.
Unit tests and the Docker smoke test are not substitutes for that end-to-end gate.
