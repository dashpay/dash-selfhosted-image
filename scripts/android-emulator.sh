#!/usr/bin/env bash
# Runs a command under the prebaked, manifest-selected emulator; never invokes sdkmanager.
set -euo pipefail
if [ "$#" -eq 0 ]; then echo 'Usage: ci-android-emulator command [args...]' >&2; exit 2; fi
export ANDROID_USER_HOME="$HOME/.android"
export ANDROID_EMULATOR_HOME="$ANDROID_USER_HOME"
export ANDROID_AVD_HOME="$ANDROID_USER_HOME/avd"
export ANDROID_SDK_HOME="$HOME"
mkdir -p "$ANDROID_AVD_HOME"
name="ci-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-${BASHPID}"
serial=emulator-5554
test -r /dev/kvm && test -w /dev/kvm
system_image=$(python3 -c 'import json; print(json.load(open("/opt/ci/image.lock.json"))["android"]["system_image"])')
# A fresh AVD avoids stale lockscreen and snapshot state between jobs.
printf 'no\n' | avdmanager create avd --force --name "$name" \
  --package "$system_image" --device pixel_6
log="${RUNNER_TEMP:-/tmp}/android-emulator.log"
emulator -avd "$name" -port 5554 -accel on -gpu swiftshader \
  -no-snapshot -no-window -no-audio -no-boot-anim -camera-back none \
  -dns-server 8.8.8.8,1.1.1.1 >"$log" 2>&1 &
emulator_pid=$!
cleanup() {
  adb -s "$serial" emu kill >/dev/null 2>&1 || true
  kill "$emulator_pid" 2>/dev/null || true
  wait "$emulator_pid" 2>/dev/null || true
  avdmanager delete avd --name "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
booted=false
for ((attempt=0; attempt<180; attempt++)); do
  if ! kill -0 "$emulator_pid" 2>/dev/null; then tail -80 "$log"; exit 1; fi
  if [ "$(timeout 5 adb -s "$serial" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ]; then booted=true; break; fi
  sleep 2
done
if [ "$booted" != true ]; then tail -80 "$log"; echo 'Emulator boot timed out' >&2; exit 1; fi
export ANDROID_SERIAL="$serial"
adb shell settings put global window_animation_scale 0
adb shell settings put global transition_animation_scale 0
adb shell settings put global animator_duration_scale 0
"$@"
