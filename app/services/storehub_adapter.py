"""StoreHub POS integration — webhook receiver + order→TrackedTransaction mapper.

Per docs/rfc-storehub-fasttrack.md (Wave K5 / shipped Wave L).

Two paths:
  1. OAuth handshake: merchant authorises KiX to read their StoreHub account
  2. Webhook receiver: StoreHub POSTs order.completed → we map → wallet charge

This module exposes pure functions for the mapping + signature verify.
The actual FastAPI route lives in app/routers/integrations/storehub.py
(separate file so this stays testable without a server).

Idempotency: storehub_order_id is the de-dup key. Repeated webhooks for
the same order are no-op.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Signature verification ──

def verify_storehub_signature(
    body: bytes,
    signature_header: str,
    secret: str,
) -> bool:
    """Verify StoreHub webhook HMAC-SHA256 signature.

    StoreHub sends `X-StoreHub-Signature: sha256=<hex>` header.
    Returns True if the signature matches the body hashed with the secret.
    Constant-time compare to prevent timing attacks.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    provided = signature_header.split("=", 1)[1].strip()
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided)


# ── Mapper ──

@dataclass
class StoreHubOrder:
    """Slim model of a StoreHub order payload we care about."""
    order_id: str
    completed_at: datetime
    total_cents: int
    currency: str
    customer_phone_e164: Optional[str]
    customer_email: Optional[str]
    outlet_id: Optional[str]
    line_items: list[dict[str, Any]]
    raw: dict[str, Any]


@dataclass
class TrackedTransactionDraft:
    """What we propose to bill — final billing happens via wallet service."""
    storehub_order_id: str
    brand_id: str
    occurred_at: datetime
    amount_cents: int
    currency: str
    hashed_consumer_phone: Optional[str]
    hashed_consumer_email: Optional[str]
    outlet_id: Optional[str]
    redemption_code_match: bool   # was there a KiX voucher code on this order
    fraud_flagged: bool
    fraud_reason: Optional[str]
    raw_event: dict[str, Any]


def parse_storehub_order(payload: dict[str, Any]) -> StoreHubOrder:
    """Map raw StoreHub webhook payload → StoreHubOrder. Defensive about
    missing fields (StoreHub's payload shape is documented but field names
    have shifted between minor versions)."""
    order_id = payload.get("order_id") or payload.get("id") or ""
    if not order_id:
        raise ValueError("missing order_id in StoreHub payload")

    completed_at_str = payload.get("completed_at") or payload.get("created_at")
    if not completed_at_str:
        raise ValueError(f"missing completed_at on order {order_id}")
    completed_at = _parse_iso8601(completed_at_str)

    total_cents = _to_cents(
        payload.get("total") or payload.get("total_amount") or 0,
        payload.get("currency", "SGD"),
    )

    customer = payload.get("customer") or {}
    raw_phone = customer.get("phone") or customer.get("phone_number") or ""
    phone_e164 = _normalize_e164(raw_phone) if raw_phone else None
    email = (customer.get("email") or "").lower().strip() or None

    return StoreHubOrder(
        order_id=order_id,
        completed_at=completed_at,
        total_cents=total_cents,
        currency=payload.get("currency", "SGD"),
        customer_phone_e164=phone_e164,
        customer_email=email,
        outlet_id=payload.get("outlet_id") or payload.get("store_id"),
        line_items=payload.get("line_items") or payload.get("items") or [],
        raw=payload,
    )


def _parse_iso8601(s: str) -> datetime:
    """Defensive ISO-8601 parser. Strips trailing Z, handles +00:00 form."""
    if not s:
        raise ValueError("empty timestamp")
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Last-ditch: assume YYYY-MM-DDTHH:MM:SS
        dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _to_cents(amount: float | int | str, currency: str) -> int:
    """Normalize money amount to cents/sen. StoreHub sends decimals."""
    try:
        f = float(amount)
    except (TypeError, ValueError):
        return 0
    # IDR / VND are zero-decimal currencies on the StoreHub side too
    if currency.upper() in {"IDR", "VND", "JPY", "KRW"}:
        return int(round(f))
    return int(round(f * 100))


def _normalize_e164(raw: str) -> Optional[str]:
    """Best-effort E.164 normalization. Does NOT do country lookup —
    expects merchant to configure default country code in portal."""
    cleaned = "".join(c for c in raw if c.isdigit() or c == "+")
    if cleaned.startswith("+") and len(cleaned) >= 8:
        return cleaned
    # Bare 8-digit local SG number → assume +65
    if len(cleaned) == 8 and cleaned.isdigit():
        return "+65" + cleaned
    # Bare 10-11 digit MY number → assume +60
    if len(cleaned) in (10, 11) and cleaned.startswith("0"):
        return "+6" + cleaned
    return None


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ── Tracked-transaction builder ──

def map_to_tracked_transaction(
    order: StoreHubOrder,
    brand_id: str,
    *,
    matched_voucher_code: bool,
    fraud_check_result: tuple[bool, Optional[str]] = (False, None),
) -> TrackedTransactionDraft:
    """Convert a parsed StoreHub order into a draft TrackedTransaction.

    Caller (the FastAPI route) is responsible for:
    - Looking up brand_id from StoreHub merchant_id
    - Checking if the order's voucher code matches a live KiX redemption
    - Running fraud heuristics
    - Deduplicating against existing storehub_order_id
    """
    fraud_flagged, fraud_reason = fraud_check_result
    return TrackedTransactionDraft(
        storehub_order_id=order.order_id,
        brand_id=brand_id,
        occurred_at=order.completed_at,
        amount_cents=order.total_cents,
        currency=order.currency,
        hashed_consumer_phone=_sha256_hex(order.customer_phone_e164) if order.customer_phone_e164 else None,
        hashed_consumer_email=_sha256_hex(order.customer_email) if order.customer_email else None,
        outlet_id=order.outlet_id,
        redemption_code_match=matched_voucher_code,
        fraud_flagged=fraud_flagged,
        fraud_reason=fraud_reason,
        raw_event=order.raw,
    )


# ── Fraud check (simple, extensible) ──

def basic_fraud_check(
    order: StoreHubOrder,
    *,
    velocity_window_24h_count: int,
    same_outlet_owner_phones: set[str],
) -> tuple[bool, Optional[str]]:
    """Run a couple of basic fraud heuristics. Returns (flagged, reason)."""
    # Heuristic 1: phone matches outlet-owner phone list (likely staff redeem)
    if order.customer_phone_e164 and order.customer_phone_e164 in same_outlet_owner_phones:
        return True, "phone_matches_owner_or_staff"
    # Heuristic 2: >3 redemptions in 24h from same phone
    if velocity_window_24h_count > 3:
        return True, f"velocity_24h_exceeded ({velocity_window_24h_count})"
    # Heuristic 3: zero-total order (suspicious)
    if order.total_cents <= 0:
        return True, "zero_or_negative_total"
    return False, None
