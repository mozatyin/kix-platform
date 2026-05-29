"""Disputes router tests — open → freeze charge → admin resolve flow,
refund_full vs refund_partial vs reject decisions, dispute uniqueness and
deadline / policy guards.

Covers the high-priority untested surface called out in the Trinity-E audit.
"""

from __future__ import annotations

import pytest


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


async def _topup(client, brand_id: str, amount_cents: int = 100_000) -> None:
    res = await client.post(
        f"/api/v1/wallet/{brand_id}/topup",
        json={"amount_cents": amount_cents, "payment_method": "stripe"},
    )
    assert res.status_code == 200, res.text
    tid = res.json()["topup_id"]
    res = await client.post(
        f"/api/v1/wallet/{brand_id}/topup/{tid}/confirm",
        json={"payment_gateway_response": {}},
    )
    assert res.status_code == 200, res.text


async def _charge(
    client, brand_id: str, amount_cents: int = 5_000, ref: str = "ref_c"
) -> str:
    """Make a charge against the brand wallet; return charge_id."""
    res = await client.post(
        f"/api/v1/wallet/{brand_id}/charge",
        json={
            "amount_cents": amount_cents,
            "reason": "cpa_conversion",
            "reference_id": ref,
        },
    )
    assert res.status_code == 200, res.text
    return res.json()["charge_id"]


# ──────────────────────────────────────────────────────────────────────────
# Open dispute
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_dispute_freezes_charge(client, clean_redis):
    """Opening a dispute against a 'completed' charge flips it to 'disputed'."""
    await _topup(client, "merchant_a", 100_000)
    # Use a large charge above the auto-refund threshold so the dispute stays
    # in pending_review.
    charge_id = await _charge(
        client, "merchant_a", amount_cents=20_000, ref="ref_big_1"
    )

    res = await client.post(
        "/api/v1/disputes/open",
        json={
            "brand_id": "merchant_a",
            "charge_id": charge_id,
            "category": "fake_user",
            "evidence_text": "User reused phone number; appears bot.",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["dispute_id"]
    assert body["auto_pause_charge"] is True
    # Large amount → not auto-resolved.
    assert body["auto_resolved"] is False
    assert body["status"] == "pending_review"

    # Verify the charge hash now shows disputed status.
    r = clean_redis
    charge = await r.hgetall(f"wallet:charge:{charge_id}")
    assert charge.get("status") == "disputed"
    assert charge.get("dispute_id") == body["dispute_id"]


@pytest.mark.asyncio
async def test_open_dispute_requires_one_ref(client, clean_redis):
    """BUG-BAIT: at least one of charge_id, conversion_id, or
    impression_token must be supplied."""
    res = await client.post(
        "/api/v1/disputes/open",
        json={
            "brand_id": "m1",
            "category": "other",
            "evidence_text": "no refs at all",
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_open_dispute_duplicate_charge_409(client, clean_redis):
    """Only one open dispute per charge_id allowed."""
    await _topup(client, "merchant_b")
    charge_id = await _charge(
        client, "merchant_b", amount_cents=15_000, ref="ref_dup_test"
    )

    res1 = await client.post(
        "/api/v1/disputes/open",
        json={
            "brand_id": "merchant_b",
            "charge_id": charge_id,
            "category": "fraud_suspected",
            "evidence_text": "first dispute",
        },
    )
    assert res1.status_code == 200, res1.text

    res2 = await client.post(
        "/api/v1/disputes/open",
        json={
            "brand_id": "merchant_b",
            "charge_id": charge_id,
            "category": "fraud_suspected",
            "evidence_text": "second dispute",
        },
    )
    assert res2.status_code == 409, res2.text
    detail = res2.json().get("detail", {})
    assert detail.get("error") == "dispute_already_exists_for_charge"


# ──────────────────────────────────────────────────────────────────────────
# Auto-approve micro-refunds (small amount + good standing)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_dispute_auto_refund_small_amount(client, clean_redis):
    """Charges under DEFAULT_AUTO_REFUND_UNDER_CENTS (1000) for a brand
    with clean dispute history auto-resolve immediately.

    The auto-approve gate requires brand_dispute_rate < 5%. The rate is
    computed as recent_disputes / max(tx_list_count, 20), so we pad the
    brand's tx history with prior charges to keep the rate well below 5%.
    """
    await _topup(client, "tiny_merch", 1_000_000)
    # Seed 30 prior completed charges so the denom is 30, giving 1/30 ≈ 3.3%.
    for i in range(30):
        await _charge(
            client, "tiny_merch", amount_cents=100, ref=f"prior_charge_{i}",
        )

    charge_id = await _charge(
        client, "tiny_merch", amount_cents=500, ref="ref_small_target",
    )

    res = await client.post(
        "/api/v1/disputes/open",
        json={
            "brand_id": "tiny_merch",
            "charge_id": charge_id,
            "category": "fake_user",
            "evidence_text": "micro dispute",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["auto_resolved"] is True
    assert body["status"] == "resolved_refund_full"
    assert body["refund_id"] is not None


# ──────────────────────────────────────────────────────────────────────────
# Admin resolve — full refund / partial / reject
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_resolve_refund_full(client, clean_redis):
    await _topup(client, "merchant_c", 100_000)
    charge_id = await _charge(
        client, "merchant_c", amount_cents=25_000, ref="ref_full_1"
    )

    res = await client.post(
        "/api/v1/disputes/open",
        json={
            "brand_id": "merchant_c",
            "charge_id": charge_id,
            "category": "fraud_suspected",
            "evidence_text": "Confirmed bot ring",
        },
    )
    did = res.json()["dispute_id"]

    # Track balance before resolution.
    bal_before = (
        await client.get("/api/v1/wallet/merchant_c")
    ).json()["balance_cents"]

    res = await client.post(
        f"/api/v1/disputes/{did}/admin/resolve",
        json={"decision": "refund_full", "reason": "verified_fraud"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "resolved_refund_full"
    assert body["refund_cents"] == 25_000

    # Wallet was credited back by the full charge amount.
    bal_after = (
        await client.get("/api/v1/wallet/merchant_c")
    ).json()["balance_cents"]
    assert bal_after - bal_before == 25_000


@pytest.mark.asyncio
async def test_admin_resolve_refund_partial(client, clean_redis):
    await _topup(client, "merchant_d", 100_000)
    charge_id = await _charge(
        client, "merchant_d", amount_cents=30_000, ref="ref_partial_1"
    )

    res = await client.post(
        "/api/v1/disputes/open",
        json={
            "brand_id": "merchant_d",
            "charge_id": charge_id,
            "category": "wrong_attribution",
            "evidence_text": "Half the clicks weren't ours",
        },
    )
    did = res.json()["dispute_id"]

    bal_before = (
        await client.get("/api/v1/wallet/merchant_d")
    ).json()["balance_cents"]

    res = await client.post(
        f"/api/v1/disputes/{did}/admin/resolve",
        json={
            "decision": "refund_partial",
            "refund_cents": 12_000,
            "reason": "partial_attribution_evidence",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "resolved_refund_partial"
    assert body["refund_cents"] == 12_000

    bal_after = (
        await client.get("/api/v1/wallet/merchant_d")
    ).json()["balance_cents"]
    assert bal_after - bal_before == 12_000


@pytest.mark.asyncio
async def test_admin_resolve_partial_exceeds_charge_400(client, clean_redis):
    """BUG-BAIT: partial refund cannot exceed the original charge amount."""
    await _topup(client, "merchant_e", 100_000)
    charge_id = await _charge(
        client, "merchant_e", amount_cents=5_000, ref="ref_over_1"
    )

    res = await client.post(
        "/api/v1/disputes/open",
        json={
            "brand_id": "merchant_e",
            "charge_id": charge_id,
            "category": "other",
            "evidence_text": "challenged",
        },
    )
    did = res.json()["dispute_id"]

    res = await client.post(
        f"/api/v1/disputes/{did}/admin/resolve",
        json={
            "decision": "refund_partial",
            "refund_cents": 99_999,
            "reason": "over the charge",
        },
    )
    assert res.status_code == 400, res.text
    detail = res.json().get("detail", {})
    assert detail.get("error") == "partial_exceeds_charge"


@pytest.mark.asyncio
async def test_admin_resolve_reject_unfreezes_charge(client, clean_redis):
    """Rejecting a dispute returns the charge to 'completed' (collectable)."""
    await _topup(client, "merchant_f", 100_000)
    charge_id = await _charge(
        client, "merchant_f", amount_cents=10_000, ref="ref_reject_1"
    )

    res = await client.post(
        "/api/v1/disputes/open",
        json={
            "brand_id": "merchant_f",
            "charge_id": charge_id,
            "category": "fake_user",
            "evidence_text": "weak case",
        },
    )
    did = res.json()["dispute_id"]

    res = await client.post(
        f"/api/v1/disputes/{did}/admin/resolve",
        json={"decision": "reject", "reason": "insufficient_evidence"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "resolved_reject"
    assert body["refund_cents"] is None

    # Charge unfrozen → back to completed.
    r = clean_redis
    charge = await r.hgetall(f"wallet:charge:{charge_id}")
    assert charge.get("status") == "completed"


@pytest.mark.asyncio
async def test_admin_resolve_already_resolved_409(client, clean_redis):
    """BUG-BAIT: a terminal dispute cannot be re-resolved."""
    await _topup(client, "merchant_g", 100_000)
    charge_id = await _charge(
        client, "merchant_g", amount_cents=8_000, ref="ref_twice_1"
    )

    res = await client.post(
        "/api/v1/disputes/open",
        json={
            "brand_id": "merchant_g",
            "charge_id": charge_id,
            "category": "other",
            "evidence_text": "open",
        },
    )
    did = res.json()["dispute_id"]

    # First resolution
    res = await client.post(
        f"/api/v1/disputes/{did}/admin/resolve",
        json={"decision": "reject", "reason": "first"},
    )
    assert res.status_code == 200

    # Second resolution must 409.
    res = await client.post(
        f"/api/v1/disputes/{did}/admin/resolve",
        json={"decision": "refund_full", "reason": "second"},
    )
    assert res.status_code == 409, res.text
    detail = res.json().get("detail", {})
    assert detail.get("error") == "dispute_already_resolved"


# ──────────────────────────────────────────────────────────────────────────
# Get / list / withdraw
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_dispute_not_found_404(client, clean_redis):
    res = await client.get("/api/v1/disputes/dispute_does_not_exist")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_merchant_withdraw_unfreezes_and_terminates(client, clean_redis):
    await _topup(client, "merchant_w", 100_000)
    charge_id = await _charge(
        client, "merchant_w", amount_cents=7_500, ref="ref_with_1"
    )

    res = await client.post(
        "/api/v1/disputes/open",
        json={
            "brand_id": "merchant_w",
            "charge_id": charge_id,
            "category": "other",
            "evidence_text": "to be withdrawn",
        },
    )
    did = res.json()["dispute_id"]

    res = await client.post(
        f"/api/v1/disputes/{did}/merchant/withdraw",
        json={"reason": "changed_mind"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "withdrawn"

    # Subsequent resolve attempt → 409.
    res = await client.post(
        f"/api/v1/disputes/{did}/admin/resolve",
        json={"decision": "reject", "reason": "too_late"},
    )
    assert res.status_code == 409
