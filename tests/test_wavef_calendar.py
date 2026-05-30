"""Tests — Wave F calendar daily-reveal campaign."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.main import app
from app.deps import get_current_user
from app.services import wavef_calendar as svc


def _override_user(user_id: str = "u-cal-1"):
    async def _fake():
        return {
            "sub": user_id,
            "brand_id": "b1",
            "device_sig": "dev",
            "session_id": "s",
            "is_day1": False,
            "exp": 0,
        }
    return _fake


def _make_days(n: int = 5) -> list[dict]:
    return [
        {
            "day": i,
            "item_type": "voucher",
            "payload": {"amount": i * 5},
        }
        for i in range(1, n + 1)
    ]


# ── Service ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_read_campaign(clean_redis):
    r = clean_redis
    start = date.today().strftime("%Y-%m-%d")
    cmp = await svc.create_campaign(r, "b1", "Holiday", start, _make_days(3))
    assert cmp["campaign_id"]
    got = await svc.get_campaign(r, cmp["campaign_id"])
    assert got["ttl_days"] == 3
    assert len(got["days"]) == 3


@pytest.mark.asyncio
async def test_today_reveals_current_day(clean_redis):
    r = clean_redis
    today = date.today()
    cmp = await svc.create_campaign(
        r, "b1", "X", today.strftime("%Y-%m-%d"), _make_days(3),
    )
    piece = await svc.today_piece(r, cmp["campaign_id"], today=today)
    assert piece["day"] == 1


@pytest.mark.asyncio
async def test_today_returns_none_before_start(clean_redis):
    r = clean_redis
    future = date.today() + timedelta(days=10)
    cmp = await svc.create_campaign(
        r, "b1", "X", future.strftime("%Y-%m-%d"), _make_days(3),
    )
    piece = await svc.today_piece(r, cmp["campaign_id"], today=date.today())
    assert piece is None


@pytest.mark.asyncio
async def test_today_returns_none_after_end(clean_redis):
    r = clean_redis
    past = date.today() - timedelta(days=30)
    cmp = await svc.create_campaign(
        r, "b1", "X", past.strftime("%Y-%m-%d"), _make_days(3),
    )
    piece = await svc.today_piece(r, cmp["campaign_id"], today=date.today())
    assert piece is None


@pytest.mark.asyncio
async def test_claim_once_succeeds_twice_fails(clean_redis):
    r = clean_redis
    today = date.today()
    cmp = await svc.create_campaign(
        r, "b1", "X", today.strftime("%Y-%m-%d"), _make_days(3),
    )
    res1 = await svc.claim_today(r, cmp["campaign_id"], "u1", today=today)
    assert res1["claimed"] is True
    with pytest.raises(ValueError):
        await svc.claim_today(r, cmp["campaign_id"], "u1", today=today)


@pytest.mark.asyncio
async def test_timeline_shows_revealed_days(clean_redis):
    r = clean_redis
    start = date.today() - timedelta(days=2)
    cmp = await svc.create_campaign(
        r, "b1", "X", start.strftime("%Y-%m-%d"), _make_days(5),
    )
    tl = await svc.timeline(r, cmp["campaign_id"], "u1")
    # Days 1..3 revealed (start, start+1, today)
    assert tl["today_day_index"] == 3
    assert len(tl["revealed"]) == 3
    assert all(d["claimed"] is False for d in tl["revealed"])


@pytest.mark.asyncio
async def test_duplicate_day_indices_rejected(clean_redis):
    r = clean_redis
    days = [
        {"day": 1, "item_type": "v", "payload": {}},
        {"day": 1, "item_type": "v", "payload": {}},
    ]
    with pytest.raises(ValueError):
        await svc.create_campaign(r, "b1", "X", "2026-01-01", days)


# ── Router ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_full_calendar_flow(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user("creator")
    today = date.today().strftime("%Y-%m-%d")
    try:
        r = await client.post(
            "/api/v1/wavef/calendar/campaigns",
            json={
                "brand_id": "b1",
                "name": "Holiday",
                "start_date": today,
                "days": [
                    {"day": 1, "item_type": "voucher", "payload": {"amt": 5}},
                    {"day": 2, "item_type": "voucher", "payload": {"amt": 10}},
                ],
            },
        )
        assert r.status_code == 200, r.text
        cid = r.json()["campaign_id"]

        # today
        t = await client.get(
            f"/api/v1/wavef/calendar/campaigns/{cid}/today",
        )
        assert t.status_code == 200
        assert t.json()["day"] == 1

        # claim once
        app.dependency_overrides[get_current_user] = _override_user("u1")
        c = await client.post(
            f"/api/v1/wavef/calendar/campaigns/{cid}/claim",
        )
        assert c.status_code == 200
        # claim again → 409
        c2 = await client.post(
            f"/api/v1/wavef/calendar/campaigns/{cid}/claim",
        )
        assert c2.status_code == 409

        # timeline
        tl = await client.get(
            f"/api/v1/wavef/calendar/campaigns/{cid}/timeline",
        )
        assert tl.status_code == 200
        body = tl.json()
        assert body["revealed"][0]["claimed"] is True
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_404_on_unknown_campaign(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        res = await client.get(
            "/api/v1/wavef/calendar/campaigns/does-not-exist/today",
        )
        assert res.status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)
