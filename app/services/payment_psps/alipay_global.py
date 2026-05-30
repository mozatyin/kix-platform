"""Alipay (Global / Cross-Border) PSP wrapper.

Alipay+ Global lets non-China merchants accept Alipay from CN, HK, SG,
MY consumers (and the rest of the Alipay+ network). Live integration
is via the AntChain OpenAPI gateway with RSA2 signatures.

Modes
-----
* ``live`` — ``ALIPAY_LIVE_APP_ID`` + ``ALIPAY_LIVE_PRIVATE_KEY`` set
* ``test`` — ``ALIPAY_TEST_APP_ID`` set (Alipay openapi sandbox)
* ``mock`` — neither set; deterministic test fixtures

Flow
----
1. ``create_charge`` → POST /v1/payments/pay → returns either:
   - ``checkout_url`` (browser redirect for web payments), or
   - ``qr_code`` (for in-app / point-of-sale)
2. Customer authorises in Alipay app → Ant fires webhook.
3. ``verify_webhook`` verifies RSA-SHA256 signature against Ant's
   public key. In mock mode we fall back to HMAC for testability.
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


class AlipayGlobalClient(PSPClient):
    psp_code = "alipay"
    _SUPPORTED_CURRENCIES = {"CNY", "HKD", "SGD", "MYR", "USD"}

    def get_mode(self) -> str:
        return detect_mode(
            live_key="ALIPAY_LIVE_APP_ID",
            test_key="ALIPAY_TEST_APP_ID",
        )

    def _hmac_secret(self) -> str:
        # Used in mock mode for webhook verification; in live mode we
        # would use RSA2 against ``ALIPAY_PUBLIC_KEY`` instead.
        return os.getenv("ALIPAY_WEBHOOK_SECRET", "whsec_alipay_stub")

    def _has_rsa_keys(self) -> bool:
        return bool(
            os.getenv("ALIPAY_LIVE_PRIVATE_KEY")
            and os.getenv("ALIPAY_PUBLIC_KEY")
        )

    def _rsa_verify(
        self, payload: bytes | str, signature: str
    ) -> bool:
        """Best-effort RSA-SHA256 verification.

        If ``cryptography`` isn't installed or no public key is set, fall
        back to HMAC (suitable for test/mock). This avoids a hard runtime
        dep when most deployments use the mock path.
        """
        pub_key = os.getenv("ALIPAY_PUBLIC_KEY", "")
        if not pub_key:
            return hmac_verify(payload, signature, self._hmac_secret())
        try:
            import base64

            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding

            if isinstance(payload, str):
                payload = payload.encode("utf-8")
            key = serialization.load_pem_public_key(pub_key.encode("utf-8"))
            sig_bytes = base64.b64decode(signature)
            key.verify(  # type: ignore[union-attr]
                sig_bytes, payload, padding.PKCS1v15(), hashes.SHA256()
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("alipay RSA verify failed: %s", exc)
            return False

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
                f"Alipay does not accept {cur}; supported: {sorted(self._SUPPORTED_CURRENCIES)}"
            )

        meta = dict(metadata or {})
        ref = meta.get("reference_id") or uuid4().hex[:16]
        charge_id = mock_charge_id("alipay", ref)
        mode = self.get_mode()
        delivery = meta.get("delivery", "browser")  # browser | qr | app

        result: dict[str, Any] = {
            "psp": self.psp_code,
            "charge_id": charge_id,
            "amount": amount,
            "currency": cur,
            "mode": mode,
            "metadata": meta,
        }
        host = "mock.alipay.local" if mode == "mock" else "openapi.alipay.com"
        if delivery == "qr":
            result["qr_code"] = f"https://qr.{host}/bax/{charge_id}"
        else:
            result["checkout_url"] = f"https://{host}/gateway.do?charge={charge_id}"

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
        # Prefer RSA if we have a public key configured, else HMAC.
        if self._has_rsa_keys():
            ok = self._rsa_verify(payload, signature)
        else:
            ok = hmac_verify(payload, signature, self._hmac_secret())
        if not ok:
            emit_audit(
                {
                    "psp": self.psp_code,
                    "action": "verify_webhook",
                    "result": "invalid_signature",
                }
            )
            raise ValueError("Alipay webhook signature mismatch")
        return parse_payload(payload)

    def process_event(self, event: dict[str, Any]) -> dict[str, Any]:
        # Alipay notification shape: { "notify_type", "out_trade_no",
        # "total_amount", "trade_status", "passback_params": {brand_id,...}}
        # We map both Alipay-native and our standardised shapes.
        event_type = (
            event.get("notify_type")
            or event.get("event_type")
            or ""
        )
        canonical_type = {
            "trade_status_sync": "charge.succeeded",
            "TRADE_SUCCESS": "charge.succeeded",
            "TRADE_FINISHED": "charge.succeeded",
            "TRADE_CLOSED": "charge.failed",
            "REFUND_SUCCESS": "refund.succeeded",
        }.get(event_type, event_type or "charge.succeeded")

        meta = event.get("metadata") or event.get("passback_params") or {}
        if isinstance(meta, str):
            meta = parse_payload(meta)
        amount_raw = (
            event.get("amount")
            or event.get("total_amount")
            or 0
        )
        try:
            amount_cents = int(amount_raw)
        except (TypeError, ValueError):
            # Alipay sends total_amount as "12.34" (yuan, not fen) in some
            # envelopes; normalise to cents.
            amount_cents = int(float(amount_raw) * 100)

        out = {
            "psp": self.psp_code,
            "event_type": canonical_type,
            "charge_id": event.get("out_trade_no") or event.get("charge_id"),
            "amount_cents": amount_cents,
            "currency": normalize_currency(event.get("currency"), default="CNY"),
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
        refund_id = mock_charge_id("alipay_rf", charge_id[-8:])
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
        ready = mode != "live" or self._has_rsa_keys()
        return {
            "psp": self.psp_code,
            "mode": mode,
            "ready": ready,
            "last_charge_ts": get_last_charge_ts(self.psp_code),
        }
