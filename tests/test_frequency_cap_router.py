"""Frequency cap router — check / record / status / admin config."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_check_allows_first_impression(client, clean_redis):
    res = await client.post(
        "/api/v1/frequency-cap/check",
        json={"user_id": "u1", "brand_id": "b1", "slot": "feed"},
    )
    assert res.status_code == 200
    assert res.json()["allow"] is True


@pytest.mark.asyncio
async def test_check_invalid_slot_422(client, clean_redis):
    res = await client.post(
        "/api/v1/frequency-cap/check",
        json={"user_id": "u1", "brand_id": "b1", "slot": "INVALID"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_check_requires_user_or_device(client, clean_redis):
    res = await client.post(
        "/api/v1/frequency-cap/check",
        json={"brand_id": "b1", "slot": "feed"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_record_increments_counters(client, clean_redis):
    res = await client.post(
        "/api/v1/frequency-cap/record",
        json={
            "user_id": "u1",
            "brand_id": "b1",
            "slot": "feed",
            "impression_token": "tok_1",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["brand_today"] >= 1


@pytest.mark.asyncio
async def test_user_status_empty(client, clean_redis):
    res = await client.get("/api/v1/frequency-cap/user/u_empty/status")
    assert res.status_code == 200
    body = res.json()
    assert body["global_today"] == 0


@pytest.mark.asyncio
async def test_admin_get_config(client, clean_redis):
    res = await client.get("/api/v1/frequency-cap/admin/config")
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_effective_caps_endpoint(client, clean_redis):
    res = await client.get("/api/v1/frequency-cap/effective-caps")
    assert res.status_code == 200
