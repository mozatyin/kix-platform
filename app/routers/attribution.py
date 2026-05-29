"""Attribution System — KiX monetization spine.

Tracks the chain of touchpoints from invite token → impression → click →
visit → game_play → conversion, then attributes conversions to the
source brand via a 7-day last-touch window. Without this, billing is
impossible.

All state lives in Redis. Keys are brand-namespaced and event TTLs match
the attribution window to keep memory bounded.

Key schema
----------
    attr:{event_id}                    HASH   — the event record
    user:{user_id}:attr_journey        LIST   — chronological event_ids
    device:{fp}:attr_journey           LIST   — anonymous journey
    brand:{bid}:attr_incoming          ZSET   — score=ts, member=event_id
    brand:{bid}:attr_outgoing          ZSET   — score=ts, member=event_id
    invite_token:{token}               HASH   — token metadata (+EXPIRE)
    invite_token:{token}:uses          STRING — INCR counter, replay guard
    fraud:rate_limit:{uid}:{action}    STRING — INCR, EX 3600
    fraud:blacklist                    SET    — device/ip fingerprints
    fraud:geo:{user_id}                HASH   — last geo (lat/lng/ts)
    brand:{bid}:fraud_signals          LIST   — audit log (LPUSH+LTRIM)
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import secrets
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Constants ──────────────────────────────────────────────────────────────

ATTRIBUTION_WINDOW_SECONDS = 7 * 24 * 60 * 60  # 7 days
EVENT_TTL_SECONDS = ATTRIBUTION_WINDOW_SECONDS + 24 * 60 * 60  # +1 day grace
JOURNEY_MAX_LEN = 500  # cap LIST length per user/device

STAGE_IMPRESSION = "impression"
STAGE_CLICK = "click"
STAGE_INSTALL = "install"
STAGE_VISIT = "visit"
STAGE_GAME_PLAY = "game_play"
STAGE_CONVERSION = "conversion"

ATTRIBUTABLE_STAGES = {STAGE_CLICK, STAGE_IMPRESSION, STAGE_INSTALL, STAGE_VISIT}

# Anti-fraud thresholds
RATE_LIMIT_PER_HOUR = 10
INVITE_TOKEN_MAX_USES = 50           # global replay cap per token
GEO_ANOMALY_KM_PER_MIN = 15.0        # > 15 km/min → physically implausible
FRAUD_SIGNAL_LOG_MAX = 200

# Commerce defaults — until billing rules table exists
DEFAULT_COMMISSION_RATE = 0.10        # 10% of conversion to platform+source
DEFAULT_KIX_TAKE_FRACTION = 0.30      # KiX keeps 30% of commission

# Multi-touch attribution
SUPPORTED_MTA_MODELS = {
    "last_touch", "first_touch", "linear",
    "time_decay", "position_based", "data_driven",
}
TIME_DECAY_HALF_LIFE_SECONDS = 86400 * 2  # 2 days
VIEW_THROUGH_WEIGHT = 0.3                 # 30% credit for impression-only touchpoints

# Take rate ladder defaults — used when no custom ladder is configured
DEFAULT_TAKE_RATE_LADDER = [
    {"min_gmv_cents": 0,           "commission_rate": 0.10, "kix_take": 0.30, "label": "starter"},
    {"min_gmv_cents": 1_000_000,   "commission_rate": 0.08, "kix_take": 0.25, "label": "growth"},
    {"min_gmv_cents": 10_000_000,  "commission_rate": 0.06, "kix_take": 0.20, "label": "premium"},
    {"min_gmv_cents": 100_000_000, "commission_rate": 0.05, "kix_take": 0.15, "label": "enterprise"},
]
TAKE_RATE_LADDER_KEY = "attribution:take_rate_ladder"


# ── Pydantic models ────────────────────────────────────────────────────────

class TokenCreateRequest(BaseModel):
    brand_id: str
    user_id: str
    ttl_seconds: int = Field(default=ATTRIBUTION_WINDOW_SECONDS, ge=60, le=30 * 24 * 3600)
    context: dict[str, Any] = Field(default_factory=dict)


class TokenCreateResponse(BaseModel):
    invite_token: str
    share_url_suffix: str
    expires_at: float


class AttributionEventCreate(BaseModel):
    """Generic event payload for /track/* endpoints."""
    invite_token: str | None = None
    user_id: str | None = None
    device_fingerprint: str | None = None
    target_brand: str | None = None
    source_brand: str | None = None
    value_cents: int = 0
    context: dict[str, Any] = Field(default_factory=dict)
    ip_hash: str | None = None
    ua_hash: str | None = None
    geo_hint: str | None = None


class AttributionEventResponse(BaseModel):
    ok: bool
    event_id: str
    stage: str
    timestamp: float


class VisitRequest(BaseModel):
    invite_token: str
    user_id: str
    target_brand: str
    geo: dict[str, float] | None = None  # {lat, lng}
    context: dict[str, Any] = Field(default_factory=dict)


class ConversionCheckRequest(BaseModel):
    user_id: str
    target_brand: str
    order_id: str
    amount_cents: int = Field(ge=0)
    source_brand: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class ConversionCheckResponse(BaseModel):
    attributed: bool
    event_id: str
    source_brand: str | None = None
    campaign_id: str | None = None
    commission_rate: float | None = None
    commission_cents: int | None = None
    kix_take_cents: int | None = None
    source_brand_take_cents: int | None = None
    attributed_event_id: str | None = None
    window_seconds: int = ATTRIBUTION_WINDOW_SECONDS


class GenericEventRequest(BaseModel):
    user_id: str | None = None
    device_fingerprint: str | None = None
    brand_id: str
    event_type: str
    value: dict[str, Any] = Field(default_factory=dict)


class JourneyEntry(BaseModel):
    event_id: str
    stage: str
    timestamp: float
    source_brand: str | None = None
    target_brand: str | None = None
    value_cents: int = 0
    invite_token: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class JourneyResponse(BaseModel):
    user_id: str
    brand_id: str | None = None
    count: int
    entries: list[JourneyEntry]


class BrandFlowResponse(BaseModel):
    brand_id: str
    direction: Literal["incoming", "outgoing"]
    from_ts: float
    to_ts: float
    count: int
    entries: list[JourneyEntry]
    by_source: dict[str, int] = Field(default_factory=dict)
    by_campaign: dict[str, int] = Field(default_factory=dict)


class FraudCheckRequest(BaseModel):
    user_id: str | None = None
    device_fingerprint: str | None = None
    brand_id: str
    action_type: str
    source_brand: str | None = None
    target_brand: str | None = None
    invite_token: str | None = None
    geo: dict[str, float] | None = None


class FraudCheckResponse(BaseModel):
    valid: bool
    fraud_score: int  # 0–100, higher = more suspicious
    reasons: list[str]


class DeviceFingerprintRequest(BaseModel):
    screen_size: str | None = None      # e.g. "1920x1080"
    user_agent: str | None = None
    language: str | None = None
    timezone: str | None = None
    hardware_concurrency: int | None = None
    platform: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class DeviceFingerprintResponse(BaseModel):
    fingerprint: str


# ── Helpers ────────────────────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _new_event_id() -> str:
    return uuid4().hex


def _new_token() -> str:
    """URL-safe 128-bit random token."""
    return secrets.token_urlsafe(16)


def _hash_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def compute_device_fingerprint(req: DeviceFingerprintRequest) -> str:
    """Stable SHA-256 fingerprint over canonical client signal set."""
    parts = [
        req.screen_size or "",
        req.user_agent or "",
        req.language or "",
        req.timezone or "",
        str(req.hardware_concurrency or ""),
        req.platform or "",
        json.dumps(req.extra, sort_keys=True) if req.extra else "",
    ]
    canonical = "|".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (lat, lng) points, km."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _serialize_event(
    event_id: str,
    stage: str,
    *,
    user_id: str | None,
    device_fingerprint: str | None,
    source_brand: str | None,
    target_brand: str | None,
    invite_token: str | None,
    value_cents: int,
    timestamp: float,
    ip_hash: str | None,
    ua_hash: str | None,
    geo_hint: str | None,
    meta: dict[str, Any],
) -> dict[str, str]:
    return {
        "event_id": event_id,
        "stage": stage,
        "user_id": user_id or "",
        "device_fingerprint": device_fingerprint or "",
        "source_brand": source_brand or "",
        "target_brand": target_brand or "",
        "invite_token": invite_token or "",
        "value_cents": str(int(value_cents or 0)),
        "timestamp": f"{timestamp:.6f}",
        "ip_hash": ip_hash or "",
        "ua_hash": ua_hash or "",
        "geo_hint": geo_hint or "",
        "meta": json.dumps(meta or {}, separators=(",", ":")),
    }


def _deserialize_event(raw: dict[str, str]) -> JourneyEntry | None:
    if not raw:
        return None
    try:
        meta = json.loads(raw.get("meta") or "{}")
    except json.JSONDecodeError:
        meta = {}
    return JourneyEntry(
        event_id=raw.get("event_id", ""),
        stage=raw.get("stage", ""),
        timestamp=float(raw.get("timestamp", 0) or 0),
        source_brand=raw.get("source_brand") or None,
        target_brand=raw.get("target_brand") or None,
        value_cents=int(raw.get("value_cents", 0) or 0),
        invite_token=raw.get("invite_token") or None,
        meta=meta,
    )


async def _load_invite_token(r: aioredis.Redis, token: str) -> dict[str, Any] | None:
    raw = await r.hgetall(f"invite_token:{token}")
    if not raw:
        return None
    out: dict[str, Any] = dict(raw)
    if "context" in out:
        try:
            out["context"] = json.loads(out["context"])
        except json.JSONDecodeError:
            out["context"] = {}
    return out


async def _persist_event(
    r: aioredis.Redis,
    *,
    stage: str,
    user_id: str | None = None,
    device_fingerprint: str | None = None,
    source_brand: str | None = None,
    target_brand: str | None = None,
    invite_token: str | None = None,
    value_cents: int = 0,
    ip_hash: str | None = None,
    ua_hash: str | None = None,
    geo_hint: str | None = None,
    meta: dict[str, Any] | None = None,
) -> tuple[str, float]:
    """Write an event hash + journey/brand indices atomically."""
    event_id = _new_event_id()
    ts = _now()
    payload = _serialize_event(
        event_id,
        stage,
        user_id=user_id,
        device_fingerprint=device_fingerprint,
        source_brand=source_brand,
        target_brand=target_brand,
        invite_token=invite_token,
        value_cents=value_cents,
        timestamp=ts,
        ip_hash=ip_hash,
        ua_hash=ua_hash,
        geo_hint=geo_hint,
        meta=meta or {},
    )

    pipe = r.pipeline(transaction=True)
    pipe.hset(f"attr:{event_id}", mapping=payload)
    pipe.expire(f"attr:{event_id}", EVENT_TTL_SECONDS)

    if user_id:
        pipe.lpush(f"user:{user_id}:attr_journey", event_id)
        pipe.ltrim(f"user:{user_id}:attr_journey", 0, JOURNEY_MAX_LEN - 1)
        pipe.expire(f"user:{user_id}:attr_journey", EVENT_TTL_SECONDS)

    if device_fingerprint:
        pipe.lpush(f"device:{device_fingerprint}:attr_journey", event_id)
        pipe.ltrim(f"device:{device_fingerprint}:attr_journey", 0, JOURNEY_MAX_LEN - 1)
        pipe.expire(f"device:{device_fingerprint}:attr_journey", EVENT_TTL_SECONDS)

    if target_brand:
        pipe.zadd(f"brand:{target_brand}:attr_incoming", {event_id: ts})
    if source_brand:
        pipe.zadd(f"brand:{source_brand}:attr_outgoing", {event_id: ts})

    await pipe.execute()
    return event_id, ts


async def find_attribution(
    r: aioredis.Redis,
    user_id: str,
    target_brand: str,
    window_seconds: int = ATTRIBUTION_WINDOW_SECONDS,
    *,
    device_fingerprint: str | None = None,
) -> dict[str, str] | None:
    """Walk journey backwards, return most-recent attributable event.

    Last-touch model: most recent click > impression > install > visit
    where source_brand is set and != target_brand and within window.
    Falls back to device journey if user has none.
    """
    now = _now()
    journeys: list[str] = []
    if user_id:
        journeys.append(f"user:{user_id}:attr_journey")
    if device_fingerprint:
        journeys.append(f"device:{device_fingerprint}:attr_journey")

    seen: set[str] = set()
    for jkey in journeys:
        event_ids = await r.lrange(jkey, 0, 200)
        for event_id in event_ids:
            if event_id in seen:
                continue
            seen.add(event_id)
            event = await r.hgetall(f"attr:{event_id}")
            if not event:
                continue
            try:
                ts = float(event.get("timestamp", 0) or 0)
            except ValueError:
                continue
            if now - ts > window_seconds:
                # journey is reverse-chrono; everything after is older
                break
            if event.get("stage") not in ATTRIBUTABLE_STAGES:
                continue
            src = event.get("source_brand") or ""
            if not src or src == target_brand:
                continue
            return event
    return None


async def _log_fraud_signal(
    r: aioredis.Redis,
    brand_id: str,
    user_or_device: str,
    reasons: list[str],
    fraud_score: int,
) -> None:
    if not reasons:
        return
    payload = json.dumps(
        {
            "ts": _now(),
            "subject": user_or_device,
            "score": fraud_score,
            "reasons": reasons,
        },
        separators=(",", ":"),
    )
    key = f"brand:{brand_id}:fraud_signals"
    pipe = r.pipeline(transaction=True)
    pipe.lpush(key, payload)
    pipe.ltrim(key, 0, FRAUD_SIGNAL_LOG_MAX - 1)
    await pipe.execute()


async def _run_fraud_checks(
    r: aioredis.Redis,
    *,
    user_id: str | None,
    device_fingerprint: str | None,
    brand_id: str,
    action_type: str,
    source_brand: str | None = None,
    target_brand: str | None = None,
    invite_token: str | None = None,
    geo: dict[str, float] | None = None,
) -> tuple[int, list[str]]:
    """Return (fraud_score, reasons). Score 0–100."""
    score = 0
    reasons: list[str] = []
    subject = user_id or device_fingerprint or "anonymous"

    # 1. rate limit
    if user_id:
        rl_key = f"fraud:rate_limit:{user_id}:{action_type}"
        count = await r.incr(rl_key)
        if count == 1:
            await r.expire(rl_key, 3600)
        if count > RATE_LIMIT_PER_HOUR:
            score += min(50, 5 * (count - RATE_LIMIT_PER_HOUR))
            reasons.append(f"rate_limit:{count}/hr")

    # 2. self-attribution (source == target)
    if source_brand and target_brand and source_brand == target_brand:
        score += 100
        reasons.append("self_attribution")

    # 3. blacklist
    if device_fingerprint:
        if await r.sismember("fraud:blacklist", device_fingerprint):
            score += 100
            reasons.append("device_blacklisted")
    if user_id:
        if await r.sismember("fraud:blacklist", user_id):
            score += 100
            reasons.append("user_blacklisted")

    # 4. invite_token replay
    if invite_token:
        uses_key = f"invite_token:{invite_token}:uses"
        uses = await r.incr(uses_key)
        if uses == 1:
            await r.expire(uses_key, EVENT_TTL_SECONDS)
        if uses > INVITE_TOKEN_MAX_USES:
            score += 80
            reasons.append(f"token_replay:{uses}")

    # 5. geo anomaly (speed of travel)
    if user_id and geo and "lat" in geo and "lng" in geo:
        geo_key = f"fraud:geo:{user_id}"
        prev = await r.hgetall(geo_key)
        now = _now()
        if prev and "lat" in prev and "lng" in prev and "ts" in prev:
            try:
                prev_lat = float(prev["lat"])
                prev_lng = float(prev["lng"])
                prev_ts = float(prev["ts"])
                dist_km = _haversine_km((prev_lat, prev_lng), (geo["lat"], geo["lng"]))
                dt_min = max((now - prev_ts) / 60.0, 1e-6)
                speed = dist_km / dt_min
                if speed > GEO_ANOMALY_KM_PER_MIN:
                    score += 70
                    reasons.append(f"geo_anomaly:{speed:.1f}km/min")
            except (ValueError, TypeError):
                pass
        await r.hset(
            geo_key,
            mapping={"lat": str(geo["lat"]), "lng": str(geo["lng"]), "ts": str(now)},
        )
        await r.expire(geo_key, EVENT_TTL_SECONDS)

    score = min(score, 100)
    if reasons:
        await _log_fraud_signal(r, brand_id, subject, reasons, score)
    return score, reasons


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/token/create", response_model=TokenCreateResponse)
async def create_invite_token(
    req: TokenCreateRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Mint a fresh invite_token bound to (brand_id, user_id).

    Stored as a HASH with EXPIRE = ttl_seconds. The token doubles as the
    URL slug suffix; clients build the share URL themselves.
    """
    token = _new_token()
    now = _now()
    expires_at = now + req.ttl_seconds
    mapping = {
        "brand_id": req.brand_id,
        "user_id": req.user_id,
        "created_at": f"{now:.6f}",
        "expires_at": f"{expires_at:.6f}",
        "context": json.dumps(req.context or {}, separators=(",", ":")),
    }
    pipe = r.pipeline(transaction=True)
    pipe.hset(f"invite_token:{token}", mapping=mapping)
    pipe.expire(f"invite_token:{token}", req.ttl_seconds)
    await pipe.execute()

    return TokenCreateResponse(
        invite_token=token,
        share_url_suffix=f"?ref={token}",
        expires_at=expires_at,
    )


async def _resolve_invite(
    r: aioredis.Redis, token: str | None
) -> tuple[str | None, str | None, str | None]:
    """Return (source_brand, source_user_id, campaign_id) from token."""
    if not token:
        return None, None, None
    data = await _load_invite_token(r, token)
    if not data:
        return None, None, None
    ctx = data.get("context") or {}
    if not isinstance(ctx, dict):
        ctx = {}
    return (
        data.get("brand_id"),
        data.get("user_id"),
        ctx.get("campaign_id"),
    )


@router.post("/track/impression", response_model=AttributionEventResponse)
async def track_impression(
    req: AttributionEventCreate,
    r: aioredis.Redis = Depends(get_redis),
):
    if not (req.user_id or req.device_fingerprint):
        raise HTTPException(status_code=400, detail="user_id or device_fingerprint required")
    if not req.target_brand:
        raise HTTPException(status_code=400, detail="target_brand required")

    source_brand, source_user_id, campaign_id = await _resolve_invite(r, req.invite_token)
    meta = dict(req.context or {})
    if source_user_id:
        meta.setdefault("source_user_id", source_user_id)
    if campaign_id:
        meta.setdefault("campaign_id", campaign_id)

    event_id, ts = await _persist_event(
        r,
        stage=STAGE_IMPRESSION,
        user_id=req.user_id,
        device_fingerprint=req.device_fingerprint,
        source_brand=source_brand or req.source_brand,
        target_brand=req.target_brand,
        invite_token=req.invite_token,
        value_cents=0,
        ip_hash=req.ip_hash,
        ua_hash=req.ua_hash,
        geo_hint=req.geo_hint,
        meta=meta,
    )
    return AttributionEventResponse(
        ok=True, event_id=event_id, stage=STAGE_IMPRESSION, timestamp=ts
    )


@router.post("/track/click", response_model=AttributionEventResponse)
async def track_click(
    req: AttributionEventCreate,
    r: aioredis.Redis = Depends(get_redis),
):
    if not req.device_fingerprint and not req.user_id:
        raise HTTPException(status_code=400, detail="device_fingerprint or user_id required")
    if not req.target_brand:
        raise HTTPException(status_code=400, detail="target_brand required")

    source_brand, source_user_id, campaign_id = await _resolve_invite(r, req.invite_token)
    meta = dict(req.context or {})
    if source_user_id:
        meta.setdefault("source_user_id", source_user_id)
    if campaign_id:
        meta.setdefault("campaign_id", campaign_id)

    event_id, ts = await _persist_event(
        r,
        stage=STAGE_CLICK,
        user_id=req.user_id,
        device_fingerprint=req.device_fingerprint,
        source_brand=source_brand or req.source_brand,
        target_brand=req.target_brand,
        invite_token=req.invite_token,
        ip_hash=req.ip_hash,
        ua_hash=req.ua_hash,
        geo_hint=req.geo_hint,
        meta=meta,
    )

    # passive fraud signal — replay/rate
    await _run_fraud_checks(
        r,
        user_id=req.user_id,
        device_fingerprint=req.device_fingerprint,
        brand_id=req.target_brand,
        action_type="click",
        source_brand=source_brand or req.source_brand,
        target_brand=req.target_brand,
        invite_token=req.invite_token,
    )

    return AttributionEventResponse(
        ok=True, event_id=event_id, stage=STAGE_CLICK, timestamp=ts
    )


@router.post("/track/visit", response_model=AttributionEventResponse)
async def track_visit(
    req: VisitRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Records the first physical or digital visit to the target brand.

    Side-effect: registers user under brand's known-user set, marking
    the user as 'arrived' so future events can be cleanly attributed.
    """
    source_brand, source_user_id, campaign_id = await _resolve_invite(r, req.invite_token)
    meta = dict(req.context or {})
    if source_user_id:
        meta.setdefault("source_user_id", source_user_id)
    if campaign_id:
        meta.setdefault("campaign_id", campaign_id)
    if req.geo:
        meta["geo"] = req.geo

    geo_hint = None
    if req.geo and "lat" in req.geo and "lng" in req.geo:
        geo_hint = f"{req.geo['lat']:.4f},{req.geo['lng']:.4f}"

    event_id, ts = await _persist_event(
        r,
        stage=STAGE_VISIT,
        user_id=req.user_id,
        source_brand=source_brand,
        target_brand=req.target_brand,
        invite_token=req.invite_token,
        geo_hint=geo_hint,
        meta=meta,
    )

    # Mark user as 'known' to this brand (first-visit acquisition record).
    is_new = await r.sadd(f"brand:{req.target_brand}:users", req.user_id)
    if is_new:
        await r.hset(
            f"brand:{req.target_brand}:user_first_seen",
            req.user_id,
            f"{ts:.6f}",
        )

    # Passive geo anomaly check
    await _run_fraud_checks(
        r,
        user_id=req.user_id,
        device_fingerprint=None,
        brand_id=req.target_brand,
        action_type="visit",
        source_brand=source_brand,
        target_brand=req.target_brand,
        invite_token=req.invite_token,
        geo=req.geo,
    )

    return AttributionEventResponse(
        ok=True, event_id=event_id, stage=STAGE_VISIT, timestamp=ts
    )


@router.post("/track/conversion", response_model=ConversionCheckResponse)
async def track_conversion(
    req: ConversionCheckRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Records the conversion and resolves last-touch attribution.

    Returns commission split if a valid source_brand was found in the
    7-day attribution window. If req.source_brand is provided, it acts
    as a forced override (for closed-loop reconciliation feeds).
    """
    # Idempotency: same order_id + target_brand → reuse previously stored.
    idem_key = f"attr:order:{req.target_brand}:{req.order_id}"
    existing = await r.get(idem_key)
    if existing:
        cached = await r.hgetall(f"attr:{existing}")
        if cached:
            return _conversion_from_event(cached, req)

    # Forced override path
    attributed_event: dict[str, str] | None = None
    if req.source_brand and req.source_brand != req.target_brand:
        attributed_event = {
            "source_brand": req.source_brand,
            "event_id": "forced",
            "timestamp": str(_now()),
        }
    else:
        attributed_event = await find_attribution(
            r, req.user_id, req.target_brand, ATTRIBUTION_WINDOW_SECONDS
        )

    # Reject obvious fraud
    if attributed_event:
        source_brand = attributed_event.get("source_brand") or ""
        if source_brand == req.target_brand:
            attributed_event = None

    meta = dict(req.context or {})
    meta["order_id"] = req.order_id
    if attributed_event:
        try:
            meta["attributed_event_id"] = attributed_event.get("event_id")
            try:
                meta["attributed_meta"] = json.loads(attributed_event.get("meta", "{}"))
            except json.JSONDecodeError:
                meta["attributed_meta"] = {}
        except Exception:
            pass

    event_id, ts = await _persist_event(
        r,
        stage=STAGE_CONVERSION,
        user_id=req.user_id,
        source_brand=(attributed_event or {}).get("source_brand") or None,
        target_brand=req.target_brand,
        value_cents=req.amount_cents,
        meta=meta,
    )

    # Idempotency record — tie order_id to event_id for replay protection.
    await r.set(idem_key, event_id, ex=EVENT_TTL_SECONDS)

    if not attributed_event:
        return ConversionCheckResponse(
            attributed=False,
            event_id=event_id,
        )

    src = attributed_event.get("source_brand")
    # campaign_id pulled from attributed event meta
    campaign_id: str | None = None
    try:
        attr_meta = json.loads(attributed_event.get("meta", "{}") or "{}")
        if isinstance(attr_meta, dict):
            campaign_id = attr_meta.get("campaign_id")
    except json.JSONDecodeError:
        campaign_id = None

    # Take-rate ladder: brand-tier-based commission, falls back to defaults.
    tier = await _pick_take_rate_tier(r, src)
    commission_rate = tier["commission_rate"]
    kix_fraction = tier["kix_take"]
    commission_cents = int(round(req.amount_cents * commission_rate))
    kix_take_cents = int(round(commission_cents * kix_fraction))
    source_brand_take_cents = commission_cents - kix_take_cents

    # Roll up per-brand commission accounting (audit trail).
    pipe = r.pipeline(transaction=True)
    pipe.hincrby(f"brand:{src}:commission_owed", "cents", source_brand_take_cents)
    pipe.hincrby("kix:commission_collected", "cents", kix_take_cents)
    pipe.hincrby(f"brand:{req.target_brand}:commission_paid", "cents", commission_cents)
    # Lifetime GMV is keyed off the *source* brand — that's what the ladder
    # tiers against (acquisition-driver volume).
    pipe.incrby(f"brand:{src}:gmv_lifetime", req.amount_cents)
    await pipe.execute()

    return ConversionCheckResponse(
        attributed=True,
        event_id=event_id,
        source_brand=src,
        campaign_id=campaign_id,
        commission_rate=commission_rate,
        commission_cents=commission_cents,
        kix_take_cents=kix_take_cents,
        source_brand_take_cents=source_brand_take_cents,
        attributed_event_id=attributed_event.get("event_id"),
    )


def _conversion_from_event(
    cached: dict[str, str], req: ConversionCheckRequest
) -> ConversionCheckResponse:
    """Re-derive ConversionCheckResponse from a previously stored conversion."""
    src = cached.get("source_brand") or None
    amount = int(cached.get("value_cents", 0) or 0)
    if not src:
        return ConversionCheckResponse(
            attributed=False, event_id=cached.get("event_id", "")
        )
    commission_rate = DEFAULT_COMMISSION_RATE
    commission_cents = int(round(amount * commission_rate))
    kix_take_cents = int(round(commission_cents * DEFAULT_KIX_TAKE_FRACTION))
    return ConversionCheckResponse(
        attributed=True,
        event_id=cached.get("event_id", ""),
        source_brand=src,
        commission_rate=commission_rate,
        commission_cents=commission_cents,
        kix_take_cents=kix_take_cents,
        source_brand_take_cents=commission_cents - kix_take_cents,
    )


@router.post("/track/event", response_model=AttributionEventResponse)
async def track_generic_event(
    req: GenericEventRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Generic SDK event — game_play, level_up, share, etc."""
    if not (req.user_id or req.device_fingerprint):
        raise HTTPException(status_code=400, detail="user_id or device_fingerprint required")
    if not req.brand_id:
        raise HTTPException(status_code=400, detail="brand_id required")

    meta = {"event_type": req.event_type, "value": req.value}
    event_id, ts = await _persist_event(
        r,
        stage=req.event_type if req.event_type in (STAGE_GAME_PLAY, STAGE_INSTALL) else STAGE_GAME_PLAY,
        user_id=req.user_id,
        device_fingerprint=req.device_fingerprint,
        target_brand=req.brand_id,
        meta=meta,
    )
    return AttributionEventResponse(
        ok=True, event_id=event_id, stage=req.event_type, timestamp=ts
    )


@router.get("/user/{user_id}/journey", response_model=JourneyResponse)
async def user_journey(
    user_id: str,
    brand_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    r: aioredis.Redis = Depends(get_redis),
):
    """Reverse-chronological journey for a user, optionally filtered to one brand."""
    event_ids = await r.lrange(f"user:{user_id}:attr_journey", 0, limit - 1)
    entries: list[JourneyEntry] = []
    for eid in event_ids:
        raw = await r.hgetall(f"attr:{eid}")
        entry = _deserialize_event(raw)
        if not entry:
            continue
        if brand_id and entry.target_brand != brand_id and entry.source_brand != brand_id:
            continue
        entries.append(entry)
    return JourneyResponse(
        user_id=user_id,
        brand_id=brand_id,
        count=len(entries),
        entries=entries,
    )


async def _brand_flow(
    r: aioredis.Redis,
    brand_id: str,
    direction: Literal["incoming", "outgoing"],
    from_ts: float,
    to_ts: float,
    campaign_id: str | None,
    limit: int,
) -> BrandFlowResponse:
    key = f"brand:{brand_id}:attr_{direction}"
    event_ids = await r.zrevrangebyscore(key, to_ts, from_ts, start=0, num=limit)
    entries: list[JourneyEntry] = []
    by_source: dict[str, int] = {}
    by_campaign: dict[str, int] = {}
    for eid in event_ids:
        raw = await r.hgetall(f"attr:{eid}")
        entry = _deserialize_event(raw)
        if not entry:
            continue
        if campaign_id:
            cid = (entry.meta or {}).get("campaign_id")
            if cid != campaign_id:
                continue
        entries.append(entry)
        if entry.source_brand:
            by_source[entry.source_brand] = by_source.get(entry.source_brand, 0) + 1
        cid = (entry.meta or {}).get("campaign_id")
        if cid:
            by_campaign[cid] = by_campaign.get(cid, 0) + 1
    return BrandFlowResponse(
        brand_id=brand_id,
        direction=direction,
        from_ts=from_ts,
        to_ts=to_ts,
        count=len(entries),
        entries=entries,
        by_source=by_source,
        by_campaign=by_campaign,
    )


@router.get("/brand/{brand_id}/incoming", response_model=BrandFlowResponse)
async def brand_incoming(
    brand_id: str,
    from_ts: float | None = Query(default=None, alias="from"),
    to_ts: float | None = Query(default=None, alias="to"),
    campaign_id: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    r: aioredis.Redis = Depends(get_redis),
):
    """Events where this brand was the target."""
    now = _now()
    return await _brand_flow(
        r,
        brand_id,
        "incoming",
        from_ts if from_ts is not None else now - ATTRIBUTION_WINDOW_SECONDS,
        to_ts if to_ts is not None else now,
        campaign_id,
        limit,
    )


@router.get("/brand/{brand_id}/outgoing", response_model=BrandFlowResponse)
async def brand_outgoing(
    brand_id: str,
    from_ts: float | None = Query(default=None, alias="from"),
    to_ts: float | None = Query(default=None, alias="to"),
    campaign_id: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    r: aioredis.Redis = Depends(get_redis),
):
    """Events where this brand drove the user (source_brand=this)."""
    now = _now()
    return await _brand_flow(
        r,
        brand_id,
        "outgoing",
        from_ts if from_ts is not None else now - ATTRIBUTION_WINDOW_SECONDS,
        to_ts if to_ts is not None else now,
        campaign_id,
        limit,
    )


@router.post("/anti-fraud/check", response_model=FraudCheckResponse)
async def fraud_check(
    req: FraudCheckRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    score, reasons = await _run_fraud_checks(
        r,
        user_id=req.user_id,
        device_fingerprint=req.device_fingerprint,
        brand_id=req.brand_id,
        action_type=req.action_type,
        source_brand=req.source_brand,
        target_brand=req.target_brand,
        invite_token=req.invite_token,
        geo=req.geo,
    )
    return FraudCheckResponse(
        valid=score < 70,
        fraud_score=score,
        reasons=reasons,
    )


@router.post("/anti-fraud/blacklist")
async def fraud_blacklist(
    fingerprint: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Add a device fingerprint / user_id to the blacklist."""
    await r.sadd("fraud:blacklist", fingerprint)
    return {"ok": True, "blacklisted": fingerprint}


@router.post("/device/fingerprint", response_model=DeviceFingerprintResponse)
async def device_fingerprint(req: DeviceFingerprintRequest):
    """Compute a stable SHA-256 device fingerprint."""
    fp = compute_device_fingerprint(req)
    return DeviceFingerprintResponse(fingerprint=fp)


@router.get("/health")
async def attribution_health(r: aioredis.Redis = Depends(get_redis)):
    pong = await r.ping()
    return {
        "ok": bool(pong),
        "window_seconds": ATTRIBUTION_WINDOW_SECONDS,
        "default_commission_rate": DEFAULT_COMMISSION_RATE,
        "default_kix_take_fraction": DEFAULT_KIX_TAKE_FRACTION,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Multi-Touch Attribution
# ═══════════════════════════════════════════════════════════════════════════

class MultiTouchConversionRequest(BaseModel):
    user_id: str
    target_brand: str
    order_id: str
    amount_cents: int = Field(ge=0)
    model: Literal[
        "linear", "time_decay", "position_based",
        "first_touch", "last_touch", "data_driven",
    ] = "linear"
    window_seconds: int = ATTRIBUTION_WINDOW_SECONDS
    context: dict[str, Any] = Field(default_factory=dict)


class MultiTouchSplit(BaseModel):
    source_brand: str
    touchpoint_id: str
    stage: str
    weight: float
    view_through: bool = False
    kix_take_cents: int
    source_brand_take_cents: int
    commission_cents: int


class MultiTouchConversionResponse(BaseModel):
    attributed: bool
    event_id: str
    model: str
    splits: list[MultiTouchSplit]
    total_kix_take_cents: int
    total_commission_cents: int


def compute_weights(tps: list[dict[str, Any]], model: str) -> list[float]:
    """Pure weight computation. Inputs assumed chronological (first → last)."""
    n = len(tps)
    if n == 0:
        return []
    if model == "last_touch":
        return [0.0] * (n - 1) + [1.0]
    if model == "first_touch":
        return [1.0] + [0.0] * (n - 1)
    if model == "linear":
        return [1.0 / n] * n
    if model == "position_based":
        if n == 1:
            return [1.0]
        if n == 2:
            return [0.5, 0.5]
        middle = (n - 2)
        return [0.4] + [0.2 / middle] * middle + [0.4]
    if model == "time_decay":
        now = time.time()
        raw = [
            math.exp(-(now - float(t.get("timestamp", now) or now)) / TIME_DECAY_HALF_LIFE_SECONDS)
            for t in tps
        ]
        s = sum(raw) or 1.0
        return [w / s for w in raw]
    if model == "data_driven":
        # Shapley-ish: each touchpoint's weight ∝ a static stage lift prior,
        # boosted by historical conversion lift for its source_brand if known.
        stage_prior = {
            STAGE_CLICK: 1.0,
            STAGE_VISIT: 0.7,
            STAGE_INSTALL: 0.6,
            STAGE_IMPRESSION: 0.3,
        }
        raw: list[float] = []
        for t in tps:
            base = stage_prior.get(t.get("stage", ""), 0.5)
            lift = float(t.get("_dd_lift", 1.0))
            raw.append(base * lift)
        s = sum(raw) or 1.0
        return [w / s for w in raw]
    # default fall-through: linear
    return [1.0 / n] * n


async def _enrich_with_data_driven_lift(
    r: aioredis.Redis, tps: list[dict[str, Any]]
) -> None:
    """For 'data_driven' model: lookup per-source lift = conv/exposure ratio."""
    for t in tps:
        src = t.get("source_brand")
        if not src:
            t["_dd_lift"] = 1.0
            continue
        gmv = await r.get(f"brand:{src}:gmv_lifetime")
        # crude proxy: log scale of lifetime GMV (more historical lift → higher weight).
        try:
            gmv_v = int(gmv) if gmv else 0
        except (TypeError, ValueError):
            gmv_v = 0
        t["_dd_lift"] = 1.0 + math.log1p(gmv_v) / 20.0


def _is_view_through(tp: dict[str, Any]) -> bool:
    """Impression-only touchpoint (never followed by a click for that source)."""
    return tp.get("stage") == STAGE_IMPRESSION


async def attribute_multitouch(
    user_id: str,
    target_brand: str,
    model: str,
    r: aioredis.Redis,
    window: int = ATTRIBUTION_WINDOW_SECONDS,
) -> list[tuple[dict[str, Any], float]] | None:
    """Collect attributable touchpoints, compute weights, return zipped list.

    Returns None if no valid touchpoints exist in the window.
    """
    journey = await r.lrange(f"user:{user_id}:attr_journey", 0, 200)
    touchpoints: list[dict[str, Any]] = []
    now = _now()

    # Walk reverse-chrono; stop at first out-of-window event.
    click_sources: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for eid in journey:
        e = await r.hgetall(f"attr:{eid}")
        if not e:
            continue
        try:
            ts = float(e.get("timestamp", 0) or 0)
        except (TypeError, ValueError):
            continue
        if now - ts > window:
            break
        if e.get("stage") not in ATTRIBUTABLE_STAGES:
            continue
        src = e.get("source_brand") or ""
        if not src or src == target_brand:
            continue
        candidates.append(e)
        if e.get("stage") == STAGE_CLICK:
            click_sources.add(src)

    if not candidates:
        return None

    # Flag view-through: an impression for a source that never produced a click.
    for c in candidates:
        c["_view_through"] = (
            c.get("stage") == STAGE_IMPRESSION
            and (c.get("source_brand") or "") not in click_sources
        )

    # Chronological order: first → last.
    candidates.reverse()

    if model == "data_driven":
        await _enrich_with_data_driven_lift(r, candidates)

    weights = compute_weights(candidates, model)

    # Apply view-through downweighting *after* normalization, then renormalize.
    adjusted: list[float] = []
    for tp, w in zip(candidates, weights):
        if tp.get("_view_through"):
            adjusted.append(w * VIEW_THROUGH_WEIGHT)
        else:
            adjusted.append(w)
    s = sum(adjusted)
    if s > 0:
        adjusted = [a / s for a in adjusted]
    else:
        adjusted = weights

    return list(zip(candidates, adjusted))


@router.post(
    "/track/conversion-multi",
    response_model=MultiTouchConversionResponse,
)
async def track_conversion_multi(
    req: MultiTouchConversionRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Records a conversion and splits credit across multiple touchpoints.

    Does NOT replace /track/conversion — runs in parallel. Idempotency key
    is namespaced to the model so the same order may have a last-touch
    *and* a linear record without colliding.
    """
    if req.model not in SUPPORTED_MTA_MODELS:
        raise HTTPException(status_code=400, detail=f"unsupported model: {req.model}")

    idem_key = f"attr:order_mta:{req.target_brand}:{req.model}:{req.order_id}"
    existing = await r.get(idem_key)
    if existing:
        cached_payload = await r.get(f"attr:mta_result:{existing}")
        if cached_payload:
            try:
                return MultiTouchConversionResponse(**json.loads(cached_payload))
            except (json.JSONDecodeError, TypeError):
                pass

    attributed = await attribute_multitouch(
        req.user_id, req.target_brand, req.model, r, req.window_seconds
    )

    meta = dict(req.context or {})
    meta["order_id"] = req.order_id
    meta["mta_model"] = req.model

    event_id, _ts = await _persist_event(
        r,
        stage=STAGE_CONVERSION,
        user_id=req.user_id,
        target_brand=req.target_brand,
        value_cents=req.amount_cents,
        meta=meta,
    )

    if not attributed:
        resp = MultiTouchConversionResponse(
            attributed=False,
            event_id=event_id,
            model=req.model,
            splits=[],
            total_kix_take_cents=0,
            total_commission_cents=0,
        )
        await r.set(idem_key, event_id, ex=EVENT_TTL_SECONDS)
        await r.set(f"attr:mta_result:{event_id}", resp.json(), ex=EVENT_TTL_SECONDS)
        return resp

    splits: list[MultiTouchSplit] = []
    total_kix = 0
    total_commission = 0
    pipe = r.pipeline(transaction=True)
    for tp, weight in attributed:
        src = tp.get("source_brand") or ""
        if not src or weight <= 0:
            continue
        tier = await _pick_take_rate_tier(r, src)
        share_amount = int(round(req.amount_cents * weight))
        commission = int(round(share_amount * tier["commission_rate"]))
        kix_cents = int(round(commission * tier["kix_take"]))
        src_cents = commission - kix_cents

        splits.append(MultiTouchSplit(
            source_brand=src,
            touchpoint_id=tp.get("event_id", ""),
            stage=tp.get("stage", ""),
            weight=round(float(weight), 6),
            view_through=bool(tp.get("_view_through")),
            commission_cents=commission,
            kix_take_cents=kix_cents,
            source_brand_take_cents=src_cents,
        ))
        total_kix += kix_cents
        total_commission += commission

        pipe.hincrby(f"brand:{src}:commission_owed", "cents", src_cents)
        pipe.hincrby(f"brand:{src}:gmv_lifetime", 0)  # touch key for tiering
        pipe.incrby(f"brand:{src}:gmv_lifetime", share_amount)
        if tp.get("_view_through"):
            pipe.hincrby(f"brand:{src}:view_through_conversions", "count", 1)
        else:
            pipe.hincrby(f"brand:{src}:click_conversions", "count", 1)

    pipe.hincrby("kix:commission_collected", "cents", total_kix)
    pipe.hincrby(f"brand:{req.target_brand}:commission_paid", "cents", total_commission)
    await pipe.execute()

    resp = MultiTouchConversionResponse(
        attributed=True,
        event_id=event_id,
        model=req.model,
        splits=splits,
        total_kix_take_cents=total_kix,
        total_commission_cents=total_commission,
    )
    await r.set(idem_key, event_id, ex=EVENT_TTL_SECONDS)
    await r.set(f"attr:mta_result:{event_id}", resp.json(), ex=EVENT_TTL_SECONDS)
    return resp


# ═══════════════════════════════════════════════════════════════════════════
# Take Rate Ladder
# ═══════════════════════════════════════════════════════════════════════════

class TakeRateTier(BaseModel):
    min_gmv_cents: int = Field(ge=0)
    commission_rate: float = Field(ge=0.0, le=1.0)
    kix_take: float = Field(ge=0.0, le=1.0)
    label: str


class TakeRateConfigureRequest(BaseModel):
    tiers: list[TakeRateTier]


class TakeRateConfigureResponse(BaseModel):
    ok: bool
    tiers: list[TakeRateTier]


class BrandTakeRateResponse(BaseModel):
    brand_id: str
    gmv_lifetime_cents: int
    current_tier: TakeRateTier
    next_tier: TakeRateTier | None = None
    gmv_to_next_tier_cents: int | None = None


async def _get_take_rate_ladder(r: aioredis.Redis) -> list[dict[str, Any]]:
    """Return the configured ladder (sorted by min_gmv asc), or defaults."""
    raw = await r.lrange(TAKE_RATE_LADDER_KEY, 0, -1)
    if not raw:
        return list(DEFAULT_TAKE_RATE_LADDER)
    tiers: list[dict[str, Any]] = []
    for item in raw:
        try:
            tiers.append(json.loads(item))
        except json.JSONDecodeError:
            continue
    if not tiers:
        return list(DEFAULT_TAKE_RATE_LADDER)
    tiers.sort(key=lambda t: int(t.get("min_gmv_cents", 0)))
    return tiers


async def _pick_take_rate_tier(
    r: aioredis.Redis, brand_id: str | None
) -> dict[str, Any]:
    """Pick the tier for this brand based on lifetime GMV."""
    if not brand_id:
        return {
            "min_gmv_cents": 0,
            "commission_rate": DEFAULT_COMMISSION_RATE,
            "kix_take": DEFAULT_KIX_TAKE_FRACTION,
            "label": "default",
        }
    tiers = await _get_take_rate_ladder(r)
    gmv_raw = await r.get(f"brand:{brand_id}:gmv_lifetime")
    try:
        gmv = int(gmv_raw) if gmv_raw else 0
    except (TypeError, ValueError):
        gmv = 0
    chosen = tiers[0]
    for tier in tiers:
        if gmv >= int(tier.get("min_gmv_cents", 0)):
            chosen = tier
        else:
            break
    return chosen


@router.post(
    "/admin/take-rate/configure",
    response_model=TakeRateConfigureResponse,
)
async def configure_take_rate(
    req: TakeRateConfigureRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Set the global brand-tier take-rate ladder.

    Replaces any existing ladder. Tiers are stored sorted by min_gmv_cents.
    """
    if not req.tiers:
        raise HTTPException(status_code=400, detail="at least one tier required")
    sorted_tiers = sorted(req.tiers, key=lambda t: t.min_gmv_cents)
    pipe = r.pipeline(transaction=True)
    pipe.delete(TAKE_RATE_LADDER_KEY)
    for tier in sorted_tiers:
        pipe.rpush(TAKE_RATE_LADDER_KEY, json.dumps(tier.dict(), separators=(",", ":")))
    await pipe.execute()
    return TakeRateConfigureResponse(ok=True, tiers=sorted_tiers)


@router.get("/admin/take-rate", response_model=TakeRateConfigureResponse)
async def get_take_rate(r: aioredis.Redis = Depends(get_redis)):
    tiers = await _get_take_rate_ladder(r)
    return TakeRateConfigureResponse(
        ok=True,
        tiers=[TakeRateTier(**t) for t in tiers],
    )


@router.get(
    "/brand/{brand_id}/take-rate-tier",
    response_model=BrandTakeRateResponse,
)
async def brand_take_rate_tier(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    tiers = await _get_take_rate_ladder(r)
    gmv_raw = await r.get(f"brand:{brand_id}:gmv_lifetime")
    try:
        gmv = int(gmv_raw) if gmv_raw else 0
    except (TypeError, ValueError):
        gmv = 0
    current = tiers[0]
    nxt: dict[str, Any] | None = None
    for tier in tiers:
        if gmv >= int(tier.get("min_gmv_cents", 0)):
            current = tier
        else:
            nxt = tier
            break
    return BrandTakeRateResponse(
        brand_id=brand_id,
        gmv_lifetime_cents=gmv,
        current_tier=TakeRateTier(**current),
        next_tier=TakeRateTier(**nxt) if nxt else None,
        gmv_to_next_tier_cents=(
            int(nxt["min_gmv_cents"]) - gmv if nxt else None
        ),
    )


# ═══════════════════════════════════════════════════════════════════════════
# View-Through Summary
# ═══════════════════════════════════════════════════════════════════════════

class ViewThroughSummaryResponse(BaseModel):
    brand_id: str
    click_conversions: int
    view_through_conversions: int
    total_conversions: int
    view_through_rate: float


@router.get(
    "/brand/{brand_id}/view-through-summary",
    response_model=ViewThroughSummaryResponse,
)
async def view_through_summary(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    click_raw = await r.hget(f"brand:{brand_id}:click_conversions", "count")
    vt_raw = await r.hget(f"brand:{brand_id}:view_through_conversions", "count")
    try:
        clicks = int(click_raw) if click_raw else 0
    except (TypeError, ValueError):
        clicks = 0
    try:
        vt = int(vt_raw) if vt_raw else 0
    except (TypeError, ValueError):
        vt = 0
    total = clicks + vt
    rate = (vt / total) if total > 0 else 0.0
    return ViewThroughSummaryResponse(
        brand_id=brand_id,
        click_conversions=clicks,
        view_through_conversions=vt,
        total_conversions=total,
        view_through_rate=round(rate, 6),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Incrementality A/B Tests
# ═══════════════════════════════════════════════════════════════════════════

class IncrementalityCreateRequest(BaseModel):
    brand_id: str
    name: str
    holdout_pct: float = Field(ge=0.0, le=0.5, default=0.10)


class IncrementalityCreateResponse(BaseModel):
    test_id: str
    brand_id: str
    name: str
    holdout_pct: float
    created_at: float


class IncrementalityResultsResponse(BaseModel):
    test_id: str
    brand_id: str
    name: str
    holdout_pct: float
    treatment_users: int
    control_users: int
    treatment_conversions: int
    control_conversions: int
    treatment_conversion_rate: float
    control_conversion_rate: float
    lift_pct: float
    statistical_significance: float  # z-score p-value proxy in (0,1]


def _bucket_for_fingerprint(fingerprint: str, modulo: int = 10) -> int:
    """Stable bucket assignment: hex digest → int → mod."""
    h = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return int(h[:8], 16) % modulo


def _assign_arm(fingerprint: str, holdout_pct: float) -> str:
    """Return 'control' or 'treatment'."""
    bucket = _bucket_for_fingerprint(fingerprint, modulo=1000)
    cutoff = int(round(holdout_pct * 1000))
    return "control" if bucket < cutoff else "treatment"


@router.post(
    "/incrementality/create",
    response_model=IncrementalityCreateResponse,
)
async def incrementality_create(
    req: IncrementalityCreateRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    test_id = uuid4().hex[:16]
    now = _now()
    mapping = {
        "test_id": test_id,
        "brand_id": req.brand_id,
        "name": req.name,
        "holdout_pct": str(req.holdout_pct),
        "created_at": f"{now:.6f}",
        "status": "active",
    }
    pipe = r.pipeline(transaction=True)
    pipe.hset(f"incrementality:{test_id}", mapping=mapping)
    pipe.sadd(f"brand:{req.brand_id}:incrementality_tests", test_id)
    await pipe.execute()
    return IncrementalityCreateResponse(
        test_id=test_id,
        brand_id=req.brand_id,
        name=req.name,
        holdout_pct=req.holdout_pct,
        created_at=now,
    )


@router.post("/incrementality/{test_id}/assign")
async def incrementality_assign(
    test_id: str,
    user_id: str,
    fingerprint: str | None = None,
    r: aioredis.Redis = Depends(get_redis),
):
    """Assign a user to control/treatment for this test.

    Bucketing is deterministic from `fingerprint` (falls back to user_id),
    so the same user always lands in the same arm.
    """
    cfg = await r.hgetall(f"incrementality:{test_id}")
    if not cfg:
        raise HTTPException(status_code=404, detail="test not found")
    try:
        holdout_pct = float(cfg.get("holdout_pct", "0.1"))
    except (TypeError, ValueError):
        holdout_pct = 0.1

    key_basis = fingerprint or user_id
    arm = _assign_arm(key_basis, holdout_pct)

    pipe = r.pipeline(transaction=True)
    if arm == "control":
        pipe.sadd(f"incrementality:{test_id}:control", user_id)
        pipe.srem(f"incrementality:{test_id}:treatment", user_id)
    else:
        pipe.sadd(f"incrementality:{test_id}:treatment", user_id)
        pipe.srem(f"incrementality:{test_id}:control", user_id)
    await pipe.execute()

    return {
        "test_id": test_id,
        "user_id": user_id,
        "arm": arm,
        "suppress_ads": arm == "control",
    }


@router.post("/incrementality/{test_id}/record-conversion")
async def incrementality_record_conversion(
    test_id: str,
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Mark that a user (in either arm) converted. Idempotent per (test,user)."""
    in_t = await r.sismember(f"incrementality:{test_id}:treatment", user_id)
    in_c = await r.sismember(f"incrementality:{test_id}:control", user_id)
    if not (in_t or in_c):
        raise HTTPException(status_code=404, detail="user not enrolled in test")
    arm = "treatment" if in_t else "control"
    added = await r.sadd(f"incrementality:{test_id}:converted:{arm}", user_id)
    return {"test_id": test_id, "user_id": user_id, "arm": arm, "new": bool(added)}


def _approx_p_value(z: float) -> float:
    """Two-sided p-value from a z-score using the error function."""
    return max(0.0, min(1.0, math.erfc(abs(z) / math.sqrt(2))))


@router.get(
    "/incrementality/{test_id}/results",
    response_model=IncrementalityResultsResponse,
)
async def incrementality_results(
    test_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    cfg = await r.hgetall(f"incrementality:{test_id}")
    if not cfg:
        raise HTTPException(status_code=404, detail="test not found")

    t_users = await r.scard(f"incrementality:{test_id}:treatment")
    c_users = await r.scard(f"incrementality:{test_id}:control")
    t_conv = await r.scard(f"incrementality:{test_id}:converted:treatment")
    c_conv = await r.scard(f"incrementality:{test_id}:converted:control")

    t_rate = (t_conv / t_users) if t_users > 0 else 0.0
    c_rate = (c_conv / c_users) if c_users > 0 else 0.0
    lift_pct = ((t_rate - c_rate) / c_rate * 100.0) if c_rate > 0 else 0.0

    # Two-proportion z-test
    pooled = (t_conv + c_conv) / (t_users + c_users) if (t_users + c_users) > 0 else 0.0
    if pooled > 0 and pooled < 1 and t_users > 0 and c_users > 0:
        se = math.sqrt(pooled * (1 - pooled) * (1 / t_users + 1 / c_users))
        z = (t_rate - c_rate) / se if se > 0 else 0.0
    else:
        z = 0.0
    p_value = _approx_p_value(z)

    try:
        holdout_pct = float(cfg.get("holdout_pct", "0.1"))
    except (TypeError, ValueError):
        holdout_pct = 0.1

    return IncrementalityResultsResponse(
        test_id=test_id,
        brand_id=cfg.get("brand_id", ""),
        name=cfg.get("name", ""),
        holdout_pct=holdout_pct,
        treatment_users=t_users,
        control_users=c_users,
        treatment_conversions=t_conv,
        control_conversions=c_conv,
        treatment_conversion_rate=round(t_rate, 6),
        control_conversion_rate=round(c_rate, 6),
        lift_pct=round(lift_pct, 4),
        statistical_significance=round(1.0 - p_value, 6),
    )
