"""Leaderboard router — smoke tests for auth gate + schema."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_leaderboard_requires_auth(client, clean_redis):
    res = await client.get(
        "/api/v1/leaderboard/",
        params={"brand_id": "b1", "game_id": "g1"},
    )
    assert res.status_code in (401, 403, 422)


@pytest.mark.asyncio
async def test_leaderboard_missing_brand_id(client, clean_redis):
    res = await client.get(
        "/api/v1/leaderboard/",
        params={"game_id": "g1"},
        headers={"Authorization": "Bearer x"},
    )
    # 422 from missing query param OR 401 from invalid bearer
    assert res.status_code in (422, 401, 403)


@pytest.mark.asyncio
async def test_nearby_requires_auth(client, clean_redis):
    res = await client.get(
        "/api/v1/leaderboard/nearby",
        params={"brand_id": "b1", "game_id": "g1"},
    )
    assert res.status_code in (401, 403, 422)
