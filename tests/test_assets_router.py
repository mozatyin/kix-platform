"""Assets router — upload, list, get, serve smoke."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_upload_missing_form_fields_422(client, clean_redis):
    res = await client.post("/api/v1/assets/upload")
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_upload_from_url_missing_body_422(client, clean_redis):
    res = await client.post("/api/v1/assets/upload-from-url", json={})
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_get_asset_404(client, clean_redis):
    res = await client.get("/api/v1/assets/asset_doesnotexist")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_serve_asset_404(client, clean_redis):
    res = await client.get("/api/v1/assets/asset_doesnotexist/serve")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_list_brand_assets_empty(client, clean_redis):
    res = await client.get("/api/v1/assets/brand/no_such_brand")
    assert res.status_code == 200
