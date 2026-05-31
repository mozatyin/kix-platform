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

echo "[$(date +%T)] STAGE 4a · ELTM smoke (G-A3)" | tee -a "$LOG"
ELTM_URL="${ELTM_HEALTH_URL:-http://localhost:8000/internal/eltm/health}"
if curl -sf --max-time 5 "$ELTM_URL" >/dev/null 2>&1; then
  echo "  ✓ ELTM reachable at $ELTM_URL" | tee -a "$LOG"
else
  echo "  ⚠ ELTM unreachable at $ELTM_URL — non-blocking warning" | tee -a "$LOG"
fi

echo "[$(date +%T)] STAGE 4b · coverage measurement (G-A15)" | tee -a "$LOG"
"$PY" -m pytest \
  tests/test_landing_gen.py tests/test_verdict_gate.py tests/test_customer_vocab.py \
  tests/test_pricing_canon.py tests/test_vertical_benchmarks.py \
  tests/test_brand_inject_preview.py tests/test_nightly_creative_refresh.py \
  tests/test_eltm_callback_verdict_gate.py \
  --cov=app/services --cov=app/workers --cov-report=term-missing:skip-covered \
  -q --no-header 2>&1 | tail -15 | tee -a "$LOG" || true

echo "[$(date +%T)] STAGE 5 · bible_check (drift + claim audits)" | tee -a "$LOG"
"$PY" -m scripts.bible_check --strict 2>&1 | tee -a "$LOG"

echo "[$(date +%T)] STAGE 6 · verdict_gate sweep" | tee -a "$LOG"
"$PY" -m scripts.verify_generated_brands 2>&1 | tee -a "$LOG"

# Wave N · Phase C — buyer journey conversion sim (5 buyer types)
echo "[$(date +%T)] STAGE 7 · buyer_journey_sim (5 personas)" | tee -a "$LOG"
"$PY" -m scripts.buyer_journey_sim --round-id "cron-$(date +%Y%m%d)" \
  --json /tmp/cron-journey.json 2>&1 | tee -a "$LOG"
JOURNEY_TOTAL=$(grep "Total ARR value" "$LOG" | tail -1 | grep -oE '\$[0-9,]+' | tr -d '$,' || echo "0")
echo "  → Total simulated ARR: S\$${JOURNEY_TOTAL}" | tee -a "$LOG"

# Pull just the AGGREGATE lines for the alert
REJECTS=$(grep -E "AGGREGATE.*REJECT" "$LOG" | wc -l | tr -d ' ')
ACCEPTS=$(grep -E "AGGREGATE.*ACCEPT" "$LOG" | wc -l | tr -d ' ')

echo "[$(date +%T)] DONE — accepts=$ACCEPTS rejects=$REJECTS · log=$LOG" | tee -a "$LOG"

# F · Webhook alerting on REJECT (Slack-compatible JSON payload).
# Set NIGHTLY_ALERT_WEBHOOK_URL to enable. Falls back silently otherwise.
if [ "$REJECTS" -gt 0 ] && [ -n "${NIGHTLY_ALERT_WEBHOOK_URL:-}" ]; then
  REJECT_DETAIL=$(grep -E "AGGREGATE.*REJECT" "$LOG" | head -5 | sed 's/"/\\"/g')
  TOP_REASONS=$(grep -A1 "Top rejection reasons" "$LOG" | tail -1 | head -c 400 | sed 's/"/\\"/g')
  PAYLOAD=$(cat <<JSON
{
  "text": "🚨 KiX nightly creative refresh · ${REJECTS} brand(s) REJECTED · ${ACCEPTS} accepted",
  "attachments": [{
    "color": "danger",
    "title": "Rejection detail",
    "text": "${REJECT_DETAIL}\n\nTop reasons: ${TOP_REASONS}",
    "footer": "Log: ${LOG}",
    "ts": $(date +%s)
  }]
}
JSON
)
  curl -s -X POST -H "Content-Type: application/json" \
    --data "$PAYLOAD" --max-time 10 \
    "$NIGHTLY_ALERT_WEBHOOK_URL" >/dev/null 2>&1 \
    && echo "  ✓ alert webhook fired" | tee -a "$LOG" \
    || echo "  ⚠ alert webhook failed" | tee -a "$LOG"
fi

if [ "$REJECTS" -gt 0 ]; then
  echo "GATE REJECTED $REJECTS brand(s) — see $LOG" >&2
  exit 1
fi
exit 0
