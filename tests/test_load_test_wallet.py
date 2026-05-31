"""Tests for scripts/load_test_wallet.py — A · load-SLA harness."""
import asyncio
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from load_test_wallet import LoadSLA, _run_smoke_load  # noqa: E402


@pytest.mark.asyncio
async def test_load_runs_and_returns_report():
    sla = LoadSLA(sustained_ops_per_sec=500, duration_seconds=2,
                  p99_latency_ms=500, p99_9_latency_ms=1000)
    report = await _run_smoke_load(sla, "smoke-test")
    assert report.actual_ops > 0
    assert report.actual_duration_s >= 1.5
    assert report.p50_latency_ms > 0
    assert report.p99_latency_ms > 0


@pytest.mark.asyncio
async def test_load_marks_pass_when_sla_clean():
    """The simulated wallet runs at ~5ms latency — under 500ms SLA easily."""
    sla = LoadSLA(sustained_ops_per_sec=200, duration_seconds=1,
                  p99_latency_ms=500, p99_9_latency_ms=1500,
                  max_error_rate_pct=1.0)
    report = await _run_smoke_load(sla, "smoke-test")
    # Simulated wallet has ~3-5ms base; p99 should be well under 500ms
    assert report.p99_latency_ms < 500
    assert report.error_rate_pct < 1.0


@pytest.mark.asyncio
async def test_load_marks_fail_when_p99_too_strict():
    """Set p99 SLA to 1ms — impossible for the simulator, should FAIL."""
    sla = LoadSLA(sustained_ops_per_sec=100, duration_seconds=1,
                  p99_latency_ms=0.5, p99_9_latency_ms=1.0)
    report = await _run_smoke_load(sla, "smoke-test")
    assert not report.passed_sla
    assert any("p99" in f for f in report.sla_failures)


def test_load_sla_dataclass_defaults():
    sla = LoadSLA()
    assert sla.sustained_ops_per_sec == 10_000
    assert sla.p99_latency_ms == 500.0
    assert sla.max_deadlocks == 0
