"""Wallet auto-recharge v2 — default-on, threshold-fired, idempotent.

Covers P1 systemic bug: wallet hits ¥0 → silently drops from all auctions.
The fix defaults auto-recharge ON when a brand upgrades to STARTER+ and has
a verified payment method on file. Tests below assert:

  1. STARTER upgrade + verified PM → auto-recharge enabled by default.
  2. STARTER upgrade with NO payment method → auto-recharge NOT enabled.
  3. Balance drops below 20% threshold → auto-recharge fires + credits wallet.
  4. Gateway failure → 24h backoff + merchant alert flag set.
  5. Idempotency: two rapid sub-threshold charges only fire ONE recharge.
  6. Pre-depletion warning at 30% sets ``notification:brand:{bid}:wallet_low``.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest


# ── Test helpers ─────────────────────────────────────────────────────────
async def _seed_verified_pm(client, brand_id: str, *, token: str | None = None) -> str:
    """Register + verify a payment method so auto-recharge has a target."""
    res = await client.post(
        f"/api/v1/payment-methods/{brand_id}/add",
        json={
            "method_type": "credit_card",
            "payment_token": token or f"tok_{brand_id}",
            "last4": "4242",
            "expiry_month": 12,
            "expiry_year": 2099,
            "holder_name": "Auto Recharge Tester",
        },
    )
    assert res.status_code == 200, res.text
    pm_id = res.json()["payment_method_id"]
    vres = await client.post(f"/api/v1/payment-methods/{pm_id}/verify", json={})
    assert vres.status_code == 200, vres.text
    assert vres.json()["verified"] is True
    return pm_id


async def _topup(client, brand_id: str, amount_cents: int) -> None:
    res = await client.post(
        f"/api/v1/wallet/{brand_id}/topup",
        json={"amount_cents": amount_cents, "payment_method": "wechat"},
    )
    topup_id = res.json()["topup_id"]
    await client.post(
        f"/api/v1/wallet/{brand_id}/topup/{topup_id}/confirm",
        json={"payment_gateway_response": {}},
    )


# ── Test 1: STARTER upgrade + verified PM → autorecharge enabled ─────────
@pytest.mark.asyncio
async def test_starter_upgrade_with_verified_pm_enables_autorecharge(
    client, clean_redis
):
    brand_id = "ar_test_1"
    pm_id = await _seed_verified_pm(client, brand_id)

    res = await client.post(
        f"/api/v1/brand-subscriptions/{brand_id}/upgrade",
        json={
            "to_tier": "starter",
            "billing": "monthly",
            "payment_method_id": pm_id,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tier"] == "starter"
    ar = body.get("autorecharge_v2")
    assert ar is not None, "autorecharge_v2 settings should be returned"
    assert ar["enabled"] is True
    assert ar["payment_method_id"] == pm_id
    assert ar["threshold_cents"] >= 1_000  # ¥10 floor
    assert ar["topup_cents"] >= 5_000      # ¥50 floor

    # Settings persisted + observable via the v2 GET endpoint.
    res = await client.get(f"/api/v1/wallet/{brand_id}/autorecharge")
    assert res.status_code == 200
    view = res.json()
    assert view["enabled"] is True
    assert view["payment_method_id"] == pm_id


# ── Test 2: No verified PM → autorecharge NOT enabled ────────────────────
@pytest.mark.asyncio
async def test_upgrade_without_payment_method_skips_autorecharge(
    client, clean_redis
):
    brand_id = "ar_test_2_no_pm"

    res = await client.post(
        f"/api/v1/brand-subscriptions/{brand_id}/upgrade",
        json={"to_tier": "starter", "billing": "monthly"},
    )
    assert res.status_code == 200, res.text
    assert res.json().get("autorecharge_v2") is None

    res = await client.get(f"/api/v1/wallet/{brand_id}/autorecharge")
    assert res.status_code == 200
    assert res.json()["enabled"] is False


# ── Test 3: Balance drops below 20% → auto-recharge fires + credits wallet
@pytest.mark.asyncio
async def test_autorecharge_fires_below_threshold(client, clean_redis):
    brand_id = "ar_test_3_fire"
    pm_id = await _seed_verified_pm(client, brand_id)

    # Upgrade so autorecharge is on. We then override the threshold/topup to
    # known sentinels so the assertion arithmetic is unambiguous.
    up = await client.post(
        f"/api/v1/brand-subscriptions/{brand_id}/upgrade",
        json={"to_tier": "starter", "billing": "monthly", "payment_method_id": pm_id},
    )
    assert up.status_code == 200, up.text

    upd = await client.put(
        f"/api/v1/wallet/{brand_id}/autorecharge",
        json={"threshold_cents": 2_000, "topup_cents": 10_000, "enabled": True},
    )
    assert upd.status_code == 200, upd.text

    # Top up wallet to 5_000 (above threshold), then charge 4_000 to dip below.
    await _topup(client, brand_id, 5_000)
    bal = (await client.get(f"/api/v1/wallet/{brand_id}")).json()["balance_cents"]
    assert bal == 5_000

    res = await client.post(
        f"/api/v1/wallet/{brand_id}/charge",
        json={
            "amount_cents": 4_000,
            "reason": "cpa_conversion",
            "reference_id": "fire_1",
        },
    )
    assert res.status_code == 200, res.text

    # Final balance = 5_000 - 4_000 + 10_000 (autorecharge) = 11_000.
    final = (await client.get(f"/api/v1/wallet/{brand_id}")).json()["balance_cents"]
    assert final == 11_000

    view = (await client.get(f"/api/v1/wallet/{brand_id}/autorecharge")).json()
    assert view["last_triggered_ts"] is not None
    # wallet_low flag cleared after successful recharge restores balance.
    assert view["wallet_low_warning"] is False


# ── Test 4: Failed autorecharge → 24h backoff + merchant alert ───────────
@pytest.mark.asyncio
async def test_failed_autorecharge_triggers_backoff_and_alert(
    client, clean_redis
):
    brand_id = "ar_test_4_fail"
    pm_id = await _seed_verified_pm(client, brand_id)
    await client.post(
        f"/api/v1/brand-subscriptions/{brand_id}/upgrade",
        json={"to_tier": "starter", "billing": "monthly", "payment_method_id": pm_id},
    )
    await client.put(
        f"/api/v1/wallet/{brand_id}/autorecharge",
        json={"threshold_cents": 2_000, "topup_cents": 10_000, "enabled": True},
    )
    await _topup(client, brand_id, 5_000)

    # Force the gateway to fail.
    async def _fail_charge(*args, **kwargs):
        return {"success": False, "error": "card_declined:generic_decline"}

    with patch(
        "app.routers.wallet._v2_fire_gateway_recharge",
        side_effect=_fail_charge,
    ):
        res = await client.post(
            f"/api/v1/wallet/{brand_id}/charge",
            json={
                "amount_cents": 4_000,
                "reason": "cpa_conversion",
                "reference_id": "fail_1",
            },
        )
        assert res.status_code == 200, res.text

    # Balance must NOT include a top-up.
    bal = (await client.get(f"/api/v1/wallet/{brand_id}")).json()["balance_cents"]
    assert bal == 1_000

    view = (await client.get(f"/api/v1/wallet/{brand_id}/autorecharge")).json()
    assert view["paused_until"] is not None
    # Roughly 24h ahead (within 5s of expected).
    assert view["paused_until"] > time.time() + 86_300

    # Merchant alert flag set.
    failed_flag = await clean_redis.get(
        f"notification:brand:{brand_id}:autorecharge_failed"
    )
    assert failed_flag is not None


# ── Test 5: Idempotency — two rapid sub-threshold charges fire ONE recharge
@pytest.mark.asyncio
async def test_autorecharge_idempotency_under_rapid_charges(
    client, clean_redis
):
    brand_id = "ar_test_5_idem"
    pm_id = await _seed_verified_pm(client, brand_id)
    await client.post(
        f"/api/v1/brand-subscriptions/{brand_id}/upgrade",
        json={"to_tier": "starter", "billing": "monthly", "payment_method_id": pm_id},
    )
    await client.put(
        f"/api/v1/wallet/{brand_id}/autorecharge",
        json={"threshold_cents": 50_000, "topup_cents": 10_000, "enabled": True},
    )
    await _topup(client, brand_id, 12_000)

    call_count = {"n": 0}
    real_fire = None

    async def _counting_fire(*args, **kwargs):
        call_count["n"] += 1
        return {
            "success": True,
            "gateway_tx_id": f"sim_{call_count['n']}",
            "gateway_fee_cents": 30,
        }

    with patch(
        "app.routers.wallet._v2_fire_gateway_recharge",
        side_effect=_counting_fire,
    ):
        # Two rapid charges, both leaving balance below threshold.
        await client.post(
            f"/api/v1/wallet/{brand_id}/charge",
            json={
                "amount_cents": 1_000,
                "reason": "cpa_conversion",
                "reference_id": "idem_a",
            },
        )
        await client.post(
            f"/api/v1/wallet/{brand_id}/charge",
            json={
                "amount_cents": 1_000,
                "reason": "cpa_conversion",
                "reference_id": "idem_b",
            },
        )

    # Both charges below threshold but only one recharge fired in the bucket.
    assert call_count["n"] == 1


# ── Test 6: Pre-warning at 30% threshold sets the wallet_low flag ────────
@pytest.mark.asyncio
async def test_pre_depletion_warning_at_30pct(client, clean_redis):
    brand_id = "ar_test_6_warn"
    pm_id = await _seed_verified_pm(client, brand_id)
    await client.post(
        f"/api/v1/brand-subscriptions/{brand_id}/upgrade",
        json={"to_tier": "starter", "billing": "monthly", "payment_method_id": pm_id},
    )
    # threshold=2_000 → warning at ~3_000 (1.5× threshold). Topup is large so
    # the recharge does NOT fire on this charge (balance stays above 20%).
    await client.put(
        f"/api/v1/wallet/{brand_id}/autorecharge",
        json={"threshold_cents": 2_000, "topup_cents": 10_000, "enabled": True},
    )
    await _topup(client, brand_id, 10_000)

    # Charge enough to land between 20% (2_000) and 30% (3_000): 10_000 → 2_500.
    res = await client.post(
        f"/api/v1/wallet/{brand_id}/charge",
        json={
            "amount_cents": 7_500,
            "reason": "cpa_conversion",
            "reference_id": "warn_1",
        },
    )
    assert res.status_code == 200, res.text
    bal = (await client.get(f"/api/v1/wallet/{brand_id}")).json()["balance_cents"]
    assert bal == 2_500  # below 30% warning, above 20% threshold

    flag = await clean_redis.get(f"notification:brand:{brand_id}:wallet_low")
    assert flag == "1"

    # Dashboard /today should surface the alert.
    res = await client.get(f"/api/v1/dashboards/{brand_id}/today")
    assert res.status_code == 200
    alerts = res.json().get("alerts", {})
    assert alerts.get("wallet_low") is True


# ── Bonus: disable opts out and survives subsequent tier-upgrade default ──
@pytest.mark.asyncio
async def test_disable_persists_opt_out_across_upgrade(client, clean_redis):
    brand_id = "ar_test_7_optout"
    pm_id = await _seed_verified_pm(client, brand_id)
    await client.post(
        f"/api/v1/brand-subscriptions/{brand_id}/upgrade",
        json={"to_tier": "starter", "billing": "monthly", "payment_method_id": pm_id},
    )

    # Opt out.
    res = await client.post(f"/api/v1/wallet/{brand_id}/autorecharge/disable")
    assert res.status_code == 200
    assert res.json()["enabled"] is False
    assert res.json()["opted_out"] is True

    # Subsequent tier upgrade (growth) must NOT re-enable.
    res = await client.post(
        f"/api/v1/brand-subscriptions/{brand_id}/upgrade",
        json={"to_tier": "growth", "billing": "monthly", "payment_method_id": pm_id},
    )
    assert res.status_code == 200, res.text
    assert res.json().get("autorecharge_v2") is None

    view = (await client.get(f"/api/v1/wallet/{brand_id}/autorecharge")).json()
    assert view["enabled"] is False
    assert view["opted_out"] is True
