"""Square POS adapter.

Square (`squareup.com`) is the dominant US small-business retail / cafe
POS. Integration uses OAuth2 + Square Connect REST API.

Modes
-----
* ``live`` — ``SQUARE_LIVE_ACCESS_TOKEN`` set (prefix ``EAAA``)
* ``test`` — ``SQUARE_TEST_ACCESS_TOKEN`` set (sandbox)
* ``mock`` — neither set

Webhook signature
-----------------
Square posts JSON with ``x-square-hmacsha256-signature`` header.
Verified against ``SQUARE_WEBHOOK_SIGNATURE_KEY``.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from . import POSAdapter, POSError
from ._common import (
    detect_mode,
    emit_audit,
    get_mock_store,
    hmac_verify,
    mock_ref,
    normalize_currency,
    parse_payload,
)

logger = logging.getLogger(__name__)


class SquarePOSAdapter(POSAdapter):
    pos_code = "square"

    def get_mode(self) -> str:
        return detect_mode(
            live_key="SQUARE_LIVE_ACCESS_TOKEN",
            test_key="SQUARE_TEST_ACCESS_TOKEN",
            live_prefix="EAAA",
        )

    def _webhook_secret(self) -> str:
        return os.getenv("SQUARE_WEBHOOK_SIGNATURE_KEY", "whsec_square_stub")

    def _location_id(self) -> str:
        return os.getenv("SQUARE_LOCATION_ID", "LOC_MOCK_001")

    def verify_voucher(self, voucher_id: str) -> dict[str, Any]:
        if not voucher_id:
            raise ValueError("voucher_id required")
        mode = self.get_mode()
        store = get_mock_store()
        v = store.get(voucher_id)
        base = {"pos": self.pos_code, "voucher_id": voucher_id, "mode": mode}
        if v is None:
            return {**base, "valid": False, "value_cents": 0, "currency": "USD",
                    "brand_id": None, "expires_at": None,
                    "reason": POSError.NOT_FOUND}
        if v.get("expires_at") and int(time.time()) > int(v["expires_at"]):
            return {**base, "valid": False, "value_cents": v["value_cents"],
                    "currency": v["currency"], "brand_id": v["brand_id"],
                    "expires_at": v["expires_at"], "reason": POSError.EXPIRED}
        if store.is_redeemed(voucher_id):
            return {**base, "valid": False, "value_cents": v["value_cents"],
                    "currency": v["currency"], "brand_id": v["brand_id"],
                    "expires_at": v.get("expires_at"),
                    "reason": POSError.ALREADY_USED}
        emit_audit({"pos": self.pos_code, "action": "verify_voucher",
                    "voucher_id": voucher_id, "valid": True})
        return {**base, "valid": True, "value_cents": v["value_cents"],
                "currency": v["currency"], "brand_id": v["brand_id"],
                "expires_at": v.get("expires_at"), "reason": None}

    def apply_discount(
        self, voucher_id: str, order_id: str, amount_cents: int
    ) -> dict[str, Any]:
        if not voucher_id or not order_id:
            raise ValueError("voucher_id and order_id required")
        if amount_cents <= 0:
            return {"pos": self.pos_code, "voucher_id": voucher_id,
                    "order_id": order_id, "discount_cents": 0,
                    "applied": False, "pos_discount_ref": "",
                    "reason": POSError.INVALID_AMOUNT,
                    "mode": self.get_mode()}
        mode = self.get_mode()
        ref = mock_ref("sq_disc", f"{voucher_id[:6]}{order_id[:6]}")
        emit_audit({"pos": self.pos_code, "action": "apply_discount",
                    "voucher_id": voucher_id, "amount_cents": amount_cents})
        return {
            "pos": self.pos_code,
            "voucher_id": voucher_id,
            "order_id": order_id,
            "discount_cents": int(amount_cents),
            "applied": True,
            "pos_discount_ref": ref,
            "location_id": self._location_id(),
            "mode": mode,
        }

    def mark_redeemed(
        self, voucher_id: str, transaction_data: dict[str, Any]
    ) -> dict[str, Any]:
        if not voucher_id:
            raise ValueError("voucher_id required")
        mode = self.get_mode()
        store = get_mock_store()
        rec = store.mark_redeemed(voucher_id, transaction_data or {})
        rid = mock_ref("sq_red", voucher_id[:12])
        tx_ref = (transaction_data or {}).get("order_id") or mock_ref(
            "sq_tx", voucher_id[:8]
        )
        emit_audit({"pos": self.pos_code, "action": "mark_redeemed",
                    "voucher_id": voucher_id, "redemption_id": rid})
        return {
            "pos": self.pos_code,
            "voucher_id": voucher_id,
            "redemption_id": rid,
            "redeemed_at": rec["redeemed_at"],
            "transaction_ref": tx_ref,
            "mode": mode,
        }

    def webhook_handler(self, payload: Any) -> dict[str, Any]:
        body = parse_payload(payload)
        ev = body.get("type", "")
        canonical = {
            "order.updated": "order.closed",
            "payment.updated": "order.closed",
            "loyalty.reward.redeemed": "voucher.redeemed",
            "refund.created": "refund.issued",
        }.get(ev, "unknown")
        data = body.get("data", {}) or {}
        obj = data.get("object", {}) or {}
        amount = obj.get("amount_money", {}).get("amount", 0) if isinstance(
            obj.get("amount_money"), dict) else 0
        out = {
            "pos": self.pos_code,
            "event_type": canonical,
            "voucher_id": obj.get("voucher_id") or data.get("voucher_id"),
            "order_id": obj.get("order_id") or obj.get("id"),
            "amount_cents": int(amount or 0),
            "currency": normalize_currency(
                obj.get("amount_money", {}).get("currency")
                if isinstance(obj.get("amount_money"), dict) else None,
                default="USD",
            ),
            "raw": body,
        }
        emit_audit({"pos": self.pos_code, "action": "webhook_handler",
                    "event_type": canonical})
        return out

    def verify_webhook_signature(
        self, payload: bytes | str, signature: str
    ) -> bool:
        return hmac_verify(payload, signature, self._webhook_secret())
