"""Tests — Wave F game-speed/difficulty service + router."""

from __future__ import annotations

import pytest

from app.main import app
from app.deps import get_current_user
from app.services import wavef_game_speed as svc


def _override_user():
    async def _fake():
        return {
            "sub": "u-gs",
            "brand_id": "b-gs",
            "device_sig": "dev",
            "session_id": "s",
            "is_day1": False,
            "exp": 0,
        }
    return _fake


# ── Service ──────────────────────────────────────────────────────────────


def test_clamp_difficulty_normalises():
    assert svc.clamp_difficulty(1) == 1
    assert svc.clamp_difficulty(5) == 5
    assert svc.clamp_difficulty(0) == 1
    assert svc.clamp_difficulty(99) == 5
    assert svc.clamp_difficulty(None) == svc.DIFFICULTY_DEFAULT
    assert svc.clamp_difficulty("garbage") == svc.DIFFICULTY_DEFAULT


def test_win_probability_spec_table_holds():
    # Spec § per-template interpretation guarantees:
    assert svc.win_probability("spin_wheel", 1) == pytest.approx(0.60)
    assert svc.win_probability("spin_wheel", 5) == pytest.approx(0.08)
    assert svc.win_probability("scratch_card", 1) == pytest.approx(0.50)
    assert svc.win_probability("scratch_card", 5) == pytest.approx(0.04)


def test_win_rates_strictly_decrease_with_difficulty():
    for tpl in ("spin_wheel", "scratch_card"):
        rates = [svc.win_probability(tpl, d) for d in range(1, 6)]
        assert rates == sorted(rates, reverse=True)


def test_difficulty_1_vs_5_statistically_distinct_per_spec():
    # Spec §29 — "produce statistically different win rates."
    delta = svc.win_probability("spin_wheel", 1) - svc.win_probability(
        "spin_wheel", 5
    )
    assert delta > 0.3  # 0.60 - 0.08 = 0.52


def test_default_3_used_when_unspecified():
    # Spec §30 — "Default 3 used when unspecified."
    assert svc.clamp_difficulty(None) == 3
    assert svc.win_probability("spin_wheel", None) == pytest.approx(0.25)


def test_template_params_memory_match_grid_grows():
    g1 = svc.template_params("memory_match", 1)["grid"]
    g5 = svc.template_params("memory_match", 5)["grid"]
    assert g1 == [4, 4]
    assert g5 == [6, 6]
    # Total cells grow strictly with difficulty.
    cells = [
        svc.template_params("memory_match", d)["grid"][0]
        * svc.template_params("memory_match", d)["grid"][1]
        for d in range(1, 6)
    ]
    assert cells == sorted(cells)


def test_template_params_reaction_time_shrinks():
    w1 = svc.template_params("reaction_time", 1)["window_ms"]
    w5 = svc.template_params("reaction_time", 5)["window_ms"]
    assert w1 == 800
    assert w5 == 250
    assert w1 > w5


def test_template_params_trivia_grows_options_and_adds_timer():
    t1 = svc.template_params("trivia", 1)
    t5 = svc.template_params("trivia", 5)
    assert t1["options"] == 3
    assert t1["timer_s"] is None
    assert t5["options"] == 5
    assert t5["timer_s"] == 10


def test_template_params_unknown_template_returns_empty():
    assert svc.template_params("no-such-template", 3) == {}


@pytest.mark.asyncio
async def test_set_and_get_difficulty(clean_redis):
    r = clean_redis
    assert (await svc.get_difficulty(r, "c1")) == svc.DIFFICULTY_DEFAULT
    await svc.set_difficulty(r, "c1", 5)
    assert (await svc.get_difficulty(r, "c1")) == 5


@pytest.mark.asyncio
async def test_resolve_session(clean_redis):
    r = clean_redis
    await svc.set_difficulty(r, "c2", 4)
    res = await svc.resolve_session(r, "c2", "spin_wheel")
    assert res["difficulty"] == 4
    assert res["win_probability"] == pytest.approx(0.15)


# ── Router ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_put_and_get(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        r1 = await client.put(
            "/api/v1/wavef/game-speed/cmp-A", json={"difficulty": 5}
        )
        assert r1.status_code == 200, r1.text
        r2 = await client.get("/api/v1/wavef/game-speed/cmp-A")
        assert r2.json()["difficulty"] == 5
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_validates_range(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        r = await client.put(
            "/api/v1/wavef/game-speed/cmp-X", json={"difficulty": 99}
        )
        assert r.status_code == 422
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_resolve(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        await client.put(
            "/api/v1/wavef/game-speed/cmp-R", json={"difficulty": 1}
        )
        r = await client.get(
            "/api/v1/wavef/game-speed/cmp-R/resolve",
            params={"template": "spin_wheel"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["difficulty"] == 1
        assert body["win_probability"] == pytest.approx(0.60)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_templates_listing(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user()
    try:
        r = await client.get("/api/v1/wavef/game-speed/_/templates")
        assert r.status_code == 200
        names = r.json()["templates"]
        for required in (
            "spin_wheel", "scratch_card", "memory_match",
            "reaction_time", "trivia",
        ):
            assert required in names
    finally:
        app.dependency_overrides.pop(get_current_user, None)
