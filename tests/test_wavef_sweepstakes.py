"""Tests — Wave F sweepstakes service + router."""

from __future__ import annotations

import os

import pytest

from app.main import app
from app.deps import get_current_user
from app.services import wavef_sweepstakes as svc


def _override_user(user_id: str = "u-test-1"):
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


# ── Service layer ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_service_enter_and_count(clean_redis):
    r = clean_redis
    res1 = await svc.enter(r, "cmp1", "u1")
    res2 = await svc.enter(r, "cmp1", "u2")
    assert res1["total_entries"] == 1
    assert res2["total_entries"] == 2
    assert res1["entry_id"] != res2["entry_id"]
    assert (await svc.count(r, "cmp1")) == 2


@pytest.mark.asyncio
async def test_service_draw_picks_unique_winners(clean_redis):
    r = clean_redis
    for i in range(10):
        await svc.enter(r, "cmp2", f"u{i}")
    winners = await svc.draw(r, "cmp2", n_winners=3, seed=42)
    assert len(winners) == 3
    assert len({w["entry_id"] for w in winners}) == 3
    # Winners are removed from pool.
    assert (await svc.count(r, "cmp2")) == 7
    audit = await svc.winners(r, "cmp2")
    assert len(audit) == 3


@pytest.mark.asyncio
async def test_service_draw_empty_pool(clean_redis):
    r = clean_redis
    winners = await svc.draw(r, "cmp_empty", n_winners=5)
    assert winners == []


# ── Router layer ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_enter_requires_auth(client, clean_redis):
    res = await client.post("/api/v1/wavef/sweepstakes/c1/enter")
    assert res.status_code in (401, 403)


@pytest.mark.asyncio
async def test_router_enter_and_count_via_api(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user("user-A")
    try:
        r1 = await client.post(
            "/api/v1/wavef/sweepstakes/c1/enter",
            json={"method": "voucher"},
        )
        assert r1.status_code == 200, r1.text
        body = r1.json()
        assert body["total_entries"] == 1
        assert body["method"] == "voucher"

        r2 = await client.get(
            "/api/v1/wavef/sweepstakes/c1/count",
        )
        assert r2.status_code == 200
        assert r2.json()["total_entries"] == 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_draw_requires_admin(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user("user-B")
    try:
        # seed an entry
        await client.post("/api/v1/wavef/sweepstakes/c2/enter")
        # draw without admin token -> 403
        r = await client.post("/api/v1/wavef/sweepstakes/c2/draw", json={"n_winners": 1})
        assert r.status_code == 403
        # with admin token -> 200
        admin = os.environ.get("KIX_ADMIN_TOKEN", "admin-dev-token")
        r2 = await client.post(
            "/api/v1/wavef/sweepstakes/c2/draw",
            json={"n_winners": 1, "seed": 1},
            headers={"X-Admin-Token": admin},
        )
        assert r2.status_code == 200, r2.text
        winners = r2.json()["winners"]
        assert len(winners) == 1
        assert winners[0]["user_id"] == "user-B"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
