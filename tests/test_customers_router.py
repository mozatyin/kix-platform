"""Customers router — create/get/update/by-brand + idempotency + 404."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_create_customer_happy_path(client, clean_redis):
    res = await client.post(
        "/api/v1/customers/create",
        json={"brand_id": "brand_x", "name": "Acme Corp"},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["customer_id"].startswith("cus_")
    assert body["brand_id"] == "brand_x"
    assert body["name"] == "Acme Corp"


@pytest.mark.asyncio
async def test_create_customer_missing_brand_id_422(client, clean_redis):
    res = await client.post("/api/v1/customers/create", json={"name": "X"})
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_create_customer_is_idempotent_on_brand_id(client, clean_redis):
    r1 = await client.post(
        "/api/v1/customers/create",
        json={"brand_id": "brand_idem", "name": "First"},
    )
    r2 = await client.post(
        "/api/v1/customers/create",
        json={"brand_id": "brand_idem", "name": "Second"},
    )
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["customer_id"] == r2.json()["customer_id"]


@pytest.mark.asyncio
async def test_get_customer_404_when_missing(client, clean_redis):
    res = await client.get("/api/v1/customers/cus_nonexistent")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_get_customer_returns_record(client, clean_redis):
    create = await client.post(
        "/api/v1/customers/create",
        json={"brand_id": "brand_get", "name": "Lookup"},
    )
    cid = create.json()["customer_id"]
    res = await client.get(f"/api/v1/customers/{cid}")
    assert res.status_code == 200
    assert res.json()["customer_id"] == cid


@pytest.mark.asyncio
async def test_update_customer_changes_fields(client, clean_redis):
    create = await client.post(
        "/api/v1/customers/create",
        json={"brand_id": "brand_u", "name": "Old"},
    )
    cid = create.json()["customer_id"]
    res = await client.post(
        f"/api/v1/customers/{cid}/update",
        json={"name": "New", "tax_id": "TX-123"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "New"
    assert body["tax_id"] == "TX-123"


@pytest.mark.asyncio
async def test_update_customer_404_unknown(client, clean_redis):
    res = await client.post(
        "/api/v1/customers/cus_doesnotexist/update",
        json={"name": "X"},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_get_customer_by_brand(client, clean_redis):
    create = await client.post(
        "/api/v1/customers/create",
        json={"brand_id": "brand_lookup", "name": "ByBrand"},
    )
    res = await client.get("/api/v1/customers/by-brand/brand_lookup")
    assert res.status_code == 200
    assert res.json()["customer_id"] == create.json()["customer_id"]
