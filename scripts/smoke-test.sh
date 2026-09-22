#!/usr/bin/env bash
set -euo pipefail
: "${1:?Usage: smoke-test.sh image [--kvm]}"
image=$1
runtime=(--rm --init --user 1001:1001 --cap-drop ALL --security-opt no-new-privileges=true)
if [[ "${2:-}" == --kvm ]]; then
  test -c /dev/kvm
  runtime+=(--device /dev/kvm:/dev/kvm:rw --group-add "$(stat -c '%g' /dev/kvm)")
  docker run "${runtime[@]}" "$image" verify --confined --kvm
  docker run "${runtime[@]}" --cpus 4 --memory 6g "$image" \
    ci-android-emulator bash -euo pipefail -c '
      test "$(adb shell getprop ro.build.version.sdk | tr -d "\r")" = 35
      test "$(adb shell getprop ro.product.cpu.abi | tr -d "\r")" = x86_64
      echo ANDROID_API_35_BOOT_OK
    '
else
  docker run "${runtime[@]}" --network none "$image" verify --confined
  # The service must fail closed until explicitly registered. No token or PAT
  # is baked in, and no workflow can silently register a runner.
  if output=$(docker run "${runtime[@]}" --network none "$image" run 2>&1); then
    echo 'Unregistered runner unexpectedly started' >&2
    exit 1
  fi
  grep -q 'Register once' <<< "$output"
  echo UNREGISTERED_RUNNER_FAILS_CLOSED
fi
