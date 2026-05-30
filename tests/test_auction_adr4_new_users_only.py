"""ADR #4 enforcement tests — auction.py default target_audience='new_users_only'.

Bible ADR #4: "Don't buy back your own customers."
Default for new campaigns: target_audience='new_users_only' — existing
customers of the advertising brand are FILTERED OUT of the auction.

This protects merchants from accidentally paying CPA on users they
already acquired (Google/Meta default behavior).
"""
import pytest
from datetime import datetime

from app.redis_client import get_redis
from app.routers.campaigns import DEFAULT_TARGET_AUDIENCE, VALID_TARGET_AUDIENCES
from app.routers.auction import _is_existing_customer

pytestmark = pytest.mark.asyncio


async def _setup_campaign(cid, brand_id, target_audience=None):
    r = await get_redis()
    mapping = {
        "campaign_id": cid,
        "brand_id": brand_id,
        "status": "active",
        "bid_cents": "100",
        "name": f"test-{cid}",
        "quality_score": "1.0",
    }
    if target_audience is not None:
        mapping["target_audience"] = target_audience
    await r.hset(f"campaign:{cid}", mapping=mapping)


async def _mark_existing_customer(brand_id, user_id):
    r = await get_redis()
    # auction._is_existing_customer probes `brand:{bid}:users` SET (source #1)
    await r.sadd(f"brand:{brand_id}:users", user_id)


async def test_default_target_audience_is_new_users_only():
    assert DEFAULT_TARGET_AUDIENCE == "new_users_only"


async def test_valid_target_audiences_canon():
    assert VALID_TARGET_AUDIENCES == {"new_users_only", "retargeting_only", "all"}


async def test_existing_customer_detected_via_set():
    r = await get_redis()
    await _mark_existing_customer("adr4-brand-A", "u-existing")
    assert await _is_existing_customer(r, "u-existing", "adr4-brand-A") is True


async def test_unknown_user_is_not_existing_customer():
    r = await get_redis()
    assert await _is_existing_customer(r, "u-fresh-unknown", "adr4-brand-X") is False


async def test_no_user_id_returns_false():
    r = await get_redis()
    assert await _is_existing_customer(r, None, "adr4-brand-Y") is False
    assert await _is_existing_customer(r, "", "adr4-brand-Y") is False


async def test_existing_customer_isolated_per_brand():
    r = await get_redis()
    await _mark_existing_customer("adr4-brand-only-A", "u-cross")
    assert await _is_existing_customer(r, "u-cross", "adr4-brand-only-A") is True
    assert await _is_existing_customer(r, "u-cross", "adr4-brand-only-B") is False


async def test_auction_skips_existing_customer_when_default():
    """End-to-end: existing customer filtered from default campaign."""
    from app.main import app
    from httpx import ASGITransport, AsyncClient

    await _setup_campaign("adr4-c1", "adr4-brand-acq")
    await _mark_existing_customer("adr4-brand-acq", "u-loyal")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/api/v1/auction/run",
            json={"user_id": "u-loyal", "context": {"category": "f_and_b"}},
        )
    if r.status_code == 200:
        winning = r.json().get("winning_campaign_id")
        assert winning != "adr4-c1", (
            f"ADR #4 violated: campaign {winning} won for existing customer "
            f"with default target_audience"
        )


async def test_audit_skip_counter_increments_when_filter_fires():
    """Direct test of _record_existing_customer_skip — proves audit hook works."""
    from app.routers.auction import (
        _record_existing_customer_skip,
        AUCTION_SKIPPED_EXISTING_KEY,
        _today_utc,
    )
    r = await get_redis()
    key = AUCTION_SKIPPED_EXISTING_KEY.format(
        brand_id="adr4-brand-direct", date=_today_utc()
    )
    await r.delete(key)
    await _record_existing_customer_skip(r, "adr4-brand-direct")
    count = int(await r.get(key) or 0)
    assert count == 1, "Audit skip counter should increment on direct call"


async def test_invalid_target_audience_rejected_at_create():
    from app.main import app
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/api/v1/campaigns/create",
            json={
                "brand_id": "adr4-validation",
                "name": "Bad audience test",
                "bid_cents": 100,
                "target_audience": "everyone",
            },
        )
    assert r.status_code in (400, 422), (
        f"Expected 400/422 for invalid target_audience, got {r.status_code}"
    )
