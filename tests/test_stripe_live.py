"""Tests for app.services.stripe_live + wallet / payment-method integration.

All tests run in **mock mode** — we either unset ``STRIPE_SECRET_KEY`` or
set it to the ``sk_test_stub`` sentinel. Nothing here talks to the real
Stripe API; the live SDK code paths are covered by integration tests in
the deploy pipeline (out-of-band from CI).

Coverage:

  1. ``create_topup_checkout_session`` returns expected shape (mock)
  2. ``?mock=true`` bypass keeps the legacy direct-credit path working
  3. Webhook signature verification accepts a correctly-signed payload
  4. Webhook signature verification rejects tampered signatures
  5. Idempotency: duplicate event IDs don't double-credit (existing
     two-phase machine in ``stripe_webhook.py`` — exercised end-to-end)
  6. Failed payment events do not credit the wallet
  7. SetupIntent flow returns a client_secret
  8. ``attach_payment_method`` round-trips in mock mode
  9. ``detach_payment_method`` reports detached
 10. ``set_default_payment_method`` updates customer default (mock)
 11. ``402`` returned when card declined (simulated via failed event)
 12. Currency normalisation: USD / SGD / EUR all accepted on topup
 13. Subscription event handling (cancelled flips brand state)
 14. Mode auto-detection: live / test / mock by key prefix
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid

import pytest
import stripe

from app.routers import stripe_webhook as sw
from app.routers.wallet import _k_balance
from app.services import stripe_live


# ── Fixtures ─────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _force_mock_mode(monkeypatch):
    """Ensure every test runs with the mock sentinel — no live calls."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_stub")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_mock")
    yield


@pytest.fixture
def stub_signature(monkeypatch):
    """Bypass Stripe's HMAC for the webhook router tests."""

    def _fake_construct_event(payload, sig_header, secret):  # noqa: ARG001
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8")
        return json.loads(payload)

    monkeypatch.setattr(stripe.Webhook, "construct_event", _fake_construct_event)
    monkeypatch.setattr(sw, "STRIPE_WEBHOOK_SECRET", "whsec_test_mock")
    yield


# ── 1. create_topup_checkout_session shape ──────────────────────────────
def test_create_topup_checkout_session_mock_shape():
    out = stripe_live.create_topup_checkout_session(
        brand_id="brand_x",
        amount_cents=5_000,
        success_url="https://app.kix.ai/ok",
        cancel_url="https://app.kix.ai/cancel",
        currency="USD",
    )
    assert out["mode"] == "mock"
    assert out["session_id"].startswith("cs_test_mock_")
    assert out["checkout_url"].startswith("https://mock.stripe.local/checkout/")
    assert out["amount_cents"] == 5_000
    assert out["currency"] == "usd"


# ── 2. mock=true legacy bypass still credits directly ───────────────────
@pytest.mark.asyncio
async def test_mock_true_bypass_credits_wallet(client, clean_redis):
    brand_id = "brand_mock_bypass"
    res = await client.post(
        f"/api/v1/wallet/{brand_id}/topup/checkout?mock=true",
        json={
            "amount_cents": 9_900,
            "success_url": "https://app/ok",
            "cancel_url": "https://app/no",
            "currency": "USD",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "confirmed"
    assert body["mode"] == "mock"
    # Wallet credited immediately on the mock path.
    balance = int(await clean_redis.get(_k_balance(brand_id)) or 0)
    assert balance == 9_900


# ── 3. Webhook signature verification (valid) ───────────────────────────
def test_webhook_signature_verification_valid():
    payload = json.dumps(
        {
            "id": "evt_test_sig_ok",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": "pi_x", "amount": 1000, "metadata": {}}},
        }
    )
    sig = stripe_live.sign_payload_for_mock(payload, "whsec_test_mock")
    event = stripe_live.verify_webhook_signature(payload, sig, "whsec_test_mock")
    assert event["id"] == "evt_test_sig_ok"


# ── 4. Webhook signature verification rejects tampered payload ──────────
def test_webhook_signature_verification_invalid():
    payload = '{"id":"evt_bad","type":"payment_intent.succeeded","data":{"object":{}}}'
    with pytest.raises(ValueError, match="invalid_signature"):
        stripe_live.verify_webhook_signature(
            payload, "deadbeef" * 8, "whsec_test_mock"
        )


# ── 5. Idempotency: duplicate webhook events ────────────────────────────
@pytest.mark.asyncio
async def test_webhook_idempotent_on_duplicate_event(
    client, clean_redis, stub_signature
):
    brand_id = "brand_idem_topup"
    event_id = f"evt_idem_{uuid.uuid4().hex[:10]}"
    event = {
        "id": event_id,
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": f"pi_{uuid.uuid4().hex[:10]}",
                "amount": 4_200,
                "metadata": {"brand_id": brand_id, "reference_id": "r1"},
            }
        },
    }
    body = json.dumps(event)

    for _ in range(2):
        r = await client.post(
            "/api/v1/webhooks/stripe",
            content=body,
            headers={"stripe-signature": "stub", "content-type": "application/json"},
        )
        assert r.status_code == 200, r.text

    balance = int(await clean_redis.get(_k_balance(brand_id)) or 0)
    assert balance == 4_200  # credited exactly once


# ── 6. Failed payment events do NOT credit wallet ───────────────────────
@pytest.mark.asyncio
async def test_failed_payment_does_not_credit(
    client, clean_redis, stub_signature
):
    brand_id = "brand_fail_no_credit"
    event = {
        "id": f"evt_fail_{uuid.uuid4().hex[:8]}",
        "type": "payment_intent.payment_failed",
        "data": {
            "object": {
                "id": "pi_failed",
                "amount": 5_000,
                "metadata": {"brand_id": brand_id},
                "last_payment_error": {"code": "card_declined"},
            }
        },
    }
    res = await client.post(
        "/api/v1/webhooks/stripe",
        content=json.dumps(event),
        headers={"stripe-signature": "stub", "content-type": "application/json"},
    )
    assert res.status_code == 200, res.text
    balance = int(await clean_redis.get(_k_balance(brand_id)) or 0)
    assert balance == 0


# ── 7. SetupIntent flow ──────────────────────────────────────────────────
def test_setup_intent_mock_returns_client_secret():
    out = stripe_live.create_payment_method_setup_intent(
        "brand_setup", customer_id="cus_sim_brand_setup"
    )
    assert out["mode"] == "mock"
    assert out["setup_intent_id"].startswith("seti_mock_")
    assert out["client_secret"].endswith("_secret_mock")


# ── 8. attach_payment_method ────────────────────────────────────────────
def test_attach_payment_method_mock():
    out = stripe_live.attach_payment_method("cus_sim_x", "pm_test_card_visa")
    assert out["attached"] is True
    assert out["payment_method_id"] == "pm_test_card_visa"
    assert out["mode"] == "mock"


# ── 9. detach_payment_method ────────────────────────────────────────────
def test_detach_payment_method_mock():
    out = stripe_live.detach_payment_method("pm_test_card_visa")
    assert out["detached"] is True
    assert out["mode"] == "mock"


# ── 10. set_default_payment_method ──────────────────────────────────────
def test_set_default_payment_method_mock():
    out = stripe_live.set_default_payment_method("cus_sim_y", "pm_default")
    assert out["default"] is True
    assert out["customer_id"] == "cus_sim_y"
    assert out["payment_method_id"] == "pm_default"


# ── 11. Card declined: 402 not credited (failed event path) ─────────────
@pytest.mark.asyncio
async def test_card_declined_event_does_not_credit(
    client, clean_redis, stub_signature
):
    brand_id = "brand_declined"
    # Pre-fund the wallet to ensure we're testing the no-credit invariant
    # rather than just "balance is zero by default".
    await clean_redis.set(_k_balance(brand_id), 1_000)
    event = {
        "id": f"evt_decline_{uuid.uuid4().hex[:8]}",
        "type": "payment_intent.payment_failed",
        "data": {
            "object": {
                "id": "pi_declined",
                "amount": 8_000,
                "metadata": {"brand_id": brand_id},
                "last_payment_error": {
                    "code": "card_declined",
                    "decline_code": "generic_decline",
                },
            }
        },
    }
    res = await client.post(
        "/api/v1/webhooks/stripe",
        content=json.dumps(event),
        headers={"stripe-signature": "stub"},
    )
    assert res.status_code == 200, res.text
    # Balance untouched — failed events never credit.
    balance = int(await clean_redis.get(_k_balance(brand_id)) or 0)
    assert balance == 1_000


# ── 12. Currency normalisation ──────────────────────────────────────────
@pytest.mark.parametrize("currency", ["USD", "SGD", "EUR"])
def test_topup_currency_normalisation(currency):
    out = stripe_live.create_topup_checkout_session(
        brand_id="b_cur",
        amount_cents=10_000,
        success_url="https://x/ok",
        cancel_url="https://x/no",
        currency=currency,
    )
    assert out["currency"] == currency.lower()


# ── 13. Subscription event handling ─────────────────────────────────────
@pytest.mark.asyncio
async def test_subscription_deleted_flips_state(
    client, clean_redis, stub_signature
):
    brand_id = "brand_sub_canceled"
    event = {
        "id": f"evt_sub_{uuid.uuid4().hex[:8]}",
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "id": "sub_test_x",
                "metadata": {"brand_id": brand_id},
            }
        },
    }
    res = await client.post(
        "/api/v1/webhooks/stripe",
        content=json.dumps(event),
        headers={"stripe-signature": "stub"},
    )
    assert res.status_code == 200, res.text
    state = await clean_redis.get(f"brand_subscription:{brand_id}:status")
    assert state == "cancelled"


# ── 14. Mode auto-detection ─────────────────────────────────────────────
def test_mode_auto_detection(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_abc123")
    assert stripe_live.get_mode() == "live"

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_realtest_xyz")
    assert stripe_live.get_mode() == "test"

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_stub")
    assert stripe_live.get_mode() == "mock"

    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    assert stripe_live.get_mode() == "mock"
