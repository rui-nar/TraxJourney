#!/bin/bash
# Triggered by the `webhook` service (hooks.yaml) whenever docker-build.yml
# finishes successfully off the `validation` tag (issue #205). Runs locally
# on the VPS — the same pull/up-d deploy.ps1 does over SSH from a dev
# machine, minus the human.
#
# See docs/DEPLOYMENT_VPS.md's "Auto-deploy validation" section for setup.
set -euo pipefail

VAL_DIR="/opt/traxjourney-val"
LOCK_FILE="/tmp/traxjourney-deploy-validation.lock"
LOG_FILE="$VAL_DIR/deploy.log"

log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG_FILE"; }

# GitHub retries webhook deliveries on a slow response; a second delivery
# for the same build landing mid-deploy shouldn't run a second
# pull/up-d concurrently against the same compose project.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "deploy already in progress — skipping this delivery"
  exit 0
fi

log "validation deploy triggered"
cd "$VAL_DIR"

if ! docker compose pull >>"$LOG_FILE" 2>&1; then
  log "docker compose pull FAILED"
  exit 1
fi

if ! docker compose up -d >>"$LOG_FILE" 2>&1; then
  log "docker compose up -d FAILED"
  exit 1
fi

log "validation deploy succeeded"
