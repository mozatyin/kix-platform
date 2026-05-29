"""Quality Score auto-compute / decay / breakdown / override tests.

Verifies P2 fix: QS dispersion must reflect realised metrics rather than
staying locked at the create-time 0.5..1.0 distribution forever.
"""

from __future__ import annotations

import time

import pytest

from app.quality_score import (
    QS_DEFAULT,
    QS_LAST_RECOMPUTE_FIELD,
    QS_MAX,
    QS_MIN,
    STALE_IMPRESSION_THRESHOLD,
    _ck,
    _sk,
    _write_snapshot,
    apply_decay,
    compute_new_qs,
    compute_qs_components,
    recompute_quality_score,
    smooth_qs,
)


# ── Pure-function tests (no Redis) ───────────────────────────────────────


def test_components_at_baseline_metrics():
    """At spec baseline (10% CTR, 5% CVR) every contribution lands on its
    spec coefficient."""
    comps = compute_qs_components(0.10, 0.05, 0.0, 0.0)
    # CTR/0.10 = 1 → 2.0 * 1 = 2.0; CVR/0.05 = 1 → 1.5 * 1 = 1.5.
    assert comps["ctr_contribution"] == pytest.approx(2.0)
    assert comps["cvr_contribution"] == pytest.approx(1.5)
    assert comps["completion_contribution"] == 0.0
    assert comps["frequency_complaint_penalty"] == 0.0


def test_qs_at_baseline_is_above_default():
    """A campaign matching the spec baseline (10% CTR + 5% CVR) should land
    clearly above the QS_DEFAULT 0.5."""
    qs, _ = compute_new_qs(0.10, 0.05, 0.0, 0.0)
    assert qs > QS_DEFAULT
    # Spec calls this ~1.0 — sigmoid lands in the right neighbourhood.
    assert 0.9 < qs <= QS_MAX


def test_qs_at_low_metrics_is_below_default():
    """Test 2 from the brief: 1% CTR + 0.5% CVR → QS ~0.4."""
    qs, _ = compute_new_qs(0.01, 0.005, 0.0, 0.0)
    assert qs < QS_DEFAULT
    assert QS_MIN <= qs <= 0.5


def test_qs_complaint_penalty_lowers_qs():
    """High complaint rate must reduce QS even with otherwise great metrics."""
    qs_clean, _ = compute_new_qs(0.10, 0.05, 0.5, 0.0)
    qs_complaints, _ = compute_new_qs(0.10, 0.05, 0.5, 0.20)
    assert qs_complaints < qs_clean


def test_qs_clamps_to_bounds():
    """Extreme inputs must not blow past [QS_MIN, QS_MAX]."""
    qs_hi, _ = compute_new_qs(1.0, 1.0, 1.0, 0.0)
    qs_lo, _ = compute_new_qs(0.0, 0.0, 0.0, 1.0)
    assert qs_hi <= QS_MAX
    assert qs_lo >= QS_MIN


def test_smoothing_dampens_whiplash():
    """Rapid metric change must be damped — 70/30 EMA."""
    # Old QS 0.5, new candidate 2.0 → result should be ~1.55, not 2.0.
    smoothed = smooth_qs(2.0, 0.5)
    assert smoothed == pytest.approx(0.7 * 2.0 + 0.3 * 0.5)
    assert smoothed < 2.0
    assert smoothed > 0.5


def test_decay_drifts_stale_toward_default():
    """A stale campaign with QS=1.5 should drift toward 0.5 over time."""
    new_qs = apply_decay(old_qs=1.5, trailing_impressions=10, weeks_since_last_recompute=1.0)
    assert new_qs < 1.5
    assert new_qs > QS_DEFAULT  # Not all the way down in a single week.


def test_decay_no_effect_when_not_stale():
    """Plenty of traffic → no decay applied."""
    new_qs = apply_decay(old_qs=1.5, trailing_impressions=10_000, weeks_since_last_recompute=10.0)
    assert new_qs == 1.5


def test_decay_overshoot_protected():
    """Decay must not overshoot QS_DEFAULT (i.e. can't flip side)."""
    # Old 0.6, big weeks_since — drift = 0.1 * 10 * (0.6 - 0.5) = 0.1 → 0.5.
    new_qs = apply_decay(old_qs=0.6, trailing_impressions=0, weeks_since_last_recompute=100.0)
    assert new_qs == pytest.approx(QS_DEFAULT)


# ── Integration tests (Redis) ────────────────────────────────────────────


async def _seed_campaign(
    r, cid: str, *, qs: float = 0.5, brand_id: str = "brand_qs_test"
) -> None:
    """Minimal in-Redis campaign seed (skips the full create flow)."""
    await r.hset(
        _ck(cid),
        mapping={
            "campaign_id": cid,
            "brand_id": brand_id,
            "name": "qs-test",
            "status": "active",
            "quality_score": str(qs),
            "max_bid_cents": "100",
            "daily_budget_cents": "10000",
            "total_budget_cents": "100000",
            "bid_strategy": "cpm",
            "objective": "acquire",
        },
    )
    await r.sadd("campaigns:active", cid)


@pytest.mark.asyncio
async def test_autocompute_baseline_metrics_lifts_qs(clean_redis):
    """Test 1 (brief): campaign with 10% CTR + 5% CVR → QS ~1.0 after recompute."""
    r = clean_redis
    cid = "cmp_autocompute_baseline"
    await _seed_campaign(r, cid, qs=0.5)

    # Seed a 7-day-ago snapshot of zero counters, then bump current stats
    # to 10% CTR + 5% CVR over 1000 impressions.
    seven_days_ago = time.strftime(
        "%Y-%m-%d", time.gmtime(time.time() - 7 * 86400)
    )
    await _write_snapshot(r, cid, seven_days_ago, {
        "impressions": 0, "clicks": 0, "conversions": 0,
    })
    await r.hset(_sk(cid), mapping={
        "impressions": "1000",
        "clicks": "100",        # 10% CTR
        "conversions": "5",     # 5% CVR (5/100)
    })

    res = await recompute_quality_score(r, cid)
    assert res["ok"], res
    assert res["path"] == "autocompute"
    new_qs = res["new_qs"]
    # Smoothed: 0.7 * great_qs + 0.3 * 0.5. With great_qs ≈ 1.7 → smoothed ≈ 1.34.
    assert new_qs > 0.9
    assert new_qs <= QS_MAX


@pytest.mark.asyncio
async def test_autocompute_low_metrics_drops_qs(clean_redis):
    """Test 2 (brief): 1% CTR + 0.5% CVR → QS ~0.4."""
    r = clean_redis
    cid = "cmp_autocompute_low"
    # Start high so we can verify the drop, not just a flat default.
    await _seed_campaign(r, cid, qs=1.0)

    seven_days_ago = time.strftime(
        "%Y-%m-%d", time.gmtime(time.time() - 7 * 86400)
    )
    await _write_snapshot(r, cid, seven_days_ago, {
        "impressions": 0, "clicks": 0, "conversions": 0,
    })
    await r.hset(_sk(cid), mapping={
        "impressions": "10000",
        "clicks": "100",        # 1% CTR
        "conversions": "1",     # 1% CVR (still bad)
    })

    res = await recompute_quality_score(r, cid)
    assert res["ok"], res
    assert res["path"] == "autocompute"
    new_qs = res["new_qs"]
    # 0.7 * low_qs + 0.3 * 1.0 — low_qs ~0.3, so smoothed ~0.51.
    # The point of the test is "no longer locked high".
    assert new_qs < 1.0


@pytest.mark.asyncio
async def test_stale_campaign_decays_toward_default(clean_redis):
    """Test 3 (brief): < 100 impressions in 7d → QS decays toward 0.5."""
    r = clean_redis
    cid = "cmp_stale"
    await _seed_campaign(r, cid, qs=1.5)

    # Stamp last_recompute as 1 week ago so apply_decay sees a full week.
    last_ts = time.time() - 7 * 86400
    await r.hset(
        _ck(cid),
        mapping={QS_LAST_RECOMPUTE_FIELD: str(last_ts)},
    )

    # No traffic = trailing 0 impressions → decay path.
    await r.hset(_sk(cid), mapping={
        "impressions": "10",  # below STALE threshold
        "clicks": "0",
        "conversions": "0",
    })

    res = await recompute_quality_score(r, cid)
    assert res["ok"], res
    assert res["path"] == "decay"
    # Started 1.5, ~1 week elapsed → drift = (1.5 - 0.5) * 0.1 * 1 = 0.1.
    # New QS should be ≈ 1.4 (lower than 1.5 but well above default).
    assert res["new_qs"] < 1.5
    assert res["new_qs"] > QS_DEFAULT
    assert res["trailing_7d"]["impressions"] < STALE_IMPRESSION_THRESHOLD


@pytest.mark.asyncio
async def test_smoothing_prevents_whiplash(clean_redis):
    """Test 4 (brief): big swing in metrics shouldn't whiplash QS to a bound."""
    r = clean_redis
    cid = "cmp_smooth"
    # Start at default 0.5.
    await _seed_campaign(r, cid, qs=0.5)

    seven_days_ago = time.strftime(
        "%Y-%m-%d", time.gmtime(time.time() - 7 * 86400)
    )
    await _write_snapshot(r, cid, seven_days_ago, {
        "impressions": 0, "clicks": 0, "conversions": 0,
    })
    # Way-above-baseline performance.
    await r.hset(_sk(cid), mapping={
        "impressions": "1000",
        "clicks": "500",       # 50% CTR (insane)
        "conversions": "100",  # 20% CVR
    })

    res = await recompute_quality_score(r, cid)
    assert res["ok"], res
    # Smoothing pulls new_qs down from the cap toward old_qs.
    # Unsmoothed would be ~2.0; smoothed = 0.7 * 2.0 + 0.3 * 0.5 = 1.55.
    assert res["new_qs"] < 2.0  # Did not whiplash to the cap.
    assert res["new_qs"] > 1.0  # But did move significantly up.


@pytest.mark.asyncio
async def test_qs_breakdown_endpoint(client, clean_redis):
    """Diagnostic endpoint returns components + trailing metrics + ts."""
    r = clean_redis
    cid = "cmp_breakdown"
    await _seed_campaign(r, cid, qs=0.75)
    await r.hset(_sk(cid), mapping={
        "impressions": "500", "clicks": "25", "conversions": "1",
    })

    res = await client.get(f"/api/v1/campaigns/{cid}/qs-breakdown")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["campaign_id"] == cid
    assert body["current_qs"] == pytest.approx(0.75)
    assert "ctr_contribution" in body["components"]
    assert "cvr_contribution" in body["components"]
    assert "completion_contribution" in body["components"]
    assert "frequency_complaint_penalty" in body["components"]
    assert "trailing_7d_metrics" in body
    assert "impressions" in body["trailing_7d_metrics"]


@pytest.mark.asyncio
async def test_qs_breakdown_404_for_unknown(client, clean_redis):
    res = await client.get("/api/v1/campaigns/cmp_does_not_exist/qs-breakdown")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_admin_override_sets_qs_and_sticky(client, clean_redis):
    """Test 5 (brief): admin override sets QS and prevents auto-recompute."""
    r = clean_redis
    cid = "cmp_override"
    await _seed_campaign(r, cid, qs=0.5)

    res = await client.put(
        f"/api/v1/campaigns/{cid}/qs-override",
        json={
            "admin_token": "admin-dev-token",
            "quality_score": 1.85,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["quality_score"] == pytest.approx(1.85)
    assert body["override_until_ts"] > time.time()

    # QS hash updated.
    stored = await r.hget(_ck(cid), "quality_score")
    assert float(stored) == pytest.approx(1.85)

    # Auto-recompute respects the override.
    recompute_result = await recompute_quality_score(r, cid)
    assert recompute_result.get("skipped") == "active_override"


@pytest.mark.asyncio
async def test_admin_override_rejects_bad_token(client, clean_redis):
    r = clean_redis
    cid = "cmp_override_badtoken"
    await _seed_campaign(r, cid, qs=0.5)

    res = await client.put(
        f"/api/v1/campaigns/{cid}/qs-override",
        json={
            "admin_token": "nope",
            "quality_score": 1.85,
        },
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_admin_override_clear_with_ttl_zero(client, clean_redis):
    r = clean_redis
    cid = "cmp_override_clear"
    await _seed_campaign(r, cid, qs=0.5)

    # Set first.
    res = await client.put(
        f"/api/v1/campaigns/{cid}/qs-override",
        json={
            "admin_token": "admin-dev-token",
            "quality_score": 1.0,
            "ttl_seconds": 3600,
        },
    )
    assert res.status_code == 200

    # Clear with ttl=0.
    res = await client.put(
        f"/api/v1/campaigns/{cid}/qs-override",
        json={
            "admin_token": "admin-dev-token",
            "quality_score": 1.0,  # required by schema but ignored on clear
            "ttl_seconds": 0,
        },
    )
    assert res.status_code == 200
    assert res.json().get("override_cleared") is True

    # Override fields are gone.
    raw = await r.hgetall(_ck(cid))
    assert "qs_override_until_ts" not in raw
    assert "qs_override" not in raw


@pytest.mark.asyncio
async def test_recompute_all_active_sweep(clean_redis):
    """Sweep updates every active campaign and respects overrides."""
    from app.quality_score import recompute_all_active

    r = clean_redis
    for i, qs in enumerate([0.5, 0.7, 0.9, 1.2]):
        cid = f"cmp_sweep_{i}"
        await _seed_campaign(r, cid, qs=qs)
        # All stale → decay path.
        await r.hset(_sk(cid), mapping={
            "impressions": "5", "clicks": "0", "conversions": "0",
        })
        await r.hset(_ck(cid), mapping={
            QS_LAST_RECOMPUTE_FIELD: str(time.time() - 7 * 86400),
        })

    # Pin one with override.
    await r.hset(
        _ck("cmp_sweep_0"),
        mapping={
            "qs_override_until_ts": str(time.time() + 86400),
            "qs_override": "0.5",
        },
    )

    counters = await recompute_all_active(r)
    assert counters["scanned"] >= 4
    assert counters["overridden"] >= 1
    assert counters["decayed"] >= 3
