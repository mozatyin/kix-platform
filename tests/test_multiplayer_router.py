"""Multiplayer router — coop-quest, raid, squad, territory (smoke)."""

from __future__ import annotations

import pytest


async def _create_coop(client, **over) -> dict:
    payload = {"brand_id": "b1", "quest_id": "q1", "name": "Test Quest", "goal_total": 100}
    payload.update(over)
    return await client.post("/api/v1/multiplayer/coop-quest/create", json=payload)


@pytest.mark.asyncio
async def test_coop_create_happy(client, clean_redis):
    res = await _create_coop(client)
    assert res.status_code in (200, 201)
    assert "coop_id" in res.json() or "id" in res.json()


@pytest.mark.asyncio
async def test_coop_create_missing_fields_422(client, clean_redis):
    res = await client.post("/api/v1/multiplayer/coop-quest/create", json={})
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_coop_create_negative_goal_422(client, clean_redis):
    res = await _create_coop(client, goal_total=0)
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_coop_join_unknown_404(client, clean_redis):
    res = await client.post(
        "/api/v1/multiplayer/coop-quest/nope/join",
        json={"user_id": "u1"},
    )
    assert res.status_code in (400, 404)


@pytest.mark.asyncio
async def test_coop_get_404(client, clean_redis):
    res = await client.get("/api/v1/multiplayer/coop-quest/nope")
    assert res.status_code == 404
