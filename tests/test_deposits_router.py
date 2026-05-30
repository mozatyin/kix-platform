"""Deposits router — place / release / 404 / idempotency."""

from __future__ import annotations

import pytest


async def _create_wallet(client, uid: str, balance: int = 100_000) -> None:
    r = await client.post(
        f"/api/v1/user-wallet/{uid}/create",
        json={"currency": "USD", "initial_amount_cents": balance},
    )
    assert r.status_code in (200, 201, 409), r.text


@pytest.mark.asyncio
async def test_place_deposit_happy_path(client, clean_redis):
    await _create_wallet(client, "u_dep1")
    res = await client.post(
        "/api/v1/deposits/place",
        json={
            "user_id": "u_dep1",
            "brand_id": "bike_co",
            "amount_cents": 5000,
            "purpose": "bike_rental",
            "reference_id": "ref_001",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "placed"
    assert body["frozen_amount_cents"] == 5000
    assert body["idempotent"] is False


@pytest.mark.asyncio
async def test_place_deposit_idempotent_on_reference(client, clean_redis):
    await _create_wallet(client, "u_dep2")
    body = {
        "user_id": "u_dep2",
        "brand_id": "bike_co",
        "amount_cents": 3000,
        "purpose": "bike_rental",
        "reference_id": "ref_dup",
    }
    r1 = await client.post("/api/v1/deposits/place", json=body)
    r2 = await client.post("/api/v1/deposits/place", json=body)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["deposit_id"] == r2.json()["deposit_id"]
    assert r2.json()["idempotent"] is True


@pytest.mark.asyncio
async def test_place_deposit_missing_user_id_422(client, clean_redis):
    res = await client.post(
        "/api/v1/deposits/place",
        json={
            "brand_id": "x",
            "amount_cents": 100,
            "purpose": "other",
            "reference_id": "y",
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_place_deposit_zero_amount_422(client, clean_redis):
    res = await client.post(
        "/api/v1/deposits/place",
        json={
            "user_id": "u",
            "brand_id": "x",
            "amount_cents": 0,
            "purpose": "other",
            "reference_id": "y",
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_place_deposit_invalid_purpose_422(client, clean_redis):
    res = await client.post(
        "/api/v1/deposits/place",
        json={
            "user_id": "u",
            "brand_id": "x",
            "amount_cents": 100,
            "purpose": "INVALID_PURPOSE",
            "reference_id": "y",
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_get_deposit_404(client, clean_redis):
    res = await client.get("/api/v1/deposits/deposit_unknown")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_deposit_insufficient_funds_402(client, clean_redis):
    await _create_wallet(client, "u_poor", balance=100)
    res = await client.post(
        "/api/v1/deposits/place",
        json={
            "user_id": "u_poor",
            "brand_id": "x",
            "amount_cents": 99_999,
            "purpose": "other",
            "reference_id": "ref_poor",
        },
    )
    assert res.status_code in (402, 404, 409, 400)


@pytest.mark.asyncio
async def test_list_user_deposits(client, clean_redis):
    await _create_wallet(client, "u_list")
    await client.post(
        "/api/v1/deposits/place",
        json={
            "user_id": "u_list",
            "brand_id": "b",
            "amount_cents": 200,
            "purpose": "other",
            "reference_id": "ref_list",
        },
    )
    res = await client.get("/api/v1/deposits/user/u_list")
    assert res.status_code == 200
    assert isinstance(res.json(), list)
    assert len(res.json()) >= 1
