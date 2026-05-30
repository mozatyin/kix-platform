"""Foodzaps POS adapter.

Foodzaps (`foodzaps.com`) is a SEA-focused F&B SMB POS popular in
Malaysia and Singapore. Integration uses API key + REST.

Modes
-----
* ``live`` — ``FOODZAPS_LIVE_API_KEY`` set
* ``test`` — ``FOODZAPS_TEST_API_KEY`` set
* ``mock`` — neither set
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


class FoodzapsPOSAdapter(POSAdapter):
    pos_code = "foodzaps"

    def get_mode(self) -> str:
        return detect_mode(
            live_key="FOODZAPS_LIVE_API_KEY",
            test_key="FOODZAPS_TEST_API_KEY",
        )

    def _webhook_secret(self) -> str:
        return os.getenv("FOODZAPS_WEBHOOK_SECRET", "whsec_foodzaps_stub")

    def _outlet_id(self) -> str:
        return os.getenv("FOODZAPS_OUTLET_ID", "outlet_mock_sea_01")

    def verify_voucher(self, voucher_id: str) -> dict[str, Any]:
        if not voucher_id:
            raise ValueError("voucher_id required")
        mode = self.get_mode()
        store = get_mock_store()
        v = store.get(voucher_id)
        base = {"pos": self.pos_code, "voucher_id": voucher_id, "mode": mode}
        if v is None:
            return {**base, "valid": False, "value_cents": 0, "currency": "MYR",
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
        ref = mock_ref("fz_disc", f"{voucher_id[:6]}{order_id[:8]}")
        emit_audit({"pos": self.pos_code, "action": "apply_discount",
                    "voucher_id": voucher_id, "amount_cents": amount_cents})
        return {
            "pos": self.pos_code,
            "voucher_id": voucher_id,
            "order_id": order_id,
            "discount_cents": int(amount_cents),
            "applied": True,
            "pos_discount_ref": ref,
            "outlet_id": self._outlet_id(),
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
        rid = mock_ref("fz_red", voucher_id[:12])
        tx_ref = (transaction_data or {}).get("order_id") or mock_ref(
            "fz_tx", voucher_id[:8]
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
        ev = body.get("event_type", body.get("type", ""))
        canonical = {
            "bill.closed": "order.closed",
            "bill.completed": "order.closed",
            "promo.redeemed": "voucher.redeemed",
            "bill.refunded": "refund.issued",
        }.get(ev, "unknown")
        bill = body.get("bill") or body
        out = {
            "pos": self.pos_code,
            "event_type": canonical,
            "voucher_id": bill.get("voucher_code") or body.get("voucher_id"),
            "order_id": bill.get("bill_no") or bill.get("order_id"),
            "amount_cents": int(bill.get("total_cents") or body.get("amount") or 0),
            "currency": normalize_currency(
                bill.get("currency") or body.get("currency"), default="MYR"
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
