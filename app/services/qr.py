"""QR token generation and validation service for KiX Platform R5.

Implements HMAC-SHA256 signed tokens with:
- 15-30 minute rotation periods
- 30-second grace period after expiry
- 4-hour cooldown per brand per user
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta, timezone

from app.config import settings


def _b64url_encode(data: bytes) -> str:
    """Base64url encode without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    """Base64url decode with padding restoration."""
    # Restore padding
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def _sign(payload_encoded: str, secret: str) -> str:
    """HMAC-SHA256 sign the encoded payload."""
    sig = hmac.new(
        secret.encode("utf-8"),
        payload_encoded.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return _b64url_encode(sig)


def generate_qr_token(
    brand_id: str,
    location_id: str,
    duration_minutes: int = 15,
    brand_slug: str | None = None,
) -> tuple[str, str, datetime, datetime]:
    """Generate a signed QR token for a brand location.

    Args:
        brand_id: The brand identifier.
        location_id: The physical location identifier.
        duration_minutes: Token validity period (15-30 minutes).
        brand_slug: URL-friendly brand slug for QR URL. Defaults to brand_id.

    Returns:
        Tuple of (qr_token, qr_url, valid_until, next_rotation_at).
    """
    # Clamp duration to 15-30 minutes
    duration_minutes = max(15, min(30, duration_minutes))

    now = datetime.now(timezone.utc)
    period_start = now
    period_end = now + timedelta(minutes=duration_minutes)

    # Random 6-char nonce for uniqueness
    nonce = secrets.token_urlsafe(4)[:6]

    payload = {
        "b": brand_id,
        "l": location_id,
        "s": int(period_start.timestamp()),
        "e": int(period_end.timestamp()),
        "n": nonce,
    }

    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    encoded_payload = _b64url_encode(payload_json.encode("utf-8"))

    signature = _sign(encoded_payload, settings.qr_signing_secret)
    qr_token = f"{encoded_payload}.{signature}"

    qr_url = f"play.html?brand={brand_id}&qr={qr_token}"

    # Next rotation is at the end of the current period
    next_rotation_at = period_end

    return qr_token, qr_url, period_end, next_rotation_at


def validate_qr_token(
    qr_token: str,
    expected_brand_id: str,
) -> tuple[bool, str, str]:
    """Validate a QR token's signature, time window, and brand.

    Args:
        qr_token: The full token string (payload.signature).
        expected_brand_id: The brand_id we expect in the token.

    Returns:
        Tuple of (is_valid, nonce, error_message).
        On success: (True, nonce, "").
        On failure: (False, "", error_description).
    """
    # ── Split token ─────────────────────────────────────────────────────
    parts = qr_token.split(".")
    if len(parts) != 2:
        return False, "", "invalid_token_format"

    encoded_payload, provided_sig = parts

    # ── Verify HMAC signature ───────────────────────────────────────────
    expected_sig = _sign(encoded_payload, settings.qr_signing_secret)
    if not hmac.compare_digest(provided_sig, expected_sig):
        return False, "", "invalid_signature"

    # ── Decode payload ──────────────────────────────────────────────────
    try:
        payload_bytes = _b64url_decode(encoded_payload)
        payload = json.loads(payload_bytes)
    except (json.JSONDecodeError, ValueError):
        return False, "", "malformed_payload"

    # ── Check required fields ───────────────────────────────────────────
    for field in ("b", "l", "s", "e", "n"):
        if field not in payload:
            return False, "", f"missing_field_{field}"

    # ── Check time window with 30-second grace ──────────────────────────
    now_ts = int(time.time())
    period_start = payload["s"]
    period_end = payload["e"]
    grace_seconds = 30

    if now_ts < period_start:
        return False, "", "token_not_yet_valid"

    if now_ts > period_end + grace_seconds:
        return False, "", "token_expired"

    # ── Check brand matches ─────────────────────────────────────────────
    if payload["b"] != expected_brand_id:
        return False, "", "brand_mismatch"

    nonce = payload["n"]
    return True, nonce, ""
