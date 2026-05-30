"""Tutorials router — from-recipe, advance, skip smoke."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_from_recipe_missing_fields_422(client, clean_redis):
    res = await client.post("/api/v1/tutorials/from-recipe", json={})
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_get_tutorial_404(client, clean_redis):
    res = await client.get("/api/v1/tutorials/tut_doesnotexist")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_advance_unknown_tutorial_404(client, clean_redis):
    res = await client.post(
        "/api/v1/tutorials/tut_nope/advance",
        json={},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_skip_unknown_tutorial_404(client, clean_redis):
    res = await client.post("/api/v1/tutorials/tut_nope/skip")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_from_recipe_invalid_language_422(client, clean_redis):
    res = await client.post(
        "/api/v1/tutorials/from-recipe",
        json={"brand_id": "b1", "recipe_id": "r1", "language": "fr"},
    )
    assert res.status_code == 422
