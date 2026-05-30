"""Tests — Wave F scratch-card mechanic."""

from __future__ import annotations

import random

import pytest

from app.main import app
from app.deps import get_current_user
from app.services import wavef_scratch as svc


def _override_user(user_id: str = "u-scratch-1"):
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
async def test_create_and_read_config(clean_redis):
    r = clean_redis
    cfg = await svc.create_config(r, "b1", 0.5, win_payload={"v": 1})
    got = await svc.get_config(r, cfg["config_id"])
    assert got["win_probability"] == 0.5
    assert len(got["symbol_pool"]) >= 6


@pytest.mark.asyncio
async def test_create_rejects_bad_probability(clean_redis):
    r = clean_redis
    with pytest.raises(ValueError):
        await svc.create_config(r, "b1", 0)
    with pytest.raises(ValueError):
        await svc.create_config(r, "b1", 1.5)


def test_win_grids_always_have_three_of_a_kind():
    rng = random.Random(0)
    pool = ["a", "b", "c", "d", "e", "f", "g", "h"]
    for _ in range(200):
        grid = svc.generate_grid(rng, pool, win=True)
        assert len(grid) == 9
        # at least one symbol appears exactly 3+ times
        max_count = max(grid.count(s) for s in set(grid))
        assert max_count >= 3


def test_lose_grids_never_have_three_of_a_kind():
    rng = random.Random(1)
    pool = ["a", "b", "c", "d", "e", "f", "g", "h"]
    for _ in range(200):
        grid = svc.generate_grid(rng, pool, win=False)
        assert len(grid) == 9
        max_count = max(grid.count(s) for s in set(grid))
        assert max_count <= 2


@pytest.mark.asyncio
async def test_issue_card_stores_outcome(clean_redis):
    r = clean_redis
    cfg = await svc.create_config(r, "b1", 1.0, win_payload={"v": 5})
    issued = await svc.issue_card(r, cfg["config_id"], "u1")
    assert issued["card_id"]
    assert issued["grid_masked"] == ["?"] * 9


@pytest.mark.asyncio
async def test_reveal_is_idempotent(clean_redis):
    r = clean_redis
    cfg = await svc.create_config(r, "b1", 1.0, win_payload={"v": 5})
    issued = await svc.issue_card(r, cfg["config_id"], "u1")
    res1 = await svc.reveal_card(r, issued["card_id"], "u1")
    res2 = await svc.reveal_card(r, issued["card_id"], "u1")
    assert res1["grid"] == res2["grid"]
    assert res1["won"] == res2["won"]
    assert res1["won"] is True  # win_probability=1.0


@pytest.mark.asyncio
async def test_reveal_rejects_wrong_user(clean_redis):
    r = clean_redis
    cfg = await svc.create_config(r, "b1", 1.0)
    issued = await svc.issue_card(r, cfg["config_id"], "u1")
    with pytest.raises(ValueError, match="not your card"):
        await svc.reveal_card(r, issued["card_id"], "u2")


@pytest.mark.asyncio
async def test_reveal_unknown_card(clean_redis):
    r = clean_redis
    with pytest.raises(ValueError, match="card not found"):
        await svc.reveal_card(r, "nope", "u1")


# ── Router ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_full_scratch_flow(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user("creator")
    try:
        r = await client.post(
            "/api/v1/wavef/scratch/configs",
            json={
                "brand_id": "b1",
                "win_probability": 1.0,
                "win_payload": {"voucher": "20OFF"},
            },
        )
        assert r.status_code == 200, r.text
        cid = r.json()["config_id"]

        # User issues a card.
        app.dependency_overrides[get_current_user] = _override_user("u1")
        c = await client.post(
            "/api/v1/wavef/scratch/cards", json={"config_id": cid},
        )
        assert c.status_code == 200
        card_id = c.json()["card_id"]
        assert c.json()["grid_masked"] == ["?"] * 9

        rev = await client.post(
            f"/api/v1/wavef/scratch/cards/{card_id}/reveal",
        )
        assert rev.status_code == 200
        body = rev.json()
        assert body["won"] is True
        assert body["payload"]["voucher"] == "20OFF"
        assert len(body["grid"]) == 9
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_404_on_missing_config(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user("u1")
    try:
        res = await client.post(
            "/api/v1/wavef/scratch/cards", json={"config_id": "missing"},
        )
        assert res.status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)
