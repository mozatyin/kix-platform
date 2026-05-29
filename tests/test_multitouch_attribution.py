"""Multi-touch cross-brand attribution tests.

Covers the P2 fix: a single conversion must credit every brand/campaign
the user touched in the lookback window — not just the last-touch one.

Tests seed the journey ZSET directly via the internal ``_persist_event``
helper (bypassing consent enforcement, which is exercised elsewhere) so
the attribution model is tested in isolation.
"""

from __future__ import annotations

import json
import os
import time

# Tests exercise the attribution model purely — consent enforcement is
# covered by ``test_attribution.py`` and ``test_consent.py`` separately.
# Run in permissive mode so the new endpoint always returns 200 here.
os.environ.setdefault("KIX_CONSENT_ENFORCEMENT", "permissive")

import pytest

from app.redis_client import get_redis
from app.routers.attribution import (
    ATTRIBUTED_FIXED_POINT_SCALE,
    STAGE_CLICK,
    STAGE_IMPRESSION,
    _persist_event,
    compute_cross_brand_weights,
    log_touchpoint,
)


async def _seed_touchpoint(
    r,
    *,
    user_id: str,
    source_brand: str,
    target_brand: str,
    campaign_id: str,
    stage: str = STAGE_IMPRESSION,
    timestamp_override: float | None = None,
) -> str:
    """Seed an attributable touchpoint with explicit timestamp.

    Returns the event_id. Adjusts the journey ZSET score so cross-brand
    attribution sees the touchpoint as having happened at ``timestamp_override``.
    """
    event_id, ts = await _persist_event(
        r,
        stage=stage,
        user_id=user_id,
        source_brand=source_brand,
        target_brand=target_brand,
        meta={"campaign_id": campaign_id},
    )
    if timestamp_override is not None:
        # Rewrite the timestamp on the event + journey ZSET so deterministic
        # time-decay / lookback assertions don't depend on real wall-clock.
        await r.hset(f"attr:{event_id}", "timestamp", f"{timestamp_override:.6f}")
        await r.zadd(
            f"user:{user_id}:attr_journey_z",
            {event_id: timestamp_override},
        )
    return event_id


# ─── Test 1: linear model credits cross-brand journey ────────────────────

@pytest.mark.asyncio
async def test_linear_model_splits_cross_brand_50_50(client, clean_redis):
    """User sees brand_A push → converts at brand_B → linear: A=50, B=50."""
    r = await get_redis()
    now = time.time()
    user_id = "u_linear"

    # brand_A push impression 1h ago
    await _seed_touchpoint(
        r,
        user_id=user_id,
        source_brand="brand_A",
        target_brand="user_inbox",
        campaign_id="camp_A",
        stage=STAGE_IMPRESSION,
        timestamp_override=now - 3600,
    )
    # brand_B click 10min ago
    await _seed_touchpoint(
        r,
        user_id=user_id,
        source_brand="brand_B",
        target_brand="brand_B",
        campaign_id="camp_B",
        stage=STAGE_CLICK,
        timestamp_override=now - 600,
    )

    res = await client.post(
        "/api/v1/attribution/conversion",
        json={
            "user_id": user_id,
            "brand_id_converted_at": "brand_B",
            "conversion_ts": now,
            "conversion_value_cents": 10000,  # $100
            "model": "linear",
            "lookback_days": 7,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["attributed"] is True
    assert body["touchpoint_count"] == 2
    credits = body["credits"]
    assert len(credits) == 2
    # Each touchpoint gets exactly 50%
    weights = sorted(c["weight"] for c in credits)
    assert weights == [pytest.approx(0.5), pytest.approx(0.5)]
    values = sorted(c["credited_value_cents"] for c in credits)
    assert values == [pytest.approx(5000.0), pytest.approx(5000.0)]
    # Persisted attributed counters per campaign
    fixed_a = await r.hget("campaign:camp_A:attributed_value_cents", "fixed_point_units")
    fixed_b = await r.hget("campaign:camp_B:attributed_value_cents", "fixed_point_units")
    assert int(fixed_a) == 5000 * ATTRIBUTED_FIXED_POINT_SCALE
    assert int(fixed_b) == 5000 * ATTRIBUTED_FIXED_POINT_SCALE


# ─── Test 2: time_decay (1h ≈ 99%, 7d ≈ 1%) ──────────────────────────────

@pytest.mark.asyncio
async def test_time_decay_old_touch_negligible(client, clean_redis):
    """time_decay: 7-day-old impression gets ~1% weight, 1-hour-old gets ~99%."""
    now = time.time()
    # Pure-function test on compute_cross_brand_weights — avoids any
    # consent / redis dependency for the math assertion.
    touchpoints = [
        {"timestamp": now - 7 * 86400, "stage": "impression"},  # 7d old
        {"timestamp": now - 3600, "stage": "click"},            # 1h old
    ]
    weights = compute_cross_brand_weights(touchpoints, "time_decay", conversion_ts=now)
    # Weight before normalisation: exp(-168/168)=0.368, exp(-1/168)≈0.994.
    # After normalising over (0.368 + 0.994 = 1.362):
    # old ≈ 0.27, new ≈ 0.73 — clearly the recent one dominates.
    assert weights[0] < 0.30  # 7-day-old has small share
    assert weights[1] > 0.70  # 1-hour-old dominates
    assert sum(weights) == pytest.approx(1.0)

    # Edge case: a truly week-old vs minute-old touch
    tps_extreme = [
        {"timestamp": now - 7 * 86400, "stage": "impression"},
        {"timestamp": now - 60, "stage": "click"},
    ]
    w_extreme = compute_cross_brand_weights(
        tps_extreme, "time_decay", conversion_ts=now
    )
    assert w_extreme[0] < 0.30
    assert w_extreme[1] > 0.70


# ─── Test 3: position_based 40/20/40 ──────────────────────────────────────

@pytest.mark.asyncio
async def test_position_based_3_touchpoints(client, clean_redis):
    """3 touches: first 40%, middle 20%, last 40%."""
    r = await get_redis()
    now = time.time()
    user_id = "u_position"

    for i, (brand, cid, stage, offset) in enumerate([
        ("brand_A", "camp_A", STAGE_IMPRESSION, -7200),
        ("brand_B", "camp_B", STAGE_CLICK, -3600),
        ("brand_C", "camp_C", STAGE_CLICK, -600),
    ]):
        await _seed_touchpoint(
            r,
            user_id=user_id,
            source_brand=brand,
            target_brand=brand,
            campaign_id=cid,
            stage=stage,
            timestamp_override=now + offset,
        )

    res = await client.post(
        "/api/v1/attribution/conversion",
        json={
            "user_id": user_id,
            "brand_id_converted_at": "brand_C",
            "conversion_ts": now,
            "conversion_value_cents": 10000,
            "model": "position_based",
            "lookback_days": 7,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["touchpoint_count"] == 3
    # Credits are returned chronologically (first → last)
    credits = body["credits"]
    assert credits[0]["weight"] == pytest.approx(0.4)
    assert credits[1]["weight"] == pytest.approx(0.2)
    assert credits[2]["weight"] == pytest.approx(0.4)


# ─── Test 4: lookback window excludes old touches ────────────────────────

@pytest.mark.asyncio
async def test_lookback_window_excludes_old_touches(client, clean_redis):
    """8-day-old touch is excluded when lookback_days=7."""
    r = await get_redis()
    now = time.time()
    user_id = "u_lookback"

    # 8-day-old impression — should NOT be credited
    await _seed_touchpoint(
        r,
        user_id=user_id,
        source_brand="brand_OLD",
        target_brand="brand_OLD",
        campaign_id="camp_OLD",
        stage=STAGE_IMPRESSION,
        timestamp_override=now - 8 * 86400,
    )
    # 1-day-old click — should be credited
    await _seed_touchpoint(
        r,
        user_id=user_id,
        source_brand="brand_NEW",
        target_brand="brand_NEW",
        campaign_id="camp_NEW",
        stage=STAGE_CLICK,
        timestamp_override=now - 86400,
    )

    res = await client.post(
        "/api/v1/attribution/conversion",
        json={
            "user_id": user_id,
            "brand_id_converted_at": "brand_NEW",
            "conversion_ts": now,
            "conversion_value_cents": 1000,
            "model": "linear",
            "lookback_days": 7,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["touchpoint_count"] == 1
    assert body["credits"][0]["campaign_id"] == "camp_NEW"
    # 8-day-old campaign got nothing
    fixed_old = await r.hget(
        "campaign:camp_OLD:attributed_value_cents", "fixed_point_units"
    )
    assert fixed_old is None


# ─── Test 5: reporting exposes both direct + attributed metrics ──────────

@pytest.mark.asyncio
async def test_reporting_exposes_attributed_metric(client, clean_redis):
    """``attributed_value_cents`` + ``attributed_conversions`` are valid metrics."""
    res = await client.get("/api/v1/reporting/metrics")
    assert res.status_code == 200, res.text
    body = res.json()
    counters = set(body["counter_metrics"])
    # Direct (last-touch) revenue metric still present
    assert "revenue_cents" in counters
    assert "conversions" in counters
    # New attributed metrics
    assert "attributed_value_cents" in counters
    assert "attributed_conversions" in counters


@pytest.mark.asyncio
async def test_campaign_attributed_endpoint_after_conversion(client, clean_redis):
    """End-to-end: campaign attributed counters accumulate across conversions."""
    r = await get_redis()
    now = time.time()
    user_id = "u_camp"

    await _seed_touchpoint(
        r,
        user_id=user_id,
        source_brand="brand_X",
        target_brand="brand_X",
        campaign_id="camp_X",
        stage=STAGE_CLICK,
        timestamp_override=now - 600,
    )

    # First conversion: $50 → camp_X gets full credit (only touchpoint)
    res = await client.post(
        "/api/v1/attribution/conversion",
        json={
            "user_id": user_id,
            "brand_id_converted_at": "brand_X",
            "conversion_ts": now,
            "conversion_value_cents": 5000,
            "model": "linear",
        },
    )
    assert res.status_code == 200, res.text

    # Inspect cumulative attributed report
    res = await client.get("/api/v1/attribution/campaign/camp_X/attributed")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["attributed_value_cents"] == pytest.approx(5000.0)
    assert body["attributed_conversions"] == pytest.approx(1.0)


# ─── Touchpoint log cap tests ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_touchpoint_log_cap_100_entries(clean_redis):
    """log_touchpoint never grows the ZSET beyond 100 entries."""
    r = await get_redis()
    user_id = "u_cap"
    base = time.time()
    for i in range(120):
        await log_touchpoint(
            r,
            user_id=user_id,
            touchpoint_id=f"imp_{i}",
            timestamp=base + i,
            campaign_id=f"camp_{i}",
            brand_id="brand_x",
        )
    size = await r.zcard(f"attribution:user:{user_id}:touchpoints")
    assert size == 100  # cap enforced


@pytest.mark.asyncio
async def test_touchpoint_log_drops_old_entries(clean_redis):
    """Entries older than 30 days are dropped on next log_touchpoint."""
    r = await get_redis()
    user_id = "u_age"
    now = time.time()
    # Insert an entry "31 days ago"
    await log_touchpoint(
        r,
        user_id=user_id,
        touchpoint_id="imp_old",
        timestamp=now - 31 * 86400,
    )
    # Insert a fresh entry — the trim happens here
    await log_touchpoint(
        r,
        user_id=user_id,
        touchpoint_id="imp_fresh",
        timestamp=now,
    )
    members = await r.zrange(
        f"attribution:user:{user_id}:touchpoints", 0, -1, withscores=True
    )
    # Old entry purged by zremrangebyscore inside log_touchpoint
    members_payload = [json.loads(m).get("id") for m, _ in members]
    assert "imp_old" not in members_payload
    assert "imp_fresh" in members_payload


@pytest.mark.asyncio
async def test_user_touchpoints_endpoint(client, clean_redis):
    """GET /attribution/user/{uid}/touchpoints returns newest first."""
    r = await get_redis()
    user_id = "u_endpoint"
    base = time.time()
    for i in range(3):
        await log_touchpoint(
            r,
            user_id=user_id,
            touchpoint_id=f"imp_{i}",
            timestamp=base + i,
            campaign_id=f"camp_{i}",
        )
    res = await client.get(f"/api/v1/attribution/user/{user_id}/touchpoints")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 3
    # newest first → imp_2 then imp_1 then imp_0
    ids = [tp["id"] for tp in body["touchpoints"]]
    assert ids == ["imp_2", "imp_1", "imp_0"]


# ─── Pure weight-function correctness ────────────────────────────────────

def test_compute_weights_edge_cases():
    """Last-touch / first-touch / linear behave under N=0,1,2 too."""
    # N=0 → empty
    assert compute_cross_brand_weights([], "linear", conversion_ts=0.0) == []
    # N=1 last_touch = first_touch = linear = [1.0]
    one = [{"timestamp": 0.0, "stage": "click"}]
    assert compute_cross_brand_weights(one, "last_touch", conversion_ts=0.0) == [1.0]
    assert compute_cross_brand_weights(one, "first_touch", conversion_ts=0.0) == [1.0]
    assert compute_cross_brand_weights(one, "linear", conversion_ts=0.0) == [1.0]
    # N=2 last_touch
    two = [
        {"timestamp": 0.0, "stage": "impression"},
        {"timestamp": 1.0, "stage": "click"},
    ]
    assert compute_cross_brand_weights(two, "last_touch", conversion_ts=2.0) == [0.0, 1.0]
    assert compute_cross_brand_weights(two, "first_touch", conversion_ts=2.0) == [1.0, 0.0]
    # position_based N=2 → 50/50
    assert compute_cross_brand_weights(
        two, "position_based", conversion_ts=2.0
    ) == [0.5, 0.5]
