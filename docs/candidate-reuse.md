# Exact-contract candidate reuse

Candidate builds may reuse a previously published root filesystem instead of
redownloading the same frozen Ubuntu snapshot and compiler/Android toolchains.
This is **not** a general shared BuildKit cache or a relaxed image contract.

## Admission

- Same-repository publications only; forks and other architectures use
  the original cold-build path. Fork runner admission is unchanged.
- Exact canonical requirements fingerprint, including the immutable recipe SHA
  and architecture. Similar-but-not-identical requirements are a cache miss.
- A completed successful `pull_request_target` candidate publication whose
  referenced orchestration revision is the current trusted caller pin or the
  explicitly reviewed pre-reuse controller `7d901150` (full SHA in code).
- The source PR/head's **latest** full status must have the exact context, trusted
  GitHub Actions creator, immutable digest, and link to that exact source run.
  Cross-PR reuse preserves the source PR identity rather than substituting the
  destination PR context/labels. Newer failures, case variants and spoofed statuses cannot fall back to older
  successes. API errors fail closed; they do not enable unchecked reuse.
- Inspect the immutable source config for Linux/AMD64, UID/GID 1001, exact original
  PR/head/requirements/recipe labels and absence of `ONBUILD` triggers.

## New candidate and publication

The trusted controller creates a context containing only `FROM <immutable digest>`.
No PR source enters it. Both build/export invocations supply the **new head's**
labels. Every build still runs confinement/compiler smoke tests, exact in-image
contract verification, and KVM/emulator boot. Export regenerates provenance and
SBOM, then the separate fresh publisher VM rechecks the current PR/head and
image labels before publishing a new per-head candidate/status. Ordinary PR
runners receive no cache or registry write authority. Main-channel promotion
still requires the existing real candidate-job and merge gates.

Discovery examines at most 30 successful candidate publications. A missing,
unapproved or incompatible source falls back to the original locked cold build.
This intentionally trades some reuse opportunities for bounded, auditable trust.
A registry/API outage is not treated as permission to consume a floating tag.

## Validation

`python3 -m unittest discover -s tests -v` covers positive reuse, approved pins,
manifest/recipe incompatibility, wrong provenance/PR/fork, spoofed and shadowing
statuses, immutable references, stale heads, ONBUILD and unchanged workflow gates.
The image PR's standard confinement/KVM/OCI transfer checks remain required
before adoption. After merging, update protected Platform caller pins and verify
a real reuse build plus downstream execution; local tests alone are not live proof.
