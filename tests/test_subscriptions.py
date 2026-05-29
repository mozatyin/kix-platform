"""Subscriptions router tests — create/upgrade/downgrade/cancel/renew
lifecycle, MRR/ARR delta accounting, NDR/GRR computation, seat-change for
B2B.

Covers the high-priority untested surface called out in the Trinity-E
audit.
"""

from __future__ import annotations

import time

import pytest


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


async def _create_sub(
    client,
    *,
    brand_id: str = "brand_sub",
    user_id: str | None = "user_1",
    account_id: str | None = None,
    plan_id: str = "starter",
    monthly_amount_cents: int = 1_000,
    seats: int = 1,
    billing_cycle: str = "monthly",
) -> dict:
    body = {
        "brand_id": brand_id,
        "plan_id": plan_id,
        "monthly_amount_cents": monthly_amount_cents,
        "seats": seats,
        "billing_cycle": billing_cycle,
        "starts_at": time.time(),
        "auto_renew": True,
    }
    if user_id:
        body["user_id"] = user_id
    if account_id:
        body["account_id"] = account_id
    res = await client.post("/api/v1/subscriptions/create", json=body)
    assert res.status_code == 200, res.text
    return res.json()


# ──────────────────────────────────────────────────────────────────────────
# Create / fetch
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_subscription_seeds_mrr_and_active_set(client, clean_redis):
    sub = await _create_sub(
        client, monthly_amount_cents=2_000, seats=3,
    )
    assert sub["mrr_cents"] == 6_000  # 2000 * 3 seats
    assert sub["status"] == "active"
    assert sub["subscription_id"].startswith("sub_")

    # Active brand set must include the sub.
    res = await client.get("/api/v1/subscriptions/brand/brand_sub/active")
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 1
    assert body["subscriptions"][0]["subscription_id"] == sub["subscription_id"]


@pytest.mark.asyncio
async def test_create_subscription_requires_user_or_account(client, clean_redis):
    """BUG-BAIT: at least one of user_id/account_id required."""
    res = await client.post(
        "/api/v1/subscriptions/create",
        json={
            "brand_id": "b1",
            "plan_id": "p",
            "monthly_amount_cents": 100,
            "starts_at": time.time(),
        },
    )
    assert res.status_code == 422, res.text


@pytest.mark.asyncio
async def test_get_subscription_not_found_404(client, clean_redis):
    res = await client.get("/api/v1/subscriptions/sub_doesnotexist")
    assert res.status_code == 404


# ──────────────────────────────────────────────────────────────────────────
# Upgrade / downgrade lifecycle
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upgrade_records_expansion_movement(client, clean_redis):
    sub = await _create_sub(client, monthly_amount_cents=1_000)
    sid = sub["subscription_id"]

    res = await client.post(
        f"/api/v1/subscriptions/{sid}/upgrade",
        json={
            "new_plan_id": "pro",
            "new_monthly_amount_cents": 2_500,
            "prorated": True,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["mrr_cents"] == 2_500
    assert body["delta_mrr_cents"] == 1_500
    assert body["movement"] == "expansion"


@pytest.mark.asyncio
async def test_upgrade_must_increase_mrr(client, clean_redis):
    """BUG-BAIT: upgrade endpoint rejects an MRR decrease."""
    sub = await _create_sub(client, monthly_amount_cents=5_000)
    sid = sub["subscription_id"]

    res = await client.post(
        f"/api/v1/subscriptions/{sid}/upgrade",
        json={
            "new_plan_id": "shrink",
            "new_monthly_amount_cents": 1_000,
            "prorated": False,
        },
    )
    assert res.status_code == 400, res.text
    detail = res.json().get("detail")
    assert detail == "upgrade_must_increase_mrr"


@pytest.mark.asyncio
async def test_downgrade_immediate_drops_mrr(client, clean_redis):
    sub = await _create_sub(client, monthly_amount_cents=5_000)
    sid = sub["subscription_id"]

    res = await client.post(
        f"/api/v1/subscriptions/{sid}/downgrade",
        json={
            "new_plan_id": "basic",
            "new_monthly_amount_cents": 2_000,
            "effective": "immediate",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["mrr_cents"] == 2_000
    assert body["delta_mrr_cents"] == -3_000
    assert body["movement"] == "contraction"


@pytest.mark.asyncio
async def test_downgrade_end_of_period_keeps_mrr_until_renew(client, clean_redis):
    """End-of-period downgrade queues; MRR doesn't change until renew."""
    sub = await _create_sub(client, monthly_amount_cents=5_000)
    sid = sub["subscription_id"]

    res = await client.post(
        f"/api/v1/subscriptions/{sid}/downgrade",
        json={
            "new_plan_id": "basic",
            "new_monthly_amount_cents": 2_000,
            "effective": "end_of_period",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["mrr_cents"] == 5_000  # unchanged
    assert body["delta_mrr_cents"] == 0
    assert body["movement"] is None

    # Apply the scheduled downgrade by renewing.
    res = await client.post(
        f"/api/v1/subscriptions/{sid}/renew", json={"payment_method_id": None}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["mrr_cents"] == 2_000
    assert body["movement"] == "contraction"


# ──────────────────────────────────────────────────────────────────────────
# Seat change (B2B)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seat_change_expands_mrr(client, clean_redis):
    sub = await _create_sub(
        client, monthly_amount_cents=1_500, seats=2,
    )
    sid = sub["subscription_id"]
    assert sub["mrr_cents"] == 3_000

    res = await client.post(
        f"/api/v1/subscriptions/{sid}/seat-change",
        json={"new_seat_count": 5},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["mrr_cents"] == 1_500 * 5
    assert body["delta_mrr_cents"] == 1_500 * 3
    assert body["movement"] == "expansion"


@pytest.mark.asyncio
async def test_seat_change_noop_when_same_count(client, clean_redis):
    sub = await _create_sub(client, monthly_amount_cents=1_000, seats=3)
    sid = sub["subscription_id"]
    res = await client.post(
        f"/api/v1/subscriptions/{sid}/seat-change",
        json={"new_seat_count": 3},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["delta_mrr_cents"] == 0
    assert body["movement"] is None


# ──────────────────────────────────────────────────────────────────────────
# Cancel + churn
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_immediate_records_churn(client, clean_redis):
    sub = await _create_sub(client, monthly_amount_cents=4_000)
    sid = sub["subscription_id"]

    res = await client.post(
        f"/api/v1/subscriptions/{sid}/cancel",
        json={"effective": "immediate", "reason": "user_quit"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "cancelled"
    assert body["mrr_cents"] == 0
    assert body["delta_mrr_cents"] == -4_000
    assert body["movement"] == "churn"

    # Status now non-active; further upgrade should 409.
    res = await client.post(
        f"/api/v1/subscriptions/{sid}/upgrade",
        json={"new_plan_id": "x", "new_monthly_amount_cents": 9_000},
    )
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_cancel_end_of_period_keeps_mrr_then_applies_on_renew(
    client, clean_redis
):
    sub = await _create_sub(client, monthly_amount_cents=4_000)
    sid = sub["subscription_id"]

    res = await client.post(
        f"/api/v1/subscriptions/{sid}/cancel",
        json={"effective": "end_of_period", "reason": "scheduled"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "active"  # still active until end of period
    assert body["mrr_cents"] == 4_000

    # Renew triggers the actual termination.
    res = await client.post(
        f"/api/v1/subscriptions/{sid}/renew", json={}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "cancelled"
    assert body["mrr_cents"] == 0
    assert body["movement"] == "churn"


# ──────────────────────────────────────────────────────────────────────────
# NDR / GRR brand metrics
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_brand_metrics_computes_ndr_grr_after_movements(client, clean_redis):
    """Create + expand + churn → NDR/GRR ratios surface correctly."""
    # Sub 1: 1000 MRR, expand to 3000.
    s1 = await _create_sub(
        client, brand_id="metric_brand", user_id="u1", monthly_amount_cents=1_000,
    )
    await client.post(
        f"/api/v1/subscriptions/{s1['subscription_id']}/upgrade",
        json={"new_plan_id": "pro", "new_monthly_amount_cents": 3_000},
    )

    # Sub 2: 5000 MRR, cancel immediately.
    s2 = await _create_sub(
        client, brand_id="metric_brand", user_id="u2", monthly_amount_cents=5_000,
    )
    await client.post(
        f"/api/v1/subscriptions/{s2['subscription_id']}/cancel",
        json={"effective": "immediate", "reason": "lost"},
    )

    res = await client.get(
        "/api/v1/subscriptions/brand/metric_brand/metrics", params={"period": "monthly"},
    )
    assert res.status_code == 200, res.text
    m = res.json()
    # new = 1000+5000 = 6000 MRR  → new_arr = 72_000
    # expansion = 2000 MRR        → expansion_arr = 24_000
    # churn = 5000 MRR            → churn_arr = 60_000
    assert m["new_arr_cents"] == 6_000 * 12
    assert m["expansion_arr_cents"] == 2_000 * 12
    assert m["churn_arr_cents"] == 5_000 * 12
    assert m["customer_count"] == 1  # sub1 only; sub2 cancelled
    # NDR/GRR must be finite, non-NaN, and non-negative.
    assert m["ndr"] >= 0
    assert 0 <= m["grr"] <= 1.0001
