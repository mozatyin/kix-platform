"""Tests — Wave F refer-friend both-win mechanic."""

from __future__ import annotations

import pytest

from app.main import app
from app.deps import get_current_user
from app.services import wavef_referral as svc


def _override_user(user_id: str):
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
async def test_invite_generates_token_and_share_url(clean_redis):
    r = clean_redis
    res = await svc.create_invite(r, "alice", "b1")
    assert res["invite_token"]
    assert res["share_url"].endswith(res["invite_token"])


@pytest.mark.asyncio
async def test_invite_reuses_outstanding_open_invite(clean_redis):
    r = clean_redis
    res1 = await svc.create_invite(r, "alice", "b1")
    res2 = await svc.create_invite(r, "alice", "b1")
    assert res1["invite_token"] == res2["invite_token"]


@pytest.mark.asyncio
async def test_accept_records_inviter(clean_redis):
    r = clean_redis
    inv = await svc.create_invite(r, "alice", "b1")
    ok = await svc.accept_invite(r, inv["invite_token"], "bob")
    assert ok["accepted"] is True
    assert ok["already_pending"] is False
    s = await svc.stats(r, "alice")
    assert s["accepted"] == 1


@pytest.mark.asyncio
async def test_accept_rejects_self_invite(clean_redis):
    r = clean_redis
    inv = await svc.create_invite(r, "alice", "b1")
    with pytest.raises(ValueError):
        await svc.accept_invite(r, inv["invite_token"], "alice")


@pytest.mark.asyncio
async def test_accept_unknown_token_rejected(clean_redis):
    r = clean_redis
    with pytest.raises(ValueError):
        await svc.accept_invite(r, "deadbeef", "bob")


@pytest.mark.asyncio
async def test_complete_triggers_double_voucher(clean_redis):
    r = clean_redis
    inv = await svc.create_invite(r, "alice", "b1")
    await svc.accept_invite(r, inv["invite_token"], "bob")
    res = await svc.on_referee_complete(r, "b1", "bob")
    assert res["vouchered"] is True
    assert res["inviter_user_id"] == "alice"
    assert res["referee_user_id"] == "bob"
    s = await svc.stats(r, "alice")
    assert s["completed"] == 1
    assert s["earned_voucher_count"] == 1


@pytest.mark.asyncio
async def test_complete_is_idempotent(clean_redis):
    r = clean_redis
    inv = await svc.create_invite(r, "alice", "b1")
    await svc.accept_invite(r, inv["invite_token"], "bob")
    r1 = await svc.on_referee_complete(r, "b1", "bob")
    r2 = await svc.on_referee_complete(r, "b1", "bob")
    assert r1["vouchered"] is True
    assert r2["vouchered"] is False
    assert r2["reason"] == "already_vouchered"


@pytest.mark.asyncio
async def test_complete_without_pending_invite(clean_redis):
    r = clean_redis
    res = await svc.on_referee_complete(r, "b1", "ghost")
    assert res["vouchered"] is False
    assert res["reason"] == "no_pending_invite"


# ── Router ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_full_referral_flow(client, clean_redis):
    # Alice creates invite.
    app.dependency_overrides[get_current_user] = _override_user("alice")
    try:
        r = await client.post(
            "/api/v1/wavef/referral/invite", json={"brand_id": "b1"},
        )
        assert r.status_code == 200, r.text
        token = r.json()["invite_token"]

        # Bob accepts.
        app.dependency_overrides[get_current_user] = _override_user("bob")
        a = await client.post(
            "/api/v1/wavef/referral/accept", json={"invite_token": token},
        )
        assert a.status_code == 200
        assert a.json()["accepted"] is True

        # Bob completes a first game (system callback).
        c = await client.post(
            "/api/v1/wavef/referral/complete", json={"brand_id": "b1"},
        )
        assert c.status_code == 200
        body = c.json()
        assert body["vouchered"] is True
        assert body["inviter_user_id"] == "alice"

        # Stats reflect Alice's completion.
        app.dependency_overrides[get_current_user] = _override_user("alice")
        s = await client.get("/api/v1/wavef/referral/alice/stats")
        assert s.status_code == 200
        assert s.json()["completed"] == 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_invite_requires_brand(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user("alice")
    try:
        r = await client.post(
            "/api/v1/wavef/referral/invite", json={"brand_id": ""},
        )
        assert r.status_code in (400, 422)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
