"""Conversions API (CAPI) — server-to-server conversion ingestion.

Pixel events fired from the browser are increasingly lossy: 3P-cookie
deprecation, Safari ITP, ad-blockers, "Limit Ad Tracking", network
failures and battery-saving keepalive drops together cost merchants
~15–35 % of conversion signal. The industry answer — TikTok Events API,
Meta CAPI, Google Enhanced Conversions / Offline Conversions — is to
let merchants POST conversions server-side, from their own backend,
deduplicated against any matching browser pixel event by a shared
``event_id``.

This module is the KiX equivalent. It runs alongside ``pixel.py``:

  * **Pixel** (``/api/v1/pixel/event``)           browser-fired, cookie/FP
  * **CAPI**  (``/api/v1/capi/conversion``)       backend-fired, server auth

Both paths converge on the same :func:`pixel._process_event` so attribution,
audit, stats, refund handling and Enhanced-Conversions matching stay
consistent. The merchant supplies a stable ``event_id`` on both, and a
1-hour Redis NX lock collapses the duplicate.

Authentication
--------------
A merchant authenticates with ``Authorization: Bearer <capi_api_key>``.
Keys are minted by ``POST /api/v1/capi/key`` (one per brand) and stored
under ``capi:key:{api_key}`` → brand_id. Production should rotate these
on the brands router; we expose a thin admin-gated rotation endpoint.

Redis schema (additive — pixel module owns most of the keys)
------------------------------------------------------------
::

    capi:key:{api_key}            STRING   brand_id (reverse lookup)
    brand:{bid}:capi_key          STRING   api_key (forward lookup)
    capi:dedup:{event_id}         STRING   "1" EX 3600 (set by pixel._dedup)

The dedup window is shared with browser pixel events: if either side
arrives first, the second is flagged ``deduplicated=true`` and skips
attribution + stats (audit still recorded).

Limits
------
  * Per-key: 5 000 events/minute (CAPI is server→server so the budget
    is 5× a web pixel; merchants doing batch backfills hit the batch
    endpoint instead).
  * Batch: up to 1 000 events per ``POST /conversion-batch`` call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator
import redis.asyncio as aioredis

from app.redis_client import get_redis
from app.routers import pixel as pixel_mod

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ─────────────────────────────────────────────────────────────

CAPI_RATE_LIMIT_PER_MINUTE = 5000      # per api_key (server → server)
CAPI_MAX_BATCH = 1000                  # cap per /conversion-batch call
CAPI_DEDUP_WINDOW_SECONDS = 3600       # 1h — must equal pixel side
CAPI_KEY_PREFIX = "capi_"

ADMIN_TOKEN_ENV_KEY = "KIX_CAPI_ADMIN_TOKEN"
_DEFAULT_ADMIN_TOKEN = "kix-capi-admin-dev"


# ── Pydantic models ───────────────────────────────────────────────────────


class CAPIKeyMintRequest(BaseModel):
    """Brand-owner mints a CAPI api key for their backend.

    Auth is via the admin token in dev / the brand-owner JWT in prod.
    Stored mapping is one key per brand; rotating reissues a fresh key
    and *invalidates the previous one* immediately.
    """
    brand_id: str = Field(..., min_length=1, max_length=64)


class CAPIKeyMintResponse(BaseModel):
    brand_id: str
    api_key: str
    rotated: bool          # true if an older key was just invalidated
    created_at: float


class CAPIUserData(BaseModel):
    """Hashed PII + low-trust identity hints, copied straight onto the
    underlying pixel event for Enhanced-Conversions matching.

    ``client_ip`` and ``user_agent`` are recorded for forensics + bot
    filtering but never sent back to the merchant. ``external_id`` is the
    merchant's own CRM id; we index it under ``identity:external:<id>``
    when present so future events match without PII.
    """
    email_sha256: str | None = Field(None, min_length=64, max_length=64)
    phone_sha256: str | None = Field(None, min_length=64, max_length=64)
    first_name_sha256: str | None = Field(None, min_length=64, max_length=64)
    last_name_sha256: str | None = Field(None, min_length=64, max_length=64)
    address_hash: str | None = Field(None, min_length=32, max_length=128)
    device_fingerprint: str | None = Field(None, max_length=128)
    external_id: str | None = Field(None, max_length=128)
    client_ip: str | None = Field(None, max_length=64)
    user_agent: str | None = Field(None, max_length=512)


class CAPICustomData(BaseModel):
    """Value-bearing data on the conversion."""
    currency: str | None = Field(None, max_length=8)
    value_cents: int | None = Field(default=None, ge=0)
    order_id: str | None = Field(None, max_length=128)
    items: list[dict[str, Any]] = Field(default_factory=list)
    extras: dict[str, Any] = Field(default_factory=dict)


class CAPIConversionRequest(BaseModel):
    """Single CAPI conversion event."""
    event_type: str = Field(..., min_length=1, max_length=64)
    event_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description=(
            "Required dedup key — must match the browser pixel event for "
            "the same conversion. Without it, browser + server signals "
            "double-count."
        ),
    )
    event_time: float = Field(
        ...,
        description="Unix seconds when the conversion happened on the "
                    "merchant side (we accept any positive value).",
    )
    brand_id: str = Field(..., min_length=1, max_length=64)
    user_data: CAPIUserData
    custom_data: CAPICustomData = Field(default_factory=CAPICustomData)
    action_source: str | None = Field(
        None,
        max_length=32,
        description="One of: website, app, offline, system_generated, "
                    "physical_store, chat, email, phone_call.",
    )

    @field_validator("event_time")
    @classmethod
    def _sane_event_time(cls, v: float) -> float:
        if v < 0:
            raise ValueError("event_time must be >= 0")
        # Reject obvious clock skew (>14 days in future, >180 days in past).
        now = time.time()
        if v > now + 14 * 86400:
            raise ValueError("event_time too far in future")
        if v < now - 180 * 86400:
            raise ValueError("event_time too far in past")
        return v


class CAPIConversionResponse(BaseModel):
    ok: bool
    event_id: str
    audit_event_id: str | None = None
    deduplicated: bool = False
    matched: bool = False
    kid: str | None = None
    attributed: bool | None = None


class CAPIBatchRequest(BaseModel):
    events: list[CAPIConversionRequest] = Field(default_factory=list)

    @field_validator("events")
    @classmethod
    def _cap_events(cls, v: list[CAPIConversionRequest]) -> list[CAPIConversionRequest]:
        if not v:
            raise ValueError("events must not be empty")
        if len(v) > CAPI_MAX_BATCH:
            raise ValueError(f"events exceeds max batch size {CAPI_MAX_BATCH}")
        return v


class CAPIBatchResult(BaseModel):
    index: int
    event_id: str
    status: str             # "accepted" | "rejected"
    audit_event_id: str | None = None
    deduplicated: bool = False
    matched: bool = False
    kid: str | None = None
    attributed: bool | None = None
    error: str | None = None


class CAPIBatchResponse(BaseModel):
    ok: bool
    accepted: int
    rejected: int
    results: list[CAPIBatchResult]


# ── Helpers ───────────────────────────────────────────────────────────────


def _now() -> float:
    return time.time()


def _admin_token() -> str:
    return os.environ.get(ADMIN_TOKEN_ENV_KEY, _DEFAULT_ADMIN_TOKEN)


def _check_admin(token: str | None) -> None:
    from app.security import constant_time_eq

    if not constant_time_eq(token, _admin_token()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin_token required",
        )


def _mint_api_key() -> str:
    return CAPI_KEY_PREFIX + secrets.token_urlsafe(32)


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


async def _resolve_brand_from_key(
    r: aioredis.Redis, api_key: str
) -> str | None:
    if not api_key or not api_key.startswith(CAPI_KEY_PREFIX):
        return None
    bid = await r.get(f"capi:key:{api_key}")
    return bid or None


async def _check_rate_limit(r: aioredis.Redis, api_key: str) -> None:
    """Per-api_key sliding-minute rate limit (server-side budget).

    CAPI is server→server so we allow 5× a web pixel — but the cap exists
    to bound a runaway loop in a merchant's job queue.
    """
    minute = int(_now() // 60)
    key = f"capi:ratelimit:{api_key}:{minute}"
    cnt = await r.incr(key)
    if cnt == 1:
        await r.expire(key, 120)
    if cnt > CAPI_RATE_LIMIT_PER_MINUTE:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate_limit_exceeded",
        )


async def _find_capi_pixel(
    r: aioredis.Redis, brand_id: str
) -> dict[str, Any] | None:
    """Resolve the brand's primary pixel record so CAPI events flow into
    the same audit + stats space.

    CAPI conversions still need a ``pixel_id`` so the pixel module's
    audit/index/stats/refund-dispute machinery keeps working unchanged.
    We prefer a pixel explicitly tagged ``status=capi-default``; otherwise
    fall back to the first active pixel under the brand. If none exists,
    we auto-mint a dedicated "capi" pixel so the merchant doesn't have to
    register one just to use CAPI.
    """
    pids = await r.smembers(f"brand:{brand_id}:pixels")
    candidates: list[str] = sorted(pids) if pids else []
    for pid in candidates:
        rec = await r.hgetall(f"pixel:{pid}")
        if rec and rec.get("status") not in ("deleted",):
            try:
                return await pixel_mod._load_pixel(r, pid)
            except HTTPException:
                continue
    # Auto-mint a CAPI-only pixel — origins empty (CAPI doesn't carry one).
    pixel_id = pixel_mod._new_pixel_id()
    ts = _now()
    record = {
        "brand_id": brand_id,
        "allowed_origins": json.dumps([]),
        "refund_eligible_within_days": str(pixel_mod.DEFAULT_REFUND_WINDOW_DAYS),
        "created_at": f"{ts:.6f}",
        "status": "active",
        "source": "capi_auto_mint",
    }
    pipe = r.pipeline(transaction=True)
    pipe.hset(f"pixel:{pixel_id}", mapping=record)
    pipe.sadd(f"brand:{brand_id}:pixels", pixel_id)
    await pipe.execute()
    logger.info(
        "capi: auto-minted pixel for brand_id=%s pixel_id=%s",
        brand_id, pixel_id,
    )
    return await pixel_mod._load_pixel(r, pixel_id)


async def _index_external_id(
    r: aioredis.Redis, external_id: str | None, kid: str | None
) -> None:
    """If the merchant supplied an external CRM id and we already resolved a
    kid (via hashed PII), persist the mapping so future events match without
    needing the PII hash again."""
    if not external_id or not kid:
        return
    try:
        await r.set(f"identity:external:{external_id}", kid)
    except Exception:  # pragma: no cover — non-fatal
        pass


# ── Endpoints: API key management ─────────────────────────────────────────


@router.post("/key", response_model=CAPIKeyMintResponse)
async def mint_capi_key(
    body: CAPIKeyMintRequest,
    x_admin_token: str | None = Header(None, alias="X-Admin-Token"),
    r: aioredis.Redis = Depends(get_redis),
) -> CAPIKeyMintResponse:
    """Mint (or rotate) a brand's CAPI api key.

    Stored as ``brand:{bid}:capi_key`` + reverse ``capi:key:{key}`` →
    brand_id. Rotating burns the previous reverse entry so old keys stop
    working immediately. Admin-gated to prevent cross-brand key issuance;
    in production this should sit behind the brand-owner JWT instead.
    """
    _check_admin(x_admin_token)

    prev_key = await r.get(f"brand:{body.brand_id}:capi_key")
    new_key = _mint_api_key()

    pipe = r.pipeline(transaction=True)
    pipe.set(f"brand:{body.brand_id}:capi_key", new_key)
    pipe.set(f"capi:key:{new_key}", body.brand_id)
    if prev_key:
        pipe.delete(f"capi:key:{prev_key}")
    await pipe.execute()

    logger.info(
        "capi key minted: brand_id=%s rotated=%s",
        body.brand_id, bool(prev_key),
    )
    return CAPIKeyMintResponse(
        brand_id=body.brand_id,
        api_key=new_key,
        rotated=bool(prev_key),
        created_at=_now(),
    )


@router.delete("/key/{brand_id}", status_code=204)
async def revoke_capi_key(
    brand_id: str,
    x_admin_token: str | None = Header(None, alias="X-Admin-Token"),
    r: aioredis.Redis = Depends(get_redis),
) -> None:
    """Revoke a brand's CAPI api key (no replacement)."""
    _check_admin(x_admin_token)

    key = await r.get(f"brand:{brand_id}:capi_key")
    if not key:
        raise HTTPException(status_code=404, detail="capi_key_not_found")
    pipe = r.pipeline(transaction=True)
    pipe.delete(f"capi:key:{key}")
    pipe.delete(f"brand:{brand_id}:capi_key")
    await pipe.execute()
    logger.info("capi key revoked: brand_id=%s", brand_id)


# ── Endpoints: conversion ingestion ───────────────────────────────────────


async def _ingest_one(
    r: aioredis.Redis,
    *,
    body: CAPIConversionRequest,
    pixel: dict[str, Any],
) -> tuple[str | None, bool, bool, str | None, bool | None]:
    """Run a single CAPI event through the shared pixel processing path.

    Returns ``(audit_event_id, matched, deduplicated, matched_kid, attributed)``.
    Raises HTTPException for client errors so the caller can surface them.
    """
    # Project CAPI user_data → pixel EnhancedData shape.
    ud = body.user_data
    enhanced: dict[str, Any] = {}
    if ud.email_sha256:
        enhanced["email_sha256"] = ud.email_sha256
    if ud.phone_sha256:
        enhanced["phone_sha256"] = ud.phone_sha256
    if ud.first_name_sha256:
        enhanced["first_name_sha256"] = ud.first_name_sha256
    if ud.last_name_sha256:
        enhanced["last_name_sha256"] = ud.last_name_sha256
    if ud.address_hash:
        enhanced["address_hash"] = ud.address_hash
    if ud.external_id:
        enhanced["external_id"] = ud.external_id

    # CAPI has no browser fingerprint; fall back to external_id, then a
    # synthetic-from-event_id hash so attribution still gets a stable key.
    device_fp = (
        ud.device_fingerprint
        or (f"ext:{ud.external_id}" if ud.external_id else None)
        or f"capi:{body.event_id}"
    )

    cd = body.custom_data
    meta = {
        "source": "capi",
        "event_time": body.event_time,
        "action_source": body.action_source or "system_generated",
        "user_agent": ud.user_agent,
        "client_ip": ud.client_ip,
        "items": cd.items,
    }
    if cd.extras:
        meta["extras"] = cd.extras

    # Synthetic origin — CAPI doesn't have a browser Origin, so we stamp a
    # server-side identifier (`kix-native:capi:<brand>`) that satisfies the
    # pixel validator + matches the brand's allowed_origins if they ever
    # opt into strict-origin enforcement on CAPI.
    origin = f"kix-native:capi-{body.brand_id}"

    (
        audit_event_id,
        attributed,
        _src,
        _attr_id,
        matched,
        matched_kid,
        deduplicated,
    ) = await pixel_mod._process_event(
        r,
        pixel=pixel,
        event_type=body.event_type,
        user_id=None,                    # CAPI relies on enhanced_data
        device_fingerprint=device_fp,
        order_id=cd.order_id,
        amount_cents=cd.value_cents,
        currency=cd.currency,
        meta=meta,
        referrer=None,
        origin=origin,
        url=None,
        enhanced_data=enhanced or None,
        client_event_id=body.event_id,
    )

    # If the merchant told us an external_id and we matched a kid, persist
    # the mapping so future PII-less events still resolve to the same kid.
    if matched_kid and ud.external_id:
        await _index_external_id(r, ud.external_id, matched_kid)

    return audit_event_id, matched, deduplicated, matched_kid, attributed


@router.post("/conversion", response_model=CAPIConversionResponse)
async def record_conversion(
    body: CAPIConversionRequest,
    authorization: str | None = Header(default=None),
    r: aioredis.Redis = Depends(get_redis),
) -> CAPIConversionResponse:
    """Server-to-server conversion event.

    Authenticates with the brand's CAPI api key, validates that the
    body's ``brand_id`` matches the key, dedupes against any pixel
    event with the same ``event_id`` (1h window), and runs the event
    through the shared pixel pipeline so attribution + audit + stats
    stay unified.
    """
    token = _bearer(authorization)
    brand_from_key = await _resolve_brand_from_key(r, token)
    if not brand_from_key:
        raise HTTPException(status_code=403, detail="invalid_api_key")
    if brand_from_key != body.brand_id:
        raise HTTPException(status_code=403, detail="brand_id_mismatch")

    if body.event_type not in pixel_mod.SUPPORTED_EVENTS:
        raise HTTPException(status_code=422, detail="unsupported_event_type")

    await _check_rate_limit(r, token)

    pixel = await _find_capi_pixel(r, body.brand_id)
    if pixel is None:  # pragma: no cover — _find_capi_pixel always returns
        raise HTTPException(status_code=500, detail="pixel_unavailable")

    try:
        audit_event_id, matched, deduplicated, matched_kid, attributed = (
            await _ingest_one(r, body=body, pixel=pixel)
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "capi conversion failure: brand=%s event_id=%s err=%s",
            body.brand_id, body.event_id, exc,
        )
        raise HTTPException(status_code=500, detail="internal_error") from exc

    return CAPIConversionResponse(
        ok=True,
        event_id=body.event_id,
        audit_event_id=audit_event_id,
        deduplicated=deduplicated,
        matched=matched,
        kid=matched_kid,
        attributed=attributed,
    )


@router.post("/conversion-batch", response_model=CAPIBatchResponse)
async def record_conversion_batch(
    body: CAPIBatchRequest,
    authorization: str | None = Header(default=None),
    r: aioredis.Redis = Depends(get_redis),
) -> CAPIBatchResponse:
    """Batch up to 1 000 server-side conversions in one round-trip.

    Per-event results land in ``results[i]`` with ``status`` =
    ``accepted`` or ``rejected``. The envelope is ``ok=True`` whenever
    at least one event accepted. Each event is rate-limit-charged
    individually (batching is for round-trip latency, not for quota
    arbitrage).
    """
    token = _bearer(authorization)
    brand_from_key = await _resolve_brand_from_key(r, token)
    if not brand_from_key:
        raise HTTPException(status_code=403, detail="invalid_api_key")

    # One rate-limit charge per event — batching saves round-trips, not budget.
    # Single up-front IN-MEMORY check on len(events) so we don't even start
    # work on a clearly-over-budget batch.
    if len(body.events) > CAPI_RATE_LIMIT_PER_MINUTE:
        raise HTTPException(status_code=429, detail="batch_exceeds_minute_budget")

    # Resolve pixel once per batch (all events must agree on brand_id).
    declared_brands = {e.brand_id for e in body.events}
    if len(declared_brands) > 1:
        raise HTTPException(status_code=400, detail="batch_brand_mismatch")
    declared_brand = next(iter(declared_brands))
    if declared_brand != brand_from_key:
        raise HTTPException(status_code=403, detail="brand_id_mismatch")

    pixel = await _find_capi_pixel(r, brand_from_key)
    if pixel is None:  # pragma: no cover
        raise HTTPException(status_code=500, detail="pixel_unavailable")

    async def _one(idx: int, ev: CAPIConversionRequest) -> CAPIBatchResult:
        if ev.event_type not in pixel_mod.SUPPORTED_EVENTS:
            return CAPIBatchResult(
                index=idx,
                event_id=ev.event_id,
                status="rejected",
                error="unsupported_event_type",
            )
        try:
            await _check_rate_limit(r, token)
        except HTTPException as http_exc:
            return CAPIBatchResult(
                index=idx,
                event_id=ev.event_id,
                status="rejected",
                error=str(http_exc.detail) if isinstance(http_exc.detail, str)
                else "rate_limited",
            )
        try:
            audit_event_id, matched, deduplicated, matched_kid, attributed = (
                await _ingest_one(r, body=ev, pixel=pixel)
            )
            return CAPIBatchResult(
                index=idx,
                event_id=ev.event_id,
                status="accepted",
                audit_event_id=audit_event_id,
                deduplicated=deduplicated,
                matched=matched,
                kid=matched_kid,
                attributed=attributed,
            )
        except HTTPException as http_exc:
            detail = (
                http_exc.detail
                if isinstance(http_exc.detail, str) else "validation_error"
            )
            return CAPIBatchResult(
                index=idx,
                event_id=ev.event_id,
                status="rejected",
                error=detail,
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("capi batch event %d failed: %s", idx, exc)
            return CAPIBatchResult(
                index=idx,
                event_id=ev.event_id,
                status="rejected",
                error="internal_error",
            )

    results = await asyncio.gather(
        *[_one(i, ev) for i, ev in enumerate(body.events)]
    )
    accepted = sum(1 for r_ in results if r_.status == "accepted")
    rejected = len(results) - accepted

    return CAPIBatchResponse(
        ok=accepted > 0,
        accepted=accepted,
        rejected=rejected,
        results=list(results),
    )


@router.get("/health")
async def health() -> dict[str, Any]:
    """Lightweight health probe — useful for merchant-side connection tests
    before they trust the integration with production traffic."""
    return {"ok": True, "service": "capi", "ts": _now()}
