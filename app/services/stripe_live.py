"""Stripe live integration wrapper.

Single source-of-truth for hitting the Stripe API across wallet top-ups,
payment-method onboarding and webhook ingress. Designed for three modes:

  * ``live``  — ``STRIPE_SECRET_KEY`` starts with ``sk_live_``. Real API.
  * ``test``  — ``STRIPE_SECRET_KEY`` starts with ``sk_test_``. Real API but
                Stripe's test mode (test cards, no real money).
  * ``mock``  — no key set, or sentinel ``sk_test_stub``. NEVER touches the
                network; returns deterministic, plausibly-shaped fake data
                so dev / CI / pytest runs are network-isolated.

The router code (``app.routers.wallet`` + ``app.routers.payment_methods``)
calls only the helpers in this module — never ``stripe.*`` directly — so we
get one central place to swap providers, mock cleanly, and audit live calls.

Currency is always passed in lowercase ISO-4217 per Stripe convention.
Amounts are always in the smallest currency unit (cents / 分 / 円 etc.).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Any
from uuid import uuid4

import stripe

logger = logging.getLogger(__name__)


# ── Mode detection ──────────────────────────────────────────────────────
# Centralised so wallet + payment_methods + tests all agree on which mode
# we're in. Re-read on every call (don't cache at import) so tests can
# monkeypatch ``os.environ`` without restarting the process.

_SENTINEL_STUB_KEY = "sk_test_stub"


def _api_key() -> str:
    return os.getenv("STRIPE_SECRET_KEY", "")


def _webhook_secret() -> str:
    return os.getenv("STRIPE_WEBHOOK_SECRET", "")


def get_mode() -> str:
    """Return one of ``"live"``, ``"test"``, ``"mock"``.

    Mock mode wins when the key is empty or the well-known dev sentinel.
    """
    key = _api_key()
    if not key or key == _SENTINEL_STUB_KEY:
        return "mock"
    if key.startswith("sk_live_"):
        return "live"
    if key.startswith("sk_test_"):
        return "test"
    # Unknown prefix → treat as mock so we never accidentally call live.
    logger.warning("STRIPE_SECRET_KEY has unknown prefix; falling back to mock")
    return "mock"


def is_mock() -> bool:
    return get_mode() == "mock"


def _sync_sdk_key() -> None:
    """Keep ``stripe.api_key`` aligned with the env var.

    Other modules (notably ``app.routers.payment_methods``) read
    ``stripe.api_key`` at import time. We re-sync before every live call
    so a late env-var change is honoured.
    """
    key = _api_key()
    if key:
        stripe.api_key = key


# ── Currency helpers ────────────────────────────────────────────────────
# Stripe expects ISO-4217 lowercase. The wallet layer hands us upper-case
# codes (CNY/USD/EUR/SGD…) — normalise here so callers don't have to think
# about it.

def _normalize_currency(currency: str | None) -> str:
    return (currency or "usd").strip().lower()


# ── Public API ──────────────────────────────────────────────────────────
def create_topup_checkout_session(
    brand_id: str,
    amount_cents: int,
    success_url: str,
    cancel_url: str,
    *,
    currency: str = "USD",
    reference_id: str | None = None,
) -> dict[str, Any]:
    """Create a Stripe Checkout session for a wallet top-up.

    Returns ``{session_id, checkout_url, mode}``. The ``mode`` field lets
    the caller log which path was used; clients should redirect the user
    to ``checkout_url``.

    Mock mode returns a deterministic fake URL pointing at our own host so
    integration tests can short-circuit the redirect.
    """
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")
    ref = reference_id or uuid4().hex
    cur = _normalize_currency(currency)

    if is_mock():
        session_id = f"cs_test_mock_{ref[:16]}"
        return {
            "session_id": session_id,
            "checkout_url": f"https://mock.stripe.local/checkout/{session_id}",
            "mode": "mock",
            "amount_cents": amount_cents,
            "currency": cur,
        }

    _sync_sdk_key()
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": cur,
                        "product_data": {"name": f"KiX wallet top-up ({brand_id})"},
                        "unit_amount": amount_cents,
                    },
                    "quantity": 1,
                }
            ],
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=ref,
            metadata={
                "brand_id": brand_id,
                "reference_id": ref,
                "purpose": "wallet_topup",
            },
            payment_intent_data={
                "metadata": {
                    "brand_id": brand_id,
                    "reference_id": ref,
                    "purpose": "wallet_topup",
                }
            },
        )
    except stripe.error.StripeError as exc:  # type: ignore[attr-defined]
        logger.exception("stripe checkout session create failed: %s", exc)
        raise

    return {
        "session_id": session.id,
        "checkout_url": session.url,
        "mode": get_mode(),
        "amount_cents": amount_cents,
        "currency": cur,
    }


def create_payment_method_setup_intent(
    brand_id: str,
    *,
    customer_id: str | None = None,
) -> dict[str, Any]:
    """Create a SetupIntent so the client can collect card details via Elements.

    Returns ``{client_secret, setup_intent_id, mode}``. The client uses
    ``client_secret`` with Stripe Elements; on success Stripe fires
    ``setup_intent.succeeded`` which the webhook attaches to the customer.
    """
    if is_mock():
        si_id = f"seti_mock_{uuid4().hex[:18]}"
        return {
            "client_secret": f"{si_id}_secret_mock",
            "setup_intent_id": si_id,
            "mode": "mock",
        }

    _sync_sdk_key()
    kwargs: dict[str, Any] = {
        "usage": "off_session",
        "payment_method_types": ["card"],
        "metadata": {"brand_id": brand_id, "purpose": "add_payment_method"},
    }
    if customer_id:
        kwargs["customer"] = customer_id

    try:
        si = stripe.SetupIntent.create(**kwargs)
    except stripe.error.StripeError as exc:  # type: ignore[attr-defined]
        logger.exception("stripe SetupIntent create failed: %s", exc)
        raise

    return {
        "client_secret": si.client_secret,
        "setup_intent_id": si.id,
        "mode": get_mode(),
    }


def attach_payment_method(
    customer_id: str, pm_id: str
) -> dict[str, Any]:
    """Attach a Stripe PaymentMethod to a Customer (post SetupIntent success).

    Idempotent — re-attaching an already-attached PM is a no-op in Stripe.
    """
    if not pm_id:
        raise ValueError("pm_id is required")
    if is_mock():
        return {
            "attached": True,
            "payment_method_id": pm_id,
            "customer_id": customer_id,
            "mode": "mock",
        }

    _sync_sdk_key()
    try:
        pm = stripe.PaymentMethod.attach(pm_id, customer=customer_id)
    except stripe.error.InvalidRequestError as exc:
        # "already attached" surfaces here; surface as success.
        if "already" in (str(exc) or "").lower():
            return {
                "attached": True,
                "payment_method_id": pm_id,
                "customer_id": customer_id,
                "mode": get_mode(),
                "note": "already_attached",
            }
        raise
    return {
        "attached": True,
        "payment_method_id": pm.id,
        "customer_id": customer_id,
        "mode": get_mode(),
    }


def detach_payment_method(pm_id: str) -> dict[str, Any]:
    """Detach a Stripe PaymentMethod from its customer."""
    if not pm_id:
        raise ValueError("pm_id is required")
    if is_mock():
        return {"detached": True, "payment_method_id": pm_id, "mode": "mock"}

    _sync_sdk_key()
    try:
        stripe.PaymentMethod.detach(pm_id)
    except stripe.error.InvalidRequestError as exc:
        # Already detached → treat as success.
        msg = (str(exc) or "").lower()
        if "not attached" in msg or "no longer" in msg:
            return {
                "detached": True,
                "payment_method_id": pm_id,
                "mode": get_mode(),
                "note": "already_detached",
            }
        raise
    return {"detached": True, "payment_method_id": pm_id, "mode": get_mode()}


def set_default_payment_method(
    customer_id: str, pm_id: str
) -> dict[str, Any]:
    """Set the given pm as the customer's default for invoices / off-session."""
    if not customer_id or not pm_id:
        raise ValueError("customer_id and pm_id required")
    if is_mock():
        return {
            "default": True,
            "customer_id": customer_id,
            "payment_method_id": pm_id,
            "mode": "mock",
        }

    _sync_sdk_key()
    try:
        stripe.Customer.modify(
            customer_id,
            invoice_settings={"default_payment_method": pm_id},
        )
    except stripe.error.StripeError as exc:  # type: ignore[attr-defined]
        logger.exception("set_default_payment_method failed: %s", exc)
        raise
    return {
        "default": True,
        "customer_id": customer_id,
        "payment_method_id": pm_id,
        "mode": get_mode(),
    }


# ── Webhook verification ────────────────────────────────────────────────
def verify_webhook_signature(
    payload: bytes | str, signature: str, secret: str | None = None
) -> dict[str, Any]:
    """Verify a Stripe webhook signature and return the parsed event.

    Uses Stripe's official ``Webhook.construct_event`` in live/test mode.
    In mock mode we accept any signature that matches a simple HMAC-SHA256
    of the payload using the configured ``STRIPE_WEBHOOK_SECRET`` (or
    ``"whsec_mock"`` if unset), so dev tooling can forge events without
    invoking the SDK.
    """
    body = payload.encode("utf-8") if isinstance(payload, str) else payload
    secret = secret or _webhook_secret()

    if is_mock():
        mock_secret = secret or "whsec_mock"
        expected = hmac.new(
            mock_secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        if signature and not hmac.compare_digest(signature, expected):
            raise ValueError("invalid_signature")
        import json as _json
        try:
            return _json.loads(body.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"invalid_payload: {exc}") from exc

    if not secret:
        raise ValueError("STRIPE_WEBHOOK_SECRET not configured")
    try:
        event = stripe.Webhook.construct_event(body, signature, secret)
    except stripe.error.SignatureVerificationError as exc:
        raise ValueError("invalid_signature") from exc
    except ValueError as exc:
        raise ValueError(f"invalid_payload: {exc}") from exc
    return dict(event)


def process_webhook(
    payload: bytes | str, signature: str, secret: str | None = None
) -> dict[str, Any]:
    """Verify + classify a webhook event.

    Returns ``{event_id, event_type, object, mode, verified_at}``. The
    actual side-effects (credit wallet, attach PM, mark sub) live in
    ``app.routers.stripe_webhook`` so this stays a pure dispatcher and
    keeps mock/live parity simple.
    """
    event = verify_webhook_signature(payload, signature, secret)
    try:
        obj = event["data"]["object"]
    except (KeyError, TypeError) as exc:
        raise ValueError("event missing data.object") from exc
    return {
        "event_id": event.get("id", ""),
        "event_type": event.get("type", ""),
        "object": obj,
        "mode": get_mode(),
        "verified_at": time.time(),
    }


# ── Misc helpers exported for routers ───────────────────────────────────
def sign_payload_for_mock(payload: bytes | str, secret: str | None = None) -> str:
    """Test helper — produce a valid mock signature for the given payload.

    Routers / tests use this to construct webhook posts in mock mode
    without pulling Stripe-SDK internals.
    """
    body = payload.encode("utf-8") if isinstance(payload, str) else payload
    mock_secret = (secret or _webhook_secret() or "whsec_mock").encode("utf-8")
    return hmac.new(mock_secret, body, hashlib.sha256).hexdigest()


__all__ = [
    "get_mode",
    "is_mock",
    "create_topup_checkout_session",
    "create_payment_method_setup_intent",
    "attach_payment_method",
    "detach_payment_method",
    "set_default_payment_method",
    "verify_webhook_signature",
    "process_webhook",
    "sign_payload_for_mock",
]
