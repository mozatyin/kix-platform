"""WhatsApp Business OTP service — phone-based login/registration.

Wave E item 6: replace email/password with phone-OTP delivered via the
WhatsApp Business Cloud API. SMB merchants and SEA consumers strongly
prefer WhatsApp over email; this service is the back-end that lets the
auth router send and verify a 6-digit code without storing a password.

Two modes
---------
* ``live``  — credentials present in env (``WHATSAPP_API_TOKEN`` +
  ``WHATSAPP_PHONE_NUMBER_ID``). Real HTTP POST to the Graph API.
* ``mock``  — credentials absent. The 6-digit code is generated, stored
  in Redis with the same TTL/rate-limit envelope as live, and echoed
  back in the API response so dev / CI / Cypress can pick it up without
  any actual SMS / WhatsApp side-effects.

Mock mode is the default everywhere except production. Tests force mock
by simply not setting the env vars; the auth-router tests do the same.

Storage / TTL
-------------
All state lives in Redis (no PG schema changes required for the OTP
itself — the user link uses ``user_profiles.auth_method`` from the
additive migration in the sibling commit).

Keys::

    whatsapp_otp:code:{phone}      hash {code, attempts, sent_at}  TTL 300
    whatsapp_otp:rate:{phone}      integer counter                 TTL 3600
    whatsapp_otp:short_token:{tk}  hash {phone, brand_id, verified_at} TTL 600

The 5-minute code TTL matches the user-facing copy ("Valid for 5
minutes"). Rate limit is 3 sends per phone per rolling hour — high
enough for "I lost my phone" retries, low enough that scripted enum
attacks die quickly.

Audit
-----
Every send + verify is mirrored to the durable audit log via
``record_event_fire_and_forget`` with ``action=auth.whatsapp.otp_send``
or ``auth.whatsapp.otp_verify``. Phone numbers are E.164-normalised
before hashing into ``target_id``; the raw phone never lands in the
audit payload (defence-in-depth — the audit service also strips PII).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any

import httpx
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────

OTP_TTL_SECONDS = 300            # 5 minutes — matches user-facing copy
RATE_LIMIT_WINDOW = 3600         # 1 hour
RATE_LIMIT_MAX_SENDS = 3         # max OTPs per phone per window
SHORT_TOKEN_TTL_SECONDS = 600    # 10 minutes — caller swaps for JWT
MAX_VERIFY_ATTEMPTS = 5          # per code, before invalidation
OTP_LENGTH = 6

GRAPH_API_BASE = "https://graph.facebook.com/v19.0"

SUPPORTED_LOCALES = ("en", "zh", "ms", "id", "th", "vi")
DEFAULT_LOCALE = "en"


# ── Localised OTP body templates ──────────────────────────────────────────
# Pure data, no game-specific copy — these are platform-level messages
# every brand on the platform uses.
_OTP_TEMPLATES: dict[str, str] = {
    "en": "Your KiX code is {code}. Valid for 5 minutes.",
    "zh": "您的 KiX 验证码是 {code}, 5 分钟内有效。",
    "ms": "Kod KiX anda ialah {code}. Sah selama 5 minit.",
    "id": "Kode KiX Anda adalah {code}. Berlaku selama 5 menit.",
    "th": "รหัส KiX ของคุณคือ {code} ใช้ได้ภายใน 5 นาที",
    "vi": "Mã KiX của bạn là {code}. Có hiệu lực trong 5 phút.",
}


# ── Mode detection ────────────────────────────────────────────────────────


def _live_mode() -> bool:
    """True iff WhatsApp Business creds are present in the environment."""
    return bool(
        os.environ.get("WHATSAPP_API_TOKEN")
        and os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    )


def get_mode() -> str:
    return "live" if _live_mode() else "mock"


# ── Helpers ───────────────────────────────────────────────────────────────


_E164_RE = re.compile(r"^\+?[1-9]\d{6,14}$")


def normalise_phone(phone: str) -> str:
    """Strip spaces / dashes and prepend ``+`` if missing.

    Not a full libphonenumber validation — the format gate sits at the
    router boundary via ``i18n_validators``. This helper only collapses
    common input variations so the Redis key is deterministic.
    """
    if not phone:
        raise ValueError("phone is required")
    cleaned = re.sub(r"[\s\-().]", "", phone.strip())
    if cleaned.startswith("00"):  # international "00" prefix → "+"
        cleaned = "+" + cleaned[2:]
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    if not _E164_RE.match(cleaned):
        raise ValueError(f"phone is not valid E.164: {phone!r}")
    return cleaned


def _hash_phone(phone: str) -> str:
    """Short, opaque identifier for audit / rate-limit keys."""
    return hashlib.sha256(phone.encode()).hexdigest()[:16]


def _resolve_locale(locale: str | None) -> str:
    if not locale:
        return DEFAULT_LOCALE
    base = locale.split("-")[0].split("_")[0].lower()
    return base if base in SUPPORTED_LOCALES else DEFAULT_LOCALE


def render_message(code: str, locale: str | None) -> str:
    """Localised OTP body. Falls back to English on unknown locale."""
    return _OTP_TEMPLATES[_resolve_locale(locale)].format(code=code)


def _generate_code() -> str:
    """6-digit zero-padded code from a CSPRNG."""
    n = secrets.randbelow(10**OTP_LENGTH)
    return str(n).zfill(OTP_LENGTH)


# Test seam — pytest can override to get deterministic codes.
_code_generator = _generate_code


def set_code_generator(fn) -> None:  # pragma: no cover — test helper
    global _code_generator
    _code_generator = fn


def reset_code_generator() -> None:  # pragma: no cover — test helper
    global _code_generator
    _code_generator = _generate_code


# ── Audit hook ────────────────────────────────────────────────────────────


async def _audit(
    action: str,
    phone: str,
    *,
    result: str,
    brand_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget durable audit. Never raises into the caller."""
    try:
        from app.services.audit_log_service import (
            record_event_fire_and_forget,
        )
        phone_hash = _hash_phone(phone)
        clean_payload = dict(payload or {})
        # Defence-in-depth: never let a raw phone slip into audit row.
        clean_payload.pop("phone", None)
        await record_event_fire_and_forget(
            actor_id=phone_hash,
            actor_type="customer",
            action=action,
            target_type="phone",
            target_id=phone_hash,
            brand_id=brand_id,
            result=result,
            payload=clean_payload,
        )
    except Exception as exc:  # pragma: no cover — hook-side resilience
        logger.warning("whatsapp_otp audit (%s) skipped: %s", action, exc)


# ── Rate limit ────────────────────────────────────────────────────────────


async def _check_and_bump_rate_limit(
    r: aioredis.Redis, phone: str
) -> tuple[bool, int]:
    """Atomic INCR with first-write EXPIRE.

    Returns (allowed, current_count). When ``allowed`` is False the
    caller MUST refuse to send another OTP and return a 429.
    """
    key = f"whatsapp_otp:rate:{_hash_phone(phone)}"
    count = await r.incr(key)
    if count == 1:
        await r.expire(key, RATE_LIMIT_WINDOW)
    return (count <= RATE_LIMIT_MAX_SENDS), int(count)


# ── Live WhatsApp Business send ───────────────────────────────────────────


async def _send_via_graph_api(phone: str, body: str) -> dict[str, Any]:
    """POST to the WhatsApp Business Cloud API.

    Uses a plain text message rather than a template for now — templates
    require Facebook approval per locale and we want OTPs to work in all
    six languages on day one. Production deploys SHOULD migrate to
    approved templates per Meta policy (otherwise messages from a fresh
    business account may be throttled / blocked).
    """
    token = os.environ["WHATSAPP_API_TOKEN"]
    phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    url = f"{GRAPH_API_BASE}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": phone.lstrip("+"),
        "type": "text",
        "text": {"body": body},
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as cx:
        res = await cx.post(url, json=payload, headers=headers)
    if res.status_code >= 400:
        logger.error(
            "whatsapp_otp graph API rejected send: status=%d body=%s",
            res.status_code, res.text[:200],
        )
        raise RuntimeError(
            f"WhatsApp API error {res.status_code}: {res.text[:120]}"
        )
    try:
        return res.json()
    except Exception:  # pragma: no cover — defensive
        return {"raw": res.text}


# ── Public API ────────────────────────────────────────────────────────────


async def send_otp(
    r: aioredis.Redis,
    phone: str,
    locale: str | None = "en",
    *,
    brand_id: str | None = None,
) -> dict[str, Any]:
    """Generate a 6-digit code, deliver via WhatsApp, store in Redis.

    Returns a dict::

        {
          "status": "sent",
          "mode": "live" | "mock",
          "phone": "+6591234567",
          "expires_in": 300,
          "rate_remaining": <int>,
          # mock only:
          "debug_code": "123456",
        }

    Raises:
        ValueError       — phone format invalid (router → 422).
        PermissionError  — rate limit exceeded (router → 429).
        RuntimeError     — live API rejected the send (router → 502).
    """
    normalised = normalise_phone(phone)
    allowed, count = await _check_and_bump_rate_limit(r, normalised)
    if not allowed:
        await _audit(
            "auth.whatsapp.otp_send",
            normalised,
            result="rate_limited",
            brand_id=brand_id,
            payload={"locale": _resolve_locale(locale), "count": count},
        )
        raise PermissionError(
            f"rate limit exceeded: {RATE_LIMIT_MAX_SENDS} sends per "
            f"{RATE_LIMIT_WINDOW // 60} minutes"
        )

    code = _code_generator()
    body = render_message(code, locale)
    now = datetime.now(timezone.utc)

    # Persist code BEFORE attempting delivery — a successful WhatsApp
    # delivery that the user already typed in must never fail the verify
    # leg because we crashed before storing.
    code_key = f"whatsapp_otp:code:{_hash_phone(normalised)}"
    await r.hset(
        code_key,
        mapping={
            "code": code,
            "attempts": "0",
            "sent_at": now.isoformat(),
            "locale": _resolve_locale(locale),
            "brand_id": brand_id or "",
        },
    )
    await r.expire(code_key, OTP_TTL_SECONDS)

    mode = get_mode()
    delivery_meta: dict[str, Any] = {}
    if mode == "live":
        try:
            api_res = await _send_via_graph_api(normalised, body)
            delivery_meta["wa_message_id"] = (
                api_res.get("messages", [{}])[0].get("id")
            )
        except Exception as exc:
            await _audit(
                "auth.whatsapp.otp_send",
                normalised,
                result="delivery_failed",
                brand_id=brand_id,
                payload={"locale": _resolve_locale(locale), "mode": mode},
            )
            raise RuntimeError(f"WhatsApp delivery failed: {exc}") from exc

    await _audit(
        "auth.whatsapp.otp_send",
        normalised,
        result="success",
        brand_id=brand_id,
        payload={
            "locale": _resolve_locale(locale),
            "mode": mode,
            "rate_count": count,
            **delivery_meta,
        },
    )

    result: dict[str, Any] = {
        "status": "sent",
        "mode": mode,
        "phone": normalised,
        "expires_in": OTP_TTL_SECONDS,
        "rate_remaining": max(0, RATE_LIMIT_MAX_SENDS - count),
    }
    if mode == "mock":
        # Echo back the code so dev/CI/Cypress flows can chain into
        # verify_otp without needing a real WhatsApp inbox.
        result["debug_code"] = code
        result["debug_message"] = body
    return result


async def verify_otp(
    r: aioredis.Redis,
    phone: str,
    code: str,
    *,
    brand_id: str | None = None,
) -> dict[str, Any]:
    """Validate a code and mint a short-lived verification token.

    The short-lived token (10 min) is the bridge into the auth router:
    the router exchanges it for the long-lived JWT + refresh pair after
    confirming the phone-to-user link.

    Returns::

        {
          "status": "verified",
          "phone": "+6591234567",
          "short_token": "<opaque>",
          "expires_in": 600,
        }

    Raises:
        ValueError       — wrong code / expired / too many attempts.
    """
    normalised = normalise_phone(phone)
    if not code or not code.isdigit() or len(code) != OTP_LENGTH:
        await _audit(
            "auth.whatsapp.otp_verify",
            normalised,
            result="malformed",
            brand_id=brand_id,
        )
        raise ValueError("code must be a 6-digit string")

    code_key = f"whatsapp_otp:code:{_hash_phone(normalised)}"
    stored = await r.hgetall(code_key)
    if not stored:
        await _audit(
            "auth.whatsapp.otp_verify",
            normalised,
            result="expired_or_unknown",
            brand_id=brand_id,
        )
        raise ValueError("OTP expired or never sent")

    stored_code = stored.get("code")
    attempts = int(stored.get("attempts") or "0")

    if attempts >= MAX_VERIFY_ATTEMPTS:
        # Invalidate immediately to prevent brute force.
        await r.delete(code_key)
        await _audit(
            "auth.whatsapp.otp_verify",
            normalised,
            result="too_many_attempts",
            brand_id=brand_id,
            payload={"attempts": attempts},
        )
        raise ValueError("too many failed attempts")

    if stored_code != code:
        await r.hincrby(code_key, "attempts", 1)
        await _audit(
            "auth.whatsapp.otp_verify",
            normalised,
            result="mismatch",
            brand_id=brand_id,
            payload={"attempts": attempts + 1},
        )
        raise ValueError("invalid code")

    # Success — consume the code and mint a short-lived bridge token.
    await r.delete(code_key)
    short_token = secrets.token_urlsafe(24)
    short_key = f"whatsapp_otp:short_token:{short_token}"
    now = datetime.now(timezone.utc)
    await r.hset(
        short_key,
        mapping={
            "phone": normalised,
            "brand_id": brand_id or "",
            "verified_at": now.isoformat(),
        },
    )
    await r.expire(short_key, SHORT_TOKEN_TTL_SECONDS)

    await _audit(
        "auth.whatsapp.otp_verify",
        normalised,
        result="success",
        brand_id=brand_id,
        payload={"mode": get_mode()},
    )

    return {
        "status": "verified",
        "phone": normalised,
        "short_token": short_token,
        "expires_in": SHORT_TOKEN_TTL_SECONDS,
    }


async def consume_short_token(
    r: aioredis.Redis, short_token: str
) -> dict[str, str] | None:
    """One-shot read+delete of a short-lived bridge token.

    The auth router calls this when exchanging the bridge token for the
    long-lived JWT. Rotation: the bridge token is deleted on read so it
    can never be replayed.
    """
    key = f"whatsapp_otp:short_token:{short_token}"
    data = await r.hgetall(key)
    if not data:
        return None
    await r.delete(key)
    return dict(data)


async def health_check(r: aioredis.Redis | None = None) -> dict[str, Any]:
    """Cheap, side-effect-free probe for ops dashboards."""
    info: dict[str, Any] = {
        "service": "whatsapp_otp",
        "mode": get_mode(),
        "templates_loaded": len(_OTP_TEMPLATES),
        "supported_locales": list(SUPPORTED_LOCALES),
        "rate_limit_per_hour": RATE_LIMIT_MAX_SENDS,
        "otp_ttl_seconds": OTP_TTL_SECONDS,
    }
    if r is not None:
        try:
            await r.ping()
            info["redis"] = "ok"
        except Exception as exc:  # pragma: no cover
            info["redis"] = f"error: {exc}"
    return info


__all__ = [
    "send_otp",
    "verify_otp",
    "consume_short_token",
    "health_check",
    "render_message",
    "normalise_phone",
    "get_mode",
    "set_code_generator",
    "reset_code_generator",
    "OTP_TTL_SECONDS",
    "RATE_LIMIT_MAX_SENDS",
    "RATE_LIMIT_WINDOW",
    "SHORT_TOKEN_TTL_SECONDS",
    "SUPPORTED_LOCALES",
]
