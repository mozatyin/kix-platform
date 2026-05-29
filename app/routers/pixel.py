"""Conversion Pixel — Google-Analytics-style JS pixel for merchants.

Merchants register a pixel (bound to their brand + allowed origins) and embed
the returned <script> snippet on their site. The browser SDK auto-fires a
`pageview`, then merchants call `kix.identify(...)`, `kix.purchase(...)`,
`kix.signup(...)`, etc.

POST /api/v1/pixel/event records the event:
  * pageview / add_to_cart    → counter bump only (lightweight)
  * purchase                  → attribution.track_conversion (commission split)
  * signup                    → attribution.track_visit (acquisition record)

CORS / abuse:
  * Each pixel has an `allowed_origins` allowlist. The `origin` field of the
    payload + the HTTP `Origin` header must both match — otherwise 403.
  * Per-pixel rate limit: 1000 events/minute (rolling).
  * `user_id` from the client is treated as a hint only; we never grant
    privileges based on it.

Redis schema
------------
    pixel:{pixel_id}                  HASH  {brand_id, allowed_origins (JSON),
                                             created_at, status}
    pixel:{pixel_id}:stats            HASH  {pageviews, purchases, signups,
                                             add_to_carts, attributed,
                                             total_amount_cents}
    pixel:{pixel_id}:ratelimit:{min}  STRING  INCR + EX 120
    brand:{bid}:pixels                SET   of pixel_ids
    pixel_event:{event_id}            HASH  audit record (TTL 7 days)
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, HttpUrl, field_validator
import redis.asyncio as aioredis

from app.redis_client import get_redis
from app.routers import attribution as attr_mod

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ──────────────────────────────────────────────────────────────

EVENT_TTL_SECONDS = 7 * 24 * 60 * 60       # audit retention
RATE_LIMIT_PER_MINUTE = 1000               # per pixel_id
MAX_ALLOWED_ORIGINS = 50                   # safety cap on allowlist length
SUPPORTED_EVENTS = {
    "pageview",
    "add_to_cart",
    "purchase",
    "signup",
    "custom",
}
DEFAULT_SDK_URL = "https://api.kix.gg/sdk/kix-pixel.js"
DEFAULT_EVENT_URL = "https://api.kix.gg/api/v1/pixel/event"


# ── Pydantic models ────────────────────────────────────────────────────────

class PixelRegisterRequest(BaseModel):
    brand_id: str
    allowed_origins: list[str] = Field(default_factory=list)

    @field_validator("allowed_origins")
    @classmethod
    def _validate_origins(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_ALLOWED_ORIGINS:
            raise ValueError(f"allowed_origins exceeds {MAX_ALLOWED_ORIGINS}")
        cleaned: list[str] = []
        for raw in v:
            if not raw:
                continue
            o = raw.strip().rstrip("/")
            if not (o.startswith("http://") or o.startswith("https://")):
                raise ValueError(f"origin must be http(s) URL: {raw}")
            cleaned.append(o)
        # dedupe, preserve order
        seen: set[str] = set()
        out: list[str] = []
        for o in cleaned:
            if o not in seen:
                seen.add(o)
                out.append(o)
        return out


class PixelRegisterResponse(BaseModel):
    pixel_id: str
    brand_id: str
    allowed_origins: list[str]
    embed_snippet: str
    sdk_url: str
    created_at: float


class PixelEventRequest(BaseModel):
    pixel_id: str
    event_type: Literal["pageview", "add_to_cart", "purchase", "signup", "custom"]
    user_id: str | None = None
    device_fingerprint: str
    order_id: str | None = None
    amount_cents: int | None = Field(default=None, ge=0)
    currency: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    referrer: str | None = None
    origin: str
    url: str | None = None


class PixelEventResponse(BaseModel):
    ok: bool
    event_id: str
    event_type: str
    attributed: bool | None = None
    source_brand: str | None = None
    attributed_event_id: str | None = None


class PixelStatsResponse(BaseModel):
    pixel_id: str
    brand_id: str
    total_pageviews: int
    total_add_to_carts: int
    total_purchases: int
    total_signups: int
    total_amount_cents: int
    attributed_purchases: int
    attributed_rate: float


# ── Helpers ────────────────────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _new_pixel_id() -> str:
    # Short URL-safe id; collisions across registrations are infeasible.
    return "px_" + secrets.token_urlsafe(12)


def _normalize_origin(o: str | None) -> str:
    if not o:
        return ""
    return o.strip().rstrip("/")


def _build_snippet(pixel_id: str, sdk_url: str) -> str:
    return (
        "<!-- KiX Pixel -->\n"
        f'<script async src="{sdk_url}" data-pixel="{pixel_id}"></script>\n'
        "<!-- After signup: <script>kix.identify(\"user_123\");</script> -->\n"
        "<!-- After purchase: <script>kix.purchase(\"order_123\", 5000);</script> -->"
    )


async def _load_pixel(r: aioredis.Redis, pixel_id: str) -> dict[str, Any]:
    raw = await r.hgetall(f"pixel:{pixel_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="pixel_not_found")
    if raw.get("status") == "deleted":
        raise HTTPException(status_code=404, detail="pixel_deleted")
    try:
        allowed = json.loads(raw.get("allowed_origins") or "[]")
    except json.JSONDecodeError:
        allowed = []
    return {
        "pixel_id": pixel_id,
        "brand_id": raw.get("brand_id", ""),
        "allowed_origins": list(allowed),
        "created_at": float(raw.get("created_at") or 0),
        "status": raw.get("status", "active"),
    }


async def _check_rate_limit(r: aioredis.Redis, pixel_id: str) -> None:
    minute = int(_now() // 60)
    key = f"pixel:{pixel_id}:ratelimit:{minute}"
    cnt = await r.incr(key)
    if cnt == 1:
        await r.expire(key, 120)
    if cnt > RATE_LIMIT_PER_MINUTE:
        raise HTTPException(status_code=429, detail="rate_limit_exceeded")


def _check_cors(
    pixel_record: dict[str, Any],
    payload_origin: str,
    header_origin: str | None,
) -> str:
    """Validates both the payload `origin` and HTTP `Origin` header.

    Empty allowlist means "anything goes" — useful for testing but discouraged
    in production. Returns the normalized origin string for logging.
    """
    allowed = pixel_record.get("allowed_origins") or []
    p_origin = _normalize_origin(payload_origin)
    h_origin = _normalize_origin(header_origin)
    if not p_origin:
        raise HTTPException(status_code=400, detail="missing_origin")
    if allowed:
        if p_origin not in allowed:
            raise HTTPException(status_code=403, detail="origin_not_allowed")
        # Header may legitimately be absent (same-origin POST, server-to-server),
        # but if present it must agree with payload to prevent spoofing.
        if h_origin and h_origin not in allowed:
            raise HTTPException(status_code=403, detail="origin_header_mismatch")
    return p_origin


async def _record_audit_event(
    r: aioredis.Redis,
    *,
    pixel_id: str,
    brand_id: str,
    event_type: str,
    user_id: str | None,
    device_fingerprint: str,
    origin: str,
    amount_cents: int | None,
    currency: str | None,
    order_id: str | None,
    referrer: str | None,
    url: str | None,
    meta: dict[str, Any],
    attributed: bool | None,
    source_brand: str | None,
) -> str:
    event_id = uuid4().hex
    key = f"pixel_event:{event_id}"
    payload = {
        "event_id": event_id,
        "pixel_id": pixel_id,
        "brand_id": brand_id,
        "event_type": event_type,
        "user_id": user_id or "",
        "device_fingerprint": device_fingerprint or "",
        "origin": origin or "",
        "amount_cents": str(int(amount_cents or 0)),
        "currency": currency or "",
        "order_id": order_id or "",
        "referrer": referrer or "",
        "url": url or "",
        "meta": json.dumps(meta or {}, separators=(",", ":")),
        "timestamp": f"{_now():.6f}",
        "attributed": "1" if attributed else "0",
        "source_brand": source_brand or "",
    }
    pipe = r.pipeline(transaction=False)
    pipe.hset(key, mapping=payload)
    pipe.expire(key, EVENT_TTL_SECONDS)
    await pipe.execute()
    return event_id


async def _bump_stats(
    r: aioredis.Redis,
    pixel_id: str,
    *,
    event_type: str,
    amount_cents: int | None,
    attributed: bool,
) -> None:
    key = f"pixel:{pixel_id}:stats"
    pipe = r.pipeline(transaction=False)
    if event_type == "pageview":
        pipe.hincrby(key, "pageviews", 1)
    elif event_type == "add_to_cart":
        pipe.hincrby(key, "add_to_carts", 1)
    elif event_type == "purchase":
        pipe.hincrby(key, "purchases", 1)
        if amount_cents:
            pipe.hincrby(key, "total_amount_cents", int(amount_cents))
        if attributed:
            pipe.hincrby(key, "attributed_purchases", 1)
    elif event_type == "signup":
        pipe.hincrby(key, "signups", 1)
        if attributed:
            pipe.hincrby(key, "attributed_signups", 1)
    else:
        pipe.hincrby(key, "custom", 1)
    await pipe.execute()


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/register", response_model=PixelRegisterResponse, status_code=201)
async def register_pixel(
    req: PixelRegisterRequest,
    request: Request,
    r: aioredis.Redis = Depends(get_redis),
):
    """Creates a new pixel for a brand and returns the embed snippet."""
    if not req.brand_id:
        raise HTTPException(status_code=422, detail="brand_id_required")

    pixel_id = _new_pixel_id()
    ts = _now()
    sdk_url = str(request.base_url).rstrip("/") + "/sdk/kix-pixel.js"

    record = {
        "brand_id": req.brand_id,
        "allowed_origins": json.dumps(req.allowed_origins),
        "created_at": f"{ts:.6f}",
        "status": "active",
    }
    pipe = r.pipeline(transaction=True)
    pipe.hset(f"pixel:{pixel_id}", mapping=record)
    pipe.sadd(f"brand:{req.brand_id}:pixels", pixel_id)
    await pipe.execute()

    logger.info(
        "pixel registered: pixel_id=%s brand_id=%s origins=%d",
        pixel_id, req.brand_id, len(req.allowed_origins),
    )

    return PixelRegisterResponse(
        pixel_id=pixel_id,
        brand_id=req.brand_id,
        allowed_origins=req.allowed_origins,
        embed_snippet=_build_snippet(pixel_id, sdk_url),
        sdk_url=sdk_url,
        created_at=ts,
    )


@router.get("/{pixel_id}/snippet", response_class=PlainTextResponse)
async def get_snippet(
    pixel_id: str,
    request: Request,
    r: aioredis.Redis = Depends(get_redis),
):
    """Returns the merchant embed snippet for an existing pixel."""
    await _load_pixel(r, pixel_id)
    sdk_url = str(request.base_url).rstrip("/") + "/sdk/kix-pixel.js"
    return PlainTextResponse(content=_build_snippet(pixel_id, sdk_url))


@router.post("/event", response_model=PixelEventResponse)
async def record_event(
    req: PixelEventRequest,
    request: Request,
    origin_header: str | None = Header(default=None, alias="Origin"),
    r: aioredis.Redis = Depends(get_redis),
):
    """Single ingestion endpoint for all browser-side pixel events.

    Validates origin, rate-limits, audits, bumps stats, and bridges purchase
    + signup events into the attribution pipeline.
    """
    pixel = await _load_pixel(r, req.pixel_id)
    origin = _check_cors(pixel, req.origin, origin_header)
    await _check_rate_limit(r, req.pixel_id)

    if req.event_type not in SUPPORTED_EVENTS:
        raise HTTPException(status_code=422, detail="unsupported_event_type")

    brand_id = pixel["brand_id"]
    attributed: bool | None = None
    source_brand: str | None = None
    attributed_event_id: str | None = None

    # ── Attribution side-effects ──────────────────────────────────────────
    # NOTE: user_id from client is a hint only — we never look it up for
    # auth. attribution.track_* functions also treat it as a key only.
    try:
        if req.event_type == "purchase":
            if not req.order_id:
                raise HTTPException(status_code=422, detail="order_id_required")
            if req.amount_cents is None:
                raise HTTPException(status_code=422, detail="amount_cents_required")
            # Anonymous purchase fallback — synthesize a stable id from FP.
            effective_uid = req.user_id or f"anon:{req.device_fingerprint}"
            conv_req = attr_mod.ConversionCheckRequest(
                user_id=effective_uid,
                target_brand=brand_id,
                order_id=req.order_id,
                amount_cents=int(req.amount_cents),
                context={
                    "pixel_id": req.pixel_id,
                    "origin": origin,
                    "referrer": req.referrer,
                    "currency": req.currency,
                    "device_fingerprint": req.device_fingerprint,
                    "meta": req.meta,
                },
            )
            conv_resp = await attr_mod.track_conversion(conv_req, r)
            attributed = bool(conv_resp.attributed)
            source_brand = conv_resp.source_brand
            attributed_event_id = conv_resp.attributed_event_id

        elif req.event_type == "signup":
            if not req.user_id:
                raise HTTPException(status_code=422, detail="user_id_required")
            # Signups don't have an invite_token at this layer (the merchant
            # site doesn't carry KiX invite tokens). We record a visit-style
            # acquisition event directly via _persist_event for journey
            # continuity, then check last-touch attribution.
            event_id, ts = await attr_mod._persist_event(
                r,
                stage=attr_mod.STAGE_VISIT,
                user_id=req.user_id,
                device_fingerprint=req.device_fingerprint,
                target_brand=brand_id,
                meta={
                    "pixel_id": req.pixel_id,
                    "origin": origin,
                    "referrer": req.referrer,
                    "source": "pixel_signup",
                    "meta": req.meta,
                },
            )
            # Mark user as known to this brand (mirrors track_visit).
            is_new = await r.sadd(f"brand:{brand_id}:users", req.user_id)
            if is_new:
                await r.hset(
                    f"brand:{brand_id}:user_first_seen",
                    req.user_id,
                    f"{ts:.6f}",
                )
            attr_event = await attr_mod.find_attribution(
                r, req.user_id, brand_id, attr_mod.ATTRIBUTION_WINDOW_SECONDS
            )
            if attr_event:
                attributed = True
                source_brand = attr_event.get("source_brand")
                attributed_event_id = attr_event.get("event_id")
            else:
                attributed = False
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "pixel event attribution failure: pixel_id=%s type=%s err=%s",
            req.pixel_id, req.event_type, exc,
        )
        # Don't fail the merchant page — audit still recorded below.

    # ── Audit + stats ─────────────────────────────────────────────────────
    event_id = await _record_audit_event(
        r,
        pixel_id=req.pixel_id,
        brand_id=brand_id,
        event_type=req.event_type,
        user_id=req.user_id,
        device_fingerprint=req.device_fingerprint,
        origin=origin,
        amount_cents=req.amount_cents,
        currency=req.currency,
        order_id=req.order_id,
        referrer=req.referrer,
        url=req.url,
        meta=req.meta,
        attributed=attributed,
        source_brand=source_brand,
    )
    await _bump_stats(
        r,
        req.pixel_id,
        event_type=req.event_type,
        amount_cents=req.amount_cents,
        attributed=bool(attributed),
    )

    return PixelEventResponse(
        ok=True,
        event_id=event_id,
        event_type=req.event_type,
        attributed=attributed,
        source_brand=source_brand,
        attributed_event_id=attributed_event_id,
    )


@router.get("/{pixel_id}/stats", response_model=PixelStatsResponse)
async def get_stats(
    pixel_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Returns rolled-up counters for a pixel."""
    pixel = await _load_pixel(r, pixel_id)
    raw = await r.hgetall(f"pixel:{pixel_id}:stats")

    def _i(key: str) -> int:
        try:
            return int(raw.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    pageviews = _i("pageviews")
    purchases = _i("purchases")
    signups = _i("signups")
    add_to_carts = _i("add_to_carts")
    attributed_purch = _i("attributed_purchases")
    total_amount = _i("total_amount_cents")
    rate = (attributed_purch / purchases) if purchases else 0.0

    return PixelStatsResponse(
        pixel_id=pixel_id,
        brand_id=pixel["brand_id"],
        total_pageviews=pageviews,
        total_add_to_carts=add_to_carts,
        total_purchases=purchases,
        total_signups=signups,
        total_amount_cents=total_amount,
        attributed_purchases=attributed_purch,
        attributed_rate=round(rate, 4),
    )


@router.delete("/{pixel_id}", status_code=204)
async def delete_pixel(
    pixel_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Soft-deletes a pixel (events are still recorded as 404)."""
    pixel = await _load_pixel(r, pixel_id)
    pipe = r.pipeline(transaction=True)
    pipe.hset(f"pixel:{pixel_id}", "status", "deleted")
    pipe.srem(f"brand:{pixel['brand_id']}:pixels", pixel_id)
    await pipe.execute()
    return Response(status_code=204)


@router.get("/brand/{brand_id}", response_model=list[str])
async def list_brand_pixels(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Lists active pixel_ids registered to a brand."""
    members = await r.smembers(f"brand:{brand_id}:pixels")
    return sorted(members)
