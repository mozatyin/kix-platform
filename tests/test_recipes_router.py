"""Recipes router — list, get, preview, apply, catalog reload."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_list_recipes_empty(client, clean_redis):
    res = await client.get("/api/v1/recipes/")
    assert res.status_code == 200
    body = res.json()
    assert "count" in body
    assert "recipes" in body


@pytest.mark.asyncio
async def test_get_recipe_404(client, clean_redis):
    res = await client.get("/api/v1/recipes/recipe_doesnotexist")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_preview_unknown_recipe_404(client, clean_redis):
    res = await client.post(
        "/api/v1/recipes/recipe_nope/preview",
        json={"brand_id": "b1"},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_reload_catalog(client, clean_redis):
    res = await client.get("/api/v1/recipes/_catalog/reload")
    assert res.status_code == 200
    assert res.json()["ok"] is True


@pytest.mark.asyncio
async def test_list_recipes_filter_by_category(client, clean_redis):
    res = await client.get("/api/v1/recipes/?category=loyalty")
    assert res.status_code == 200
