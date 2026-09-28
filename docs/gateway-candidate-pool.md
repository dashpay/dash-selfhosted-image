# Permanent Gateway-brokered Platform candidate pool

`candidate_pool.py` is an alternative to the direct GitHub App host service.
It is suitable when the existing organization credential is held in OpenClaw's
protected store: a fresh scheduled Gateway execution authenticates each pass;
the credential never moves to a Docker host, job, configuration file or journal.
Do not enable the direct App allocator alongside this allocator for the same pool.

## Scheduling and ownership

Run the operator once per minute with a headless OpenClaw **script** automation,
explicitly owned by `main`, with only `exec` and `process` tools. A plain command
automation does not supply the protected per-execution credential context.
The script executes `candidate_pool.py --config <private-gateway-config>` on
the Gateway and polls its process to completion. Keep the execution active until
the command exits, so its protected credential lease remains valid. No model
turn or manual PR-specific intervention is needed during normal operation.

The Gateway config has `state_dir` and a `hosts` mapping for `runner1` and
`server2`; each host entry contains `key` (SSH key path), `port` and `destination`.
Keep it and its journal owner-only. Use strict SSH host verification.

The controller polls current attempts of the normal Rust, Kotlin and NPM
validation workflows, across **all PR numbers and target branches**. It validates
the current head, changed requirements, trusted publisher, full candidate image
digest, exact runner labels, workflow kind and job name before admission. It
rechecks them after preparing the image. Same-repository and the existing
`thepastaclaw` fork policy are retained; no other fork is implicitly authorized.
Closed/superseded PR work is recorded in `ignored`, not allocated.

The dedicated organization runner group remains ID 7,
`platform-image-candidates`, selected for `dashpay/platform` only. No workflow
dispatch, rerun, cancellation, source modification, publication or release work
is performed by this allocator. Candidate labels are PR/head/digest/kind-bound;
GitHub does not offer absolute job-ID binding. Multiple active matching jobs are
rejected before admission, and a foreign assignment is cleaned up.

## Hosts

Install the reviewed scripts at `/opt/dash-ci-candidate-pool/scripts`. The helper
reads the root-owned mode-0600 `/etc/dash-ci-candidate-pool/config.json` and keeps
its root-only journal at `/var/lib/dash-ci-candidate-pool`.

Host config fields: `host`, `state_dir`, `cpus`, `memory_gib`, `binaryen_cores`,
`min_free_gib`, `max_age_seconds`, `max_runners`. The installed policy permits
one worker per host, at most 20 CPUs/32 GiB, a 100 GiB disk floor, a 16 GiB memory
reserve and a three-hour lifetime. Kotlin alone gets `/dev/kvm`; all workers
are uid/gid 1001, cap-drop ALL, no-new-privileges, and have no Docker socket,
host workspace, registry credential or reusable registration. Binaryen's
thread budget is explicit.

Enable the bundled `dash-ci-candidate-pool-cleanup.timer` **before admission**.
Every 30 seconds it removes only journaled, owned stopped/expired workers and
their named, allocation-specific volumes and single-use JIT files. It works
without GitHub or the Gateway. Ordinary and release pools are not touched;
legacy candidate containers occupy a slot until their owning operator drains
them. Images/shared volumes are never pruned to meet resource floors.

## Recovery and verification

- Gateway admission is durably journaled before requesting JIT. A lost POST
  response is reconciled by exact name/group; it is never blindly retried.
- Host ownership and volume names are journaled before creating resources.
  Partial starts can therefore be cleaned up after a process crash.
- A consumed admission that never runs is surfaced in `blocked`. Investigate,
  then rerun the affected GitHub job to obtain a fresh job ID; do not erase
  ownership journals or reuse a registration/workspace.
- If a runner lease is still busy after local cleanup, keep its tombstone and
  retry deletion later. Do not delete unrelated registrations.
- `status.json` is the latest Gateway receipt: host floors, queue, admissions,
  ignored stale jobs and concrete blockers. OpenClaw automation history records
  authentication, execution and timeout failures. Inspect both it and the host
  cleanup timer/service when a job remains queued.
- Stop the schedule and wait for its lock/process to finish before changing its
  source. Stop host cleanup triggers and wait for the host lock before changing
  host helper files. Do not edit a running instance.

Rollback: disable the Gateway schedule, drain existing workers, and leave the
offline cleanup timers active until containers, volumes, JIT files and owned
GitHub registrations are absent. Do not route these jobs to ordinary runners
by adding fake digest labels.

An enabled allocator is not proof of an application test pass. Count live
recovery only after the actual current-head job is assigned and executes. A
GitHub-hosted candidate-image build still has to complete before any candidate
worker is eligible. Older target branches can also require consuming-workflow
repairs independently of this host service.
