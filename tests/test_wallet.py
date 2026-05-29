"""Wallet router tests — topup, charge idempotency, refund, daily budget."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_wallet_topup_and_balance(client, clean_redis):
    brand_id = "test_brand_topup"

    res = await client.post(
        f"/api/v1/wallet/{brand_id}/topup",
        json={"amount_cents": 100_000, "payment_method": "wechat"},
    )
    assert res.status_code == 200, res.text
    topup = res.json()
    assert topup["amount_cents"] == 100_000
    topup_id = topup["topup_id"]

    res = await client.post(
        f"/api/v1/wallet/{brand_id}/topup/{topup_id}/confirm",
        json={"payment_gateway_response": {"mock": True}},
    )
    assert res.status_code == 200, res.text

    res = await client.get(f"/api/v1/wallet/{brand_id}")
    assert res.status_code == 200
    assert res.json()["balance_cents"] == 100_000


@pytest.mark.asyncio
async def test_wallet_charge_idempotent(client, clean_redis):
    brand_id = "test_brand_idem"

    res = await client.post(
        f"/api/v1/wallet/{brand_id}/topup",
        json={"amount_cents": 100_000, "payment_method": "wechat"},
    )
    topup_id = res.json()["topup_id"]
    await client.post(
        f"/api/v1/wallet/{brand_id}/topup/{topup_id}/confirm",
        json={"payment_gateway_response": {}},
    )

    body = {
        "amount_cents": 5_000,
        "reason": "cpa_conversion",
        "reference_id": "ref_idem_1",
    }
    res1 = await client.post(f"/api/v1/wallet/{brand_id}/charge", json=body)
    res2 = await client.post(f"/api/v1/wallet/{brand_id}/charge", json=body)

    assert res1.status_code == 200, res1.text
    assert res2.status_code == 200, res2.text
    assert res1.json()["charge_id"] == res2.json()["charge_id"]
    assert res2.json().get("idempotent") is True

    res = await client.get(f"/api/v1/wallet/{brand_id}")
    assert res.json()["balance_cents"] == 95_000


@pytest.mark.asyncio
async def test_wallet_charge_insufficient_funds(client, clean_redis):
    brand_id = "test_brand_broke"

    # No topup → charge must fail with 402.
    res = await client.post(
        f"/api/v1/wallet/{brand_id}/charge",
        json={
            "amount_cents": 1_000,
            "reason": "cpa_conversion",
            "reference_id": "ref_broke_1",
        },
    )
    assert res.status_code == 402, res.text
    body = res.json()
    detail = body.get("detail", body)
    assert detail.get("reason") == "insufficient_funds"


@pytest.mark.asyncio
async def test_wallet_topup_rejects_zero_amount(client, clean_redis):
    brand_id = "test_brand_zero"
    res = await client.post(
        f"/api/v1/wallet/{brand_id}/topup",
        json={"amount_cents": 0, "payment_method": "stripe"},
    )
    # Pydantic gt=0 validation → 422.
    assert res.status_code == 422
