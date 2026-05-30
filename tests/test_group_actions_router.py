"""Group actions router — group buy, atomic group, price cut create/join/get."""

from __future__ import annotations

import pytest


async def _create_buy(client, initiator="u1") -> str:
    res = await client.post(
        "/api/v1/groups/buy/create",
        json={
            "brand_id": "b1",
            "sku_id": "sku_x",
            "initiator_user_id": initiator,
        },
    )
    assert res.status_code == 200, res.text
    return res.json()["group_id"]


@pytest.mark.asyncio
async def test_buy_create_happy(client, clean_redis):
    gid = await _create_buy(client)
    assert gid


@pytest.mark.asyncio
async def test_buy_create_missing_brand_422(client, clean_redis):
    res = await client.post(
        "/api/v1/groups/buy/create",
        json={"sku_id": "x", "initiator_user_id": "u"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_buy_create_invalid_group_size_422(client, clean_redis):
    res = await client.post(
        "/api/v1/groups/buy/create",
        json={
            "brand_id": "b",
            "sku_id": "s",
            "initiator_user_id": "u",
            "group_size": 1,  # too small
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_buy_join_unknown_group_404(client, clean_redis):
    res = await client.post(
        "/api/v1/groups/buy/no_group/join",
        json={"user_id": "u2"},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_buy_join_idempotent_for_initiator(client, clean_redis):
    gid = await _create_buy(client, initiator="u1")
    res = await client.post(
        f"/api/v1/groups/buy/{gid}/join",
        json={"user_id": "u1"},
    )
    assert res.status_code == 200
    assert res.json()["already_member"] is True


@pytest.mark.asyncio
async def test_buy_get_status(client, clean_redis):
    gid = await _create_buy(client)
    res = await client.get(f"/api/v1/groups/buy/{gid}")
    assert res.status_code == 200
