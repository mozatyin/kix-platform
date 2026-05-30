"""Media router — upload, get, delete, share."""

from __future__ import annotations

import pytest


async def _upload(client, owner="u1", media_class="general"):
    return await client.post(
        "/api/v1/media/upload",
        json={
            "owner_user_id": owner,
            "brand_id": "b1",
            "media_class": media_class,
            "storage_url": "s3://bucket/foo.png",
            "content_hash": "abc123def456",
            "mime_type": "image/png",
            "size_bytes": 1024,
        },
    )


@pytest.mark.asyncio
async def test_upload_general_media_happy(client, clean_redis):
    res = await _upload(client)
    assert res.status_code == 200, res.text
    assert res.json()["media_id"]


@pytest.mark.asyncio
async def test_upload_missing_fields_422(client, clean_redis):
    res = await client.post(
        "/api/v1/media/upload",
        json={"owner_user_id": "u"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_upload_invalid_class_422(client, clean_redis):
    res = await client.post(
        "/api/v1/media/upload",
        json={
            "owner_user_id": "u",
            "brand_id": "b",
            "media_class": "BOGUS",
            "storage_url": "s3://x",
            "content_hash": "abc12345",
            "mime_type": "image/png",
            "size_bytes": 1,
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_get_media_404(client, clean_redis):
    res = await client.get("/api/v1/media/media_doesnotexist")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_get_uploaded_media(client, clean_redis):
    up = await _upload(client)
    mid = up.json()["media_id"]
    res = await client.get(f"/api/v1/media/{mid}")
    assert res.status_code == 200
    assert res.json()["media_id"] == mid


@pytest.mark.asyncio
async def test_list_owner_media(client, clean_redis):
    await _upload(client, owner="u_list")
    res = await client.get("/api/v1/media/owner/u_list")
    assert res.status_code == 200
