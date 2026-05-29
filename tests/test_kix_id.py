"""KiX ID lifecycle tests — register, lookup, update, idempotency."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_kix_id_register_new(client, clean_redis):
    res = await client.post(
        "/api/v1/kix-id/register",
        json={
            "phone": "+8613800001111",
            "device_fingerprint": "fp_kix_new_1",
            "primary_language": "zh-CN",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["kid"].startswith("kid_")
    assert body["is_new"] is True
    assert body["created_at"] > 0


@pytest.mark.asyncio
async def test_kix_id_register_is_idempotent_by_phone(client, clean_redis):
    payload = {
        "phone": "+8613800002222",
        "device_fingerprint": "fp_kix_idem_1",
    }
    res1 = await client.post("/api/v1/kix-id/register", json=payload)
    res2 = await client.post(
        "/api/v1/kix-id/register",
        json={
            "phone": "+8613800002222",
            # different device fp → still same kid because phone matches
            "device_fingerprint": "fp_kix_idem_2",
        },
    )
    assert res1.status_code == 200
    assert res2.status_code == 200
    assert res1.json()["kid"] == res2.json()["kid"]
    assert res2.json()["is_new"] is False


@pytest.mark.asyncio
async def test_kix_id_lookup_by_phone(client, clean_redis):
    await client.post(
        "/api/v1/kix-id/register",
        json={
            "phone": "+8613800003333",
            "device_fingerprint": "fp_kix_lookup_1",
        },
    )
    res = await client.post(
        "/api/v1/kix-id/lookup",
        json={"phone": "+8613800003333"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["found"] is True
    assert body["kid"].startswith("kid_")


@pytest.mark.asyncio
async def test_kix_id_lookup_unknown_returns_not_found(client, clean_redis):
    res = await client.post(
        "/api/v1/kix-id/lookup",
        json={"phone": "+8619999999999"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["found"] is False
    assert body["kid"] is None


@pytest.mark.asyncio
async def test_kix_id_lookup_requires_one_identifier(client, clean_redis):
    res = await client.post("/api/v1/kix-id/lookup", json={})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_kix_id_register_requires_device_fingerprint(client, clean_redis):
    res = await client.post(
        "/api/v1/kix-id/register",
        json={"phone": "+8613800004444"},
    )
    # device_fingerprint is mandatory min_length=4.
    assert res.status_code == 422
