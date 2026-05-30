"""Storefront router — profile configure, fetch, follow, reviews, discover."""

from __future__ import annotations

import pytest


async def _configure(client, bid: str = "b1", display: str = "Acme Shop"):
    return await client.post(
        f"/api/v1/storefront/{bid}/configure",
        json={"display_name": display},
    )


@pytest.mark.asyncio
async def test_configure_storefront_happy(client, clean_redis):
    res = await _configure(client)
    assert res.status_code == 200, res.text


@pytest.mark.asyncio
async def test_configure_missing_display_name_422(client, clean_redis):
    res = await client.post("/api/v1/storefront/b2/configure", json={})
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_get_storefront_returns_profile(client, clean_redis):
    await _configure(client, bid="b_get", display="Get Co")
    res = await client.get("/api/v1/storefront/b_get")
    assert res.status_code == 200
    assert res.json()["display_name"] == "Get Co"


@pytest.mark.asyncio
async def test_get_storefront_404_when_missing(client, clean_redis):
    res = await client.get("/api/v1/storefront/b_missing")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_follow_then_unfollow(client, clean_redis):
    await _configure(client, bid="b_follow")
    r1 = await client.post("/api/v1/storefront/b_follow/follow", json={"user_id": "u1"})
    assert r1.status_code == 200
    count = await client.get("/api/v1/storefront/b_follow/followers/count")
    assert count.status_code == 200
    assert count.json()["count"] == 1

    r2 = await client.post("/api/v1/storefront/b_follow/unfollow", json={"user_id": "u1"})
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_add_review_valid_rating(client, clean_redis):
    await _configure(client, bid="b_rev")
    res = await client.post(
        "/api/v1/storefront/b_rev/review",
        json={"user_id": "u1", "rating": 5, "comment": "great"},
    )
    assert res.status_code == 200, res.text


@pytest.mark.asyncio
async def test_add_review_invalid_rating_422(client, clean_redis):
    await _configure(client, bid="b_rev2")
    res = await client.post(
        "/api/v1/storefront/b_rev2/review",
        json={"user_id": "u1", "rating": 6},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_discover_endpoint(client, clean_redis):
    res = await client.get("/api/v1/storefront/discover")
    assert res.status_code == 200
