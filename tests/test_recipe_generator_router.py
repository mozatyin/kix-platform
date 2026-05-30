"""Recipe generator router — match-library, catalog, brand recipes."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_match_library_returns_structure(client, clean_redis):
    res = await client.post(
        "/api/v1/recipe-gen/match-library",
        json={"description": "loyalty program for boba shop"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "matches" in body
    assert "count" in body


@pytest.mark.asyncio
async def test_match_library_missing_description_422(client, clean_redis):
    res = await client.post("/api/v1/recipe-gen/match-library", json={})
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_match_library_description_too_short_422(client, clean_redis):
    res = await client.post(
        "/api/v1/recipe-gen/match-library",
        json={"description": "x"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_catalog(client, clean_redis):
    res = await client.get("/api/v1/recipe-gen/catalog")
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_brand_recipes_empty(client, clean_redis):
    res = await client.get("/api/v1/recipe-gen/brands/b_empty/recipes")
    assert res.status_code == 200
