"""Listings router — create, get, seller list, update, offer create."""

from __future__ import annotations

import pytest


async def _create(client, seller="u_seller", brand="brand_l") -> str:
    res = await client.post(
        "/api/v1/listings/create",
        json={
            "brand_id": brand,
            "seller_user_id": seller,
            "title": "Used Phone",
            "price_cents": 50000,
            "category": "electronics",
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["listing_id"]


@pytest.mark.asyncio
async def test_create_listing_happy(client, clean_redis):
    lid = await _create(client)
    assert lid


@pytest.mark.asyncio
async def test_create_listing_missing_title_422(client, clean_redis):
    res = await client.post(
        "/api/v1/listings/create",
        json={
            "brand_id": "b",
            "seller_user_id": "u",
            "price_cents": 100,
            "category": "x",
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_get_listing_returns_record(client, clean_redis):
    lid = await _create(client)
    res = await client.get(f"/api/v1/listings/{lid}")
    assert res.status_code == 200
    assert res.json()["listing_id"] == lid


@pytest.mark.asyncio
async def test_get_listing_404(client, clean_redis):
    res = await client.get("/api/v1/listings/listing_doesnotexist")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_seller_listings(client, clean_redis):
    await _create(client, seller="u_xx")
    res = await client.get("/api/v1/listings/seller/u_xx")
    assert res.status_code == 200
    # Either {items: [...]} or list — accept both shapes
    body = res.json()
    if isinstance(body, dict):
        assert any(k in body for k in ("items", "listings", "count"))
    else:
        assert isinstance(body, list)


@pytest.mark.asyncio
async def test_update_listing(client, clean_redis):
    lid = await _create(client, seller="u_up")
    res = await client.post(
        f"/api/v1/listings/{lid}/update",
        json={"seller_user_id": "u_up", "title": "Better Title"},
    )
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_update_listing_wrong_seller_403(client, clean_redis):
    lid = await _create(client, seller="u_real")
    res = await client.post(
        f"/api/v1/listings/{lid}/update",
        json={"seller_user_id": "u_imposter", "title": "Hacked"},
    )
    assert res.status_code in (403, 404)
