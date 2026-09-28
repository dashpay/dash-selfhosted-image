# Rust runner containers on Apple Silicon Macs

The ARM64 variant is a **Linux** image for a Linux Docker VM on an Apple Silicon
Mac, not a macOS container. Keep the existing native macOS runner for Swift,
Xcode, signing and simulator jobs. The Linux runner gets its own registration,
HOME and named workspace volumes; never reuse the native runner's registration
or share its build directories.

## Shared toolchain, native architecture

`image.arm64.lock.json` uses the Rust-only profile. It pins ARM64 builds of the
same Actions runner, Rust, nextest, llvm-cov, machete and protoc versions as the
AMD64 image. `scripts/check-lock.py` rejects version drift between the two locks.
Both Dockerfiles are generated from `Dockerfile.template` and use the same
checksum-verifying installer, entrypoint and confinement/compiler smoke tests.

The Rust-only image omits Android archives and cargo-ndk. The existing AMD64
image and its KVM/emulator gates remain unchanged in scope. Do not label the
ARM64 runner `kotlin-ci`: Android emulator jobs stay on the AMD64/KVM pool.

The ARM64 base image has its own pinned digest. This is the same versioned Rust
environment, not byte-identical AMD64 and ARM64 binaries. Published tags have a
`-rust-arm64` suffix; deploy the tested immutable digest, not a floating tag.

## Build and prove

On an ARM64 Linux Docker engine (including one hosted by a Mac):

```sh
python3 scripts/check-lock.py
docker buildx build --platform linux/arm64 --file Dockerfile.arm64 \
  --build-arg IMAGE_RECIPE_REVISION="$(git rev-parse HEAD)" \
  --load --tag dash-selfhosted-image:rust-arm64-local .
scripts/smoke-test.sh dash-selfhosted-image:rust-arm64-local
```

The dedicated `rust-arm64.yml` workflow performs this natively on
`ubuntu-24.04-arm`, without QEMU. Pull requests receive no registry credentials
and publish nothing. Trusted main/tag builds publish only after the smoke gate.

## Mac deployment and Platform integration gates

Image publication alone does **not** fix or migrate Platform jobs. Its current
requirements selector, candidate publisher and controller are AMD64-oriented.
Before accepting ordinary Rust CI:

1. Provision a Linux ARM64 Docker VM under an operator-controlled service account
   using Colima, OrbStack or Docker Desktop. Confirm it starts unattended after
   reboot. Reserve CPU/RAM for the existing native runner and macOS; do not give
   both runners the host's full resources. The job container gets no Docker
   socket, host home, native workspace, KVM device or elevated privileges.
2. Register the container separately in the existing selected-repository runner
   group, initially with a validation-only label. Use the existing `compose.yaml`
   and registration procedure, without `compose.kvm.yaml`. Linux/ARM64 describes
   the container runner, even though the physical host is a Mac.
3. Add an ARM64-specific Platform requirements manifest pinned to this image's
   recipe and exact ARM64 lock. Select and verify it for `runner.arch == 'ARM64'`.
   Do not weaken `ci-image-contract` to accept an AMD64 lock on ARM64. Extend the
   candidate/promotion lifecycle before allowing automatic ARM64 requirement
   changes; the current AMD64 candidate pipeline is not ARM64 validation.
4. Prove a real Platform Rust job on that container. Only then add its Rust pool
   label and stop sending generic Rust jobs to the native macOS registration.
   Keep the native registration available for Swift/iOS. Maintain separate
   persistent Rust build caches for each architecture.

No native runner is removed or changed by building this image. Preserve the old
configuration and registrations until container startup, real CI and post-reboot
health are verified. Resolve each host's access and runtime/resource settings
before its rollout; a successful hosted ARM64 image build is not proof of a
working Mac deployment.
