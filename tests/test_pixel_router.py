"""Pixel router — register, snippet, stats, brand list, delete."""

from __future__ import annotations

import pytest


async def _register(client, brand="b1") -> dict:
    res = await client.post(
        "/api/v1/pixel/register",
        json={
            "brand_id": brand,
            "allowed_origins": ["https://example.com"],
        },
    )
    return res


@pytest.mark.asyncio
async def test_register_happy(client, clean_redis):
    res = await _register(client)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["pixel_id"]
    assert body["embed_snippet"]


@pytest.mark.asyncio
async def test_register_missing_brand_422(client, clean_redis):
    res = await client.post(
        "/api/v1/pixel/register",
        json={"allowed_origins": []},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_register_invalid_origin_422(client, clean_redis):
    res = await client.post(
        "/api/v1/pixel/register",
        json={
            "brand_id": "b",
            "allowed_origins": ["ftp://bad"],
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_get_snippet(client, clean_redis):
    reg = await _register(client, brand="b_snip")
    pid = reg.json()["pixel_id"]
    res = await client.get(f"/api/v1/pixel/{pid}/snippet")
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_get_stats_404(client, clean_redis):
    res = await client.get("/api/v1/pixel/px_nope/stats")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_list_brand_pixels(client, clean_redis):
    await _register(client, brand="b_l")
    res = await client.get("/api/v1/pixel/brand/b_l")
    assert res.status_code == 200
    assert isinstance(res.json(), list)
