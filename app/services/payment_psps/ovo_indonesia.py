"""OVO (Indonesia) PSP wrapper.

OVO is one of Indonesia's largest e-wallets. Direct merchant
integration is via the OVO Open Banking API (post-Grab acquisition,
now under Grab's umbrella but exposed as a distinct PSP for accounting
purposes). Mobile-first: charges are confirmed via push notification
to the OVO app on the customer's registered phone number.

Modes
-----
* ``live`` — ``OVO_LIVE_APP_ID`` + ``OVO_LIVE_APP_KEY`` set
* ``test`` — ``OVO_TEST_APP_ID`` set
* ``mock`` — neither set
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


class OVOClient(PSPClient):
    psp_code = "ovo"
    _SUPPORTED_CURRENCIES = {"IDR"}

    def get_mode(self) -> str:
        return detect_mode(
            live_key="OVO_LIVE_APP_ID",
            test_key="OVO_TEST_APP_ID",
        )

    def _hmac_secret(self) -> str:
        return os.getenv("OVO_WEBHOOK_SECRET", "whsec_ovo_stub")

    def create_charge(
        self,
        amount: int,
        currency: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if amount <= 0:
            raise ValueError("amount must be positive")
        cur = normalize_currency(currency, default="IDR")
        if cur not in self._SUPPORTED_CURRENCIES:
            raise ValueError(
                f"OVO only supports IDR; got {cur}"
            )

        meta = dict(metadata or {})
        phone = meta.get("ovo_phone") or meta.get("customer_phone")
        if not phone:
            # In live mode we'd require this; mock mode picks a stable
            # placeholder so callers don't have to know about it yet.
            phone = "081234567890"

        ref = meta.get("reference_id") or uuid4().hex[:16]
        charge_id = mock_charge_id("ovo", ref)
        mode = self.get_mode()
        host = "mock.ovo.local" if mode == "mock" else "api.ovo.id"

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
            "deeplink": f"ovo://pay?ref={charge_id}",
            "checkout_url": f"https://{host}/pay/{charge_id}",
            "phone_hint": (
                f"{phone[:4]}*****{phone[-2:]}" if len(phone) > 6 else phone
            ),
            "amount": amount,
            "currency": cur,
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
            raise ValueError("OVO webhook signature mismatch")
        return parse_payload(payload)

    def process_event(self, event: dict[str, Any]) -> dict[str, Any]:
        # OVO sends: { "event": "PAYMENT_SUCCESS", "transactionId",
        # "amount", "merchantReference": {...}, "metadata": {...} }
        event_type = event.get("event") or event.get("event_type") or ""
        canonical_type = {
            "PAYMENT_SUCCESS": "charge.succeeded",
            "PAYMENT_FAILED": "charge.failed",
            "REFUND_SUCCESS": "refund.succeeded",
        }.get(event_type, event_type or "charge.succeeded")

        meta = (
            event.get("metadata")
            or event.get("merchantReference")
            or {}
        )
        if isinstance(meta, str):
            meta = parse_payload(meta)

        out = {
            "psp": self.psp_code,
            "event_type": canonical_type,
            "charge_id": (
                event.get("transactionId")
                or event.get("charge_id")
            ),
            "amount_cents": int(event.get("amount") or 0),
            "currency": normalize_currency(
                event.get("currency"), default="IDR"
            ),
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
        refund_id = mock_charge_id("ovo_rf", charge_id[-8:])
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
        ready = mode != "live" or bool(os.getenv("OVO_LIVE_APP_KEY"))
        return {
            "psp": self.psp_code,
            "mode": mode,
            "ready": ready,
            "last_charge_ts": get_last_charge_ts(self.psp_code),
        }
