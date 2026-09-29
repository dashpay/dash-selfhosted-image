# Permanent Gateway-brokered release allocation

This is an alternative credential placement to the dedicated-App host service.
Use **one** allocator, not both. The Gateway's existing protected GitHub credential
stays on the Gateway. Only single-job JIT material reaches the Docker host over
SSH stdin; never export the PAT or use it as a container/service environment file.

`release_gateway.py --config <private-operator-file>` runs once per fresh Gateway
scheduler invocation. It discovers current queued/in-progress runs of Platform's
`release.yml` and `release-kotlin-sdk.yml`, then reads the exact current run attempt.
There is no per-run/per-attempt allowlist. Only Platform-owned release events at
unchanged published tags, or manual dispatches on tags/protected branches, qualify.
An explicit historical exclusion list can preserve previously unapproved legacy
publication runs without excluding future releases.

The broker permits only bounded reads, release-group JIT registration and cleanup
of its journaled registration identities. It rechecks ref/head/attempt/job/labels
immediately before registration. A durable pre-write receipt prevents ambiguous
JIT responses from being blindly replayed. Such a job stays blocked for operator
reconciliation and notification rather than creating duplicate runners.

The host uses the existing `release_runner.py` lifecycle: root-owned config and
journal, one slot, fresh registration/work volumes and HOME, nonroot workers,
capability drop/no-new-privileges, no Docker socket/devices, pinned image IDs,
bounded CPU/RAM/PIDs/lifetime, and independent offline cleanup. Each source's
manifest is checked against the actual pinned image before admission. A new,
incompatible recipe is an explicit operator-image-adoption requirement, not a
reason to weaken validation or fall back to persistent PR workers.

## Deployment and shared capacity

Stage every entry in `deploy/release-gateway.files` into an immutable versioned
directory, preserving relative paths on both Gateway and root-owned host. The
reviewed `client-codegen/lock.json` is a runtime validation dependency, not an
optional fixture. Run the staged-bundle test, not just checkout-local imports. Configure the fixed
host command in the private Gateway config. The host reads only
`/etc/dash-ci-releases/gateway.json`, with the same production allocator resource
keys except App IDs/private-key fields are absent. The Gateway config contains
`state_dir`, fixed SSH `host` (`destination`, `port`, `key`, `script`),
`candidate_lock`, `notify_session`, and `excluded_runs`; no secret values.

Before switching the existing scheduler: pause future triggers, wait for the
previous allocator invocation to finish and for an empty release allocation
journal; retain the offline cleanup guard. Do not replace active workers or edit
source while it is running. Reuse the same group/state journal. A separately
pinned previous guard is compatible while allocation names, labels, volume and
JIT paths and lifetime limits remain identical.

The Gateway serializes admission with the existing candidate-pool lock. The
release host refuses admission while candidate workers exist. **The candidate
host's capacity check must also reject `org.dash.ci.release=1` containers** before
this controller is enabled. Both checks are required: a per-invocation lock alone
does not reserve capacity for the lifetime of an already-running worker. Existing
ordinary workers remain unchanged; retain free-memory/disk checks and budget
additional ordinary listeners separately.

## Visibility and recovery

The private Gateway state directory holds `status.json`, JIT receipts, and alert
cooldown state. Queue age is distinct from worker lifetime. Unassigned release
jobs older than15minutes, rejected image contracts and allocator/host errors
produce an owner-conversation system event. Repeated unchanged alerts are limited
to once every30minutes, with a recovery event on clearance. Notification handoff
uses `openclaw system event --session-key <owner-conversation> --mode now`, not
Slack/email or PR comments. Confirm the event handoff and the owner conversation's
visible report in live acceptance; successful local notification calculation is
not delivery proof.

`release_watchdog.py --config <private-operator-file>` is a separate, headless
health check (every two minutes). It needs no GitHub credential and cannot
allocate workers. It checks schedule enablement/error streaks and requires a
non-plan allocator receipt within ten minutes. Controlled stale/disabled fixtures
exercise alert and recovery handoff without stopping real workers.

Keep this independent scheduled health check for stale allocator `checked_at`
receipts and disabled/backed-off scheduler state; the allocator cannot diagnose
its own total absence. Its owner-session report must use current-session announce
delivery so alerts persist in WebChat history. Host cleanup remains independent
of both Gateway schedules and API availability. Do not claim queue monitoring or
complete permanent adoption until those layers have been exercised.

Run focused tests and the full controller suite. Plan mode proves discovery and
manifest compatibility but is not actual assignment. Acceptance requires an
existing real queued job on a new attempt or future run, exact runner assignment,
healthy repeated scheduler ticks, one-job completion, complete local/registration
cleanup, and a controlled notification/watchdog fault proof. No retagging, manual
publication, workflow dispatch or rerun is required for allocation acceptance.
