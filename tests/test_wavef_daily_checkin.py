"""Tests — Wave F daily check-in."""

from __future__ import annotations

import pytest

from app.main import app
from app.deps import get_current_user
from app.services import wavef_daily_checkin as svc


def _override_user(user_id: str = "u-checkin-1"):
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


# ── Service ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_service_first_checkin_eligible(clean_redis):
    r = clean_redis
    res = await svc.check_in(r, "b1", "u1", day="2026-05-30")
    assert res["checked_in_today"] is True
    assert res["reward_eligible"] is True
    assert res["total_checkins"] == 1


@pytest.mark.asyncio
async def test_service_second_checkin_same_day_blocked(clean_redis):
    r = clean_redis
    a = await svc.check_in(r, "b1", "u1", day="2026-05-30")
    b = await svc.check_in(r, "b1", "u1", day="2026-05-30")
    assert a["reward_eligible"] is True
    assert b["reward_eligible"] is False
    assert b["checked_in_today"] is False
    # Total stays 1 — only the first counted.
    assert b["total_checkins"] == 1


@pytest.mark.asyncio
async def test_service_next_day_bumps_total(clean_redis):
    r = clean_redis
    await svc.check_in(r, "b1", "u1", day="2026-05-30")
    res = await svc.check_in(r, "b1", "u1", day="2026-05-31")
    assert res["reward_eligible"] is True
    assert res["total_checkins"] == 2


@pytest.mark.asyncio
async def test_service_status_reads_without_writing(clean_redis):
    r = clean_redis
    st = await svc.status(r, "b1", "u-new", day="2026-05-30")
    assert st["checked_in_today"] is False
    assert st["total_checkins"] == 0


# ── Router ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_requires_auth(client, clean_redis):
    res = await client.post(
        "/api/v1/wavef/daily-checkin/",
        json={"brand_id": "b1"},
    )
    assert res.status_code in (401, 403)


@pytest.mark.asyncio
async def test_router_first_checkin_returns_eligible(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user("user-X")
    try:
        res = await client.post(
            "/api/v1/wavef/daily-checkin/",
            json={"brand_id": "b1"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["checked_in_today"] is True
        assert body["reward_eligible"] is True
        # status endpoint should now reflect it
        st = await client.get(
            "/api/v1/wavef/daily-checkin/status",
            params={"brand_id": "b1"},
        )
        assert st.status_code == 200
        assert st.json()["checked_in_today"] is True
    finally:
        app.dependency_overrides.pop(get_current_user, None)
