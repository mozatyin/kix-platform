#!/usr/bin/env bash
# Nightly creative-refresh + verify-gate sweep.
#
# 1. Run app.workers.nightly_creative_refresh (refresh stale brand renders
#    against current pipeline_version; verdict_gate filters in/out)
# 2. Regenerate landing/brands/* from scripts/generate_landing_sites.py
# 3. Run scripts/verify_generated_brands.py (Playwright + persona LLM gate)
# 4. Log result; exit non-zero if any brand REJECTs (so cron / CI alerts)
#
# Run manually:
#   ./scripts/cron_nightly_refresh.sh
# launchd: see scripts/com.kix.nightly-refresh.plist
# crontab: 30 3 * * *  cd /Users/mozat/kix-platform && ./scripts/cron_nightly_refresh.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

LOG_DIR="$REPO_ROOT/var/log"
mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/nightly_refresh_${STAMP}.log"

PY="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [ ! -x "$PY" ]; then
  echo "python not found at $PY — set PYTHON env var" >&2
  exit 2
fi

# Ensure local static server is up; nightly job assumes verify uses Playwright
if ! curl -sf -o /dev/null "http://localhost:8765/landing/index.html"; then
  echo "[$(date +%T)] starting local static server on :8765 ..." | tee -a "$LOG"
  nohup "$PY" -m http.server 8765 >/dev/null 2>&1 &
  SERVER_PID=$!
  echo "  pid=$SERVER_PID" | tee -a "$LOG"
  sleep 3
fi

echo "[$(date +%T)] STAGE 1 · nightly_creative_refresh" | tee -a "$LOG"
"$PY" -m app.workers.nightly_creative_refresh 2>&1 | tee -a "$LOG"

echo "[$(date +%T)] STAGE 2 · regenerate brand landings" | tee -a "$LOG"
"$PY" -m scripts.generate_landing_sites 2>&1 | tee -a "$LOG"

echo "[$(date +%T)] STAGE 3 · lint_no_handcrafted_landings" | tee -a "$LOG"
"$PY" -m scripts.lint_no_handcrafted_landings 2>&1 | tee -a "$LOG" || true   # 14 legacy off-template = informational

echo "[$(date +%T)] STAGE 4 · verdict_gate sweep" | tee -a "$LOG"
"$PY" -m scripts.verify_generated_brands 2>&1 | tee -a "$LOG"

# Pull just the AGGREGATE lines for the alert
REJECTS=$(grep -E "AGGREGATE.*REJECT" "$LOG" | wc -l | tr -d ' ')
ACCEPTS=$(grep -E "AGGREGATE.*ACCEPT" "$LOG" | wc -l | tr -d ' ')

echo "[$(date +%T)] DONE — accepts=$ACCEPTS rejects=$REJECTS · log=$LOG" | tee -a "$LOG"

if [ "$REJECTS" -gt 0 ]; then
  echo "GATE REJECTED $REJECTS brand(s) — see $LOG" >&2
  exit 1
fi
exit 0
