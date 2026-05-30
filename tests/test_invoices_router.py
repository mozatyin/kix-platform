"""Invoices router — create, get, finalize, void, list, audit."""

from __future__ import annotations

import pytest


async def _make_customer(client, brand: str = "brand_inv") -> str:
    res = await client.post(
        "/api/v1/customers/create",
        json={"brand_id": brand, "name": "Inv Co"},
    )
    return res.json()["customer_id"]


@pytest.mark.asyncio
async def test_create_invoice_happy_path(client, clean_redis):
    cid = await _make_customer(client)
    res = await client.post(
        "/api/v1/invoices/create",
        json={
            "customer_id": cid,
            "line_items": [
                {"description": "Item A", "amount_cents": 1000, "quantity": 2},
            ],
            "currency": "USD",
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["customer_id"] == cid
    assert body["subtotal_cents"] == 2000
    assert body["total_cents"] == 2000


@pytest.mark.asyncio
async def test_create_invoice_missing_line_items_422(client, clean_redis):
    cid = await _make_customer(client, brand="b2")
    res = await client.post(
        "/api/v1/invoices/create",
        json={"customer_id": cid, "line_items": []},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_create_invoice_404_unknown_customer(client, clean_redis):
    res = await client.post(
        "/api/v1/invoices/create",
        json={
            "customer_id": "cus_nope",
            "line_items": [{"description": "X", "amount_cents": 100}],
        },
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_get_invoice_404(client, clean_redis):
    res = await client.get("/api/v1/invoices/inv_doesnotexist")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_invoice_with_tax_rate(client, clean_redis):
    cid = await _make_customer(client, brand="b3")
    res = await client.post(
        "/api/v1/invoices/create",
        json={
            "customer_id": cid,
            "line_items": [
                {"description": "Taxed", "amount_cents": 10000,
                 "quantity": 1, "tax_rate_pct": 10.0},
            ],
        },
    )
    assert res.status_code == 201
    body = res.json()
    assert body["subtotal_cents"] == 10000
    assert body["tax_cents"] == 1000
    assert body["total_cents"] == 11000


@pytest.mark.asyncio
async def test_list_customer_invoices(client, clean_redis):
    cid = await _make_customer(client, brand="b4")
    await client.post(
        "/api/v1/invoices/create",
        json={
            "customer_id": cid,
            "line_items": [{"description": "Y", "amount_cents": 500}],
        },
    )
    res = await client.get(f"/api/v1/invoices/customer/{cid}")
    assert res.status_code == 200
    body = res.json()
    # Either {"invoices": [...]} or list — accept both shapes
    if isinstance(body, dict):
        assert "invoices" in body or "items" in body or "count" in body
    else:
        assert isinstance(body, list)


@pytest.mark.asyncio
async def test_invoice_amount_must_be_positive(client, clean_redis):
    cid = await _make_customer(client, brand="b5")
    res = await client.post(
        "/api/v1/invoices/create",
        json={
            "customer_id": cid,
            "line_items": [{"description": "Z", "amount_cents": 0}],
        },
    )
    assert res.status_code == 422
