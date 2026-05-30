"""Toast POS adapter.

Toast (`pos.toasttab.com`) is the dominant US full-service restaurant
POS. Integration is OAuth2 client-credentials → REST API.

Modes
-----
* ``live`` — ``TOAST_LIVE_CLIENT_SECRET`` set
* ``test`` — ``TOAST_TEST_CLIENT_SECRET`` set (sandbox)
* ``mock`` — neither set; deterministic fixtures from MockVoucherStore

Webhook signature
-----------------
Toast posts JSON with ``Toast-Signature: <hex_hmac_sha256>``. Verified
against ``TOAST_WEBHOOK_SECRET``.
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


class ToastPOSAdapter(POSAdapter):
    pos_code = "toast"

    def get_mode(self) -> str:
        return detect_mode(
            live_key="TOAST_LIVE_CLIENT_SECRET",
            test_key="TOAST_TEST_CLIENT_SECRET",
        )

    def _webhook_secret(self) -> str:
        return os.getenv("TOAST_WEBHOOK_SECRET", "whsec_toast_stub")

    def _restaurant_guid(self) -> str:
        return os.getenv("TOAST_RESTAURANT_GUID", "rg_mock_00000001")

    # ── Voucher lifecycle ───────────────────────────────────────────────
    def verify_voucher(self, voucher_id: str) -> dict[str, Any]:
        if not voucher_id:
            raise ValueError("voucher_id required")
        mode = self.get_mode()
        store = get_mock_store()
        v = store.get(voucher_id)
        if v is None:
            return {
                "pos": self.pos_code,
                "voucher_id": voucher_id,
                "valid": False,
                "value_cents": 0,
                "currency": "USD",
                "brand_id": None,
                "expires_at": None,
                "reason": POSError.NOT_FOUND,
                "mode": mode,
            }
        if v.get("expires_at") and int(time.time()) > int(v["expires_at"]):
            return {
                "pos": self.pos_code,
                "voucher_id": voucher_id,
                "valid": False,
                "value_cents": v.get("value_cents", 0),
                "currency": v.get("currency", "USD"),
                "brand_id": v.get("brand_id"),
                "expires_at": v["expires_at"],
                "reason": POSError.EXPIRED,
                "mode": mode,
            }
        if store.is_redeemed(voucher_id):
            return {
                "pos": self.pos_code,
                "voucher_id": voucher_id,
                "valid": False,
                "value_cents": v.get("value_cents", 0),
                "currency": v.get("currency", "USD"),
                "brand_id": v.get("brand_id"),
                "expires_at": v.get("expires_at"),
                "reason": POSError.ALREADY_USED,
                "mode": mode,
            }
        emit_audit({"pos": self.pos_code, "action": "verify_voucher",
                    "voucher_id": voucher_id, "valid": True, "mode": mode})
        return {
            "pos": self.pos_code,
            "voucher_id": voucher_id,
            "valid": True,
            "value_cents": int(v.get("value_cents", 0)),
            "currency": v.get("currency", "USD"),
            "brand_id": v.get("brand_id"),
            "expires_at": v.get("expires_at"),
            "reason": None,
            "mode": mode,
        }

    def apply_discount(
        self, voucher_id: str, order_id: str, amount_cents: int
    ) -> dict[str, Any]:
        if not voucher_id or not order_id:
            raise ValueError("voucher_id and order_id required")
        if amount_cents <= 0:
            return {
                "pos": self.pos_code,
                "voucher_id": voucher_id,
                "order_id": order_id,
                "discount_cents": 0,
                "applied": False,
                "pos_discount_ref": "",
                "reason": POSError.INVALID_AMOUNT,
                "mode": self.get_mode(),
            }
        mode = self.get_mode()
        ref = mock_ref("toast_disc", f"{voucher_id[:8]}-{order_id[:8]}")
        emit_audit({"pos": self.pos_code, "action": "apply_discount",
                    "voucher_id": voucher_id, "order_id": order_id,
                    "amount_cents": amount_cents, "mode": mode})
        return {
            "pos": self.pos_code,
            "voucher_id": voucher_id,
            "order_id": order_id,
            "discount_cents": int(amount_cents),
            "applied": True,
            "pos_discount_ref": ref,
            "restaurant_guid": self._restaurant_guid(),
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
        redemption_id = mock_ref("toast_red", voucher_id[:12])
        tx_ref = (transaction_data or {}).get("order_id") or mock_ref(
            "toast_tx", voucher_id[:8]
        )
        emit_audit({"pos": self.pos_code, "action": "mark_redeemed",
                    "voucher_id": voucher_id, "redemption_id": redemption_id,
                    "mode": mode})
        return {
            "pos": self.pos_code,
            "voucher_id": voucher_id,
            "redemption_id": redemption_id,
            "redeemed_at": rec["redeemed_at"],
            "transaction_ref": tx_ref,
            "mode": mode,
        }

    # ── Inbound webhooks ────────────────────────────────────────────────
    def webhook_handler(self, payload: Any) -> dict[str, Any]:
        body = parse_payload(payload)
        ev = body.get("eventType", body.get("event_type", ""))
        canonical = {
            "ORDER_CLOSED": "order.closed",
            "DISCOUNT_REDEEMED": "voucher.redeemed",
            "ORDER_REFUNDED": "refund.issued",
        }.get(ev.upper() if isinstance(ev, str) else "", "unknown")
        meta = body.get("metadata") or {}
        out = {
            "pos": self.pos_code,
            "event_type": canonical,
            "voucher_id": body.get("voucherId") or meta.get("voucher_id"),
            "order_id": body.get("orderGuid") or body.get("order_id"),
            "amount_cents": int(body.get("amount") or 0),
            "currency": normalize_currency(body.get("currency"), default="USD"),
            "raw": body,
        }
        emit_audit({"pos": self.pos_code, "action": "webhook_handler",
                    "event_type": canonical})
        return out

    # ── Convenience for the router ──────────────────────────────────────
    def verify_webhook_signature(
        self, payload: bytes | str, signature: str
    ) -> bool:
        return hmac_verify(payload, signature, self._webhook_secret())
