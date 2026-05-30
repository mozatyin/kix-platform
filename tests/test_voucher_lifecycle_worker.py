"""Tests for app.workers.voucher_lifecycle_worker.

Covers:

  1. Reminder cadence per voucher value tier ($5 / $20 / $50 / $100).
  2. Quiet-hours deferral (no notification fires between 22:00–07:00 SGT).
  3. Frequency-cap deferral.
  4. Auto grace-period extension for $20+ vouchers.
  5. No grace extension for $5–$19 vouchers.
  6. Win-back offer generation after grace lapses.
  7. Idempotency: same reminder slot only fires once per voucher.
  8. Manual extend-grace endpoint.
  9. Expiring endpoint surfaces upcoming-expiry vouchers.
  10. Win-back-offers endpoint returns recorded offers.
  11. Admin expiration-stats endpoint exposes counters.
  12. Lifecycle audit trail entries written.
"""

from __future__ import annotations

import json
import time

import pytest

from app.workers import voucher_lifecycle_worker as worker


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


async def _issue_voucher(
    client,
    redis,
    *,
    user_id: str = "user_lifecycle",
    issuer: str = "brand_lifecycle",
    value_cents: int = 500,
    expires_at: int | None = None,
):
    """Issue a voucher via the API, then forcibly overwrite expires_at on
    the Redis hash so tests can pin arbitrary (past or future) timestamps.
    The API validates expires_at > now(), so we issue with a far-future
    expiry first and overwrite after.
    """
    far_future = int(time.time()) + 365 * 86400
    payload: dict = {
        "user_id": user_id,
        "value_cents": value_cents,
        "redeemable_at": "issuer_only",
        "source": "gift",
        "expires_at": far_future,
    }
    res = await client.post(
        "/api/v1/vouchers/issue",
        params={"issuer_brand_id": issuer},
        json=payload,
    )
    assert res.status_code == 201, res.text
    vid = res.json()["voucher_id"]
    if expires_at is not None:
        await redis.hset(f"voucher:{vid}", mapping={"expires_at": str(expires_at)})
    return vid


# Use a daytime SGT timestamp (12:00 SGT = 04:00 UTC) so quiet-hours
# don't gate by default. 1735_700_000 ≈ 2025-01-01 09:53 UTC ≈ 17:53 SGT
DAY_BASE_TS = 1_735_700_000


# ──────────────────────────────────────────────────────────────────────────
# 1. Reminder cadence per voucher tier
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reminder_fires_for_5_dollar_voucher_t_minus_7d(
    client, clean_redis
):
    expires_at = DAY_BASE_TS + 7 * 86400 - 60  # ~7d ahead (inside window)
    vid = await _issue_voucher(
        client, clean_redis, value_cents=500, expires_at=expires_at,
    )
    report = await worker.run_once(clean_redis, now=DAY_BASE_TS)
    assert report["reminders_sent"] == 1
    actions = [a for a in report["actions"] if a["voucher_id"] == vid]
    assert actions and actions[0]["slot"] == "T-7d"


@pytest.mark.asyncio
async def test_reminder_cadence_20_dollar_t_minus_3d(client, clean_redis):
    expires_at = DAY_BASE_TS + 3 * 86400 - 60
    vid = await _issue_voucher(
        client, clean_redis, value_cents=2_000, expires_at=expires_at,
    )
    report = await worker.run_once(clean_redis, now=DAY_BASE_TS)
    actions = [a for a in report["actions"] if a["voucher_id"] == vid]
    assert actions and actions[0]["slot"] == "T-3d"


@pytest.mark.asyncio
async def test_reminder_cadence_50_dollar_t_minus_14d(client, clean_redis):
    # $50 voucher gets the long-tail T-14d slot that $20 vouchers don't.
    expires_at = DAY_BASE_TS + 14 * 86400 - 60
    vid = await _issue_voucher(
        client, clean_redis, value_cents=5_000, expires_at=expires_at,
    )
    report = await worker.run_once(clean_redis, now=DAY_BASE_TS)
    actions = [a for a in report["actions"] if a["voucher_id"] == vid]
    assert actions and actions[0]["slot"] == "T-14d"


@pytest.mark.asyncio
async def test_below_5_dollar_voucher_gets_no_reminder(client, clean_redis):
    expires_at = DAY_BASE_TS + 86400  # 1d ahead
    await _issue_voucher(
        client, clean_redis, value_cents=100, expires_at=expires_at,  # $1
    )
    report = await worker.run_once(clean_redis, now=DAY_BASE_TS)
    assert report["reminders_sent"] == 0


# ──────────────────────────────────────────────────────────────────────────
# 2. Quiet hours
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_quiet_hours_defers_reminder(client, clean_redis):
    # DAY_BASE_TS = 1_735_700_000 → 2025-01-01 10:53 SGT (daytime).
    # +16h → 2025-01-02 02:53 SGT → inside the 22:00–07:00 quiet window.
    sgt_quiet_ts = DAY_BASE_TS + 16 * 3600
    assert worker._is_quiet_hours(sgt_quiet_ts)
    expires_at = sgt_quiet_ts + 7 * 86400 - 60
    vid = await _issue_voucher(
        client, clean_redis, value_cents=500, expires_at=expires_at,
    )
    report = await worker.run_once(clean_redis, now=sgt_quiet_ts)
    assert report["reminders_sent"] == 0
    assert report["deferred_quiet_hours"] >= 1
    assert report["quiet_hours_active"] is True
    # Idempotency key NOT set — should fire on the next run during day.
    sent = await clean_redis.exists(f"voucher:{vid}:reminder_sent:T-7d")
    assert sent == 0


# ──────────────────────────────────────────────────────────────────────────
# 3. Frequency cap
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_frequency_cap_defers_reminder(
    client, clean_redis, monkeypatch,
):
    async def _capped(**kwargs):
        return False, {"reason": "cap_exceeded"}

    monkeypatch.setattr(
        "app.routers.frequency_cap.check_internal", _capped
    )
    expires_at = DAY_BASE_TS + 7 * 86400 - 60
    await _issue_voucher(
        client, clean_redis, value_cents=500, expires_at=expires_at,
    )
    report = await worker.run_once(clean_redis, now=DAY_BASE_TS)
    assert report["reminders_sent"] == 0
    assert report["deferred_freq_cap"] >= 1


# ──────────────────────────────────────────────────────────────────────────
# 4. Grace extension for $20+ vouchers
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_grace_extension_auto_applied_for_20_dollar(
    client, clean_redis,
):
    # Voucher expired 1h ago — within the 24h grace window.
    expires_at = DAY_BASE_TS - 3600
    vid = await _issue_voucher(
        client, clean_redis, value_cents=2_000, expires_at=expires_at + 86400,
    )
    # Force expiry by overwriting the hash directly.
    await clean_redis.hset(
        f"voucher:{vid}", mapping={"expires_at": str(expires_at)}
    )
    report = await worker.run_once(clean_redis, now=DAY_BASE_TS)
    assert report["grace_extensions"] == 1
    # Voucher's expires_at should have been extended.
    v = await clean_redis.hgetall(f"voucher:{vid}")
    new_exp = int(v["expires_at"])
    assert new_exp > expires_at
    # Grace flag set
    assert await clean_redis.exists(f"voucher:{vid}:grace_applied")


# ──────────────────────────────────────────────────────────────────────────
# 5. No grace for sub-$20 vouchers
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_grace_for_5_dollar_voucher_falls_through(
    client, clean_redis,
):
    # $5 voucher's grace_hours == 0 → should go straight to win-back.
    expires_at = DAY_BASE_TS - 3600
    vid = await _issue_voucher(
        client, clean_redis, value_cents=500, expires_at=expires_at + 86400,
    )
    await clean_redis.hset(
        f"voucher:{vid}", mapping={"expires_at": str(expires_at)}
    )
    report = await worker.run_once(clean_redis, now=DAY_BASE_TS)
    assert report["grace_extensions"] == 0
    assert report["winback_offers"] == 1
    v = await clean_redis.hgetall(f"voucher:{vid}")
    assert v["status"] == "expired"


# ──────────────────────────────────────────────────────────────────────────
# 6. Win-back offer after grace lapses
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_winback_offer_after_grace_lapses(client, clean_redis):
    # $20 voucher expired 48h ago — past the 24h grace window.
    expires_at = DAY_BASE_TS - 48 * 3600
    vid = await _issue_voucher(
        client, clean_redis,
        user_id="user_wb",
        value_cents=2_000,
        expires_at=expires_at + 86400,
    )
    await clean_redis.hset(
        f"voucher:{vid}", mapping={"expires_at": str(expires_at)}
    )
    report = await worker.run_once(clean_redis, now=DAY_BASE_TS)
    assert report["winback_offers"] == 1
    # Voucher marked expired
    v = await clean_redis.hgetall(f"voucher:{vid}")
    assert v["status"] == "expired"
    # Win-back recorded on the user's list
    offers = await clean_redis.lrange("user:user_wb:voucher_winback_offers", 0, -1)
    assert len(offers) == 1
    offer = json.loads(offers[0])
    # 50 % of $20 = $10 → 1_000 cents
    assert offer["winback_value_cents"] == 1_000
    assert offer["original_voucher_id"] == vid


# ──────────────────────────────────────────────────────────────────────────
# 7. Idempotency
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reminder_idempotent_across_runs(client, clean_redis):
    expires_at = DAY_BASE_TS + 7 * 86400 - 60
    vid = await _issue_voucher(
        client, clean_redis, value_cents=500, expires_at=expires_at,
    )
    r1 = await worker.run_once(clean_redis, now=DAY_BASE_TS)
    r2 = await worker.run_once(clean_redis, now=DAY_BASE_TS + 60)
    assert r1["reminders_sent"] == 1
    assert r2["reminders_sent"] == 0  # already fired
    # The sent flag is durable
    assert await clean_redis.exists(f"voucher:{vid}:reminder_sent:T-7d")


# ──────────────────────────────────────────────────────────────────────────
# 8. Manual extend-grace endpoint
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extend_grace_endpoint(client, clean_redis):
    expires_at = int(time.time()) + 86400
    vid = await _issue_voucher(
        client, clean_redis, value_cents=2_000, expires_at=expires_at,
    )
    res = await client.post(
        f"/api/v1/vouchers/{vid}/extend-grace",
        json={"grace_hours": 24, "reason": "user_requested"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["new_expires_at"] > body["old_expires_at"]
    assert body["new_expires_at"] - body["old_expires_at"] == 24 * 3600
    # Second call must 409
    res2 = await client.post(
        f"/api/v1/vouchers/{vid}/extend-grace",
        json={"grace_hours": 24},
    )
    assert res2.status_code == 409


# ──────────────────────────────────────────────────────────────────────────
# 9. /expiring/{user_id} endpoint
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expiring_endpoint(client, clean_redis):
    soon = int(time.time()) + 2 * 86400
    far = int(time.time()) + 60 * 86400
    vid_soon = await _issue_voucher(
        client, clean_redis, user_id="user_exp", value_cents=2_000, expires_at=soon,
    )
    await _issue_voucher(
        client, clean_redis, user_id="user_exp", value_cents=2_000, expires_at=far,
    )
    res = await client.get(
        "/api/v1/vouchers/expiring/user_exp", params={"within_days": 14},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 1
    assert body["vouchers"][0]["voucher_id"] == vid_soon


# ──────────────────────────────────────────────────────────────────────────
# 10. Win-back offers endpoint
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_winback_offers_endpoint(client, clean_redis):
    # Run the worker to mint a win-back offer first.
    expires_at = DAY_BASE_TS - 48 * 3600
    vid = await _issue_voucher(
        client, clean_redis,
        user_id="user_wbe",
        value_cents=2_000,
        expires_at=expires_at + 86400,
    )
    await clean_redis.hset(
        f"voucher:{vid}", mapping={"expires_at": str(expires_at)}
    )
    await worker.run_once(clean_redis, now=DAY_BASE_TS)
    res = await client.get(
        "/api/v1/vouchers/expired/user_wbe/winback-offers"
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 1
    assert body["offers"][0]["winback_value_cents"] == 1_000


# ──────────────────────────────────────────────────────────────────────────
# 11. Admin expiration-stats
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_expiration_stats_endpoint(client, clean_redis):
    expires_at = DAY_BASE_TS + 7 * 86400 - 60
    await _issue_voucher(
        client, clean_redis, issuer="brand_stats", value_cents=500, expires_at=expires_at,
    )
    await worker.run_once(clean_redis, now=DAY_BASE_TS)
    res = await client.get(
        "/api/v1/vouchers/admin/expiration-stats",
        params={"brand_id": "brand_stats"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["reminders_sent"] == 1
    assert body["brand_id"] == "brand_stats"


# ──────────────────────────────────────────────────────────────────────────
# 12. Audit trail
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lifecycle_audit_log_entries(client, clean_redis):
    expires_at = DAY_BASE_TS + 7 * 86400 - 60
    vid = await _issue_voucher(
        client, clean_redis, value_cents=500, expires_at=expires_at,
    )
    await worker.run_once(clean_redis, now=DAY_BASE_TS)
    audit = await clean_redis.lrange(f"voucher:{vid}:lifecycle_audit", 0, -1)
    assert audit, "expected at least one audit entry after reminder fire"
    parsed = [json.loads(x) for x in audit]
    assert any(e.get("event") == "reminder_sent" for e in parsed)
