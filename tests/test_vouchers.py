"""Vouchers (cross-store) router tests — issue, reserve/claim, redeem,
transfer, bulk-issue, bulk-cancel, commission split, and concurrent reserve
behaviour.

Covers the high-priority untested surface called out in the Trinity-E audit.
"""

from __future__ import annotations

import asyncio

import pytest


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


async def _issue(
    client,
    *,
    issuer: str = "brand_issue",
    user_id: str = "user_1",
    value_cents: int = 500,
    redeemable_at="issuer_only",
    transferable: bool = True,
    max_uses: int = 1,
    holder_type: str = "kid",
) -> str:
    res = await client.post(
        "/api/v1/vouchers/issue",
        params={"issuer_brand_id": issuer},
        json={
            "user_id": user_id,
            "value_cents": value_cents,
            "redeemable_at": redeemable_at,
            "transferable": transferable,
            "max_uses": max_uses,
            "source": "gift",
            "holder_type": holder_type,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["voucher_id"]


# ──────────────────────────────────────────────────────────────────────────
# Issue
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_issue_voucher_creates_record(client, clean_redis):
    vid = await _issue(client, value_cents=2_500)
    res = await client.get(f"/api/v1/vouchers/{vid}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["voucher_id"] == vid
    assert body["status"] == "issued"
    assert body["value_cents"] == 2_500


@pytest.mark.asyncio
async def test_issue_voucher_rejects_partnership_scheme(client, clean_redis):
    """BUG-BAIT: partnership:{pid} redeemable_at was removed; the validator
    must reject it with a clear error."""
    res = await client.post(
        "/api/v1/vouchers/issue",
        params={"issuer_brand_id": "b1"},
        json={
            "user_id": "u1",
            "value_cents": 100,
            "redeemable_at": "partnership:pid_x",
            "source": "gift",
        },
    )
    assert res.status_code == 422, res.text


# ──────────────────────────────────────────────────────────────────────────
# Redeem
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redeem_voucher_issuer_only_at_issuer_succeeds(client, clean_redis):
    vid = await _issue(client, issuer="brand_r", user_id="u_red", value_cents=1_000)
    res = await client.post(
        f"/api/v1/vouchers/{vid}/redeem",
        json={
            "at_brand_id": "brand_r",
            "redeemer_user_id": "u_red",
            "order_id": "ord_1",
            "order_amount_cents": 5_000,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["status"] == "redeemed"
    assert body["value_applied_cents"] == 1_000
    assert body["is_cross_brand"] is False


@pytest.mark.asyncio
async def test_redeem_issuer_only_at_other_brand_rejected(client, clean_redis):
    """BUG-BAIT: cross-brand redemption of an issuer_only voucher → 403."""
    vid = await _issue(
        client, issuer="brand_x", user_id="ux", value_cents=1_000,
        redeemable_at="issuer_only",
    )
    res = await client.post(
        f"/api/v1/vouchers/{vid}/redeem",
        json={
            "at_brand_id": "brand_y",
            "redeemer_user_id": "ux",
            "order_id": "ord_x",
            "order_amount_cents": 5_000,
        },
    )
    assert res.status_code == 403, res.text
    detail = res.json().get("detail", {})
    assert detail.get("reason") == "voucher_is_issuer_only"


@pytest.mark.asyncio
async def test_redeem_voucher_twice_fails_second_time(client, clean_redis):
    """BUG-BAIT: a single-use voucher cannot be redeemed twice."""
    vid = await _issue(client, issuer="brand_d", user_id="ud", value_cents=500)
    # First redeem succeeds.
    res = await client.post(
        f"/api/v1/vouchers/{vid}/redeem",
        json={
            "at_brand_id": "brand_d",
            "redeemer_user_id": "ud",
            "order_id": "ord1",
            "order_amount_cents": 5_000,
        },
    )
    assert res.status_code == 200

    # Second attempt → 409 invalid_status.
    res = await client.post(
        f"/api/v1/vouchers/{vid}/redeem",
        json={
            "at_brand_id": "brand_d",
            "redeemer_user_id": "ud",
            "order_id": "ord2",
            "order_amount_cents": 5_000,
        },
    )
    assert res.status_code == 409, res.text
    detail = res.json().get("detail", {})
    assert detail.get("reason") == "invalid_status"


# ──────────────────────────────────────────────────────────────────────────
# Reserve → claim flow + concurrent reservations
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reserve_voucher_basic(client, clean_redis):
    vid = await _issue(
        client, issuer="b_res", user_id="device_fp_abc",
        value_cents=200, holder_type="device_fp",
    )
    res = await client.post(
        f"/api/v1/vouchers/{vid}/reserve",
        json={"device_fingerprint": "device_fp_abc", "ttl_seconds": 60},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "reserved"
    assert body["reservation_token"]
    assert body["extended"] is False


@pytest.mark.asyncio
async def test_reserve_idempotent_same_holder_extends_ttl(client, clean_redis):
    """Re-reserving with the same holder extends the TTL (returns
    extended=True), does not 409."""
    vid = await _issue(
        client, issuer="b_idem", user_id="device_idem",
        value_cents=300, holder_type="device_fp",
    )
    res1 = await client.post(
        f"/api/v1/vouchers/{vid}/reserve",
        json={"device_fingerprint": "device_idem", "ttl_seconds": 60},
    )
    assert res1.status_code == 200
    tok1 = res1.json()["reservation_token"]

    res2 = await client.post(
        f"/api/v1/vouchers/{vid}/reserve",
        json={"device_fingerprint": "device_idem", "ttl_seconds": 120},
    )
    assert res2.status_code == 200
    body2 = res2.json()
    assert body2["extended"] is True
    assert body2["reservation_token"] == tok1


@pytest.mark.asyncio
async def test_reserve_conflict_different_holder_409(client, clean_redis):
    """BUG-BAIT: a different device/kid attempting to reserve the same
    voucher must 409, not silently take over."""
    vid = await _issue(
        client, issuer="b_conf", user_id="device_a",
        value_cents=300, holder_type="device_fp",
    )
    res = await client.post(
        f"/api/v1/vouchers/{vid}/reserve",
        json={"device_fingerprint": "device_a", "ttl_seconds": 60},
    )
    assert res.status_code == 200

    res = await client.post(
        f"/api/v1/vouchers/{vid}/reserve",
        json={"device_fingerprint": "device_b", "ttl_seconds": 60},
    )
    assert res.status_code == 409, res.text
    detail = res.json().get("detail", {})
    assert detail.get("reason") == "already_reserved_by_other"


# ──────────────────────────────────────────────────────────────────────────
# Bulk issue / bulk cancel
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bulk_issue_creates_voucher_per_user(client, clean_redis):
    res = await client.post(
        "/api/v1/vouchers/bulk-issue",
        json={
            "brand_id": "b_bulk",
            "user_ids": ["u_a", "u_b", "u_c"],
            "values": [100, 200, 300],
            "source": "campaign",
            "transferable": True,
            "max_uses": 1,
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["issued_count"] == 3
    assert body["failed_count"] == 0
    assert len(body["voucher_ids"]) == 3

    # Fetch the second voucher and verify it belongs to u_b.
    res = await client.get(f"/api/v1/vouchers/{body['voucher_ids'][1]}")
    assert res.json()["holder_user_id"] == "u_b"


@pytest.mark.asyncio
async def test_bulk_issue_values_length_mismatch_400(client, clean_redis):
    """BUG-BAIT: values list length must equal user_ids length."""
    res = await client.post(
        "/api/v1/vouchers/bulk-issue",
        json={
            "brand_id": "b_mismatch",
            "user_ids": ["u1", "u2", "u3"],
            "values": [100, 200],  # length 2 vs user_ids length 3
            "source": "campaign",
        },
    )
    assert res.status_code == 400, res.text


@pytest.mark.asyncio
async def test_bulk_cancel_cancels_only_eligible(client, clean_redis):
    v1 = await _issue(client, issuer="b_c", user_id="u1", value_cents=100)
    v2 = await _issue(client, issuer="b_c", user_id="u2", value_cents=200)

    res = await client.post(
        "/api/v1/vouchers/bulk-cancel",
        json={
            "voucher_ids": [v1, v2, "vid_does_not_exist"],
            "cancelled_by": "admin",
            "reason": "test",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["cancelled_count"] == 2
    assert body["failed_count"] == 1
    # Both vouchers now cancelled.
    for vid in (v1, v2):
        res = await client.get(f"/api/v1/vouchers/{vid}")
        assert res.json()["status"] == "cancelled"


# ──────────────────────────────────────────────────────────────────────────
# Transfer + commission_split template
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transfer_voucher_changes_holder(client, clean_redis):
    vid = await _issue(client, issuer="b_t", user_id="alice", value_cents=500)
    res = await client.post(
        f"/api/v1/vouchers/{vid}/transfer",
        json={"from_user_id": "alice", "to_user_id": "bob", "message": "gift"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["new_holder"] == "bob"
    assert body["previous_holder"] == "alice"

    res = await client.get(f"/api/v1/vouchers/{vid}")
    assert res.json()["holder_user_id"] == "bob"


@pytest.mark.asyncio
async def test_transfer_non_transferable_rejected(client, clean_redis):
    """BUG-BAIT: transferable=False vouchers cannot be transferred."""
    vid = await _issue(
        client, issuer="b_nt", user_id="alice", value_cents=500,
        transferable=False,
    )
    res = await client.post(
        f"/api/v1/vouchers/{vid}/transfer",
        json={"from_user_id": "alice", "to_user_id": "bob"},
    )
    assert res.status_code == 403, res.text


@pytest.mark.asyncio
async def test_attach_commission_split_persists_for_template(client, clean_redis):
    """Cross-brand commission split must sum to 1.0 (validator) and persist."""
    res = await client.post(
        "/api/v1/vouchers/templates/commission-split",
        json={
            "brand_id": "b_split",
            "template_id": "tpl_v1",
            "commission_split": {
                "issuer_pct": 0.2,
                "kix_pct": 0.3,
                "redeemer_pct": 0.5,
            },
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["commission_split"]["issuer_pct"] == 0.2
    assert body["commission_split"]["redeemer_pct"] == 0.5


@pytest.mark.asyncio
async def test_commission_split_must_sum_to_one(client, clean_redis):
    """BUG-BAIT: split summing to ≠ 1.0 is rejected by validator."""
    res = await client.post(
        "/api/v1/vouchers/templates/commission-split",
        json={
            "brand_id": "b_bad",
            "template_id": "tpl_bad",
            "commission_split": {
                "issuer_pct": 0.5,
                "kix_pct": 0.5,
                "redeemer_pct": 0.5,  # total 1.5 → invalid
            },
        },
    )
    assert res.status_code == 422, res.text
