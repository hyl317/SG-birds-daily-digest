#!/usr/bin/env bash
# Deploy the SG Birds bot on the Hetzner server.
#
# Steps:
#   1. Refuse if the working tree is dirty (no surprise local edits)
#   2. Capture pre-pull SHA, git pull --ff-only
#   3. Restart sgbirds-bot.service
#   4. Run smoketest.py — if it fails, roll back to the pre-pull SHA
#
# Exit codes:
#   0  deploy succeeded, smoke test passed
#   2  pre-deploy working tree was dirty, refused to touch anything
#   3  service failed to come up after restart (rolled back)
#   4  working tree got dirty between pull and rollback (refused to reset)
#   5  smoke test failed but rollback succeeded — bot is back on previous SHA
#   6  CRITICAL: bot is broken even after rollback

set -uo pipefail

cd /root/sg-birds

log() { echo "[deploy.sh] $*"; }
fail() { echo "[deploy.sh] ERROR: $*" >&2; }

# ---- Step 1: refuse to deploy with a dirty tree ---------------------------
if ! git diff --quiet || ! git diff --cached --quiet; then
  fail "working tree is dirty, refusing to deploy"
  git status
  exit 2
fi

# ---- Step 2: capture pre-SHA, pull ----------------------------------------
PRE_SHA=$(git rev-parse HEAD)
log "Pre-pull SHA:  $PRE_SHA"

if ! git pull --ff-only; then
  fail "git pull --ff-only failed"
  exit 1
fi

NEW_SHA=$(git rev-parse HEAD)
SHORT_NEW_SHA=$(git rev-parse --short HEAD)
log "Post-pull SHA: $NEW_SHA"

if [ "$PRE_SHA" = "$NEW_SHA" ]; then
  log "Already at $NEW_SHA, nothing to deploy"
  exit 0
fi

# ---- Step 3: restart service ----------------------------------------------
systemctl restart sgbirds-bot.service
sleep 5

if ! systemctl is-active --quiet sgbirds-bot.service; then
  fail "service failed to start after restart, rolling back"
  if git diff --quiet && git diff --cached --quiet; then
    git reset --hard "$PRE_SHA"
    systemctl restart sgbirds-bot.service
    log "Rolled back to $PRE_SHA (service-start failure)"
  else
    fail "REFUSING to reset: working tree got dirty"
    exit 4
  fi
  exit 3
fi

# ---- Step 4: smoke test ---------------------------------------------------
log "Running smoke test (expecting commit $SHORT_NEW_SHA)..."
if /root/sg-birds/sg-birds-env/bin/python /root/sg-birds/smoketest.py "$SHORT_NEW_SHA"; then
  log "Smoke test passed. Deploy successful: $NEW_SHA"
  exit 0
fi

# ---- Smoke test failed: roll back ----------------------------------------
fail "smoke test failed, rolling back to $PRE_SHA"

if ! git diff --quiet || ! git diff --cached --quiet; then
  fail "REFUSING to reset: working tree got dirty between pull and rollback"
  git status
  exit 4
fi

git reset --hard "$PRE_SHA"
systemctl restart sgbirds-bot.service
sleep 5

log "Verifying rolled-back bot is healthy..."
if /root/sg-birds/sg-birds-env/bin/python /root/sg-birds/smoketest.py; then
  log "Rollback successful — bot is back on $PRE_SHA and healthy"
  exit 5
else
  fail "CRITICAL: bot is unhealthy even after rollback to $PRE_SHA"
  exit 6
fi
