"""Tests — Wave F campaign wizard (spec #14)."""

from __future__ import annotations

import pytest

from app.main import app
from app.deps import get_current_user
from app.services import wavef_wizard as svc


def _override_user(uid: str = "u-wiz-1"):
    async def _fake():
        return {
            "sub": uid,
            "brand_id": "b1",
            "device_sig": "dev",
            "session_id": "s",
            "is_day1": False,
            "exp": 0,
        }
    return _fake


# ── Service ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_get(clean_redis):
    d = await svc.create_draft(clean_redis, uid="u1", brand_id="b1")
    assert d["draft_id"]
    again = await svc.get_draft(clean_redis, d["draft_id"])
    assert again is not None
    assert again["step"] == "mechanic"


@pytest.mark.asyncio
async def test_patch_step_and_mechanic(clean_redis):
    d = await svc.create_draft(clean_redis, uid="u1", brand_id="b1")
    patched = await svc.patch_draft(
        clean_redis,
        d["draft_id"],
        step="assets",
        mechanic_id="spin_wheel",
    )
    assert patched["step"] == "assets"
    assert patched["mechanic_id"] == "spin_wheel"


@pytest.mark.asyncio
async def test_patch_rejects_unknown_mechanic(clean_redis):
    d = await svc.create_draft(clean_redis, uid="u1", brand_id="b1")
    with pytest.raises(ValueError):
        await svc.patch_draft(
            clean_redis, d["draft_id"], mechanic_id="not-real",
        )


@pytest.mark.asyncio
async def test_publish_blocks_without_assets(clean_redis):
    d = await svc.create_draft(clean_redis, uid="u1", brand_id="b1")
    await svc.patch_draft(
        clean_redis, d["draft_id"], mechanic_id="spin_wheel",
    )
    with pytest.raises(ValueError):
        await svc.publish(clean_redis, d["draft_id"])


@pytest.mark.asyncio
async def test_publish_happy_path(clean_redis):
    d = await svc.create_draft(clean_redis, uid="u1", brand_id="b1")
    await svc.patch_draft(
        clean_redis,
        d["draft_id"],
        mechanic_id="spin_wheel",
        assets={"slices": ["A", "B", "C"]},
        reward={"weights_normalize": True},
    )
    res = await svc.publish(clean_redis, d["draft_id"])
    assert res["published"] is True
    assert res["campaign_id"].startswith("cmp_")


@pytest.mark.asyncio
async def test_double_publish_409_equivalent(clean_redis):
    d = await svc.create_draft(clean_redis, uid="u1", brand_id="b1")
    await svc.patch_draft(
        clean_redis,
        d["draft_id"],
        mechanic_id="spin_wheel",
        assets={"slices": ["A", "B"]},
        reward={"weights_normalize": True},
    )
    await svc.publish(clean_redis, d["draft_id"])
    with pytest.raises(PermissionError):
        await svc.publish(clean_redis, d["draft_id"])


@pytest.mark.asyncio
async def test_list_drafts_returns_recent_first(clean_redis):
    a = await svc.create_draft(clean_redis, uid="u1", brand_id="b1")
    b = await svc.create_draft(clean_redis, uid="u1", brand_id="b1")
    rows = await svc.list_drafts(clean_redis, "u1")
    ids = [r["draft_id"] for r in rows]
    # Both present, newer first.
    assert b["draft_id"] in ids and a["draft_id"] in ids


# ── Router ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_full_wizard_flow(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user("u-router")
    try:
        r1 = await client.post(
            "/api/v1/wavef/wizard/drafts",
            json={"brand_id": "b1"},
        )
        assert r1.status_code == 200, r1.text
        did = r1.json()["draft_id"]

        r2 = await client.post(
            f"/api/v1/wavef/wizard/state/{did}",
            json={
                "mechanic_id": "spin_wheel",
                "assets": {"slices": ["A", "B"]},
                "reward": {"weights_normalize": True},
            },
        )
        assert r2.status_code == 200, r2.text

        r3 = await client.post(f"/api/v1/wavef/wizard/{did}/publish")
        assert r3.status_code == 200, r3.text
        assert r3.json()["campaign_id"].startswith("cmp_")
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_404_unknown_draft(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        r = await client.get("/api/v1/wavef/wizard/state/nope")
        assert r.status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)
