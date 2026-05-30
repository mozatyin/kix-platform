"""Tests — Wave F memory-match template."""

from __future__ import annotations

import random

import pytest

from app.main import app
from app.deps import get_current_user
from app.services import wavef_memory as svc


def _override_user(user_id: str = "u-mem-1"):
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


def _solve_session_via_oracle(deck: list[int]) -> list[int]:
    """Build a flip order that solves the deck in 2 * pairs flips by
    pairing each (a, b) where deck[a]==deck[b]."""
    seen: dict[int, int] = {}
    pairs: list[tuple[int, int]] = []
    for i, sym in enumerate(deck):
        if sym in seen:
            pairs.append((seen[sym], i))
            seen.pop(sym)
        else:
            seen[sym] = i
    order: list[int] = []
    for a, b in pairs:
        order.append(a)
        order.append(b)
    return order


# ── Service ──────────────────────────────────────────────────────────────


def test_gen_deck_has_pairs():
    rng = random.Random(0)
    deck = svc.gen_deck(4, rng)
    assert len(deck) == 16
    for s in set(deck):
        assert deck.count(s) == 2


def test_gen_deck_rejects_odd_grid():
    rng = random.Random(0)
    with pytest.raises(ValueError):
        svc.gen_deck(3, rng)


@pytest.mark.asyncio
async def test_create_session_returns_masked_deck(clean_redis):
    r = clean_redis
    sess = await svc.create_session(
        r, "b1", "u1", difficulty=1, seed=42,
    )
    assert sess["grid_size"] == 4
    assert sess["deck_layout_masked"] == ["?"] * 16


@pytest.mark.asyncio
async def test_flipping_same_tile_twice_rejected(clean_redis):
    r = clean_redis
    sess = await svc.create_session(r, "b1", "u1", difficulty=1, seed=1)
    await svc.flip(r, sess["session_id"], "u1", 0)
    with pytest.raises(ValueError, match="cannot flip same tile twice"):
        await svc.flip(r, sess["session_id"], "u1", 0)


@pytest.mark.asyncio
async def test_flip_records_count_and_detects_match(clean_redis):
    r = clean_redis
    # Use seed so deck is deterministic.
    sess = await svc.create_session(r, "b1", "u1", difficulty=1, seed=7)
    # Pull deck via internal load (white-box) to find a matching pair.
    state = await svc._load_session(r, sess["session_id"])
    deck = state["deck"]
    a = 0
    b = next(i for i in range(1, len(deck)) if deck[i] == deck[0])
    res1 = await svc.flip(r, sess["session_id"], "u1", a)
    assert res1["matched"] is False
    res2 = await svc.flip(r, sess["session_id"], "u1", b)
    assert res2["matched"] is True
    assert res2["second_flip_result"] == "match"
    assert res2["flip_count"] == 2


@pytest.mark.asyncio
async def test_complete_without_all_matches_rejected(clean_redis):
    r = clean_redis
    sess = await svc.create_session(r, "b1", "u1", difficulty=1, seed=2)
    with pytest.raises(ValueError, match="not yet solved"):
        await svc.complete(r, sess["session_id"], "u1", 0, 1)


@pytest.mark.asyncio
async def test_complete_full_flow_perfect_score(clean_redis):
    r = clean_redis
    sess = await svc.create_session(r, "b1", "u1", difficulty=1, seed=99)
    state = await svc._load_session(r, sess["session_id"])
    order = _solve_session_via_oracle(state["deck"])
    flip_count = 0
    for pos in order:
        await svc.flip(r, sess["session_id"], "u1", pos)
        flip_count += 1
    res = await svc.complete(
        r, sess["session_id"], "u1", flip_count, 12345,
    )
    assert res["won"] is True
    assert res["score"] == 1000


@pytest.mark.asyncio
async def test_complete_client_count_must_match(clean_redis):
    r = clean_redis
    sess = await svc.create_session(r, "b1", "u1", difficulty=1, seed=5)
    state = await svc._load_session(r, sess["session_id"])
    order = _solve_session_via_oracle(state["deck"])
    for pos in order:
        await svc.flip(r, sess["session_id"], "u1", pos)
    with pytest.raises(ValueError, match="client flip count mismatch"):
        await svc.complete(r, sess["session_id"], "u1", 999, 1000)


# ── Router ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_create_and_flip(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user("u1")
    try:
        c = await client.post(
            "/api/v1/wavef/memory/sessions",
            json={"brand_id": "b1", "difficulty": 1},
        )
        assert c.status_code == 200, c.text
        sid = c.json()["session_id"]
        assert c.json()["grid_size"] == 4

        f = await client.post(
            f"/api/v1/wavef/memory/sessions/{sid}/flip",
            json={"position": 0},
        )
        assert f.status_code == 200
        assert f.json()["matched"] is False
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_router_403_on_other_user_flip(client, clean_redis):
    app.dependency_overrides[get_current_user] = _override_user("u1")
    try:
        c = await client.post(
            "/api/v1/wavef/memory/sessions",
            json={"brand_id": "b1", "difficulty": 1},
        )
        sid = c.json()["session_id"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    app.dependency_overrides[get_current_user] = _override_user("intruder")
    try:
        f = await client.post(
            f"/api/v1/wavef/memory/sessions/{sid}/flip",
            json={"position": 0},
        )
        assert f.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)
