# Legacy full/AMD64 recipe regression

These files are byte-for-byte copies from Platform PR 5151's failed candidate
build, not synthetic templates or updated requirements:

- Job: https://github.com/dashpay/platform/actions/runs/36469557091/job/109088568057
- Request artifact: `runner-request-36469557091-1` (artifact `10991033001`).
- Controller: `dashpay/dash-selfhosted-image@07811cd919f6956ba9c6d69a3a1bff4550eb3761`.
- Recipe: `dashpay/dash-selfhosted-image@e49e8bc9977f5f961a76ba1d1f7673c72173679f`.
- Template source: https://github.com/dashpay/dash-selfhosted-image/blob/e49e8bc9977f5f961a76ba1d1f7673c72173679f/Dockerfile.template

Captured evidence: `platform-ci-recovery-20260928/job-109088568057/`, files
`recipe/Dockerfile.template`, `request/manifest.json`, and
`old-control-Dockerfile.rendered`. The template was independently compared with
`git show e49e8bc9977f5f961a76ba1d1f7673c72173679f:Dockerfile.template`.

SHA-256 checksums:

| Input/output | SHA-256 |
| --- | --- |
| `Dockerfile.template` | `22ec09d69cf5212a67115ccc8983647238434c17f1c21f0603b2b3917f3a01c1` |
| `manifest.json` | `7471834917bd9e0e39edb4e43ce3d877d03c92f7a9880e5e93df84cc28425399` |
| Known-good rendered Dockerfile | `93089083c5a3b6433ed4c91492b9f5f513e6d4293a4e20518517b22c6bf56961` |

The output checksum comes from rendering these exact inputs with the recipe's
matching old controller. Tests use it as an independent golden result without
vendoring a second controller or duplicate rendered Dockerfile. Keep these
fixtures pinned; current AMD64/ARM64 rendering is tested separately.
