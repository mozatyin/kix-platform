"""Webhooks outbound — register, list, disable/enable, delete, supported types."""

from __future__ import annotations

import pytest


async def _register(client, brand="b1") -> dict:
    res = await client.post(
        "/api/v1/webhooks-outbound/register",
        json={
            "brand_id": brand,
            "target_url": "https://example.com/hook",
            "event_types": ["auction.won"],
        },
    )
    return res


@pytest.mark.asyncio
async def test_register_happy(client, clean_redis):
    res = await _register(client)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["webhook_id"]
    assert body["signing_secret"]


@pytest.mark.asyncio
async def test_register_invalid_event_type_422(client, clean_redis):
    res = await client.post(
        "/api/v1/webhooks-outbound/register",
        json={
            "brand_id": "b",
            "target_url": "https://example.com/h",
            "event_types": ["bogus.event"],
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_register_missing_event_types_422(client, clean_redis):
    res = await client.post(
        "/api/v1/webhooks-outbound/register",
        json={"brand_id": "b", "target_url": "https://example.com/h", "event_types": []},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_supported_event_types(client, clean_redis):
    res = await client.get("/api/v1/webhooks-outbound/event-types/supported")
    assert res.status_code == 200
    body = res.json()
    assert "auction.won" in str(body)


@pytest.mark.asyncio
async def test_get_unknown_webhook_404(client, clean_redis):
    res = await client.get("/api/v1/webhooks-outbound/wh_nope")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_list_brand_webhooks(client, clean_redis):
    await _register(client, brand="b_list")
    res = await client.get("/api/v1/webhooks-outbound/brand/b_list")
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_disable_then_enable(client, clean_redis):
    reg = await _register(client, brand="b_de")
    wid = reg.json()["webhook_id"]
    d = await client.post(f"/api/v1/webhooks-outbound/{wid}/disable")
    assert d.status_code == 200
    e = await client.post(f"/api/v1/webhooks-outbound/{wid}/enable")
    assert e.status_code == 200
