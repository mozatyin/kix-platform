#!/usr/bin/env bash
# Orchestrates the three profiles (baseline → stress → breaking).
#
# Env overrides:
#   KIX_HOST=http://localhost:8000   (target host)
#   KIX_PG_DSN=postgres://...        (for host_metrics sidecar)
#   KIX_REDIS_URL=redis://...        (for host_metrics sidecar)
#   PROFILES="baseline stress breaking"  (subset to run)
#   WEB_UI=1                         (open locust web UI instead of headless)

set -euo pipefail
cd "$(dirname "$0")/.."

ROOT="$(pwd)"
VENV="${ROOT}/.venv"
LOCUST="${VENV}/bin/locust"
PY="${VENV}/bin/python"
RESULTS="${ROOT}/load_tests/results"
mkdir -p "${RESULTS}"

HOST="${KIX_HOST:-http://localhost:8000}"
PROFILES="${PROFILES:-baseline stress breaking}"

echo ">> Seeding data..."
"${PY}" -m load_tests.seed_data

if [[ "${WEB_UI:-0}" == "1" ]]; then
  echo ">> Web UI mode — open http://localhost:8089"
  exec "${LOCUST}" -f load_tests/locustfile.py --host "${HOST}"
fi

run_profile() {
  local name="$1"
  local runtime="$2"
  echo ">> Running profile: ${name} (${runtime})"
  local host_csv="${RESULTS}/host_${name}.csv"
  "${PY}" -m load_tests.host_metrics --out "${host_csv}" --duration 7200 &
  local host_pid=$!
  trap "kill ${host_pid} 2>/dev/null || true" EXIT

  "${LOCUST}" \
    -f "load_tests/${name}.py" \
    --host "${HOST}" \
    --headless \
    --csv "${RESULTS}/${name}" \
    --html "${RESULTS}/${name}.html" \
    --run-time "${runtime}" \
    --only-summary || echo "Profile ${name} returned non-zero (continuing)"

  kill "${host_pid}" 2>/dev/null || true
  trap - EXIT
}

for p in ${PROFILES}; do
  case "$p" in
    baseline)  run_profile baseline 6m ;;
    stress)    run_profile stress 20m ;;
    breaking)  run_profile breaking 30m ;;
    *) echo "Unknown profile: $p" >&2; exit 1 ;;
  esac
done

echo ">> Generating report..."
"${PY}" -m load_tests.analyze \
  --results "${RESULTS}" \
  --out "/Users/mozat/a-docs/load-test-report.md"

echo ">> Done. Report at /Users/mozat/a-docs/load-test-report.md"
