# Dash self-hosted runner image

For Platform-driven candidate builds, one-job PR runners and promotion after
merge, see [the PR image lifecycle](docs/platform-pr-images.md). This includes the
trusted-workflow and host-controller bootstrap; publishing code alone does not
deploy an autoscaler.

Rebuildable Linux/amd64 environment for Platform's persistent `rust-ci` and
`kotlin-ci` jobs. Source and Docker Hub publishing live here; the consuming
workflow contract lives in [Platform](https://github.com/dashpay/platform/blob/v4.3-dev/.github/SELF_HOSTED_RUNNER.md).

## Locked inputs

`image.lock.json` records the Ubuntu 24.04 **digest**, signed Ubuntu archive
snapshot, Rust 1.98.1 manifest hash, and exact download URLs + SHA-256 hashes.
No local parent image, `latest` SDK package resolution, curl-to-shell installer,
or unversioned `cargo install` is required. Android package metadata is checked
in alongside its matching archive. The installer verifies every archive before
extracting it. `/opt/ci/image.lock.json` and `/usr/share/ci-apt-packages.tsv`
remain in the image for auditing.

| Component | Version |
| --- | --- |
| GitHub Actions runner | 2.337.0 (runtime auto-update disabled) |
| Rust | 1.98.1 + rustfmt, clippy, llvm-tools; WASM + Android x86_64 targets |
| Cargo helpers | llvm-cov 0.9.1, nextest 0.9.144, machete 0.9.2, ndk 4.1.2 |
| Native tools | snapshot-pinned clang/LLVM, GCC, CMake, GMP, OpenSSL, Snappy |
| Protobuf / Java | protoc 32.0 / JDK 17 |
| Android | API 35, build-tools 35.0.0, NDK 28.1.13356709 |
| Android execution | command-line tools 19.0, platform-tools 37.0.1, emulator 37.1.11, API 35 default x86_64 image revision 2 |

“Rebuildable” means the same reviewed input versions and checksums, not a claim
that independently generated OCI manifests are byte-identical. A fixed
`SOURCE_DATE_EPOCH` reduces timestamp drift; BuildKit version and provenance
metadata can still affect digests. Always deploy the **tested published digest**.
Update the lock file and Dockerfile together, rebuild, and rerun both tests when
updating dependencies. Refresh the runner frequently enough to satisfy GitHub's
[disabled-auto-update policy](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application).

## Build and prove

Use Docker with Buildx, an amd64 Linux host, network access to the locked upstream
artifacts, and approximately 25 GiB of free build space:

```sh
python3 scripts/check-lock.py
docker buildx build --platform linux/amd64 --load -t dash-selfhosted-image:local .
scripts/smoke-test.sh dash-selfhosted-image:local
scripts/smoke-test.sh dash-selfhosted-image:local --kvm
```

The first smoke test compiles/runs Rust and C, compiles a protobuf schema, checks
tool versions, checks zero effective capabilities/default seccomp/no-new-privileges,
and verifies an unregistered runner refuses to start. It has no network or device
access. The second adds **only `/dev/kvm`**, verifies `KVM_CREATE_VM`, and actually
boots the locked API 35 emulator. Both execute as uid/gid 1001 without sudo.
KVM must already be configured by the host administrator as `root:kvm`, mode
`0660`; obtain the numeric host group with `stat -c '%g' /dev/kvm`. No job changes
host udev rules or device permissions.

## Runtime boundary

The final image contains **no sudo and no Docker CLI**. The Compose examples drop
all capabilities, enable `no-new-privileges`, keep Docker's default seccomp and
AppArmor policies, and mount only dedicated registration/work volumes. No host
Docker socket, privileged mode, host network/PID namespace, or security-policy
bypass is needed. The optional KVM overlay is the sole host-device exception.
Docker access belongs to the **operator/builder**, never to a job in this image.

System tools and Android SDK under `/opt` are root-owned and not writable by the
runner. Rustup and job caches are user-local; workflows may install their exact
repository-selected Rust toolchain there, without root. Persistent workspaces
are not a security boundary between mutually untrusted repositories: retain the
Platform fork guards and restrict runner-group repository access.

`ci-android-emulator command [args...]` creates a clean writable AVD under
`$HOME/.android`, starts the **prebaked** emulator with KVM, waits for boot, runs
the command, and stops the emulator on success or failure. It never runs
`sdkmanager` or modifies the image's SDK. Do not use an action that upgrades
Android tools at job runtime on these runners.

## Register once, then run

1. Select a digest from a successful publishing workflow. Set `RUNNER_IMAGE` to
   `dashpay/dash-selfhosted-image@sha256:...` in the operator's environment.
2. Obtain a short-lived **registration token** for the existing organization
   runner group; put it in a temporary mode-0600 file readable by container uid
   1001. Do not use a PAT, put a token in Compose, or include it in the image.
3. Register using the same named volumes that Compose will use:

```sh
export RUNNER_IMAGE=dashpay/dash-selfhosted-image@sha256:REPLACE_WITH_TESTED_DIGEST
# REGISTRATION_TOKEN_FILE is an absolute path, not the token itself.
docker compose run --rm \
  -e RUNNER_URL=https://github.com/dashpay \
  -e RUNNER_NAME=CHOOSE_A_NEW_RUNNER_NAME \
  -e RUNNER_GROUP=platform-repositories \
  -e RUNNER_LABELS=rust-ci,kotlin-ci \
  -v "$REGISTRATION_TOKEN_FILE:/run/secrets/runner-registration-token:ro" \
  runner register
# Remove the temporary token file. It is not needed for subsequent starts.
export KVM_GID="$(stat -c '%g' /dev/kvm)"
docker compose -f compose.yaml -f compose.kvm.yaml up -d
```

For a Rust-only runner omit the KVM overlay and register only `rust-ci`. Keep the
existing runner group's selected-repository scope unchanged. Registration state
persists in `/runner`; workspace state persists in `/work`. The entrypoint refuses
root and refuses to replace an existing registration. Image upgrades refresh
runner executables from the pinned image while preserving the registration.

Migrate only when the old runner is idle. Prove a new registration's intended
repository/group/labels and real Platform job before retiring the old runner.
Keep its image and registration intact for rollback. This repository does not
automatically replace or reconfigure any live runner.

## Docker Hub publication

[image.yml](.github/workflows/image.yml) uses the same Docker login/metadata/
Buildx/build-push action pattern as `dashpay/platform`, with commit-pinned actions
and organization secrets `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN`. The Docker Hub
identity needs write permission for `dashpay/dash-selfhosted-image`.

Pull requests build and test **without registry credentials**. Trusted `main`
pushes (or manual runs on `main`) and `v*` release tags publish only after compiler,
confinement, KVM and emulator-boot gates pass. Images get a full `sha-<commit>` tag
and release tags get a semantic-version tag, never `latest`. The workflow emits
the immutable image reference in its summary and downloadable artifact, with
BuildKit provenance and an SBOM attached. Protect `main` and release-tag writes
with the organization's normal review policy. New commits do not deploy runners.
