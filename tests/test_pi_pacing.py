"""PI pacing controller tests.

Targets the >30 percentage-point drift surfaced by the SG marketplace
sim (toast_box: expected_pct=0.96 actual_pct=0.00). The legacy hourly
pacing recomputed once per hour, so bursty traffic could over- or
under-shoot the daily budget by ~100% before the next correction. The
PI controller in ``app.pacing_controller`` recomputes every 60s and
clamps the rank multiplier into [0.1, 2.0].

The five tests below pin the contract the sim now relies on:

  1. Under-spending → factor > 1.0 (chase the setpoint)
  2. Over-spending  → factor < 1.0 (brake)
  3. Integral wind-up cap (factor never explodes past 2.0)
  4. Sliding-window dedup (rapid <60s charges don't double-count)
  5. Diagnostic endpoint surfaces every state key
"""

from __future__ import annotations

import json

import pytest

from app.pacing_controller import (
    FACTOR_MAX,
    FACTOR_MIN,
    INTEGRAL_CAP_CENTS,
    K_ACTUAL_WINDOW,
    K_CUM_ERROR,
    K_FACTOR,
    K_LAST_RECOMPUTE,
    K_SETPOINT,
    MINUTES_PER_DAY,
    RECOMPUTE_PERIOD,
    in_schedule_window,
    get_state,
    record_spend,
    recompute_factor,
    should_skip_for_pacing,
)


# ─── Test 1: under-spending → factor rises above 1.0 ─────────────────────


@pytest.mark.asyncio
async def test_pi_under_spending_factor_rises_above_one(clean_redis):
    """Zero spend in a 60s window for a budgeted campaign → factor > 1.0.

    Setpoint = 1440 cents/min for a $14.40 daily budget. With actual=0,
    error = +1440 → P-term = +1.0, output clamps to ``FACTOR_MAX`` (2.0).
    """
    r = clean_redis
    cid = "cmp_under"
    daily_budget = 1440 * MINUTES_PER_DAY  # cents — setpoint = 1440/min

    factor = await recompute_factor(r, cid, daily_budget, force=True, now=1_000.0)

    assert factor > 1.0, f"under-spending must raise factor, got {factor}"
    # P-term alone is +1.0 at zero actual ⇒ output saturates at FACTOR_MAX.
    assert factor == pytest.approx(FACTOR_MAX), factor


# ─── Test 2: over-spending → factor drops below 1.0 ─────────────────────


@pytest.mark.asyncio
async def test_pi_over_spending_factor_drops_below_one(clean_redis):
    """Spend of 2× setpoint in the last 60s → factor < 1.0."""
    r = clean_redis
    cid = "cmp_over"
    daily_budget = 100 * MINUTES_PER_DAY  # setpoint = 100 cents/min
    now = 1_000.0

    # Burn 200 cents in the window — twice the setpoint.
    await record_spend(r, cid, 200, now=now - 5)

    factor = await recompute_factor(r, cid, daily_budget, force=True, now=now)

    assert factor < 1.0, f"over-spending must lower factor, got {factor}"
    assert factor >= FACTOR_MIN


# ─── Test 3: integral wind-up cap ────────────────────────────────────────


@pytest.mark.asyncio
async def test_pi_integral_windup_is_capped(clean_redis):
    """Many under-spend cycles must not accumulate the I-term unbounded."""
    r = clean_redis
    cid = "cmp_windup"
    daily_budget = 1440 * MINUTES_PER_DAY  # setpoint = 1440 cents/min

    # Force 200 recomputes with no spend at all. Each adds +1440 to the
    # cumulative error pre-cap; without the cap the I-term alone would
    # blow past +14400 (≫ FACTOR_MAX). The controller must clamp.
    for i in range(200):
        await recompute_factor(
            r, cid, daily_budget, force=True, now=1_000.0 + i
        )

    cum_raw = await r.get(K_CUM_ERROR.format(cid=cid))
    cum_error = float(cum_raw)
    factor_raw = await r.get(K_FACTOR.format(cid=cid))
    factor = float(factor_raw)

    assert abs(cum_error) <= INTEGRAL_CAP_CENTS + 1e-6, (
        f"integral wound up past cap: {cum_error} > {INTEGRAL_CAP_CENTS}"
    )
    # Output factor must still respect the hard clamp.
    assert FACTOR_MIN <= factor <= FACTOR_MAX


# ─── Test 4: sub-60s rapid charges don't double-count ────────────────────


@pytest.mark.asyncio
async def test_sliding_window_does_not_double_count_rapid_charges(clean_redis):
    """N identical charges within the window must sum to N × cents.

    The ZSET uses a uniquified member per call so repeated identical
    amounts don't dedupe — but the trim cutoff still drops anything older
    than WINDOW_SECONDS, so charges outside the window are excluded.
    """
    r = clean_redis
    cid = "cmp_dedup"

    base = 5_000.0
    # 10 rapid charges within the window.
    for i in range(10):
        await record_spend(r, cid, 50, now=base + i * 0.5)
    # 1 stale charge well before the window.
    await record_spend(r, cid, 10_000, now=base - 120)

    # Probe the window at base+5s — all 10 charges are within 60s; the
    # stale 10_000-cent charge (base - 120) is well outside.
    state = await get_state(r, cid, daily_budget_cents=0, now=base + 5)
    # 10 × 50 = 500 cents in the window; stale 10000 must be excluded.
    assert state["actual_cents_last_60s"] == 500


# ─── Test 5: pi-state endpoint surfaces every PI key ─────────────────────


@pytest.mark.asyncio
async def test_pi_state_endpoint_surfaces_full_state(client, clean_redis):
    """``GET /api/v1/auction/admin/pacing/{cid}/pi-state`` returns full state."""
    r = clean_redis
    cid = "cmp_diag"
    daily_budget = 100 * MINUTES_PER_DAY

    # Seed a minimal campaign hash so the endpoint passes its 404 gate.
    await r.hset(
        f"campaign:{cid}",
        mapping={
            "campaign_id": cid,
            "brand_id": "brd_x",
            "daily_budget_cents": str(daily_budget),
            "schedule": json.dumps({"hours_local": [0, 24]}),
        },
    )

    # Trigger one PI recompute via the public skip-helper.
    await should_skip_for_pacing(
        r, cid, daily_budget, rand=0.0, now=10_000.0
    )

    res = await client.get(f"/api/v1/auction/admin/pacing/{cid}/pi-state")
    assert res.status_code == 200, res.text
    body = res.json()

    for key in (
        "setpoint_cents_per_min",
        "actual_cents_last_60s",
        "cumulative_error_cents",
        "current_factor",
        "last_recompute_ts",
        "kp",
        "ki",
        "integral_cap_cents",
        "window_seconds",
        "recompute_period_seconds",
        "campaign_id",
        "daily_budget_cents",
    ):
        assert key in body, f"missing diagnostic key: {key}"

    assert body["campaign_id"] == cid
    assert body["daily_budget_cents"] == daily_budget
    assert body["kp"] > 0 and body["ki"] > 0
    assert body["recompute_period_seconds"] == RECOMPUTE_PERIOD


# ─── Bonus: schedule-window helper preserves legacy semantics ────────────


def test_schedule_window_default_inside():
    assert in_schedule_window(None, 14) is True
    assert in_schedule_window("", 14) is True


def test_schedule_window_explicit_inside():
    sched = json.dumps({"hours_local": [9, 17]})
    assert in_schedule_window(sched, 10) is True
    assert in_schedule_window(sched, 17) is False
    assert in_schedule_window(sched, 8) is False


def test_schedule_window_wrap_around():
    sched = json.dumps({"hours_local": [22, 6]})
    assert in_schedule_window(sched, 23) is True
    assert in_schedule_window(sched, 2) is True
    assert in_schedule_window(sched, 12) is False


# ─── Bonus: should_skip_for_pacing maps factor → bernoulli skip ──────────


@pytest.mark.asyncio
async def test_should_skip_never_skips_when_under_spending(clean_redis):
    r = clean_redis
    cid = "cmp_skip_neutral"
    daily_budget = 1440 * MINUTES_PER_DAY

    # Worst case for "never skip" — the highest random draw possible.
    skip, factor = await should_skip_for_pacing(
        r, cid, daily_budget, rand=0.9999, now=1_000.0
    )
    assert factor >= 1.0
    assert skip is False


@pytest.mark.asyncio
async def test_should_skip_brakes_when_over_spending(clean_redis):
    r = clean_redis
    cid = "cmp_skip_brake"
    daily_budget = 100 * MINUTES_PER_DAY  # setpoint = 100 cents/min
    now = 1_000.0

    # Massive over-spend → factor saturates at FACTOR_MIN (0.1).
    await record_spend(r, cid, 100_000, now=now - 1)

    # rand=0.0 always triggers (skip-prob > 0 ⇒ skip).
    skip, factor = await should_skip_for_pacing(
        r, cid, daily_budget, rand=0.0, now=now
    )
    assert factor < 1.0
    assert skip is True
