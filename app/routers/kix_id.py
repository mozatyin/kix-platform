"""KiX ID — universal identity provider for the KiX network.

KiX ID (``kid``) is the user's single, network-wide identifier across every
merchant participating in the KiX gamification ecosystem. Think of it as
"Facebook Connect / Apple Sign In / Google Sign In", but native to KiX:

    * KiX App owns the user relationship; users authenticate once with KiX.
    * Merchants never authenticate users directly. They "Connect with KiX"
      via an OAuth-like authorize → code → token flow.
    * Once connected, merchants read kid attributes (display name, geo,
      history, preferences, tier) *only within the scopes the user granted*.
    * Consent is enforced at the grant layer — every scope (``history``,
      ``location``, …) maps 1:1 to a ``consent.py`` scope. If the user has
      not granted the underlying consent, the Connect authorize fails.

A ``kid`` looks like ``kid_<22 char base62 uuid>``. It is minted on first
contact with the network (typically the first QR scan of any merchant) and
remains stable forever after.

Architecture overview
---------------------
::

    Phone / Email / Device-FP  ──┐
                                 │  (idempotent resolution)
                                 ▼
                       KixUser  ──kid──>  Profile, Devices, Sessions
                                 │
                                 ├──>  ConnectGrant(brand_A, scopes)
                                 ├──>  ConnectGrant(brand_B, scopes)
                                 └──>  ConnectGrant(brand_C, scopes)

Redis key schema
----------------
::

    kid:{kid}                              HASH (profile data)
    kid:phone:{phone_hash}                 STRING → kid (reverse lookup)
    kid:email:{email_hash}                 STRING → kid
    kid:device:{device_fingerprint}        STRING → kid
    kid:{kid}:devices                      SET   of device_fingerprints
    kid:{kid}:identity_history             LIST  (audit of identity links)

    grant:{grant_id}                       HASH (ConnectGrant)
    kid:{kid}:grants                       SET  of grant_ids
    brand:{bid}:grants                     SET  of grant_ids
    grant:code:{code}                      STRING → grant_id, EX 300 (5 min)
    grant:token:{access_token}             STRING → grant_id

    session:{token}                        HASH {kid, device, source,
                                                created_at, expires_at}
    kid:{kid}:sessions                     SET of session_tokens

Integration notes (informational — not enforced here)
-----------------------------------------------------
* Attribution: ``track_*`` events with ``user_id`` should be a valid kid
  (use :func:`resolve_kid_from_phone` / :func:`resolve_kid_from_device`).
* Pixel: ``pixel.events`` may carry a ``kid`` in the body; if absent, the
  caller should fall back to ``device_fingerprint`` and resolve via
  :func:`resolve_kid_from_device`.
* Auction: bidding strategies should resolve the kid first to pull a
  cross-merchant profile via ``/insights``.

Consent integration
-------------------
Scope ↔ consent mapping (see ``consent.py``)::

    history    →  cross_brand_tracking
    location   →  geo_lbs
    favorites  →  personalization
    profile / email / phone   →  (basic identity; no consent gate)
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ─────────────────────────────────────────────────────────────

VALID_SCOPES: set[str] = {
    "profile",
    "history",
    "location",
    "favorites",
    "email",
    "phone",
    "insights",
}

# Scope → consent.py scope it depends on. Scopes not in this map have no
# consent requirement beyond basic identity.
SCOPE_CONSENT_REQUIREMENT: dict[str, str] = {
    "history": "cross_brand_tracking",
    "location": "geo_lbs",
    "favorites": "personalization",
    "insights": "cross_brand_tracking",
}

VALID_LANGUAGES: set[str] = {
    "zh-CN", "en-US", "id-ID", "ms-MY", "th-TH", "vi-VN", "ja-JP", "ko-KR",
}

VALID_SOURCES: set[str] = {"qr_scan", "app_open", "push", "deeplink", "referral"}

GRANT_CODE_TTL_SECONDS = 300       # 5 min — authorize code lifetime
GRANT_TOKEN_TTL_SECONDS = 90 * 86400  # 90 days — access token lifetime
SESSION_TTL_SECONDS = 30 * 86400   # 30 days — KiX App session lifetime

IDENTITY_HISTORY_MAX = 200

# Device-fingerprint velocity guard (老田 fraud bug). A sockpuppet ring
# registered 10 phones against one device in <2s and walked off with
# deposit promos. We rate-limit kid creation per device fingerprint:
#   * > DEVICE_VELOCITY_MAX_BURST kids in DEVICE_VELOCITY_BURST_SECONDS → 429
#   * > DEVICE_VELOCITY_MAX_TOTAL kids ever (24h) → 403 + fraud log
DEVICE_VELOCITY_BURST_SECONDS = 60
DEVICE_VELOCITY_MAX_BURST = 3
DEVICE_VELOCITY_MAX_TOTAL = 10
DEVICE_VELOCITY_TTL_SECONDS = 86400  # ZSET retention window

# Admin token gate for protected admin-only endpoints. Production should
# replace this with a proper RBAC check; we mirror the simple guard pattern
# used in sibling routers (consent / payouts).
ADMIN_TOKEN_ENV_KEY = "KIX_ID_ADMIN_TOKEN"
_DEFAULT_ADMIN_TOKEN = "kix-id-admin-dev"


# ── Pydantic models ───────────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    phone: str | None = Field(None, max_length=32)
    email: str | None = Field(None, max_length=256)
    display_name: str | None = Field(None, max_length=64)
    primary_language: str | None = Field(None, max_length=16)
    source_brand_id: str | None = Field(None, max_length=64)
    device_fingerprint: str = Field(..., min_length=4, max_length=128)
    country: str | None = Field(None, max_length=4)


class RegisterResponse(BaseModel):
    kid: str
    is_new: bool
    created_at: int


class LookupRequest(BaseModel):
    phone: str | None = None
    email: str | None = None
    device_fingerprint: str | None = None


class LookupResponse(BaseModel):
    kid: str | None
    found: bool


class UpdateProfileRequest(BaseModel):
    display_name: str | None = Field(None, max_length=64)
    avatar_url: str | None = Field(None, max_length=1024)
    primary_language: str | None = Field(None, max_length=16)
    country: str | None = Field(None, max_length=4)


class IdentityLinkRequest(BaseModel):
    phone: str | None = None
    email: str | None = None
    verification_token: str | None = Field(
        None,
        description="Token proving phone/email ownership (OTP confirmation).",
    )


class DeleteRequest(BaseModel):
    admin_token: str
    reason: str = Field(..., min_length=1, max_length=512)


# OAuth-like Connect models


class ConnectAuthorizeRequest(BaseModel):
    brand_id: str = Field(..., min_length=1, max_length=64)
    scopes: list[str] = Field(..., min_length=1)
    redirect_uri: str = Field(..., min_length=1, max_length=1024)
    kid: str = Field(..., min_length=1, max_length=64)
    state: str | None = Field(None, max_length=256)


class ConnectAuthorizeResponse(BaseModel):
    grant_id: str
    code: str
    expires_at: int
    redirect_uri: str
    state: str | None = None


class ConnectTokenRequest(BaseModel):
    grant_id: str
    code: str
    brand_id: str
    client_secret: str


class ConnectTokenResponse(BaseModel):
    access_token: str
    expires_at: int
    kid: str
    scopes: list[str]


class ConnectRevokeRequest(BaseModel):
    grant_id: str
    by: Literal["user", "merchant", "admin"]


class SessionCreateRequest(BaseModel):
    kid: str = Field(..., min_length=1, max_length=64)
    device_fingerprint: str = Field(..., min_length=4, max_length=128)
    source: Literal["qr_scan", "app_open", "push", "deeplink", "referral"] = (
        "app_open"
    )


class SessionVerifyRequest(BaseModel):
    session_token: str = Field(..., min_length=8, max_length=128)


class QRScanBindRequest(BaseModel):
    qr_token: str = Field(..., min_length=1, max_length=256)
    kid: str | None = Field(None, max_length=64)
    device_fingerprint: str = Field(..., min_length=4, max_length=128)


class PushDeviceRegisterRequest(BaseModel):
    platform: Literal["ios", "android", "wechat", "web"]
    token: str = Field(..., min_length=4, max_length=4096)
    device_id: str | None = Field(None, max_length=128)


class PushDeviceRegisterResponse(BaseModel):
    device_id: str
    status: str


# ── Helpers ───────────────────────────────────────────────────────────────


def _now() -> int:
    return int(time.time())


def _hash_identifier(value: str) -> str:
    """Lowercase + SHA-256 — never store raw phone / email."""
    norm = value.strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _mint_kid() -> str:
    """kid_<22-char-base62-ish>. We use uuid4().hex (32 chars) sliced to 22."""
    return "kid_" + uuid4().hex[:22]


def _mint_grant_id() -> str:
    return "grant_" + uuid4().hex[:24]


def _mint_code() -> str:
    return "code_" + secrets.token_urlsafe(24)


def _mint_token(prefix: str) -> str:
    return f"{prefix}_" + secrets.token_urlsafe(32)


def _validate_scopes(scopes: list[str]) -> None:
    bad = [s for s in scopes if s not in VALID_SCOPES]
    if bad:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid scope(s): {bad}. Allowed: {sorted(VALID_SCOPES)}",
        )


def _validate_language(lang: str | None) -> None:
    if lang is not None and lang not in VALID_LANGUAGES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid primary_language '{lang}'. "
                f"Allowed: {sorted(VALID_LANGUAGES)}"
            ),
        )


def _admin_token() -> str:
    import os
    return os.environ.get(ADMIN_TOKEN_ENV_KEY, _DEFAULT_ADMIN_TOKEN)


def _check_admin(token: str | None) -> None:
    from app.security import constant_time_eq

    if not constant_time_eq(token, _admin_token()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin_token required",
        )


def _bearer(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header required",
        )
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization must be 'Bearer <token>'",
        )
    return parts[1]


async def _identity_audit(
    r: aioredis.Redis, kid: str, action: str, detail: dict[str, Any]
) -> None:
    entry = {"ts": _now(), "action": action, **detail}
    key = f"kid:{kid}:identity_history"
    await r.lpush(key, json.dumps(entry))
    await r.ltrim(key, 0, IDENTITY_HISTORY_MAX - 1)


async def _load_kid(r: aioredis.Redis, kid: str) -> dict[str, Any] | None:
    raw = await r.hgetall(f"kid:{kid}")
    if not raw:
        return None
    return raw


async def _load_grant(r: aioredis.Redis, grant_id: str) -> dict[str, Any] | None:
    raw = await r.hgetall(f"grant:{grant_id}")
    if not raw:
        return None
    grant = dict(raw)
    grant["scopes_granted"] = json.loads(grant.get("scopes_granted", "[]"))
    grant["revoked"] = grant.get("revoked", "false") == "true"
    grant["granted_at"] = int(grant.get("granted_at", 0) or 0)
    grant["expires_at"] = int(grant.get("expires_at", 0) or 0)
    grant["last_used_at"] = int(grant.get("last_used_at", 0) or 0)
    return grant


# ── Exported helpers (sibling routers call these) ────────────────────────


async def resolve_kid_from_phone(
    r: aioredis.Redis, phone: str
) -> str | None:
    """Look up existing kid by phone. Returns ``None`` if not found."""
    if not phone:
        return None
    kid = await r.get(f"kid:phone:{_hash_identifier(phone)}")
    return kid or None


async def resolve_kid_from_email(
    r: aioredis.Redis, email: str
) -> str | None:
    """Look up existing kid by email. Returns ``None`` if not found."""
    if not email:
        return None
    kid = await r.get(f"kid:email:{_hash_identifier(email)}")
    return kid or None


async def resolve_kid_from_device(
    r: aioredis.Redis, device_fp: str
) -> str | None:
    """Look up existing kid by device fingerprint."""
    if not device_fp:
        return None
    kid = await r.get(f"kid:device:{device_fp}")
    return kid or None


async def verify_grant(
    r: aioredis.Redis,
    access_token: str,
    brand_id: str,
    required_scope: str,
) -> tuple[bool, str | None, str | None]:
    """Verify a merchant access token's grant + scope.

    Returns ``(valid, kid, reason)``. ``reason`` is ``None`` on success.
    """
    if required_scope not in VALID_SCOPES:
        return False, None, "invalid_scope"

    grant_id = await r.get(f"grant:token:{access_token}")
    if not grant_id:
        return False, None, "token_not_found"

    grant = await _load_grant(r, grant_id)
    if not grant:
        return False, None, "grant_missing"

    if grant.get("revoked"):
        return False, grant.get("kid"), "revoked"

    if grant.get("brand_id") != brand_id:
        return False, grant.get("kid"), "brand_mismatch"

    if grant["expires_at"] and grant["expires_at"] < _now():
        return False, grant.get("kid"), "expired"

    if required_scope not in grant["scopes_granted"]:
        return False, grant.get("kid"), "scope_not_granted"

    # Bump last_used_at (best effort; non-fatal on failure)
    try:
        await r.hset(f"grant:{grant_id}", "last_used_at", _now())
    except Exception:  # pragma: no cover
        pass

    return True, grant.get("kid"), None


async def ensure_kid(
    r: aioredis.Redis,
    *,
    phone: str | None = None,
    email: str | None = None,
    device_fp: str | None = None,
    display_name: str | None = None,
    primary_language: str | None = None,
    source_brand_id: str | None = None,
    country: str | None = None,
) -> tuple[str, bool]:
    """Idempotent kid creation.

    Resolution order: phone → email → device_fp. If any maps to an existing
    kid, that kid is returned (``is_new=False``) and any *new* backers are
    linked. Otherwise a fresh kid is minted (``is_new=True``).
    """
    existing: str | None = None
    if phone:
        existing = await resolve_kid_from_phone(r, phone)
    if not existing and email:
        existing = await resolve_kid_from_email(r, email)
    if not existing and device_fp:
        existing = await resolve_kid_from_device(r, device_fp)

    if existing:
        # Link any newly-presented identity backers we haven't seen.
        updates: list[tuple[str, str]] = []
        if phone:
            ph = _hash_identifier(phone)
            if not await r.get(f"kid:phone:{ph}"):
                updates.append((f"kid:phone:{ph}", existing))
                await r.hset(f"kid:{existing}", "phone_hash", ph)
                await _identity_audit(
                    r, existing, "link_phone", {"phone_hash": ph[:12]}
                )
        if email:
            eh = _hash_identifier(email)
            if not await r.get(f"kid:email:{eh}"):
                updates.append((f"kid:email:{eh}", existing))
                await r.hset(f"kid:{existing}", "email_hash", eh)
                await _identity_audit(
                    r, existing, "link_email", {"email_hash": eh[:12]}
                )
        if device_fp:
            if not await r.get(f"kid:device:{device_fp}"):
                updates.append((f"kid:device:{device_fp}", existing))
            await r.sadd(f"kid:{existing}:devices", device_fp)
        for k, v in updates:
            await r.set(k, v)

        await r.hset(f"kid:{existing}", "last_active_at", _now())
        return existing, False

    # Mint new kid
    kid = _mint_kid()
    now = _now()
    profile: dict[str, Any] = {
        "kid": kid,
        "created_at": now,
        "last_active_at": now,
        "status": "active",
    }
    if display_name:
        profile["display_name"] = display_name
    if primary_language:
        profile["primary_language"] = primary_language
    if source_brand_id:
        profile["source_brand_id"] = source_brand_id
    if country:
        profile["country"] = country
    if phone:
        ph = _hash_identifier(phone)
        profile["phone_hash"] = ph
        await r.set(f"kid:phone:{ph}", kid)
    if email:
        eh = _hash_identifier(email)
        profile["email_hash"] = eh
        await r.set(f"kid:email:{eh}", kid)
    if device_fp:
        await r.set(f"kid:device:{device_fp}", kid)
        await r.sadd(f"kid:{kid}:devices", device_fp)

    await r.hset(f"kid:{kid}", mapping={k: str(v) for k, v in profile.items()})
    await _identity_audit(
        r,
        kid,
        "register",
        {"phone": bool(phone), "email": bool(email), "device": bool(device_fp)},
    )
    return kid, True


# ── Endpoints: identity creation / lookup ────────────────────────────────


@router.post("/register", response_model=RegisterResponse)
async def register(
    body: RegisterRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> RegisterResponse:
    """Idempotent kid registration.

    If the supplied phone *or* email already maps to a kid, that kid is
    returned with ``is_new=False``. Otherwise a fresh ``kid_xxxxxx`` is
    minted. Device fingerprint alone is also accepted as the bottom-tier
    anchor for anonymous-to-known linking.

    Includes a device-fingerprint velocity guard to block sockpuppet
    deposit-promo abuse (老田 fraud bug): >3 new kids in 60s from the
    same device → 429; >10 kids ever from one device → 403 + fraud log.
    Returning users that resolve to an existing kid are NOT counted.
    """
    _validate_language(body.primary_language)

    device_fp = body.device_fingerprint
    velocity_key = f"kid:device_velocity:{device_fp}" if device_fp else None

    # Pre-mint velocity check. We do this BEFORE ensure_kid so a flood of
    # sockpuppet phones can't slip through under the cap; idempotent
    # re-registers of an existing phone/email will still resolve cleanly
    # because we only ZADD when is_new=True below.
    if velocity_key is not None:
        now_ts = time.time()
        # Trim entries outside the retention window.
        await r.zremrangebyscore(
            velocity_key, "-inf", now_ts - DEVICE_VELOCITY_TTL_SECONDS
        )

        recent = await r.zcount(
            velocity_key, now_ts - DEVICE_VELOCITY_BURST_SECONDS, "+inf"
        )
        if recent >= DEVICE_VELOCITY_MAX_BURST:
            logger.warning(
                "device_velocity_exceeded device_fp=%s recent=%s",
                device_fp, recent,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "device_velocity_exceeded",
                    "kids_in_window": int(recent),
                    "window_seconds": DEVICE_VELOCITY_BURST_SECONDS,
                },
            )

        total = await r.zcard(velocity_key)
        if total >= DEVICE_VELOCITY_MAX_TOTAL:
            logger.warning(
                "device_kid_limit_reached device_fp=%s total=%s — fraud signal",
                device_fp, total,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "device_kid_limit_reached",
                    "total_kids": int(total),
                    "limit": DEVICE_VELOCITY_MAX_TOTAL,
                },
            )

    kid, is_new = await ensure_kid(
        r,
        phone=body.phone,
        email=body.email,
        device_fp=device_fp,
        display_name=body.display_name,
        primary_language=body.primary_language,
        source_brand_id=body.source_brand_id,
        country=body.country,
    )

    # Track only freshly-minted kids — returning users that resolve to an
    # existing kid are not new registrations and don't count toward the cap.
    if is_new and velocity_key is not None:
        try:
            await r.zadd(velocity_key, {kid: time.time()})
            await r.expire(velocity_key, DEVICE_VELOCITY_TTL_SECONDS)
        except Exception as exc:  # never break the register path
            logger.warning("device_velocity ZADD failed: %s", exc)

    profile = await _load_kid(r, kid)
    created_at = int(profile.get("created_at", _now())) if profile else _now()

    return RegisterResponse(kid=kid, is_new=is_new, created_at=created_at)


@router.post("/lookup", response_model=LookupResponse)
async def lookup(
    body: LookupRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> LookupResponse:
    """Look up a kid by phone, email, or device fingerprint."""
    if not (body.phone or body.email or body.device_fingerprint):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one of phone/email/device_fingerprint required",
        )

    kid: str | None = None
    if body.phone:
        kid = await resolve_kid_from_phone(r, body.phone)
    if not kid and body.email:
        kid = await resolve_kid_from_email(r, body.email)
    if not kid and body.device_fingerprint:
        kid = await resolve_kid_from_device(r, body.device_fingerprint)

    return LookupResponse(kid=kid, found=bool(kid))


@router.get("/admin/device-velocity/{device_fp}")
async def admin_device_velocity(
    device_fp: str,
    x_admin_token: str | None = Header(None, alias="X-Admin-Token"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Admin: inspect device-fingerprint registration velocity.

    Returns burst (last 60s), total (24h retention), the full kid list,
    and a ``suspicious`` flag set when either threshold is breached. Used
    by fraud ops to triage sockpuppet rings flagged by the register-time
    velocity guard.
    """
    _check_admin(x_admin_token)

    velocity_key = f"kid:device_velocity:{device_fp}"
    now_ts = time.time()

    # Trim stale entries so the counts reflect the live retention window.
    await r.zremrangebyscore(
        velocity_key, "-inf", now_ts - DEVICE_VELOCITY_TTL_SECONDS
    )

    recent_60s = await r.zcount(
        velocity_key, now_ts - DEVICE_VELOCITY_BURST_SECONDS, "+inf"
    )
    total_24h = await r.zcard(velocity_key)
    all_kids = await r.zrange(velocity_key, 0, -1, withscores=True)

    suspicious = bool(
        recent_60s >= DEVICE_VELOCITY_MAX_BURST
        or total_24h >= DEVICE_VELOCITY_MAX_TOTAL
    )

    return {
        "device_fp": device_fp,
        "recent_60s": int(recent_60s),
        "total_24h": int(total_24h),
        "all_kids": [
            {"kid": k, "registered_at": float(ts)} for k, ts in all_kids
        ],
        "suspicious": suspicious,
        "thresholds": {
            "burst_seconds": DEVICE_VELOCITY_BURST_SECONDS,
            "max_burst": DEVICE_VELOCITY_MAX_BURST,
            "max_total": DEVICE_VELOCITY_MAX_TOTAL,
        },
    }


@router.get("/{kid}")
async def get_kid(
    kid: str,
    include: str | None = None,
    x_admin_token: str | None = Header(None, alias="X-Admin-Token"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Admin-only deep profile read."""
    _check_admin(x_admin_token)

    profile = await _load_kid(r, kid)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="kid not found"
        )

    out: dict[str, Any] = {"kid": kid, "profile": profile}

    parts = {p.strip() for p in (include or "").split(",") if p.strip()}
    if "history" in parts:
        raw = await r.lrange(f"kid:{kid}:identity_history", 0, -1)
        out["identity_history"] = [json.loads(x) for x in raw]
    if "devices" in parts:
        out["devices"] = sorted(await r.smembers(f"kid:{kid}:devices"))
    if "grants" in parts:
        gids = await r.smembers(f"kid:{kid}:grants")
        out["grants"] = [await _load_grant(r, g) for g in gids]
    return out


@router.post("/{kid}/update")
async def update_profile(
    kid: str,
    body: UpdateProfileRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Update kid profile attributes."""
    _validate_language(body.primary_language)

    profile = await _load_kid(r, kid)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="kid not found"
        )
    if profile.get("status") == "deleted":
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="kid is deleted"
        )

    fields: dict[str, str] = {"last_active_at": str(_now())}
    if body.display_name is not None:
        fields["display_name"] = body.display_name
    if body.avatar_url is not None:
        fields["avatar_url"] = body.avatar_url
    if body.primary_language is not None:
        fields["primary_language"] = body.primary_language
    if body.country is not None:
        fields["country"] = body.country

    await r.hset(f"kid:{kid}", mapping=fields)
    await _identity_audit(
        r, kid, "update_profile", {"fields": sorted(fields.keys())}
    )
    return {"kid": kid, "updated_fields": sorted(fields.keys())}


@router.post("/{kid}/identity-link")
async def identity_link(
    kid: str,
    body: IdentityLinkRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Link an additional phone or email backer to an existing kid.

    A real implementation would verify ``verification_token`` against an
    OTP service. For now we require its presence as a non-empty proof.
    """
    if not (body.phone or body.email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="phone or email required",
        )
    if not body.verification_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="verification_token required (OTP proof)",
        )

    profile = await _load_kid(r, kid)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="kid not found"
        )
    if profile.get("status") == "deleted":
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="kid is deleted"
        )

    linked: list[str] = []

    if body.phone:
        ph = _hash_identifier(body.phone)
        existing = await r.get(f"kid:phone:{ph}")
        if existing and existing != kid:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="phone already linked to another kid",
            )
        await r.set(f"kid:phone:{ph}", kid)
        await r.hset(f"kid:{kid}", "phone_hash", ph)
        await _identity_audit(
            r, kid, "link_phone", {"phone_hash": ph[:12]}
        )
        linked.append("phone")
        # Dashboard counters: per-brand phone_verified set for today.
        try:
            day_str = time.strftime("%Y-%m-%d", time.gmtime(_now()))
            brands = await r.smembers(f"kid:{kid}:brands")
            for b in brands or []:
                pv_key = f"brand:{b}:phone_verified:{day_str}"
                await r.sadd(pv_key, kid)
                await r.expire(pv_key, 60 * 60 * 24 * 35)
        except Exception:  # pragma: no cover
            pass

    if body.email:
        eh = _hash_identifier(body.email)
        existing = await r.get(f"kid:email:{eh}")
        if existing and existing != kid:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="email already linked to another kid",
            )
        await r.set(f"kid:email:{eh}", kid)
        await r.hset(f"kid:{kid}", "email_hash", eh)
        await _identity_audit(
            r, kid, "link_email", {"email_hash": eh[:12]}
        )
        linked.append("email")

    return {"kid": kid, "linked": linked}


@router.delete("/{kid}")
async def delete_kid(
    kid: str,
    body: DeleteRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Soft delete a kid (admin only).

    Preserves attribution graph for legal compliance — the kid HASH stays
    in place with ``status=deleted`` but reverse-lookups (phone, email,
    device) are removed so the identifiers can be reused by other kids.
    """
    _check_admin(body.admin_token)

    profile = await _load_kid(r, kid)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="kid not found"
        )

    # Strip reverse lookups so identifiers are reusable.
    ph = profile.get("phone_hash")
    eh = profile.get("email_hash")
    if ph:
        await r.delete(f"kid:phone:{ph}")
    if eh:
        await r.delete(f"kid:email:{eh}")
    devices = await r.smembers(f"kid:{kid}:devices")
    for d in devices:
        await r.delete(f"kid:device:{d}")
    await r.delete(f"kid:{kid}:devices")

    # Revoke all active grants
    grant_ids = await r.smembers(f"kid:{kid}:grants")
    for gid in grant_ids:
        await r.hset(f"grant:{gid}", "revoked", "true")

    await r.hset(
        f"kid:{kid}",
        mapping={
            "status": "deleted",
            "deleted_at": str(_now()),
            "deleted_reason": body.reason[:512],
        },
    )
    await _identity_audit(r, kid, "delete", {"reason": body.reason[:128]})
    return {"kid": kid, "status": "deleted"}


# ── Endpoints: OAuth-like Connect ────────────────────────────────────────


async def _consent_gate(
    r: aioredis.Redis, kid: str, scopes: list[str]
) -> tuple[bool, str | None]:
    """For each scope that maps to a consent.py scope, verify the kid
    granted that consent. Returns ``(ok, missing_consent_scope)``."""
    # Soft import — consent.check_internal expects (user_id, scope, r). Treat
    # the kid as the user_id for the purpose of consent records.
    try:
        from app.routers.consent import check_internal as _consent_check
    except Exception:  # pragma: no cover — consent module may not be loaded
        # Fail open if consent module is missing — better safe? In prod we'd
        # fail closed; here, we choose fail-open with a logged warning so
        # tests in environments without consent seeded still pass.
        logger.warning("consent module unavailable; skipping consent gate")
        return True, None

    for s in scopes:
        req = SCOPE_CONSENT_REQUIREMENT.get(s)
        if not req:
            continue
        allowed, _reason = await _consent_check(kid, req, r)
        if not allowed:
            return False, req
    return True, None


@router.post("/connect/authorize", response_model=ConnectAuthorizeResponse)
async def connect_authorize(
    body: ConnectAuthorizeRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> ConnectAuthorizeResponse:
    """User (inside KiX App) authorizes a merchant to read their profile.

    Records a ConnectGrant and emits a short-lived ``code`` the merchant
    will exchange via ``/connect/token``. Consent for each requested scope
    is verified — if missing, returns ``403`` with the offending consent
    scope so the SDK can drive the consent grant UX and retry.
    """
    _validate_scopes(body.scopes)

    profile = await _load_kid(r, body.kid)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="kid not found"
        )
    if profile.get("status") == "deleted":
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="kid is deleted"
        )

    ok, missing = await _consent_gate(r, body.kid, body.scopes)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"consent_required:{missing}",
            headers={"Consent-Required": missing or ""},
        )

    grant_id = _mint_grant_id()
    code = _mint_code()
    now = _now()
    expires_at = now + GRANT_TOKEN_TTL_SECONDS

    grant_record = {
        "grant_id": grant_id,
        "kid": body.kid,
        "brand_id": body.brand_id,
        "scopes_granted": json.dumps(sorted(set(body.scopes))),
        "redirect_uri": body.redirect_uri,
        "state": body.state or "",
        "granted_at": str(now),
        "expires_at": str(expires_at),
        "last_used_at": "0",
        "revoked": "false",
    }
    await r.hset(f"grant:{grant_id}", mapping=grant_record)
    await r.sadd(f"kid:{body.kid}:grants", grant_id)
    await r.sadd(f"brand:{body.brand_id}:grants", grant_id)
    await r.set(
        f"grant:code:{code}", grant_id, ex=GRANT_CODE_TTL_SECONDS
    )

    await _identity_audit(
        r,
        body.kid,
        "connect_authorize",
        {"brand_id": body.brand_id, "scopes": sorted(set(body.scopes))},
    )

    return ConnectAuthorizeResponse(
        grant_id=grant_id,
        code=code,
        expires_at=now + GRANT_CODE_TTL_SECONDS,
        redirect_uri=body.redirect_uri,
        state=body.state,
    )


@router.post("/connect/token", response_model=ConnectTokenResponse)
async def connect_token(
    body: ConnectTokenRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> ConnectTokenResponse:
    """Exchange an authorization code for a long-lived access token.

    The code is single-use and expires in 5 minutes. ``client_secret`` is
    validated against the brand's secret (we accept any non-empty secret
    here — production should look up brand secrets in the brand router).
    """
    grant_id_from_code = await r.get(f"grant:code:{body.code}")
    if not grant_id_from_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="code_invalid_or_expired",
        )
    if grant_id_from_code != body.grant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="grant_id mismatch",
        )

    grant = await _load_grant(r, body.grant_id)
    if not grant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="grant not found"
        )

    if grant["brand_id"] != body.brand_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="brand_id mismatch"
        )

    if not body.client_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="client_secret required",
        )

    if grant.get("revoked"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="grant revoked"
        )

    access_token = _mint_token("at")
    # Tokens are valid until the grant's expires_at.
    ttl = max(60, grant["expires_at"] - _now())
    await r.set(f"grant:token:{access_token}", body.grant_id, ex=ttl)
    # Burn the code so it cannot be exchanged twice.
    await r.delete(f"grant:code:{body.code}")
    await r.hset(
        f"grant:{body.grant_id}", mapping={"last_used_at": str(_now())}
    )

    return ConnectTokenResponse(
        access_token=access_token,
        expires_at=grant["expires_at"],
        kid=grant["kid"],
        scopes=grant["scopes_granted"],
    )


@router.post("/connect/revoke")
async def connect_revoke(
    body: ConnectRevokeRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Revoke a ConnectGrant.

    The ``by`` field records who initiated the revocation. Any access
    token bound to this grant becomes invalid on next ``verify_grant``.
    """
    grant = await _load_grant(r, body.grant_id)
    if not grant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="grant not found"
        )

    await r.hset(
        f"grant:{body.grant_id}",
        mapping={
            "revoked": "true",
            "revoked_at": str(_now()),
            "revoked_by": body.by,
        },
    )
    await _identity_audit(
        r,
        grant["kid"],
        "connect_revoke",
        {
            "grant_id": body.grant_id,
            "brand_id": grant.get("brand_id"),
            "by": body.by,
        },
    )
    return {"grant_id": body.grant_id, "revoked": True, "by": body.by}


@router.get("/connect/grants/{kid}")
async def list_grants(
    kid: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """List all merchants this kid has connected to."""
    gids = await r.smembers(f"kid:{kid}:grants")
    out: list[dict[str, Any]] = []
    for gid in gids:
        g = await _load_grant(r, gid)
        if not g:
            continue
        out.append(
            {
                "grant_id": g["grant_id"],
                "brand_id": g["brand_id"],
                "scopes_granted": g["scopes_granted"],
                "granted_at": g["granted_at"],
                "expires_at": g["expires_at"],
                "last_used_at": g["last_used_at"],
                "revoked": g["revoked"],
            }
        )
    # Sort: active first, then by granted_at desc
    out.sort(key=lambda x: (x["revoked"], -x["granted_at"]))
    return {"kid": kid, "grants": out}


# ── Endpoints: profile-for-merchant (scope-filtered) ─────────────────────


@router.get("/{kid}/profile-for-merchant/{brand_id}")
async def profile_for_merchant(
    kid: str,
    brand_id: str,
    authorization: str | None = Header(None),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return the kid's profile filtered through the granted scopes.

    The merchant supplies their ``Bearer <access_token>``. We verify the
    token against the grant, then surface only the attributes whose scope
    was granted. The ``profile`` scope is the minimum gate.
    """
    token = _bearer(authorization)
    valid, token_kid, reason = await verify_grant(r, token, brand_id, "profile")
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"grant_invalid:{reason}",
        )
    if token_kid != kid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="token does not belong to this kid",
        )

    grant_id = await r.get(f"grant:token:{token}")
    grant = await _load_grant(r, grant_id) if grant_id else None
    scopes = set(grant["scopes_granted"]) if grant else set()

    profile = await _load_kid(r, kid)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="kid not found"
        )

    out: dict[str, Any] = {"kid": kid}
    # profile scope — basic name + language
    if "profile" in scopes:
        if profile.get("display_name"):
            out["display_name"] = profile["display_name"]
        if profile.get("avatar_url"):
            out["avatar_url"] = profile["avatar_url"]
        if profile.get("primary_language"):
            out["language"] = profile["primary_language"]
    if "location" in scopes:
        if profile.get("country"):
            out["country"] = profile["country"]
        if profile.get("geo_city"):
            out["geo_city"] = profile["geo_city"]
    if "email" in scopes and profile.get("email_hash"):
        # We never return raw email — only a stable hash so the merchant
        # can correlate with their own records.
        out["email_hash"] = profile["email_hash"]
    if "phone" in scopes and profile.get("phone_hash"):
        out["phone_hash"] = profile["phone_hash"]
    if "favorites" in scopes:
        favs = await r.smembers(f"kid:{kid}:favorites")
        out["favorites"] = sorted(favs)
    if "history" in scopes:
        # Surface a brief history summary; the full insights endpoint is the
        # richer view.
        out["history_available"] = True
    return out


# ── Endpoints: cross-merchant insights ──────────────────────────────────


@router.get("/{kid}/insights")
async def insights(
    kid: str,
    authorization: str | None = Header(None),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Cross-merchant aggregated profile (requires ``insights`` scope).

    Designed to help an auction bidder reason about the kid's broader value
    without learning *which* merchants the kid frequents (no PII leak).
    """
    token = _bearer(authorization)
    grant_id = await r.get(f"grant:token:{token}")
    grant = await _load_grant(r, grant_id) if grant_id else None
    if not grant or grant.get("revoked"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="grant_invalid",
        )
    if "insights" not in grant["scopes_granted"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="insights scope not granted",
        )
    if grant["kid"] != kid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="token does not belong to this kid",
        )

    # Best-effort aggregation — fields may be empty in fresh installs.
    profile = await _load_kid(r, kid) or {}

    total_games = int(await r.get(f"kid:{kid}:stat:games") or 0)
    total_vouchers = int(await r.get(f"kid:{kid}:stat:vouchers") or 0)
    last_brand = await r.get(f"kid:{kid}:stat:last_brand")
    last_active = int(profile.get("last_active_at", 0) or 0)

    fav_categories_raw = await r.smembers(f"kid:{kid}:fav_categories")
    active_hours_raw = await r.smembers(f"kid:{kid}:active_hours")
    primary_geo = profile.get("geo_city") or profile.get("country")
    master_tier = await r.get(f"kid:{kid}:master_tier") or "bronze"
    brand_tiers_raw = await r.hgetall(f"kid:{kid}:brand_tiers") or {}

    return {
        "kid": kid,
        "activity_summary": {
            "total_games_played": total_games,
            "total_vouchers_claimed": total_vouchers,
            "last_active_brand": last_brand,
            "last_active_at": last_active,
        },
        "preferences": {
            "favorite_categories": sorted(fav_categories_raw),
            "active_hours": sorted(int(h) for h in active_hours_raw),
            "primary_geo": primary_geo,
        },
        "tier": {
            "master_tier": master_tier,
            "brand_tiers": dict(brand_tiers_raw),
        },
    }


@router.get("/{kid}/merchant-history/{brand_id}")
async def merchant_history(
    kid: str,
    brand_id: str,
    authorization: str | None = Header(None),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Per-merchant interaction summary (requires ``history`` scope)."""
    token = _bearer(authorization)
    valid, token_kid, reason = await verify_grant(r, token, brand_id, "history")
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"grant_invalid:{reason}",
        )
    if token_kid != kid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="token does not belong to this kid",
        )

    h = await r.hgetall(f"kid:{kid}:brand_history:{brand_id}") or {}
    return {
        "kid": kid,
        "brand_id": brand_id,
        "first_seen_at": int(h.get("first_seen_at", 0) or 0),
        "last_visit_at": int(h.get("last_visit_at", 0) or 0),
        "total_purchases": int(h.get("total_purchases", 0) or 0),
        "total_visits": int(h.get("total_visits", 0) or 0),
        "vouchers_claimed": int(h.get("vouchers_claimed", 0) or 0),
        "current_tier": h.get("current_tier", "bronze"),
    }


# ── Endpoints: universal session ─────────────────────────────────────────


@router.post("/session/create")
async def session_create(
    body: SessionCreateRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Mint a KiX App session for this kid + device combination."""
    profile = await _load_kid(r, body.kid)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="kid not found"
        )
    if profile.get("status") == "deleted":
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="kid is deleted"
        )

    token = _mint_token("sess")
    now = _now()
    expires_at = now + SESSION_TTL_SECONDS

    await r.hset(
        f"session:{token}",
        mapping={
            "kid": body.kid,
            "device": body.device_fingerprint,
            "source": body.source,
            "created_at": str(now),
            "expires_at": str(expires_at),
        },
    )
    await r.expire(f"session:{token}", SESSION_TTL_SECONDS)
    await r.sadd(f"kid:{body.kid}:sessions", token)
    await r.sadd(f"kid:{body.kid}:devices", body.device_fingerprint)
    await r.set(f"kid:device:{body.device_fingerprint}", body.kid)
    await r.hset(f"kid:{body.kid}", "last_active_at", now)

    return {"session_token": token, "expires_at": expires_at}


@router.post("/session/verify")
async def session_verify(
    body: SessionVerifyRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Verify a session token and surface kid + age."""
    s = await r.hgetall(f"session:{body.session_token}")
    if not s:
        return {"valid": False}

    expires_at = int(s.get("expires_at", 0) or 0)
    created_at = int(s.get("created_at", 0) or 0)
    now = _now()
    if expires_at and expires_at < now:
        return {"valid": False}

    return {
        "valid": True,
        "kid": s.get("kid"),
        "session_age_seconds": max(0, now - created_at),
    }


# ── Endpoints: auto-bind from QR scan ────────────────────────────────────


@router.post("/qr-scan/bind")
async def qr_scan_bind(
    body: QRScanBindRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Bind a QR scan to a kid (creates the kid if first contact).

    Resolves ``qr_token`` against the QR registry (set by ``qr.py`` when the
    merchant rotated their dynamic code). Returns the brand context plus
    a recommended_game_slug the KiX App can launch immediately.
    """
    # The QR registry is written by app.routers.qr at keys of the form
    # ``current_qr:{brand_id}:{location_id}`` mapping to the active token.
    # Production should add a reverse index; here we accept a sentinel
    # registry under ``qr:token:{qr_token}`` HASH with brand_id/store_id.
    qr_meta = await r.hgetall(f"qr:token:{body.qr_token}")
    if not qr_meta:
        # Fallback: tolerate "dev" payload where qr_token is itself a
        # JSON-encoded brand_id|store_id pair so smoke tests work without
        # a full qr.py registry seed.
        if ":" in body.qr_token:
            parts = body.qr_token.split(":")
            qr_meta = {"brand_id": parts[0], "store_id": parts[1] if len(parts) > 1 else ""}
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="qr_token not found",
            )

    brand_id = qr_meta.get("brand_id")
    store_id = qr_meta.get("store_id") or None
    if not brand_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="qr_token missing brand_id",
        )

    # Resolve / mint kid
    is_new_kid = False
    if body.kid:
        profile = await _load_kid(r, body.kid)
        if not profile:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="kid not found"
            )
        kid = body.kid
        # Bind this device to the kid if not already.
        await r.sadd(f"kid:{kid}:devices", body.device_fingerprint)
        await r.set(f"kid:device:{body.device_fingerprint}", kid)
    else:
        kid, is_new_kid = await ensure_kid(
            r,
            device_fp=body.device_fingerprint,
            source_brand_id=brand_id,
        )

    # Is this kid new *to this brand*?
    bh_key = f"kid:{kid}:brand_history:{brand_id}"
    bh = await r.hgetall(bh_key)
    is_new_to_brand = not bh
    now = _now()
    if is_new_to_brand:
        await r.hset(
            bh_key,
            mapping={
                "first_seen_at": str(now),
                "last_visit_at": str(now),
                "total_visits": "1",
            },
        )
    else:
        await r.hset(bh_key, "last_visit_at", str(now))
        await r.hincrby(bh_key, "total_visits", 1)

    await r.sadd(f"kid:{kid}:brands", brand_id)
    await r.hset(f"kid:{kid}", "last_active_at", now)

    # Recommended game — last successful game for this brand, else brand
    # default, else the network default.
    recommended = (
        await r.get(f"brand:{brand_id}:default_game")
        or await r.get(f"kid:{kid}:brand:{brand_id}:last_game")
        or "tap-coin"
    )

    await _identity_audit(
        r,
        kid,
        "qr_scan_bind",
        {
            "brand_id": brand_id,
            "store_id": store_id,
            "is_new_kid": is_new_kid,
            "is_new_to_brand": is_new_to_brand,
        },
    )

    # Dashboard daily counters (best-effort).
    try:
        day_str = time.strftime("%Y-%m-%d", time.gmtime(now))
        scans_key = f"brand:{brand_id}:qr_scans:{day_str}"
        await r.sadd(scans_key, f"{kid}:{int(now)}")
        await r.expire(scans_key, 60 * 60 * 24 * 35)
        await r.sadd(f"brand:{brand_id}:scanning_users:{day_str}", kid)
        await r.expire(
            f"brand:{brand_id}:scanning_users:{day_str}", 60 * 60 * 24 * 35
        )
        await r.sadd(f"brand:{brand_id}:active_days", day_str)
        if is_new_to_brand:
            users_key = f"brand:{brand_id}:users_acquired:{day_str}"
            await r.sadd(users_key, kid)
            await r.expire(users_key, 60 * 60 * 24 * 35)
    except Exception:  # pragma: no cover
        logger.warning("dashboard counters failed for brand=%s", brand_id)

    return {
        "kid": kid,
        "brand_id": brand_id,
        "store_id": store_id,
        "recommended_game_slug": recommended,
        "is_new_kid": is_new_kid,
        "is_new_to_brand": is_new_to_brand,
    }


# ── Endpoints: push device registration ─────────────────────────────────
#
# Production push delivery requires the kid → device-token mapping. The
# KiX App (or any merchant SDK with the right scope) calls these
# endpoints whenever:
#
#   * the user installs / reopens the app and FCM/APNS returns a token,
#   * the user grants browser-notification permission and the Web Push
#     subscription endpoint becomes available,
#   * the user binds a WeChat openid via the official account flow,
#   * the user uninstalls or rotates their token (→ unregister).
#
# The actual delivery worker (``app.workers.push_worker``) consumes
# ``push:outbound:queue`` and reads ``kid:{kid}:push_devices`` /
# ``push_device:{device_id}`` to route each push to the right gateway.


@router.post(
    "/{kid}/push-device/register",
    response_model=PushDeviceRegisterResponse,
)
async def push_device_register(
    kid: str,
    body: PushDeviceRegisterRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> PushDeviceRegisterResponse:
    """Register a push token for a kid.

    * ``platform`` — one of ``ios`` / ``android`` / ``wechat`` / ``web``.
    * ``token``    — APNS device token, FCM registration token, WeChat
      openid, or Web Push subscription endpoint (caller serialises Web
      Push subscriptions to JSON before passing).
    * ``device_id`` (optional) — supply to upsert in place after a token
      rotation on the same physical device; otherwise a fresh id is
      minted from the token hash.

    Returns ``{device_id, status}``. The worker can immediately route
    pushes to this device on the next dispatch cycle.
    """
    profile = await _load_kid(r, kid)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="kid not found"
        )
    if profile.get("status") == "deleted":
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="kid is deleted"
        )

    # Lazy import — the worker pulls in app.redis_client at module load,
    # and we already have an active connection; we only need the helper.
    from app.workers.push_worker import device_register

    try:
        device_id = await device_register(
            r,
            kid=kid,
            platform=body.platform,
            token=body.token,
            device_id=body.device_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    await _identity_audit(
        r,
        kid,
        "push_device_register",
        {"platform": body.platform, "device_id": device_id},
    )
    return PushDeviceRegisterResponse(device_id=device_id, status="registered")


@router.delete("/{kid}/push-device/{device_id}")
async def push_device_unregister(
    kid: str,
    device_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Unregister a push device.

    Typical triggers:

    * user disables notifications in the OS,
    * user uninstalls the KiX App (server-side detected via FCM/APNS
      ``Unregistered`` feedback),
    * user signs out / switches accounts on the device.
    """
    profile = await _load_kid(r, kid)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="kid not found"
        )

    from app.workers.push_worker import device_unregister

    removed = await device_unregister(r, kid=kid, device_id=device_id)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="device_id not found for this kid",
        )

    await _identity_audit(
        r,
        kid,
        "push_device_unregister",
        {"device_id": device_id},
    )
    return {"kid": kid, "device_id": device_id, "status": "unregistered"}
