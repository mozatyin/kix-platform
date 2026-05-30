"""User-wallet router — create, topup, charge, freeze, transactions."""

from __future__ import annotations

import pytest


async def _new_wallet(client, uid: str, initial: int = 0) -> dict:
    res = await client.post(
        f"/api/v1/user-wallet/{uid}/create",
        json={"currency": "USD", "initial_amount_cents": initial},
    )
    return res.json() if res.status_code in (200, 201) else {}


@pytest.mark.asyncio
async def test_create_wallet_happy(client, clean_redis):
    res = await client.post(
        "/api/v1/user-wallet/u_w1/create",
        json={"currency": "USD"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["user_id"] == "u_w1"
    assert body["balance_cents"] == 0


@pytest.mark.asyncio
async def test_create_wallet_invalid_currency_422(client, clean_redis):
    res = await client.post(
        "/api/v1/user-wallet/u_w/create",
        json={"currency": "US"},  # too short
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_get_wallet_404(client, clean_redis):
    res = await client.get("/api/v1/user-wallet/u_missing")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_topup_increases_balance(client, clean_redis):
    await _new_wallet(client, "u_top")
    res = await client.post(
        "/api/v1/user-wallet/u_top/topup",
        json={
            "amount_cents": 5000,
            "source": "card",
            "reference_id": "tx_top_1",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["new_balance_cents"] == 5000


@pytest.mark.asyncio
async def test_topup_idempotent_on_reference(client, clean_redis):
    await _new_wallet(client, "u_idem")
    payload = {
        "amount_cents": 1000,
        "source": "card",
        "reference_id": "tx_idem_1",
    }
    r1 = await client.post("/api/v1/user-wallet/u_idem/topup", json=payload)
    r2 = await client.post("/api/v1/user-wallet/u_idem/topup", json=payload)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["tx_id"] == r2.json()["tx_id"]
    assert r2.json()["idempotent"] is True
    assert r2.json()["new_balance_cents"] == 1000  # not doubled


@pytest.mark.asyncio
async def test_topup_zero_amount_422(client, clean_redis):
    await _new_wallet(client, "u_zero")
    res = await client.post(
        "/api/v1/user-wallet/u_zero/topup",
        json={
            "amount_cents": 0,
            "source": "card",
            "reference_id": "x",
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_charge_insufficient_funds(client, clean_redis):
    await _new_wallet(client, "u_broke", initial=100)
    res = await client.post(
        "/api/v1/user-wallet/u_broke/charge",
        json={
            "amount_cents": 99_999,
            "reason": "purchase",
            "brand_id": "b1",
            "reference_id": "ch_1",
        },
    )
    assert res.status_code in (402, 409, 400)


@pytest.mark.asyncio
async def test_transactions_returns_list(client, clean_redis):
    await _new_wallet(client, "u_tx")
    await client.post(
        "/api/v1/user-wallet/u_tx/topup",
        json={"amount_cents": 100, "source": "card", "reference_id": "tx_a"},
    )
    res = await client.get("/api/v1/user-wallet/u_tx/transactions")
    assert res.status_code == 200
    assert isinstance(res.json(), list)
    assert len(res.json()) >= 1
