"""Geofence router — store register, list, delete, nearby."""

from __future__ import annotations

import pytest


async def _register(client, store_id: str = "store_1", brand: str = "b1") -> None:
    res = await client.post(
        "/api/v1/geofence/stores/register",
        json={
            "brand_id": brand,
            "store_id": store_id,
            "name": "Acme Cafe",
            "lat": 37.7749,
            "lng": -122.4194,
            "radius_meters": 100,
        },
    )
    assert res.status_code == 200, res.text


@pytest.mark.asyncio
async def test_register_store_happy(client, clean_redis):
    await _register(client)


@pytest.mark.asyncio
async def test_register_invalid_lat_422(client, clean_redis):
    res = await client.post(
        "/api/v1/geofence/stores/register",
        json={
            "brand_id": "b",
            "store_id": "s",
            "name": "x",
            "lat": 999.0,
            "lng": 0.0,
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_list_brand_stores(client, clean_redis):
    await _register(client, store_id="s_list")
    res = await client.get("/api/v1/geofence/stores/b1")
    assert res.status_code == 200
    body = res.json()
    assert any(s["store_id"] == "s_list" for s in body)


@pytest.mark.asyncio
async def test_list_brand_stores_empty(client, clean_redis):
    res = await client.get("/api/v1/geofence/stores/no_such_brand")
    assert res.status_code == 200
    assert res.json() == []


@pytest.mark.asyncio
async def test_delete_store_404(client, clean_redis):
    res = await client.delete("/api/v1/geofence/stores/no_such_store")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_delete_store_success(client, clean_redis):
    await _register(client, store_id="s_del")
    res = await client.delete("/api/v1/geofence/stores/s_del")
    assert res.status_code == 200
    assert res.json()["deleted"] is True


@pytest.mark.asyncio
async def test_nearby_search(client, clean_redis):
    await _register(client, store_id="s_near")
    res = await client.post(
        "/api/v1/geofence/nearby",
        json={
            "device_fingerprint": "device_abc",
            "lat": 37.7749,
            "lng": -122.4194,
            "max_distance_km": 1.0,
        },
    )
    assert res.status_code == 200
