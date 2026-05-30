"""PayNow (Singapore) PSP wrapper.

PayNow is Singapore's interbank instant-transfer rail (run by ABS).
Merchant integration is typically via an acquiring bank (DBS, OCBC,
UOB) that exposes a REST API to mint a SGQR-encoded payload and
deliver a webhook on receipt. The actual API shape varies by bank;
this wrapper abstracts over a generic "Corporate PayNow" interface.

Modes
-----
* ``live`` — ``PAYNOW_LIVE_API_KEY`` set (production acquirer)
* ``test`` — ``PAYNOW_TEST_API_KEY`` set (sandbox acquirer)
* ``mock`` — neither set; returns deterministic test fixtures

Flow
----
1. ``create_charge`` mints a SGQR-encoded reference + QR string.
   The mobile client renders the QR or shares the proxy ID.
2. Customer's banking app debits → acquirer fires webhook.
3. ``verify_webhook`` validates HMAC-SHA256 signature.
4. ``process_event`` standardises the payload.

Currency: SGD only (registry-enforced).
"""

from __future__ import annotations

import logging
import os
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


class PayNowClient(PSPClient):
    psp_code = "paynow"
    _SUPPORTED_CURRENCIES = {"SGD"}

    def get_mode(self) -> str:
        return detect_mode(
            live_key="PAYNOW_LIVE_API_KEY", test_key="PAYNOW_TEST_API_KEY"
        )

    def _webhook_secret(self) -> str:
        return os.getenv("PAYNOW_WEBHOOK_SECRET", "whsec_paynow_stub")

    def _merchant_uen(self) -> str:
        # Singapore UEN — Unique Entity Number — used as PayNow proxy.
        return os.getenv("PAYNOW_MERCHANT_UEN", "201912345K")

    def create_charge(
        self,
        amount: int,
        currency: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if amount <= 0:
            raise ValueError("amount must be positive")
        cur = normalize_currency(currency, default="SGD")
        if cur not in self._SUPPORTED_CURRENCIES:
            raise ValueError(f"PayNow only supports SGD, got {cur}")

        meta = dict(metadata or {})
        ref = meta.get("reference_id") or uuid4().hex[:16]
        charge_id = mock_charge_id("paynow", ref)
        mode = self.get_mode()

        # SGQR payload — EMVCo-compliant string. In mock mode we mint a
        # well-formed but obviously-fake string so the mobile renderer
        # can still draw a QR without erroring.
        sgqr_payload = (
            f"00020101021126480009SG.PAYNOW010120208{self._merchant_uen()}"
            f"5204000053037025802SG540{len(str(amount))}{amount}"
            f"5917KIX Pte Ltd6209Singapore62240520{ref}6304ABCD"
        )

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
            "qr_code": sgqr_payload,
            "proxy_id": self._merchant_uen(),
            "proxy_type": "UEN",
            "amount": amount,
            "currency": cur,
            "mode": mode,
            "metadata": meta,
        }

    def verify_webhook(
        self, payload: bytes | str, signature: str
    ) -> dict[str, Any]:
        secret = self._webhook_secret()
        if not hmac_verify(payload, signature, secret):
            emit_audit(
                {
                    "psp": self.psp_code,
                    "action": "verify_webhook",
                    "result": "invalid_signature",
                }
            )
            raise ValueError("PayNow webhook signature mismatch")
        return parse_payload(payload)

    def process_event(self, event: dict[str, Any]) -> dict[str, Any]:
        # PayNow acquirer events: { "event_type", "transaction_id",
        # "amount", "currency", "metadata": {brand_id, reference_id} }
        event_type = event.get("event_type", "")
        canonical_type = {
            "payment.received": "charge.succeeded",
            "payment.failed": "charge.failed",
            "refund.completed": "refund.succeeded",
        }.get(event_type, event_type or "charge.succeeded")

        meta = event.get("metadata", {}) or {}
        out = {
            "psp": self.psp_code,
            "event_type": canonical_type,
            "charge_id": event.get("transaction_id") or event.get("charge_id"),
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
        refund_id = mock_charge_id("paynow_rf", charge_id[-8:])
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
        return {
            "psp": self.psp_code,
            "mode": mode,
            "ready": True,  # PayNow has no auth probe; UEN is local config
            "last_charge_ts": get_last_charge_ts(self.psp_code),
        }
