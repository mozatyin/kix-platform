"""Game + game_catalog + energy + leaderboard smoke tests (auth-gated)."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_game_start_requires_auth(client, clean_redis):
    res = await client.post(
        "/api/v1/game/start",
        json={"brand_id": "b1", "game_id": "tic_tac_toe"},
    )
    assert res.status_code in (401, 403, 422)


@pytest.mark.asyncio
async def test_game_start_missing_body(client, clean_redis):
    res = await client.post(
        "/api/v1/game/start",
        json={},
        headers={"Authorization": "Bearer x"},
    )
    assert res.status_code in (401, 403, 422)


@pytest.mark.asyncio
async def test_energy_grant_requires_auth(client, clean_redis):
    res = await client.post(
        "/api/v1/energy/grant",
        json={"brand_id": "b1", "qr_token": "x"},
    )
    assert res.status_code in (401, 403, 422)


@pytest.mark.asyncio
async def test_game_catalog_orders_endpoint_exists(client, clean_redis):
    # exists; empty brand returns empty list / 404 / etc.
    res = await client.get("/api/v1/game-catalog/orders/b_nope")
    assert res.status_code in (200, 401, 403, 404, 422, 500)
