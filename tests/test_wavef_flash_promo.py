"""Tests — Wave F flash-promo service + router."""

from __future__ import annotations

import time

import pytest

from app.main import app
from app.deps import get_current_user
from app.services import wavef_flash_promo as svc


def _override_user(user_id: str = "u-flash-1"):
    async def _fake():
        return {
            "sub": user_id,
            "brand_id": "b-flash",
            "device_sig": "dev",
            "session_id": "s",
            "is_day1": False,
            "exp": 0,
        }
    return _fake


# ── Service ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_get_window(clean_redis):
    r = clean_redis
    now = int(time.time())
    res = await svc.create_window(
        r, brand_id="b1", campaign_id="c1",
        starts_at=now, duration_s=600,
        bonus_payload={"mult": 2},
    )
    fetched = await svc.get_window(r, res["window_id"])
    assert fetched is not None
    assert fetched["ends_at"] == now + 600
    assert fetched["bonus_payload"] == {"mult": 2}


@pytest.mark.asyncio
async def test_active_windows_filters_by_now(clean_redis):
    r = clean_redis
    now = int(time.time())
    # past, expired
    await svc.create_window(
        r, brand_id="b2", campaign_id="c", starts_at=now - 1000, duration_s=100,
    )
    # current
    w_now = await svc.create_window(
        r, brand_id="b2", campaign_id="c", starts_at=now - 50, duration_s=200,
    )
    # future
    await svc.create_window(
        r, brand_id="b2", campaign_id="c", starts_at=now + 100, duration_s=100,
    )
    active = await svc.active_windows(r, "b2", now=now)
    assert len(active) == 1
    assert active[0]["window_id"] == w_now["window_id"]


@pytest.mark.asyncio
async def test_claim_inside_window_then_duplicate(clean_redis):
    r = clean_redis
    now = int(time.time())
    w = await svc.create_window(
        r, brand_id="b3", campaign_id="c", starts_at=now - 10, duration_s=300,
        bonus_payload={"prize": "free-coffee"},
    )
    res = await svc.claim(r, w["window_id"], "u-A", now=now)
    assert res["bonus_payload"]["prize"] == "free-coffee"
    with pytest.raises(svc.AlreadyClaimed):
        await svc.claim(r, w["window_id"], "u-A", now=now)


@pytest.mark.asyncio
async def test_claim_outside_window_raises(clean_redis):
    r = clean_redis
    now = int(time.time())
    w = await svc.create_window(
        r, brand_id="b4", campaign_id="c", starts_at=now + 1000, duration_s=60,
    )
    with pytest.raises(svc.OutOfWindow):
        await svc.claim(r, w["window_id"], "u-B", now=now)


@pytest.mark.asyncio
async def test_claim_unknown_window_raises(clean_redis):
    r = clean_redis
    with pytest.raises(svc.WindowNotFound):
        await svc.claim(r, "no-such-wid", "u-C")


@pytest.mark.asyncio
async def test_claim_count_increments(clean_redis):
    r = clean_redis
    now = int(time.time())
    w = await svc.create_window(
        r, brand_id="b5", campaign_id="c", starts_at=now - 10, duration_s=60,
    )
    await svc.claim(r, w["window_id"], "u1", now=now)
    await svc.claim(r, w["window_id"], "u2", now=now)
    assert await svc.claim_count(r, w["window_id"]) == 2


# ── Router ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_create_and_active(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        now = int(time.time())
        r1 = await client.post(
            "/api/v1/wavef/flash/windows",
            json={
                "brand_id": "rb1",
                "campaign_id": "rc1",
                "starts_at": now - 5,
                "duration_s": 600,
                "bonus_payload": {"mult": 3},
            },
        )
        assert r1.status_code == 200, r1.text
        r2 = await client.get(
            "/api/v1/wavef/flash/active", params={"brand_id": "rb1"}
        )
        assert r2.status_code == 200
        body = r2.json()
        assert body["count"] >= 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_claim_403_outside_409_duplicate(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user("u-rt")
    try:
        now = int(time.time())
        # future window
        r1 = await client.post(
            "/api/v1/wavef/flash/windows",
            json={
                "brand_id": "rb2", "campaign_id": "c",
                "starts_at": now + 1000, "duration_s": 60,
            },
        )
        wid_future = r1.json()["window_id"]
        r2 = await client.post(
            f"/api/v1/wavef/flash/{wid_future}/claim"
        )
        assert r2.status_code == 403

        # current window
        r3 = await client.post(
            "/api/v1/wavef/flash/windows",
            json={
                "brand_id": "rb2", "campaign_id": "c",
                "starts_at": now - 5, "duration_s": 300,
            },
        )
        wid_now = r3.json()["window_id"]
        ok = await client.post(f"/api/v1/wavef/flash/{wid_now}/claim")
        assert ok.status_code == 200
        dup = await client.post(f"/api/v1/wavef/flash/{wid_now}/claim")
        assert dup.status_code == 409
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_get_window_404(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        r = await client.get("/api/v1/wavef/flash/no-such-wid")
        assert r.status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)
