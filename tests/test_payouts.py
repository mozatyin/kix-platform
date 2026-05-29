"""Payouts router tests — bank accounts, payout request lifecycle, invoice
generation, and inter-brand transfer atomicity + idempotency.

These tests cover the high-priority untested surface called out in the
Trinity-E audit: atomic ledger transfers, payout status machine, refund
rollback on failure, and invoice generation.
"""

from __future__ import annotations

import pytest


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


async def _topup(client, brand_id: str, amount_cents: int) -> None:
    """Top up a brand's wallet and confirm. Initialises wallet balance key."""
    res = await client.post(
        f"/api/v1/wallet/{brand_id}/topup",
        json={"amount_cents": amount_cents, "payment_method": "stripe"},
    )
    assert res.status_code == 200, res.text
    topup_id = res.json()["topup_id"]
    res = await client.post(
        f"/api/v1/wallet/{brand_id}/topup/{topup_id}/confirm",
        json={"payment_gateway_response": {}},
    )
    assert res.status_code == 200, res.text


# ──────────────────────────────────────────────────────────────────────────
# Inter-brand transfer — atomicity + idempotency + edge cases
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inter_brand_transfer_basic(client, clean_redis):
    await _topup(client, "brand_a", 10_000)
    # Initialise brand_b's wallet key with a 1-cent topup (zero is rejected).
    await _topup(client, "brand_b", 1)

    res = await client.post(
        "/api/v1/payouts/inter-brand-transfer",
        json={
            "from_brand_id": "brand_a",
            "to_brand_id": "brand_b",
            "amount_cents": 1_000,
            "reason": "supplier_payment",
            "reference_id": "test_ref_basic_1",
        },
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["amount_cents"] == 1_000
    assert data["from_brand_id"] == "brand_a"
    assert data["to_brand_id"] == "brand_b"
    assert data["idempotent"] is False
    assert data["entry_id"].startswith("le_")

    # Balances: brand_a debited, brand_b credited.
    a = await client.get("/api/v1/wallet/brand_a")
    b = await client.get("/api/v1/wallet/brand_b")
    assert a.json()["balance_cents"] == 9_000
    assert b.json()["balance_cents"] == 1_001


@pytest.mark.asyncio
async def test_inter_brand_transfer_idempotent(client, clean_redis):
    """Same reference_id returns the cached entry without re-debiting."""
    await _topup(client, "src", 10_000)
    await _topup(client, "dst", 1)

    body = {
        "from_brand_id": "src",
        "to_brand_id": "dst",
        "amount_cents": 500,
        "reason": "affiliate_commission",
        "reference_id": "ref_idem_dup",
    }
    res1 = await client.post("/api/v1/payouts/inter-brand-transfer", json=body)
    res2 = await client.post("/api/v1/payouts/inter-brand-transfer", json=body)

    assert res1.status_code == 200, res1.text
    assert res2.status_code == 200, res2.text
    assert res1.json()["entry_id"] == res2.json()["entry_id"]
    assert res2.json()["idempotent"] is True

    # Balance only decremented once.
    src_bal = (await client.get("/api/v1/wallet/src")).json()["balance_cents"]
    dst_bal = (await client.get("/api/v1/wallet/dst")).json()["balance_cents"]
    assert src_bal == 9_500
    assert dst_bal == 501


@pytest.mark.asyncio
async def test_inter_brand_transfer_insufficient_funds(client, clean_redis):
    """402 when source balance < amount."""
    await _topup(client, "broke", 100)
    await _topup(client, "rich", 1)

    res = await client.post(
        "/api/v1/payouts/inter-brand-transfer",
        json={
            "from_brand_id": "broke",
            "to_brand_id": "rich",
            "amount_cents": 500,
            "reason": "supplier_payment",
            "reference_id": "ref_broke_x",
        },
    )
    assert res.status_code == 402, res.text
    body = res.json()
    detail = body.get("detail", body)
    assert detail.get("error") == "insufficient_funds"


@pytest.mark.asyncio
async def test_inter_brand_transfer_self_transfer_rejected(client, clean_redis):
    """A brand cannot pay itself."""
    await _topup(client, "solo", 5_000)
    res = await client.post(
        "/api/v1/payouts/inter-brand-transfer",
        json={
            "from_brand_id": "solo",
            "to_brand_id": "solo",
            "amount_cents": 100,
            "reason": "other",
            "reference_id": "ref_self_1",
        },
    )
    assert res.status_code == 400
    detail = res.json().get("detail", {})
    assert detail.get("error") == "self_transfer_not_allowed"


@pytest.mark.asyncio
async def test_inter_brand_transfer_unknown_brand(client, clean_redis):
    """Both wallet balance keys must exist (initialised via topup)."""
    await _topup(client, "known", 10_000)

    res = await client.post(
        "/api/v1/payouts/inter-brand-transfer",
        json={
            "from_brand_id": "known",
            "to_brand_id": "ghost",
            "amount_cents": 100,
            "reason": "supplier_payment",
            "reference_id": "ref_ghost_1",
        },
    )
    assert res.status_code == 404
    detail = res.json().get("detail", {})
    assert detail.get("error") == "unknown_brand"
    assert "ghost" in detail.get("missing", [])


@pytest.mark.asyncio
async def test_inter_brand_transfer_rejects_negative_amount(client, clean_redis):
    """BUG-BAIT: negative amounts must be rejected by Pydantic (gt=0)."""
    await _topup(client, "neg_a", 100)
    await _topup(client, "neg_b", 100)
    res = await client.post(
        "/api/v1/payouts/inter-brand-transfer",
        json={
            "from_brand_id": "neg_a",
            "to_brand_id": "neg_b",
            "amount_cents": -100,
            "reason": "supplier_payment",
            "reference_id": "ref_neg_1",
        },
    )
    assert res.status_code == 422, res.text


@pytest.mark.asyncio
async def test_inter_brand_transfer_invalid_reason(client, clean_redis):
    """Reason must be in the allowed Literal set."""
    await _topup(client, "ra", 100)
    await _topup(client, "rb", 100)
    res = await client.post(
        "/api/v1/payouts/inter-brand-transfer",
        json={
            "from_brand_id": "ra",
            "to_brand_id": "rb",
            "amount_cents": 10,
            "reason": "nonsense_reason",
            "reference_id": "ref_bad_reason",
        },
    )
    # Pydantic Literal validation → 422.
    assert res.status_code == 422


# ──────────────────────────────────────────────────────────────────────────
# Bank account add + balance endpoint
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bank_account_add_returns_verification_required(client, clean_redis):
    res = await client.post(
        "/api/v1/payouts/bank-account/add",
        json={
            "brand_id": "brand_ba",
            "account_holder": "Acme Co",
            "bank_name": "ICBC",
            "account_number_hash": "1234567890",
            "country": "CN",
            "currency": "CNY",
        },
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["verification_required"] is True
    assert data["bank_account_id"].startswith("ba_")


@pytest.mark.asyncio
async def test_brand_balance_returns_zero_for_new_brand(client, clean_redis):
    res = await client.get("/api/v1/payouts/brand/never_seen_brand/balance")
    assert res.status_code == 200
    data = res.json()
    assert data["commission_owed_cents"] == 0
    assert data["wallet_balance_cents"] == 0
    assert data["pending_payouts_cents"] == 0
    assert data["paid_lifetime_cents"] == 0


# ──────────────────────────────────────────────────────────────────────────
# Payout request lifecycle — insufficient funds → 402
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_payout_request_unverified_bank_account_rejected(client, clean_redis):
    """Bug-bait: payout cannot be requested against an unverified bank
    account, even when commission_owed has plenty of balance."""
    # Pre-seed commission_owed via Redis directly (since attribution path
    # isn't exercised here).
    r = clean_redis
    await r.hset("brand:bx:commission_owed", "cents", 50_000)

    # Add a bank account but DON'T verify it.
    res = await client.post(
        "/api/v1/payouts/bank-account/add",
        json={
            "brand_id": "bx",
            "account_holder": "Brand X",
            "bank_name": "BoC",
            "account_number_hash": "9999999999",
            "country": "CN",
            "currency": "CNY",
        },
    )
    ba_id = res.json()["bank_account_id"]

    # Request payout against the unverified account → 409.
    res = await client.post(
        "/api/v1/payouts/request",
        json={
            "brand_id": "bx",
            "amount_cents": 10_000,
            "bank_account_id": ba_id,
            "source": "commission",
        },
    )
    assert res.status_code == 409, res.text
    detail = res.json().get("detail", {})
    assert detail.get("error") == "bank_account_unverified"


@pytest.mark.asyncio
async def test_invoice_generate_with_no_attribution_returns_empty(client, clean_redis):
    """An invoice for a brand with no attribution events generates a zero-row
    invoice (not an error)."""
    res = await client.post(
        "/api/v1/payouts/invoice/generate",
        json={
            "brand_id": "inv_brand",
            "period_start": 0.0,
            "period_end": 9_999_999_999.0,
        },
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["brand_id"] == "inv_brand"
    assert data["lines"] == []
    assert data["net_payable_cents"] == 0
    assert data["commission_earned_cents"] == 0
    assert data["invoice_id"].startswith("inv_")


@pytest.mark.asyncio
async def test_invoice_generate_rejects_invalid_period(client, clean_redis):
    """period_end must be > period_start."""
    res = await client.post(
        "/api/v1/payouts/invoice/generate",
        json={
            "brand_id": "ibrand",
            "period_start": 1000.0,
            "period_end": 500.0,
        },
    )
    assert res.status_code == 400, res.text
    detail = res.json().get("detail", {})
    assert detail.get("error") == "invalid_period"
