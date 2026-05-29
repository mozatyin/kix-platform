"""Transactions router tests — record, refund, and concurrent-refund race.

The refund endpoint uses a Redis WATCH/MULTI optimistic loop. These tests
verify both happy paths and the concurrent-refund race condition flagged in
the Trinity-C audit: two simultaneous refund requests on the same
transaction must never produce a total refund exceeding the original amount.
"""

from __future__ import annotations

import asyncio

import pytest


# ── Helpers ────────────────────────────────────────────────────────────────


async def _record_purchase(client, *, tid: str, amount_cents: int) -> str:
    """Record a purchase transaction and return its tid."""
    res = await client.post(
        "/api/v1/transactions/record",
        json={
            "transaction_id": tid,
            "brand_id": "test_brand_refund",
            "buyer_user_id": "buyer_1",
            "seller_user_id": "seller_1",
            "amount_cents": amount_cents,
            "currency": "CNY",
            "transaction_type": "purchase",
            "payment_method": "wechat",
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["transaction_id"]


# ── Single-threaded refund happy paths ─────────────────────────────────────


@pytest.mark.asyncio
async def test_refund_full(client, clean_redis):
    tid = await _record_purchase(client, tid="tx_refund_full", amount_cents=10_000)

    res = await client.post(
        f"/api/v1/transactions/{tid}/refund",
        json={"reason": "test_full"},
    )
    assert res.status_code == 200, res.text

    res = await client.get(f"/api/v1/transactions/{tid}")
    assert res.status_code == 200
    state = res.json()
    assert int(state["refunded_cents"]) == 10_000
    assert state["status"] == "fully_refunded"


@pytest.mark.asyncio
async def test_refund_partial_then_full(client, clean_redis):
    tid = await _record_purchase(
        client, tid="tx_refund_partial", amount_cents=10_000
    )

    res = await client.post(
        f"/api/v1/transactions/{tid}/refund",
        json={"amount_cents": 3_000, "reason": "partial_1"},
    )
    assert res.status_code == 200, res.text

    res = await client.get(f"/api/v1/transactions/{tid}")
    assert int(res.json()["refunded_cents"]) == 3_000
    assert res.json()["status"] == "partially_refunded"

    # Cap-out with another partial: 7000 + 3000 == 10000.
    res = await client.post(
        f"/api/v1/transactions/{tid}/refund",
        json={"amount_cents": 7_000, "reason": "partial_2"},
    )
    assert res.status_code == 200, res.text

    res = await client.get(f"/api/v1/transactions/{tid}")
    assert int(res.json()["refunded_cents"]) == 10_000
    assert res.json()["status"] == "fully_refunded"


@pytest.mark.asyncio
async def test_refund_exceeds_remaining_rejected(client, clean_redis):
    tid = await _record_purchase(
        client, tid="tx_refund_exceeds", amount_cents=10_000
    )

    res = await client.post(
        f"/api/v1/transactions/{tid}/refund",
        json={"amount_cents": 6_000, "reason": "first"},
    )
    assert res.status_code == 200, res.text

    # Second one tries to refund 6000 more — exceeds remaining 4000.
    res = await client.post(
        f"/api/v1/transactions/{tid}/refund",
        json={"amount_cents": 6_000, "reason": "second"},
    )
    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert detail["reason"] == "refund_exceeds_remaining"
    assert detail["already_refunded"] == 6_000

    res = await client.get(f"/api/v1/transactions/{tid}")
    assert int(res.json()["refunded_cents"]) == 6_000


@pytest.mark.asyncio
async def test_refund_after_full_status_rejected(client, clean_redis):
    tid = await _record_purchase(
        client, tid="tx_refund_done", amount_cents=5_000
    )
    res = await client.post(
        f"/api/v1/transactions/{tid}/refund",
        json={"reason": "full"},
    )
    assert res.status_code == 200, res.text

    # Subsequent refund must be rejected via status-guard (409).
    res = await client.post(
        f"/api/v1/transactions/{tid}/refund",
        json={"amount_cents": 100, "reason": "should_fail"},
    )
    assert res.status_code == 409, res.text


# ── Concurrent refund race — Trinity-C audit ───────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_refunds_no_over_refund(client, clean_redis):
    """Two concurrent refund requests of 6000 against a 10000 tx.

    Exactly one must succeed (200) and the other must be rejected (422 from
    refund_exceeds_remaining, or 409 if status flipped to fully_refunded
    after the first commit). Total refunded must equal 6000, never 12000.
    """
    tid = await _record_purchase(
        client, tid="tx_refund_race", amount_cents=10_000
    )

    async def _refund() -> int:
        res = await client.post(
            f"/api/v1/transactions/{tid}/refund",
            json={"amount_cents": 6_000, "reason": "concurrent"},
        )
        return res.status_code

    # Launch both as concurrent tasks. The ASGI client serialises bytes
    # over the wire but FastAPI awaits inside the handler, giving the
    # WATCH/MULTI loop a real chance to interleave.
    status_a, status_b = await asyncio.gather(_refund(), _refund())

    statuses = sorted([status_a, status_b])
    # One success + one rejection (422 exceeds, or 409 invalid_status).
    assert statuses[0] == 200, f"expected one success, got {statuses}"
    assert statuses[1] in (409, 422), (
        f"expected one rejection (409|422), got {statuses}"
    )

    res = await client.get(f"/api/v1/transactions/{tid}")
    state = res.json()
    total_refunded = int(state["refunded_cents"])
    assert total_refunded == 6_000, (
        f"over-refund: refunded_cents={total_refunded}, expected 6000"
    )
    assert state["status"] == "partially_refunded"


@pytest.mark.asyncio
async def test_concurrent_refunds_both_fit(client, clean_redis):
    """Two concurrent refunds that together exactly equal the original.

    Both 5000 + 5000 == 10000 — both must succeed (the second will hit a
    WATCH conflict and retry, re-reading already_refunded=5000 and pass).
    Final refunded_cents must be exactly 10000.
    """
    tid = await _record_purchase(
        client, tid="tx_refund_race_fit", amount_cents=10_000
    )

    async def _refund() -> int:
        res = await client.post(
            f"/api/v1/transactions/{tid}/refund",
            json={"amount_cents": 5_000, "reason": "concurrent_fit"},
        )
        return res.status_code

    status_a, status_b = await asyncio.gather(_refund(), _refund())

    # Both should succeed — the retry loop handles the WATCH abort.
    assert (status_a, status_b) == (200, 200), (
        f"both refunds should succeed, got ({status_a}, {status_b})"
    )

    res = await client.get(f"/api/v1/transactions/{tid}")
    state = res.json()
    assert int(state["refunded_cents"]) == 10_000
    assert state["status"] == "fully_refunded"
