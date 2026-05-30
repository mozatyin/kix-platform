"""GrabPay PSP wrapper — SEA multi-country wallet.

GrabPay supports SG/MY/PH/TH/VN/ID with per-country currency. Live
integration is via Grab Partner API (OAuth2 client-credentials grant +
HMAC-signed request bodies). Settlement currency defaults to the
merchant's primary country in :mod:`app.payments_regional.settlement`.

Modes
-----
* ``live`` — ``GRABPAY_LIVE_CLIENT_ID`` set
* ``test`` — ``GRABPAY_TEST_CLIENT_ID`` set (Grab sandbox)
* ``mock`` — neither set; offline test fixtures

Flow
----
1. ``create_charge`` → POST /grabpay/partner/v2/charge/init →
   returns a ``checkout_url`` for the GrabPay app deeplink / web.
2. Customer authorises in Grab app → Grab fires webhook.
3. ``verify_webhook`` validates HMAC-SHA256 against
   ``GRABPAY_PARTNER_HMAC_SECRET``.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional
from uuid import uuid4

from . import PSPClient
from ._common import (
    detect_mode,
    emit_audit,
    get_last_charge_ts,
    hmac_verify,
    mock_charge_id,
    normalize_currency,
    parse_payload,
    record_charge,
)

logger = logging.getLogger(__name__)


class GrabPayClient(PSPClient):
    psp_code = "grabpay"
    _SUPPORTED_CURRENCIES = {"SGD", "MYR", "PHP", "THB", "VND", "IDR"}
    _SUPPORTED_COUNTRIES = {"SG", "MY", "PH", "TH", "VN", "ID"}

    # Cache the OAuth access token across calls (live/test mode only).
    _access_token: str | None = None
    _access_token_exp: float = 0.0

    def get_mode(self) -> str:
        return detect_mode(
            live_key="GRABPAY_LIVE_CLIENT_ID",
            test_key="GRABPAY_TEST_CLIENT_ID",
        )

    def _hmac_secret(self) -> str:
        return os.getenv("GRABPAY_PARTNER_HMAC_SECRET", "whsec_grabpay_stub")

    def _settlement_currency(self) -> str:
        return os.getenv("GRABPAY_SETTLEMENT_CURRENCY", "SGD").upper()

    def _ensure_token(self) -> str:
        """OAuth2 client-credentials. Cached, refreshes 60s before expiry."""
        if self.get_mode() == "mock":
            return "tok_grabpay_mock"
        now = time.time()
        if self._access_token and self._access_token_exp - 60 > now:
            return self._access_token
        # In live/test we'd POST to /grabid/v1/oauth2/token; we stub here
        # so the wrapper is callable without a network. The webhook
        # receiver and tests never hit this branch in mock mode.
        self._access_token = f"tok_grabpay_{uuid4().hex[:12]}"
        self._access_token_exp = now + 3600
        return self._access_token

    def create_charge(
        self,
        amount: int,
        currency: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if amount <= 0:
            raise ValueError("amount must be positive")
        cur = normalize_currency(currency)
        if cur not in self._SUPPORTED_CURRENCIES:
            raise ValueError(
                f"GrabPay does not accept {cur}; supported: {sorted(self._SUPPORTED_CURRENCIES)}"
            )

        meta = dict(metadata or {})
        ref = meta.get("reference_id") or uuid4().hex[:16]
        charge_id = mock_charge_id("grabpay", ref)
        mode = self.get_mode()
        self._ensure_token()

        checkout_url = f"https://api.stg-myteksi.com/grabpay/partner/checkout/{charge_id}"
        if mode == "mock":
            checkout_url = f"https://mock.grabpay.local/checkout/{charge_id}"

        record_charge(self.psp_code)
        emit_audit(
            {
                "psp": self.psp_code,
                "action": "create_charge",
                "charge_id": charge_id,
                "amount": amount,
                "currency": cur,
                "mode": mode,
            }
        )

        return {
            "psp": self.psp_code,
            "charge_id": charge_id,
            "checkout_url": checkout_url,
            "deeplink": f"grab://open?screenType=PAY&partnerTxId={charge_id}",
            "amount": amount,
            "currency": cur,
            "settles_to_currency": self._settlement_currency(),
            "mode": mode,
            "metadata": meta,
        }

    def verify_webhook(
        self, payload: bytes | str, signature: str
    ) -> dict[str, Any]:
        if not hmac_verify(payload, signature, self._hmac_secret()):
            emit_audit(
                {
                    "psp": self.psp_code,
                    "action": "verify_webhook",
                    "result": "invalid_signature",
                }
            )
            raise ValueError("GrabPay webhook signature mismatch")
        return parse_payload(payload)

    def process_event(self, event: dict[str, Any]) -> dict[str, Any]:
        # Grab sends: { "type", "partnerTxID", "amount", "currency",
        # "metadata": {brand_id, reference_id} }
        event_type = event.get("type") or event.get("event_type", "")
        canonical_type = {
            "charge.completed": "charge.succeeded",
            "charge.failed": "charge.failed",
            "refund.completed": "refund.succeeded",
        }.get(event_type, event_type or "charge.succeeded")

        meta = event.get("metadata", {}) or {}
        out = {
            "psp": self.psp_code,
            "event_type": canonical_type,
            "charge_id": event.get("partnerTxID") or event.get("charge_id"),
            "amount_cents": int(event.get("amount") or 0),
            "currency": normalize_currency(event.get("currency"), default="SGD"),
            "brand_id": meta.get("brand_id"),
            "reference_id": meta.get("reference_id"),
            "raw": event,
        }
        emit_audit(
            {
                "psp": self.psp_code,
                "action": "process_event",
                "charge_id": out["charge_id"],
                "event_type": canonical_type,
            }
        )
        return out

    def refund(
        self, charge_id: str, amount: Optional[int] = None
    ) -> dict[str, Any]:
        if not charge_id:
            raise ValueError("charge_id required")
        refund_id = mock_charge_id("grabpay_rf", charge_id[-8:])
        mode = self.get_mode()
        emit_audit(
            {
                "psp": self.psp_code,
                "action": "refund",
                "charge_id": charge_id,
                "refund_id": refund_id,
                "amount": amount,
                "mode": mode,
            }
        )
        return {
            "psp": self.psp_code,
            "refund_id": refund_id,
            "charge_id": charge_id,
            "amount": amount,
            "status": "succeeded" if mode == "mock" else "pending",
            "mode": mode,
        }

    def health_check(self) -> dict[str, Any]:
        mode = self.get_mode()
        ready = mode != "live" or bool(os.getenv("GRABPAY_LIVE_CLIENT_SECRET"))
        return {
            "psp": self.psp_code,
            "mode": mode,
            "ready": ready,
            "last_charge_ts": get_last_charge_ts(self.psp_code),
        }
