"""CAPI router — key mint/revoke, conversion ingest, health."""

from __future__ import annotations

import pytest

ADMIN = "kix-capi-admin-dev"


@pytest.mark.asyncio
async def test_health(client, clean_redis):
    res = await client.get("/api/v1/capi/health")
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_mint_key_requires_admin(client, clean_redis):
    res = await client.post(
        "/api/v1/capi/key",
        json={"brand_id": "b1"},
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_mint_key_with_admin_token(client, clean_redis):
    res = await client.post(
        "/api/v1/capi/key",
        json={"brand_id": "b1"},
        headers={"X-Admin-Token": ADMIN},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["brand_id"] == "b1"
    assert body["api_key"].startswith("capi_") or body["api_key"]
    assert body["rotated"] is False


@pytest.mark.asyncio
async def test_mint_key_rotation_flag(client, clean_redis):
    headers = {"X-Admin-Token": ADMIN}
    await client.post("/api/v1/capi/key", json={"brand_id": "b2"}, headers=headers)
    res = await client.post("/api/v1/capi/key", json={"brand_id": "b2"}, headers=headers)
    assert res.status_code == 200
    assert res.json()["rotated"] is True


@pytest.mark.asyncio
async def test_revoke_404_when_no_key(client, clean_redis):
    res = await client.delete(
        "/api/v1/capi/key/never_minted",
        headers={"X-Admin-Token": ADMIN},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_revoke_happy(client, clean_redis):
    headers = {"X-Admin-Token": ADMIN}
    await client.post("/api/v1/capi/key", json={"brand_id": "b3"}, headers=headers)
    res = await client.delete("/api/v1/capi/key/b3", headers=headers)
    assert res.status_code == 204


@pytest.mark.asyncio
async def test_conversion_missing_auth_401(client, clean_redis):
    import time as _t
    res = await client.post(
        "/api/v1/capi/conversion",
        json={
            "event_type": "purchase",
            "event_id": "evt_1",
            "event_time": _t.time(),
            "brand_id": "b",
            "user_data": {},
        },
    )
    assert res.status_code in (401, 403)
