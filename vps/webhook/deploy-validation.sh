#!/bin/bash
# Triggered by the `webhook` service (hooks.yaml) whenever docker-build.yml
# finishes successfully off the `validation` tag (issues #205, #422). Runs
# locally on the VPS as the deploy user — the same pull/up-d deploy.ps1 does
# over SSH from a dev machine, minus the human — and then checks that val
# really serves the build that triggered it.
#
#   deploy-validation.sh <workflow_run.head_sha>
#
# See docs/DEPLOYMENT_VPS.md §8 for setup. The settings below default to the
# VPS layout; tests/test_validation_webhook.py points them at a temp dir.
set -euo pipefail

VAL_DIR="${VAL_DIR:-/opt/traxjourney-val}"
HOOK_DIR="${HOOK_DIR:-$VAL_DIR/webhook}"
LOG_FILE="${LOG_FILE:-$HOOK_DIR/deploy.log}"
LOCK_FILE="${LOCK_FILE:-$HOOK_DIR/deploy.lock}"
LOCK_WAIT="${LOCK_WAIT:-900}"
VERSION_URL="${VERSION_URL:-http://127.0.0.1:8001/api/version}"
VERIFY_ATTEMPTS="${VERIFY_ATTEMPTS:-60}"
VERIFY_INTERVAL="${VERIFY_INTERVAL:-5}"

# The file is for `tail -f`; stdout reaches the journal, where webhook -verbose
# logs the command's output once it exits.
log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$LOG_FILE"; }

sha="${1:-}"
if [[ ! "$sha" =~ ^[0-9a-f]{40}$ ]]; then
  log "FAILED: expected the built commit's full sha as the only argument, got '$sha'"
  exit 2
fi
short="${sha:0:7}"
log "delivery for $sha received"

# Wait for a deploy already running rather than skip this one: that deploy may
# have pulled before this build's image was pushed, and skipping would leave
# val on the older build with nothing left to fix it.
exec 9>"$LOCK_FILE"
if ! flock -w "$LOCK_WAIT" 9; then
  log "FAILED $short: another deploy still held $LOCK_FILE after ${LOCK_WAIT}s"
  exit 1
fi

cd "$VAL_DIR"

if ! docker compose pull >>"$LOG_FILE" 2>&1; then
  log "FAILED $short: docker compose pull"
  exit 1
fi

if ! docker compose up -d >>"$LOG_FILE" 2>&1; then
  log "FAILED $short: docker compose up -d"
  exit 1
fi

# docker-build.yml bakes APP_VERSION=validation-$(git rev-parse --short <sha>).
# --short gives 7 hex digits unless git needs more to stay unambiguous, so any
# prefix of 7 or more digits of the built sha is a match. Keep polling until
# the deadline: the new container needs time to start and migrate.
reported=""
for ((attempt = 1; attempt <= VERIFY_ATTEMPTS; attempt++)); do
  body="$(curl -fsS --max-time 5 "$VERSION_URL" 2>/dev/null || true)"
  reported="$(printf '%s' "$body" | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p')"
  if [[ "$reported" =~ ^validation-([0-9a-f]{7,40})$ ]]; then
    if [[ "$sha" == "${BASH_REMATCH[1]}"* ]]; then
      log "SUCCESS $short: $VERSION_URL reports $reported"
      exit 0
    fi
  fi
  if ((attempt < VERIFY_ATTEMPTS)); then
    sleep "$VERIFY_INTERVAL"
  fi
done

log "FAILED $short: $VERSION_URL reports '${reported:-no version}', expected validation-$short"
exit 1
