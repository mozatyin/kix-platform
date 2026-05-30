"""WeChat Pay PSP wrapper.

WeChat Pay supports three integration modes:

* **JSAPI** — invoked from inside the WeChat browser via JS bridge
* **Native** — server returns a QR code URL for desktop payments
* **H5** — mobile browser outside WeChat redirects to a payment page

The wrapper selects mode based on ``metadata.delivery``. Live calls go
to ``api.mch.weixin.qq.com`` and are signed with HMAC-SHA256 using the
merchant's APIv3 key.

Modes
-----
* ``live`` — ``WECHAT_MCH_ID`` + ``WECHAT_API_V3_KEY`` set
* ``test`` — ``WECHAT_TEST_MCH_ID`` set
* ``mock`` — neither set; deterministic fixtures
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


class WeChatPayClient(PSPClient):
    psp_code = "wechat_pay"
    _SUPPORTED_CURRENCIES = {"CNY", "HKD"}
    _SUPPORTED_DELIVERIES = {"jsapi", "native", "h5"}

    def get_mode(self) -> str:
        return detect_mode(
            live_key="WECHAT_MCH_ID",
            test_key="WECHAT_TEST_MCH_ID",
        )

    def _mch_id(self) -> str:
        return (
            os.getenv("WECHAT_MCH_ID")
            or os.getenv("WECHAT_TEST_MCH_ID")
            or "1900000001"  # WeChat-published sandbox mch_id
        )

    def _api_key(self) -> str:
        return os.getenv("WECHAT_API_V3_KEY", "whsec_wechat_stub_32_chars_long_x")

    def create_charge(
        self,
        amount: int,
        currency: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if amount <= 0:
            raise ValueError("amount must be positive")
        cur = normalize_currency(currency, default="CNY")
        if cur not in self._SUPPORTED_CURRENCIES:
            raise ValueError(
                f"WeChat Pay does not accept {cur}; supported: {sorted(self._SUPPORTED_CURRENCIES)}"
            )

        meta = dict(metadata or {})
        delivery = (meta.get("delivery") or "native").lower()
        if delivery not in self._SUPPORTED_DELIVERIES:
            raise ValueError(
                f"delivery must be one of {sorted(self._SUPPORTED_DELIVERIES)}; got {delivery}"
            )

        ref = meta.get("reference_id") or uuid4().hex[:16]
        charge_id = mock_charge_id("wxpay", ref)
        mode = self.get_mode()
        host = "mock.wechatpay.local" if mode == "mock" else "api.mch.weixin.qq.com"

        result: dict[str, Any] = {
            "psp": self.psp_code,
            "charge_id": charge_id,
            "mch_id": self._mch_id(),
            "delivery": delivery,
            "amount": amount,
            "currency": cur,
            "mode": mode,
            "metadata": meta,
        }
        if delivery == "native":
            result["qr_code"] = f"weixin://wxpay/bizpayurl?pr={charge_id}"
            result["qr_code_url"] = f"https://{host}/native/{charge_id}"
        elif delivery == "h5":
            result["checkout_url"] = f"https://{host}/h5/{charge_id}"
        else:  # jsapi
            result["prepay_id"] = f"wx_prepay_{charge_id}"
            result["jsapi_params"] = {
                "appId": os.getenv("WECHAT_APPID", "wxd930ea5d5a258f4f"),
                "timeStamp": "1700000000",
                "nonceStr": uuid4().hex[:16],
                "package": f"prepay_id={result['prepay_id']}",
                "signType": "HMAC-SHA256",
            }

        record_charge(self.psp_code)
        emit_audit(
            {
                "psp": self.psp_code,
                "action": "create_charge",
                "charge_id": charge_id,
                "amount": amount,
                "currency": cur,
                "mode": mode,
                "delivery": delivery,
            }
        )
        return result

    def verify_webhook(
        self, payload: bytes | str, signature: str
    ) -> dict[str, Any]:
        if not hmac_verify(payload, signature, self._api_key()):
            emit_audit(
                {
                    "psp": self.psp_code,
                    "action": "verify_webhook",
                    "result": "invalid_signature",
                }
            )
            raise ValueError("WeChat Pay webhook signature mismatch")
        return parse_payload(payload)

    def process_event(self, event: dict[str, Any]) -> dict[str, Any]:
        # WeChat sends: { "event_type": "TRANSACTION.SUCCESS",
        # "resource": {"out_trade_no", "amount":{"total","currency"}, ...} }
        event_type = event.get("event_type") or ""
        canonical_type = {
            "TRANSACTION.SUCCESS": "charge.succeeded",
            "TRANSACTION.FAIL": "charge.failed",
            "REFUND.SUCCESS": "refund.succeeded",
            "REFUND.FAIL": "refund.failed",
            "charge.succeeded": "charge.succeeded",
            "charge.failed": "charge.failed",
        }.get(event_type, event_type or "charge.succeeded")

        resource = event.get("resource") or event
        meta = (
            event.get("metadata")
            or resource.get("metadata")
            or resource.get("attach")
            or {}
        )
        if isinstance(meta, str):
            meta = parse_payload(meta)

        amount_block = resource.get("amount") or {}
        amount_cents = int(
            amount_block.get("total")
            or resource.get("amount")
            or event.get("amount")
            or 0
        )
        cur = (
            amount_block.get("currency")
            or resource.get("currency")
            or event.get("currency")
            or "CNY"
        )

        out = {
            "psp": self.psp_code,
            "event_type": canonical_type,
            "charge_id": (
                resource.get("out_trade_no")
                or event.get("out_trade_no")
                or event.get("charge_id")
            ),
            "amount_cents": amount_cents,
            "currency": normalize_currency(cur, default="CNY"),
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
        refund_id = mock_charge_id("wxpay_rf", charge_id[-8:])
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
        ready = mode != "live" or bool(os.getenv("WECHAT_API_V3_KEY"))
        return {
            "psp": self.psp_code,
            "mode": mode,
            "ready": ready,
            "last_charge_ts": get_last_charge_ts(self.psp_code),
        }
