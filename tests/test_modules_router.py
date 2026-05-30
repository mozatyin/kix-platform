"""Modules router — roulette, league smoke."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_roulette_configure_happy(client, clean_redis):
    res = await client.post(
        "/api/v1/modules/roulette/configure",
        json={
            "brand_id": "b1",
            "wheel": [
                {"label": "Slot A", "weight": 1.0, "reward_type": "energy"},
                {"label": "Slot B", "weight": 1.0, "reward_type": "xp"},
            ],
        },
    )
    assert res.status_code == 200, res.text


@pytest.mark.asyncio
async def test_roulette_configure_min_slots_422(client, clean_redis):
    res = await client.post(
        "/api/v1/modules/roulette/configure",
        json={
            "brand_id": "b",
            "wheel": [{"label": "X", "weight": 1.0}],
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_roulette_get_config_404(client, clean_redis):
    res = await client.get("/api/v1/modules/roulette/config/b_unset")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_roulette_spin_unconfigured_404(client, clean_redis):
    res = await client.post(
        "/api/v1/modules/roulette/spin",
        json={"user_id": "u1", "brand_id": "b_unset"},
    )
    assert res.status_code == 404
