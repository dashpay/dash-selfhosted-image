#!/usr/bin/env bash
set -euo pipefail
umask 077
if [ "$(id -u)" -eq 0 ]; then
  echo 'The runner must not run as root.' >&2
  exit 1
fi
case "${1:-run}" in
  verify)
    shift
    exec /opt/ci/bin/verify-image "$@"
    ;;
  register|run)
    # /runner is the registration-state volume. Refresh executables from the
    # pinned image, never from GitHub's runtime auto-updater. Hidden registration
    # files and diagnostics are not part of the template and remain untouched.
    test -w /runner || { echo '/runner must be writable by uid 1001' >&2; exit 1; }
    cp -a --no-preserve=ownership /opt/actions-runner/. /runner/
    cd /runner
    if [ "${1:-run}" = register ]; then
      : "${RUNNER_URL:?Set RUNNER_URL to the organization or repository URL}"
      : "${RUNNER_NAME:?Set an explicit runner name}"
      test ! -f .runner || { echo 'Already registered; refusing to replace it.' >&2; exit 1; }
      token_file=${RUNNER_REGISTRATION_TOKEN_FILE:-/run/secrets/runner-registration-token}
      test -r "$token_file" || { echo 'Mount a short-lived registration token file.' >&2; exit 1; }
      token=$(cat "$token_file")
      ./config.sh --unattended --url "$RUNNER_URL" --token "$token" \
        --name "$RUNNER_NAME" --runnergroup "${RUNNER_GROUP:-Default}" \
        --labels "${RUNNER_LABELS:-rust-ci,kotlin-ci}" \
        --work /work --disableupdate
      unset token
      exit 0
    fi
    test -f .runner || { echo 'Register once with the register subcommand before starting.' >&2; exit 1; }
    exec ./run.sh
    ;;
  *) exec "$@" ;;
esac
