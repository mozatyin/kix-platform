"""Tests for the wallet → auction budget backpressure path.

Sim feedback (sg-marketplace 30-day): when a brand's daily wallet cap
fired ``daily_budget_exceeded``, the auction kept awarding impressions
to that brand. Every awarded impression then tried to charge → 402 →
log flood. This suite locks in the three-part fix:

  1. wallet.charge() sets ``auction:campaign:{cid}:budget_blocked`` and
     ``auction:brand:{bid}:budget_blocked`` with TTL until UTC midnight
  2. auction._has_budget() short-circuits on those flags so blocked
     campaigns are silently skipped (no further charge attempts)
  3. notification:brand:{bid}:budget_exhausted is emitted ONCE per day,
     not on every blocked charge attempt
"""

from __future__ import annotations

import json
import time
import uuid

import pytest


def _uniq(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ── Direct unit-level tests for the helpers ──────────────────────────────


def test_seconds_until_midnight_is_positive_and_bounded():
    """TTL must be positive and never exceed 86400."""
    from app.routers.wallet import _seconds_until_utc_midnight

    s = _seconds_until_utc_midnight()
    assert 0 < s <= 86_400


def test_seconds_until_midnight_at_specific_time():
    """Anchored at 23:00 UTC the TTL is exactly 3600 ± 1s."""
    from datetime import datetime, timezone

    from app.routers.wallet import _seconds_until_utc_midnight

    anchor = datetime(2026, 1, 1, 23, 0, 0, tzinfo=timezone.utc).timestamp()
    s = _seconds_until_utc_midnight(anchor)
    assert 3590 <= s <= 3610


@pytest.mark.asyncio
async def test_set_budget_blocked_sets_both_brand_and_campaign(clean_redis):
    """The helper must set both campaign + brand keys with a TTL."""
    from app.routers.wallet import (
        BUDGET_BLOCKED_BRAND_KEY,
        BUDGET_BLOCKED_CAMPAIGN_KEY,
        _set_budget_blocked,
    )

    bid = _uniq("bb")
    cid = _uniq("cam")
    first = await _set_budget_blocked(
        clean_redis,
        brand_id=bid,
        campaign_id=cid,
        reason="daily_budget_exceeded",
    )
    assert first is True

    # Both keys present with non-zero TTL.
    cam_key = BUDGET_BLOCKED_CAMPAIGN_KEY.format(cid=cid)
    brand_key = BUDGET_BLOCKED_BRAND_KEY.format(brand_id=bid)
    assert await clean_redis.exists(cam_key) == 1
    assert await clean_redis.exists(brand_key) == 1
    assert (await clean_redis.ttl(cam_key)) > 0
    assert (await clean_redis.ttl(brand_key)) > 0

    # Second call must NOT report first_block=True (idempotent across day).
    second = await _set_budget_blocked(
        clean_redis,
        brand_id=bid,
        campaign_id=cid,
        reason="daily_budget_exceeded",
    )
    assert second is False


@pytest.mark.asyncio
async def test_notification_emitted_once_per_day(clean_redis):
    """The brand-level notification must be emitted exactly once."""
    from app.routers.wallet import (
        BUDGET_EXHAUSTED_NOTIFICATION_KEY,
        _maybe_emit_budget_exhausted_notification,
    )

    bid = _uniq("notif")
    await _maybe_emit_budget_exhausted_notification(
        clean_redis, bid, "daily_budget_exceeded", campaign_id=None
    )

    notif_key = BUDGET_EXHAUSTED_NOTIFICATION_KEY.format(brand_id=bid)
    payload_raw = await clean_redis.get(notif_key)
    assert payload_raw is not None
    payload = json.loads(payload_raw)
    assert payload["brand_id"] == bid
    assert payload["reason"] == "daily_budget_exceeded"
    assert payload["unblock_at_ts"] > int(time.time())

    # Second emit must be a no-op (SET NX). Modify the payload between
    # calls — if the second call wins, the payload would be overwritten.
    await clean_redis.set(notif_key, "SENTINEL", keepttl=True)
    await _maybe_emit_budget_exhausted_notification(
        clean_redis, bid, "daily_budget_exceeded", campaign_id=None
    )
    assert await clean_redis.get(notif_key) == "SENTINEL"


# ── End-to-end: charge → block → auction skip ────────────────────────────


@pytest.mark.asyncio
async def test_charge_daily_budget_exceeded_sets_block_flag(client, clean_redis):
    """A 402 daily_budget_exceeded must set the budget-blocked flag."""
    brand_id = _uniq("brand_block")
    cid = _uniq("cam_block")

    # Topup + confirm so balance is high.
    topup = await client.post(
        f"/api/v1/wallet/{brand_id}/topup",
        json={"amount_cents": 1_000_000, "payment_method": "wechat"},
    )
    tid = topup.json()["topup_id"]
    await client.post(
        f"/api/v1/wallet/{brand_id}/topup/{tid}/confirm",
        json={"payment_gateway_response": {}},
    )

    # Tight daily cap.
    await client.post(
        f"/api/v1/wallet/{brand_id}/daily-budget",
        json={"daily_budget_cents": 100},
    )

    # First charge eats the cap.
    res1 = await client.post(
        f"/api/v1/wallet/{brand_id}/charge",
        json={
            "amount_cents": 100,
            "reason": "cpa_conversion",
            "reference_id": _uniq("ref1"),
            "campaign_id": cid,
        },
    )
    assert res1.status_code == 200, res1.text

    # Second charge trips the cap → 402 daily_budget_exceeded.
    res2 = await client.post(
        f"/api/v1/wallet/{brand_id}/charge",
        json={
            "amount_cents": 50,
            "reason": "cpa_conversion",
            "reference_id": _uniq("ref2"),
            "campaign_id": cid,
        },
    )
    assert res2.status_code == 402, res2.text
    body = res2.json()["detail"]
    assert body["error"] == "daily_budget_exceeded"

    # Both flags must now be set in Redis.
    assert await clean_redis.exists(
        f"auction:campaign:{cid}:budget_blocked"
    ) == 1
    assert await clean_redis.exists(
        f"auction:brand:{brand_id}:budget_blocked"
    ) == 1

    # Notification must be set (SET NX semantics so only the first hit).
    notif_raw = await clean_redis.get(
        f"notification:brand:{brand_id}:budget_exhausted"
    )
    assert notif_raw is not None
    payload = json.loads(notif_raw)
    assert payload["reason"] == "daily_budget_exceeded"
    assert payload["campaign_id"] == cid


@pytest.mark.asyncio
async def test_auction_has_budget_skips_blocked_campaign(clean_redis):
    """``_has_budget`` must return False when the block flag is set."""
    from app.routers.auction import _has_budget

    bid = _uniq("brand")
    cid = _uniq("cam")
    candidate = {
        "campaign_id": cid,
        "brand_id": bid,
        "max_bid_cents": "100",
        "daily_budget_cents": "0",  # no per-campaign cap → would normally pass
        "total_budget_cents": "0",
    }

    # Sanity: without the flag, candidate is eligible.
    assert await _has_budget(clean_redis, candidate) is True

    # Set the campaign-level flag → must skip.
    await clean_redis.set(
        f"auction:campaign:{cid}:budget_blocked", "daily_budget_exceeded", ex=3600
    )
    assert await _has_budget(clean_redis, candidate) is False

    # Clear and set the brand-level flag → still must skip.
    await clean_redis.delete(f"auction:campaign:{cid}:budget_blocked")
    await clean_redis.set(
        f"auction:brand:{bid}:budget_blocked",
        "daily_budget_exceeded",
        ex=3600,
    )
    assert await _has_budget(clean_redis, candidate) is False


@pytest.mark.asyncio
async def test_budget_status_endpoint_reports_blocked(client, clean_redis):
    """GET /api/v1/campaigns/{cid}/budget-status must reflect the flag."""
    # Create a real campaign first so the endpoint clears its 404 guard.
    bid = _uniq("brand_bs")
    topup = await client.post(
        f"/api/v1/wallet/{bid}/topup",
        json={"amount_cents": 100_000, "payment_method": "wechat"},
    )
    tid = topup.json()["topup_id"]
    await client.post(
        f"/api/v1/wallet/{bid}/topup/{tid}/confirm",
        json={"payment_gateway_response": {}},
    )

    create = await client.post(
        "/api/v1/campaigns/create",
        json={
            "brand_id": bid,
            "objective": "acquire",
            "name": "BudgetStatus Test",
            "bid_strategy": "cpm",
            "max_bid_cents": 100,
            "daily_budget_cents": 1000,
            "total_budget_cents": 10000,
        },
    )
    assert create.status_code in (200, 201), create.text
    cid = create.json()["campaign_id"]

    # Initially unblocked.
    res = await client.get(f"/api/v1/campaigns/{cid}/budget-status")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["campaign_id"] == cid
    assert body["blocked"] is False
    assert body["reason"] == ""

    # Set the flag and re-read.
    await clean_redis.set(
        f"auction:campaign:{cid}:budget_blocked",
        "daily_budget_exceeded",
        ex=3600,
    )
    res2 = await client.get(f"/api/v1/campaigns/{cid}/budget-status")
    body2 = res2.json()
    assert body2["blocked"] is True
    assert body2["reason"] == "daily_budget_exceeded"
    assert body2["unblock_at_ts"] is not None
    assert body2["unblock_at_ts"] > int(time.time())


@pytest.mark.asyncio
async def test_repeated_402s_dont_spam_notifications(client, clean_redis):
    """Subsequent over-cap charges must NOT re-emit the notification."""
    brand_id = _uniq("brand_spam")
    cid = _uniq("cam_spam")

    topup = await client.post(
        f"/api/v1/wallet/{brand_id}/topup",
        json={"amount_cents": 1_000_000, "payment_method": "wechat"},
    )
    tid = topup.json()["topup_id"]
    await client.post(
        f"/api/v1/wallet/{brand_id}/topup/{tid}/confirm",
        json={"payment_gateway_response": {}},
    )
    await client.post(
        f"/api/v1/wallet/{brand_id}/daily-budget",
        json={"daily_budget_cents": 50},
    )
    # First charge fits.
    await client.post(
        f"/api/v1/wallet/{brand_id}/charge",
        json={
            "amount_cents": 50,
            "reason": "cpa_conversion",
            "reference_id": _uniq("r"),
            "campaign_id": cid,
        },
    )
    # Trip the cap once and capture the notification payload.
    await client.post(
        f"/api/v1/wallet/{brand_id}/charge",
        json={
            "amount_cents": 25,
            "reason": "cpa_conversion",
            "reference_id": _uniq("r"),
            "campaign_id": cid,
        },
    )
    notif_key = f"notification:brand:{brand_id}:budget_exhausted"
    first_payload = await clean_redis.get(notif_key)
    assert first_payload is not None

    # Now smash the wallet with 5 more over-cap charge attempts. Each
    # should 402 but NOT overwrite the notification.
    for _ in range(5):
        res = await client.post(
            f"/api/v1/wallet/{brand_id}/charge",
            json={
                "amount_cents": 25,
                "reason": "cpa_conversion",
                "reference_id": _uniq("r"),
                "campaign_id": cid,
            },
        )
        assert res.status_code == 402

    # Same payload (same first_seen_at) → confirms SET NX semantics.
    final_payload = await clean_redis.get(notif_key)
    assert final_payload == first_payload


@pytest.mark.asyncio
async def test_block_flag_ttl_expires_at_midnight(clean_redis):
    """Flag TTL must approximate seconds-until-utc-midnight."""
    from app.routers.wallet import (
        BUDGET_BLOCKED_CAMPAIGN_KEY,
        _seconds_until_utc_midnight,
        _set_budget_blocked,
    )

    bid = _uniq("ttl_brand")
    cid = _uniq("ttl_cam")
    await _set_budget_blocked(
        clean_redis,
        brand_id=bid,
        campaign_id=cid,
        reason="daily_budget_exceeded",
    )
    ttl = await clean_redis.ttl(BUDGET_BLOCKED_CAMPAIGN_KEY.format(cid=cid))
    expected = _seconds_until_utc_midnight()
    # Allow ±5s slop — Redis SET ex is integer-rounded and we computed
    # expected after the call.
    assert abs(ttl - expected) <= 5, (ttl, expected)
