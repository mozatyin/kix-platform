"""Tests — Wave F spin-the-wheel mechanic."""

from __future__ import annotations

import random

import pytest

from app.main import app
from app.deps import get_current_user
from app.services import wavef_spin as svc


def _override_user(user_id: str = "u-spin-1"):
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


_DEFAULT_SLICES = [
    {"label": "10% off", "weight": 50, "payload": {"voucher": "10off"}},
    {"label": "20% off", "weight": 30, "payload": {"voucher": "20off"}},
    {"label": "Try again", "weight": 20, "payload": {}},
]


# ── Service ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_read_config(clean_redis):
    r = clean_redis
    cfg = await svc.create_config(r, "b1", _DEFAULT_SLICES, daily_limit=2)
    got = await svc.get_config(r, cfg["config_id"])
    assert got["daily_limit"] == 2
    assert len(got["slices"]) == 3
    # IDs are stable, sequential.
    assert [s["id"] for s in got["slices"]] == ["slc0", "slc1", "slc2"]


@pytest.mark.asyncio
async def test_create_rejects_zero_total_weight(clean_redis):
    r = clean_redis
    slices = [{"label": "a", "weight": 0}, {"label": "b", "weight": 0}]
    with pytest.raises(ValueError):
        await svc.create_config(r, "b1", slices)


@pytest.mark.asyncio
async def test_create_rejects_single_slice(clean_redis):
    r = clean_redis
    with pytest.raises(ValueError):
        await svc.create_config(r, "b1", [{"label": "a", "weight": 1}])


def test_pick_weight_zero_never_selected():
    rng = random.Random(42)
    slices = [
        {"id": "a", "label": "A", "weight": 0},
        {"id": "b", "label": "B", "weight": 1},
        {"id": "c", "label": "C", "weight": 0},
    ]
    for _ in range(200):
        chosen = svc.pick(slices, rng)
        assert chosen["id"] == "b"


def test_pick_probability_matches_weights():
    rng = random.Random(0)
    slices = [
        {"id": "a", "label": "A", "weight": 70},
        {"id": "b", "label": "B", "weight": 20},
        {"id": "c", "label": "C", "weight": 10},
    ]
    counts = {"a": 0, "b": 0, "c": 0}
    n = 10_000
    for _ in range(n):
        counts[svc.pick(slices, rng)["id"]] += 1
    # within 5% absolute of expected probability
    assert abs(counts["a"] / n - 0.70) < 0.05
    assert abs(counts["b"] / n - 0.20) < 0.05
    assert abs(counts["c"] / n - 0.10) < 0.05


@pytest.mark.asyncio
async def test_spin_returns_slice_and_increments_count(clean_redis):
    r = clean_redis
    cfg = await svc.create_config(r, "b1", _DEFAULT_SLICES, daily_limit=5)
    res = await svc.spin(r, cfg["config_id"], "u1", seed=7)
    assert res["slice_id"].startswith("slc")
    assert res["spins_used_today"] == 1
    cnt = await svc.user_spin_count(r, cfg["config_id"], "u1")
    assert cnt == 1


@pytest.mark.asyncio
async def test_daily_limit_honored(clean_redis):
    r = clean_redis
    cfg = await svc.create_config(r, "b1", _DEFAULT_SLICES, daily_limit=2)
    await svc.spin(r, cfg["config_id"], "u1")
    await svc.spin(r, cfg["config_id"], "u1")
    with pytest.raises(ValueError) as exc:
        await svc.spin(r, cfg["config_id"], "u1")
    assert "daily_limit_exceeded" in str(exc.value)


# ── Router ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_full_spin_flow(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user("creator")
    try:
        r = await client.post(
            "/api/v1/wavef/spin/configs",
            json={
                "brand_id": "b1",
                "slices": _DEFAULT_SLICES,
                "daily_limit": 1,
            },
        )
        assert r.status_code == 200, r.text
        cid = r.json()["config_id"]

        app.dependency_overrides[get_current_user] = _override_user("u1")
        s = await client.post(f"/api/v1/wavef/spin/configs/{cid}/spin")
        assert s.status_code == 200
        body = s.json()
        assert body["slice_id"]
        # exceed daily limit
        s2 = await client.post(f"/api/v1/wavef/spin/configs/{cid}/spin")
        assert s2.status_code == 429
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_404_on_unknown_config(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        res = await client.post("/api/v1/wavef/spin/configs/missing/spin")
        assert res.status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)
