"""Attribution System — KiX monetization spine.

Tracks the chain of touchpoints from invite token → impression → click →
visit → game_play → conversion, then attributes conversions to the
source brand via a 7-day last-touch window. Without this, billing is
impossible.

All state lives in Redis. Keys are brand-namespaced and event TTLs match
the attribution window to keep memory bounded.

Consent enforcement (GDPR / PIPL / PDP)
---------------------------------------
Every ``track_*`` endpoint that carries a ``user_id`` calls
``consent.check_internal(user_id, "cross_brand_tracking", r)`` before
persisting the event. If the user has no active grant for that scope the
endpoint returns ``HTTP 403`` with body
``{"error": "consent_required", "scope": "cross_brand_tracking",
"reason": <reason>}`` and header ``Consent-Required:
cross_brand_tracking`` so the SDK can drive the grant UX and retry.

* Anonymous events (``device_fingerprint`` only, no ``user_id``) are
  allowed through — there is no identifiable subject to check yet.
* Setting the env var ``KIX_CONSENT_ENFORCEMENT=permissive`` switches the
  middleware to log-only mode (a warning is emitted but the event still
  persists). Default is strict.
* If the consent router is unavailable at import time the enforcement
  helper degrades open with a warning — billing must keep working even
  if the consent service is missing in dev fixtures.

Key schema
----------
    attr:{event_id}                    HASH   — the event record
    user:{user_id}:attr_journey        LIST   — chronological event_ids (legacy)
    user:{user_id}:attr_journey_z      ZSET   — score=ts, member=event_id (O(log N) lookup)
    device:{fp}:attr_journey           LIST   — anonymous journey (legacy)
    device:{fp}:attr_journey_z         ZSET   — score=ts, member=event_id (O(log N) lookup)
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
import os
import secrets
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Consent enforcement ───────────────────────────────────────────────────
#
# Imported lazily and guarded so attribution still works in test fixtures
# that omit the consent router. If the helper is missing the wrapper below
# logs a warning and lets the event through (degrade-open is intentional
# for the dev fallback path — production deployments always have consent).

CONSENT_SCOPE_TRACKING = "cross_brand_tracking"
CONSENT_ENFORCEMENT_ENV = "KIX_CONSENT_ENFORCEMENT"  # "strict" (default) | "permissive"

try:
    from app.routers.consent import check_internal as _consent_check  # type: ignore
except Exception as _consent_import_err:  # noqa: BLE001 — wide on purpose
    _consent_check = None  # type: ignore[assignment]
    logger.warning(
        "consent.check_internal unavailable — attribution will degrade open "
        "(err=%s)", _consent_import_err,
    )


def _consent_mode_permissive() -> bool:
    return (os.getenv(CONSENT_ENFORCEMENT_ENV) or "").lower() == "permissive"


async def _enforce_consent(
    user_id: str | None,
    r: aioredis.Redis,
    *,
    endpoint: str,
) -> None:
    """Raise HTTP 403 if the user has not consented to cross_brand_tracking.

    No-op when:
      * user_id is empty / None (anonymous event — nothing to check yet).
      * consent module is unavailable (degrade-open with warning).
      * ``KIX_CONSENT_ENFORCEMENT=permissive`` (log-only mode).
    """
    if not user_id:
        return
    if _consent_check is None:
        return
    try:
        allowed, reason = await _consent_check(
            user_id, CONSENT_SCOPE_TRACKING, r
        )
    except Exception as exc:  # noqa: BLE001 — never let consent check break tracking
        logger.warning(
            "consent_check raised for uid=%s endpoint=%s err=%s — degrading open",
            user_id, endpoint, exc,
        )
        return

    if allowed:
        return

    if _consent_mode_permissive():
        logger.warning(
            "consent_missing uid=%s endpoint=%s reason=%s — permissive mode, allowing",
            user_id, endpoint, reason,
        )
        return

    raise HTTPException(
        status_code=403,
        detail={
            "error": "consent_required",
            "scope": CONSENT_SCOPE_TRACKING,
            "reason": reason,
        },
        headers={"Consent-Required": CONSENT_SCOPE_TRACKING},
    )

# ── Constants ──────────────────────────────────────────────────────────────

ATTRIBUTION_WINDOW_SECONDS = 7 * 24 * 60 * 60  # 7 days — default only
# Hard upper bound for any custom window (365 days). Healthcare annual
# boosters (老蔡 365d), travel honeymoon funnels (老梁 210d), real-estate
# cycles (老陆 180d), medical aesthetics re-treatment (老沈 180d) all need
# windows longer than the legacy 90d cap. Anything beyond a year is sketchy.
MAX_ATTRIBUTION_WINDOW_SECONDS = 365 * 86400
EVENT_TTL_SECONDS = MAX_ATTRIBUTION_WINDOW_SECONDS + 7 * 86400  # +1 week grace
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
    # Optional per-conversion attribution window override (seconds).
    # If omitted, falls back to impression_token's stored window, then
    # campaign window, then the default 7 days.
    window_seconds: int | None = Field(default=None, ge=60, le=MAX_ATTRIBUTION_WINDOW_SECONDS)
    # Optional impression_token to look up the window stored at auction time
    # (wedding / anniversary funnels need 30-90 days).
    impression_token: str | None = None
    campaign_id: str | None = None
    # Optional account_id — when present, the conversion is attributed
    # to the ACCOUNT (B2B) rather than only the single user. Buying-committee
    # members share equal-weight credit. See /track/conversion-co for the
    # explicit per-user weighted variant.
    account_id: str | None = None


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
        # Legacy LIST (kept during migration so existing readers don't break)
        pipe.lpush(f"user:{user_id}:attr_journey", event_id)
        pipe.ltrim(f"user:{user_id}:attr_journey", 0, JOURNEY_MAX_LEN - 1)
        pipe.expire(f"user:{user_id}:attr_journey", EVENT_TTL_SECONDS)
        # New ZSET — O(log N) lookups via ZREVRANGEBYSCORE. Score = timestamp
        # so the same key trims-by-rank (cap) and trims-by-score (window).
        zkey = f"user:{user_id}:attr_journey_z"
        pipe.zadd(zkey, {event_id: ts})
        pipe.zremrangebyrank(zkey, 0, -(JOURNEY_MAX_LEN + 1))
        pipe.expire(zkey, EVENT_TTL_SECONDS)

    if device_fingerprint:
        pipe.lpush(f"device:{device_fingerprint}:attr_journey", event_id)
        pipe.ltrim(f"device:{device_fingerprint}:attr_journey", 0, JOURNEY_MAX_LEN - 1)
        pipe.expire(f"device:{device_fingerprint}:attr_journey", EVENT_TTL_SECONDS)
        zkey = f"device:{device_fingerprint}:attr_journey_z"
        pipe.zadd(zkey, {event_id: ts})
        pipe.zremrangebyrank(zkey, 0, -(JOURNEY_MAX_LEN + 1))
        pipe.expire(zkey, EVENT_TTL_SECONDS)

    if target_brand:
        pipe.zadd(f"brand:{target_brand}:attr_incoming", {event_id: ts})
    if source_brand:
        pipe.zadd(f"brand:{source_brand}:attr_outgoing", {event_id: ts})

    # First-touch timestamp per (user, brand) — used to bucket cohort
    # membership. SET NX so the first event wins permanently; subsequent
    # events leave it untouched.
    if user_id and target_brand:
        pipe.set(
            f"user:{user_id}:first_brand_touch:{target_brand}",
            f"{ts:.6f}",
            ex=EVENT_TTL_SECONDS,
            nx=True,
        )

    await pipe.execute()
    return event_id, ts


def _clamp_window(window_seconds: int | None) -> int:
    """Return a sane window in seconds. None/<=0 → default; cap at MAX."""
    if not window_seconds or window_seconds <= 0:
        return ATTRIBUTION_WINDOW_SECONDS
    return min(int(window_seconds), MAX_ATTRIBUTION_WINDOW_SECONDS)


async def _campaign_attribution_window(
    r: aioredis.Redis, campaign_id: str | None
) -> int | None:
    """Read campaign-level attribution_window_seconds override, if any."""
    if not campaign_id:
        return None
    raw = await r.hget(f"campaign:{campaign_id}", "attribution_window_seconds")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _impression_token_window(
    r: aioredis.Redis, impression_token: str | None
) -> int | None:
    """Look up the per-impression window stored at auction time."""
    if not impression_token:
        return None
    # The auction agent is adding window_seconds to impression metadata —
    # we read it from a stable key shape and fall back gracefully if absent.
    raw = await r.hget(f"impression:{impression_token}", "window_seconds")
    if raw is None:
        # legacy fallback: some auction paths persisted into attr:{event_id}.
        raw = await r.hget(f"attr:{impression_token}", "window_seconds")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _resolve_effective_window(
    r: aioredis.Redis,
    *,
    explicit: int | None = None,
    impression_token: str | None = None,
    campaign_id: str | None = None,
) -> int:
    """Resolve the attribution window, precedence:
    explicit arg > impression_token's stored window > campaign override > default.
    Always clamped to [1, MAX_ATTRIBUTION_WINDOW_SECONDS].
    """
    if explicit is not None and explicit > 0:
        return _clamp_window(explicit)
    tok_win = await _impression_token_window(r, impression_token)
    if tok_win:
        return _clamp_window(tok_win)
    camp_win = await _campaign_attribution_window(r, campaign_id)
    if camp_win:
        return _clamp_window(camp_win)
    return ATTRIBUTION_WINDOW_SECONDS


async def _read_journey_recent(
    r: aioredis.Redis,
    subject_key: str,
    *,
    limit: int,
    min_score: float | None = None,
) -> list[str]:
    """Return event_ids newest-first for a journey subject.

    Strategy:
      1. Prefer the ZSET ``{subject_key}_z`` — O(log N + M) via ZREVRANGEBYSCORE.
         When ``min_score`` is given, only events with score >= min_score are
         returned (range query); otherwise the most recent ``limit`` entries.
      2. Fall back to the legacy LIST ``{subject_key}`` (LRANGE 0..limit-1)
         when the ZSET is empty / absent — covers the lazy-migration window.

    ``subject_key`` is the LIST key (e.g. ``user:alice:attr_journey``); the
    ZSET key is derived by appending ``_z``.
    """
    zkey = f"{subject_key}_z"
    if min_score is not None:
        ids = await r.zrevrangebyscore(
            zkey, max="+inf", min=min_score, start=0, num=limit,
        )
    else:
        ids = await r.zrevrange(zkey, 0, limit - 1)
    if ids:
        return list(ids)
    # Fall back to legacy LIST (lazy migration: backfill endpoint copies
    # LIST → ZSET, but until that runs the LIST is the source of truth).
    legacy = await r.lrange(subject_key, 0, limit - 1)
    return list(legacy) if legacy else []


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
    window_seconds = _clamp_window(window_seconds)
    now = _now()
    window_start = now - window_seconds
    journeys: list[str] = []
    if user_id:
        journeys.append(f"user:{user_id}:attr_journey")
    if device_fingerprint:
        journeys.append(f"device:{device_fingerprint}:attr_journey")

    seen: set[str] = set()
    for jkey in journeys:
        # O(log N + M) via ZREVRANGEBYSCORE; falls back to legacy LIST when
        # the ZSET hasn't been backfilled yet for this subject.
        event_ids = await _read_journey_recent(
            r, jkey, limit=200, min_score=window_start,
        )
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
                # Legacy LIST path is reverse-chrono; everything after is
                # older. (ZSET path is already score-bounded, so this is a
                # cheap no-op for the modern path.)
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

    await _enforce_consent(req.user_id, r, endpoint="track_impression")

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

    await _enforce_consent(req.user_id, r, endpoint="track_click")

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
    await _enforce_consent(req.user_id, r, endpoint="track_visit")

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
    await _enforce_consent(req.user_id, r, endpoint="track_conversion")

    # Idempotency: same order_id + target_brand → reuse previously stored.
    idem_key = f"attr:order:{req.target_brand}:{req.order_id}"
    existing = await r.get(idem_key)
    if existing:
        cached = await r.hgetall(f"attr:{existing}")
        if cached:
            return _conversion_from_event(cached, req)

    # Resolve effective attribution window for this conversion.
    effective_window = await _resolve_effective_window(
        r,
        explicit=req.window_seconds,
        impression_token=req.impression_token,
        campaign_id=req.campaign_id,
    )

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
            r, req.user_id, req.target_brand, effective_window
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
    if req.account_id:
        meta["account_id"] = req.account_id

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

    # Account-level rollup. When account_id is present we index the
    # conversion under the account journey so /attribution/account/{aid}/journey
    # can report ABM ROI. Also share credit equally across the buying
    # committee for downstream commission accounting.
    if req.account_id:
        try:
            await _rollup_account_conversion(
                r,
                account_id=req.account_id,
                event_id=event_id,
                ts=ts,
                amount_cents=req.amount_cents,
            )
        except Exception as exc:  # noqa: BLE001 — never block conversion path
            logger.warning(
                "account rollup failed account_id=%s err=%s",
                req.account_id, exc,
            )

    if not attributed_event:
        return ConversionCheckResponse(
            attributed=False,
            event_id=event_id,
            window_seconds=effective_window,
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
        window_seconds=effective_window,
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

    await _enforce_consent(req.user_id, r, endpoint="track_event")

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
    event_ids = await _read_journey_recent(
        r, f"user:{user_id}:attr_journey", limit=limit,
    )
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
        "consent_enforcement": (
            "permissive" if _consent_mode_permissive() else "strict"
        ),
        "consent_module_available": _consent_check is not None,
    }


@router.get("/consent-status/{user_id}")
async def consent_status(
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Debugging helper: what consent scopes does this user have?

    Returns ``{tracked: bool, scopes_granted: [...]}`` where ``tracked``
    is true iff cross_brand_tracking consent is currently active. The
    scopes list is derived from the raw ``consent:user:{uid}`` HASH and
    reflects any scope with a non-revoked grant. This endpoint is for
    SDK/dashboard debugging — production should call ``consent.check``
    proper for authoritative decisions.
    """
    tracked = False
    scopes_granted: list[str] = []
    if _consent_check is not None:
        try:
            allowed, _ = await _consent_check(
                user_id, CONSENT_SCOPE_TRACKING, r
            )
            tracked = bool(allowed)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "consent_status check failed uid=%s err=%s", user_id, exc
            )

    raw = await r.hgetall(f"consent:user:{user_id}") or {}
    for scope, rec_raw in raw.items():
        try:
            rec = json.loads(rec_raw) if rec_raw else None
        except (json.JSONDecodeError, TypeError):
            rec = None
        if not rec:
            continue
        if rec.get("revoked_at"):
            continue
        if rec.get("granted_at"):
            scopes_granted.append(scope)

    return {
        "user_id": user_id,
        "tracked": tracked,
        "scopes_granted": scopes_granted,
        "enforcement_mode": (
            "permissive" if _consent_mode_permissive() else "strict"
        ),
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
    # Optional override sources (mirrors single-touch). When provided we
    # resolve the effective window via the same precedence rules.
    impression_token: str | None = None
    campaign_id: str | None = None


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
    *,
    window_seconds: int | None = None,
) -> list[tuple[dict[str, Any], float]] | None:
    """Collect attributable touchpoints, compute weights, return zipped list.

    Returns None if no valid touchpoints exist in the window. ``window_seconds``
    (kwarg) takes precedence over the positional ``window`` if both are given,
    so callers can pass a per-conversion override.
    """
    if window_seconds is not None:
        window = window_seconds
    window = _clamp_window(window)
    journey = await _read_journey_recent(
        r,
        f"user:{user_id}:attr_journey",
        limit=200,
        min_score=_now() - window,
    )
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

    await _enforce_consent(req.user_id, r, endpoint="track_conversion_multi")

    idem_key = f"attr:order_mta:{req.target_brand}:{req.model}:{req.order_id}"
    existing = await r.get(idem_key)
    if existing:
        cached_payload = await r.get(f"attr:mta_result:{existing}")
        if cached_payload:
            try:
                return MultiTouchConversionResponse(**json.loads(cached_payload))
            except (json.JSONDecodeError, TypeError):
                pass

    # Resolve effective window: explicit req.window_seconds wins, otherwise
    # look up via impression_token / campaign_id, then fall back to default.
    explicit = (
        req.window_seconds
        if req.window_seconds and req.window_seconds != ATTRIBUTION_WINDOW_SECONDS
        else None
    )
    effective_window = await _resolve_effective_window(
        r,
        explicit=explicit,
        impression_token=req.impression_token,
        campaign_id=req.campaign_id,
    )

    attributed = await attribute_multitouch(
        req.user_id, req.target_brand, req.model, r,
        window_seconds=effective_window,
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


# ═══════════════════════════════════════════════════════════════════════════
# Cohort / Retention Reports
#
# 老李's #1 pain point: 3-month churn invisible. Without these reports
# nobody can tell which week's acquired users are sticking around. Cohort
# data changes slowly (it only really moves with each new day of data) so
# results are cached in `brand:{bid}:cohort_cache:{period}` for 1h.
# ═══════════════════════════════════════════════════════════════════════════

COHORT_BUCKETS_DAYS = (0, 1, 7, 14, 30, 60, 90)
COHORT_CACHE_TTL_SECONDS = 3600


class CohortRetention(BaseModel):
    cohort_start_ts: float
    cohort_label: str
    initial_users: int
    retention: dict[str, int]


class CohortReportResponse(BaseModel):
    brand_id: str
    cohort_period: Literal["daily", "weekly", "monthly"]
    from_ts: float
    to_ts: float
    cohorts: list[CohortRetention]


class RetentionSummaryResponse(BaseModel):
    brand_id: str
    window_days: int
    avg_d1_retention: float
    avg_d7_retention: float
    avg_d30_retention: float
    churn_rate_30d: float
    lifetime_value_estimate_cents: int
    cohort_count: int


class UserRecencyBucket(BaseModel):
    bucket: str
    user_count: int
    label: str


class UserRecencyResponse(BaseModel):
    brand_id: str
    total_users: int
    buckets: list[UserRecencyBucket]


def _cohort_label(period: str, ts: float) -> tuple[str, float]:
    """Bucket a timestamp into (label, cohort_start_ts) for the given period."""
    import datetime as _dt
    dt = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    if period == "daily":
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.strftime("%Y-%m-%d"), start.timestamp()
    if period == "weekly":
        iso_year, iso_week, _ = dt.isocalendar()
        # Monday of that ISO week.
        monday = _dt.datetime.fromisocalendar(iso_year, iso_week, 1).replace(
            tzinfo=_dt.timezone.utc
        )
        return f"{iso_year}-W{iso_week:02d}", monday.timestamp()
    # monthly
    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.strftime("%Y-%m"), start.timestamp()


async def _scan_brand_users_with_first_touch(
    r: aioredis.Redis,
    brand_id: str,
    from_ts: float,
    to_ts: float,
) -> dict[str, float]:
    """Return {user_id: first_touch_ts} for users whose first touch on
    this brand falls in [from_ts, to_ts].

    Implementation: SCAN over `user:*:first_brand_touch:{brand_id}` keys.
    """
    pattern = f"user:*:first_brand_touch:{brand_id}"
    out: dict[str, float] = {}
    async for key in r.scan_iter(match=pattern, count=200):
        # key shape: user:{user_id}:first_brand_touch:{brand_id}
        try:
            after_user = key.split("user:", 1)[1]
            user_id = after_user.split(":first_brand_touch:", 1)[0]
        except (IndexError, AttributeError):
            continue
        raw = await r.get(key)
        if raw is None:
            continue
        try:
            ts = float(raw)
        except (TypeError, ValueError):
            continue
        if from_ts <= ts <= to_ts:
            out[user_id] = ts
    return out


async def _user_active_days(
    r: aioredis.Redis, user_id: str, brand_id: str, from_ts: float, to_ts: float
) -> list[float]:
    """Return timestamps of attribution events for (user, brand) in window."""
    # Pull journey then filter; cheaper than per-event hgetall for huge users.
    # ZSET-preferred (O(log N + M)); falls back to legacy LIST.
    journey = await _read_journey_recent(
        r,
        f"user:{user_id}:attr_journey",
        limit=JOURNEY_MAX_LEN,
        min_score=from_ts,
    )
    out: list[float] = []
    for eid in journey:
        e = await r.hgetall(f"attr:{eid}")
        if not e:
            continue
        # Either the user touched the brand as target, or the brand drove them.
        if e.get("target_brand") != brand_id and e.get("source_brand") != brand_id:
            continue
        try:
            ts = float(e.get("timestamp", 0) or 0)
        except (TypeError, ValueError):
            continue
        if from_ts <= ts <= to_ts:
            out.append(ts)
    return out


@router.get(
    "/brand/{brand_id}/cohorts",
    response_model=CohortReportResponse,
)
async def brand_cohorts(
    brand_id: str,
    period: Literal["daily", "weekly", "monthly"] = Query(default="weekly"),
    from_ts: float | None = Query(default=None, alias="from"),
    to_ts: float | None = Query(default=None, alias="to"),
    size: int = Query(default=30, ge=1, le=180),
    r: aioredis.Redis = Depends(get_redis),
):
    """Cohort retention matrix for the brand.

    Cohort membership is defined by ``user:{uid}:first_brand_touch:{brand_id}``.
    Retention at day-N counts users who had any attribution event for the
    brand in the [cohort_start + N*day, cohort_start + (N+1)*day) bucket.

    Results are cached for 1h in ``brand:{bid}:cohort_cache:{period}``.
    """
    now = _now()
    # Default window scales with period × size.
    period_secs = {"daily": 86400, "weekly": 7 * 86400, "monthly": 30 * 86400}[period]
    if to_ts is None:
        to_ts = now
    if from_ts is None:
        from_ts = to_ts - size * period_secs

    # Cache lookup
    cache_key = f"brand:{brand_id}:cohort_cache:{period}"
    cache_field = f"{int(from_ts)}:{int(to_ts)}:{size}"
    cached_raw = await r.hget(cache_key, cache_field)
    if cached_raw:
        try:
            return CohortReportResponse(**json.loads(cached_raw))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    # Discover cohort members
    members = await _scan_brand_users_with_first_touch(r, brand_id, from_ts, to_ts)

    # Bucket users into cohorts.
    cohort_users: dict[str, list[tuple[str, float]]] = {}
    cohort_starts: dict[str, float] = {}
    for uid, ts in members.items():
        label, start = _cohort_label(period, ts)
        cohort_users.setdefault(label, []).append((uid, ts))
        cohort_starts[label] = start

    cohorts: list[CohortRetention] = []
    # Sort labels by cohort_start_ts asc.
    for label in sorted(cohort_starts, key=lambda L: cohort_starts[L]):
        users = cohort_users[label]
        start_ts = cohort_starts[label]
        retention: dict[str, int] = {f"d{b}": 0 for b in COHORT_BUCKETS_DAYS}
        # d0 is by definition the initial cohort size (the first-touch event).
        retention["d0"] = len(users)
        # For each bucket day d>0, count users with any activity in
        # [start + d*day, start + (d+1)*day).
        for uid, _first_ts in users:
            # Pull activity once per user.
            activity = await _user_active_days(
                r, uid, brand_id, start_ts, now
            )
            for d in COHORT_BUCKETS_DAYS:
                if d == 0:
                    continue
                lo = start_ts + d * 86400
                hi = lo + 86400
                if any(lo <= a < hi for a in activity):
                    retention[f"d{d}"] += 1
        cohorts.append(CohortRetention(
            cohort_start_ts=start_ts,
            cohort_label=label,
            initial_users=len(users),
            retention=retention,
        ))

    resp = CohortReportResponse(
        brand_id=brand_id,
        cohort_period=period,
        from_ts=from_ts,
        to_ts=to_ts,
        cohorts=cohorts,
    )
    # Cache
    try:
        await r.hset(cache_key, cache_field, resp.json())
        await r.expire(cache_key, COHORT_CACHE_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — cache write is best-effort
        logger.warning("cohort cache write failed: %s", exc)
    return resp


@router.get(
    "/brand/{brand_id}/retention-summary",
    response_model=RetentionSummaryResponse,
)
async def brand_retention_summary(
    brand_id: str,
    window_days: int = Query(default=30, ge=1, le=180),
    r: aioredis.Redis = Depends(get_redis),
):
    """Roll-up of retention KPIs across recent cohorts.

    Pulls weekly cohorts spanning ``window_days`` and averages their d1/d7/d30
    retention. ``churn_rate_30d = 1 - avg_d30_retention``. LTV estimate is a
    crude (gmv_lifetime / unique_users)·d30_retention proxy that gives the
    merchant Portal *something* to render until the billing service ships
    a proper LTV model.
    """
    now = _now()
    from_ts = now - window_days * 86400
    # Reuse the cohort endpoint logic (without HTTP round-trip).
    members = await _scan_brand_users_with_first_touch(r, brand_id, from_ts, now)

    cohort_users: dict[str, list[tuple[str, float]]] = {}
    cohort_starts: dict[str, float] = {}
    for uid, ts in members.items():
        label, start = _cohort_label("weekly", ts)
        cohort_users.setdefault(label, []).append((uid, ts))
        cohort_starts[label] = start

    d1_rates: list[float] = []
    d7_rates: list[float] = []
    d30_rates: list[float] = []
    for label, users in cohort_users.items():
        if not users:
            continue
        start_ts = cohort_starts[label]
        c1 = c7 = c30 = 0
        for uid, _ts in users:
            activity = await _user_active_days(r, uid, brand_id, start_ts, now)
            if any(start_ts + 86400 <= a < start_ts + 2 * 86400 for a in activity):
                c1 += 1
            if any(start_ts + 7 * 86400 <= a < start_ts + 8 * 86400 for a in activity):
                c7 += 1
            if any(start_ts + 30 * 86400 <= a < start_ts + 31 * 86400 for a in activity):
                c30 += 1
        n = len(users)
        d1_rates.append(c1 / n)
        d7_rates.append(c7 / n)
        d30_rates.append(c30 / n)

    def _avg(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    avg_d1 = _avg(d1_rates)
    avg_d7 = _avg(d7_rates)
    avg_d30 = _avg(d30_rates)
    churn_30 = max(0.0, 1.0 - avg_d30)

    # LTV proxy
    gmv_raw = await r.get(f"brand:{brand_id}:gmv_lifetime")
    try:
        gmv = int(gmv_raw) if gmv_raw else 0
    except (TypeError, ValueError):
        gmv = 0
    unique_users = await r.scard(f"brand:{brand_id}:users")
    ltv_cents = 0
    if unique_users > 0:
        # Per-user-GMV scaled by d30 retention as a (very) rough survival proxy.
        ltv_cents = int(round((gmv / unique_users) * (1.0 + avg_d30)))

    return RetentionSummaryResponse(
        brand_id=brand_id,
        window_days=window_days,
        avg_d1_retention=round(avg_d1, 6),
        avg_d7_retention=round(avg_d7, 6),
        avg_d30_retention=round(avg_d30, 6),
        churn_rate_30d=round(churn_30, 6),
        lifetime_value_estimate_cents=ltv_cents,
        cohort_count=len(cohort_users),
    )


@router.get(
    "/brand/{brand_id}/user-recency",
    response_model=UserRecencyResponse,
)
async def brand_user_recency(
    brand_id: str,
    segment_buckets: str = Query(
        default="7,30,60,90,180",
        description="Comma-separated day boundaries (asc). Example: 7,30,60,90,180",
    ),
    r: aioredis.Redis = Depends(get_redis),
):
    """User segmentation by days since last activity on this brand.

    Drives the "at-risk / dormant / churned" view 老李 needs to spot
    accounts before they fall off the 90-day cliff.
    """
    try:
        boundaries = sorted({int(x.strip()) for x in segment_buckets.split(",") if x.strip()})
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="segment_buckets must be ints")
    if not boundaries:
        boundaries = [7, 30, 60, 90, 180]

    # Discover users via the brand's known-users SET (populated on first
    # visit). Iterate via SSCAN for memory friendliness.
    user_ids: list[str] = []
    async for uid in r.sscan_iter(f"brand:{brand_id}:users", count=200):
        user_ids.append(uid)

    now = _now()
    # Compute days_since_last for each user.
    days_since: list[int] = []
    for uid in user_ids:
        # Fast path: most recent journey event timestamp for this brand.
        last_ts: float | None = None
        journey = await _read_journey_recent(
            r, f"user:{uid}:attr_journey", limit=50,
        )
        for eid in journey:
            e = await r.hgetall(f"attr:{eid}")
            if not e:
                continue
            if e.get("target_brand") != brand_id and e.get("source_brand") != brand_id:
                continue
            try:
                ts = float(e.get("timestamp", 0) or 0)
            except (TypeError, ValueError):
                continue
            if last_ts is None or ts > last_ts:
                last_ts = ts
        if last_ts is None:
            # Fall back to first_brand_touch.
            raw = await r.get(f"user:{uid}:first_brand_touch:{brand_id}")
            try:
                last_ts = float(raw) if raw else None
            except (TypeError, ValueError):
                last_ts = None
        if last_ts is None:
            continue
        days_since.append(int((now - last_ts) // 86400))

    # Build buckets: [0..b0], (b0..b1], (b1..b2], ..., (bN-1..bN], (bN..∞)
    labels_by_max_day = [
        (7, "active"),
        (30, "engaged"),
        (60, "at_risk"),
        (90, "dormant"),
        (180, "churned"),
    ]
    label_lookup = {k: v for k, v in labels_by_max_day}

    buckets: list[UserRecencyBucket] = []
    prev = 0
    for b in boundaries:
        # bucket "(prev+1)-b d" except first which is "0-b"
        if prev == 0:
            label_range = f"0-{b}d"
        else:
            label_range = f"{prev + 1}-{b}d"
        count = sum(1 for d in days_since if (prev if prev == 0 else prev + 1) <= d <= b) \
            if prev == 0 else sum(1 for d in days_since if prev < d <= b)
        buckets.append(UserRecencyBucket(
            bucket=label_range,
            user_count=count,
            label=label_lookup.get(b, "engaged"),
        ))
        prev = b
    # Tail bucket
    tail_count = sum(1 for d in days_since if d > prev)
    buckets.append(UserRecencyBucket(
        bucket=f"{prev}d+",
        user_count=tail_count,
        label="lost",
    ))

    return UserRecencyResponse(
        brand_id=brand_id,
        total_users=len(days_since),
        buckets=buckets,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Co-Attribution — B2B multi-decision-maker conversions
#
# A B2B sale rarely has one signer. The CEO decided, the CTO greenlit the
# tech fit, procurement actually signed. Last-touch attribution to one
# user_id loses the buying committee. /track/conversion-co records the
# split explicitly with caller-supplied weights, so each user's commission
# rolls up proportionally and downstream ABM dashboards can attribute the
# full account journey.
# ═══════════════════════════════════════════════════════════════════════════

CO_ATTRIBUTION_ROLES = {"decider", "influencer", "signer", "end_user"}


class CoAttributionEntry(BaseModel):
    user_id: str = Field(min_length=1)
    role: Literal["decider", "influencer", "signer", "end_user"]
    weight: float = Field(ge=0.0, le=1.0)


class CoAttributionConversionRequest(BaseModel):
    target_brand: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(ge=0)
    co_attribution: list[CoAttributionEntry] = Field(min_length=1)
    account_id: str | None = None
    source_brand: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class CoAttributionUserSplit(BaseModel):
    user_id: str
    role: str
    weight: float
    share_amount_cents: int
    commission_cents: int
    kix_take_cents: int
    user_take_cents: int


class CoAttributionConversionResponse(BaseModel):
    attributed: bool
    event_id: str
    target_brand: str
    account_id: str | None
    total_amount_cents: int
    total_commission_cents: int
    total_kix_take_cents: int
    attributed_users: list[CoAttributionUserSplit]


WEIGHT_SUM_TOLERANCE = 1e-3


async def _rollup_account_conversion(
    r: aioredis.Redis,
    *,
    account_id: str,
    event_id: str,
    ts: float,
    amount_cents: int,
) -> None:
    """Index a conversion under the account journey + lifetime GMV.

    This is what makes ``GET /attribution/account/{aid}/journey`` work,
    and it's what feeds ABM ROI dashboards.
    """
    pipe = r.pipeline(transaction=True)
    pipe.zadd(f"account:{account_id}:attr_journey", {event_id: ts})
    # Cap journey ZSET so a chatty account can't blow memory.
    pipe.zremrangebyrank(f"account:{account_id}:attr_journey", 0, -1001)
    pipe.expire(f"account:{account_id}:attr_journey", EVENT_TTL_SECONDS)
    if amount_cents > 0:
        pipe.incrby(f"account:{account_id}:gmv_lifetime", amount_cents)
        pipe.hincrby(f"account:{account_id}:conversions", "count", 1)
    await pipe.execute()


@router.post(
    "/track/conversion-co",
    response_model=CoAttributionConversionResponse,
)
async def track_conversion_co(
    req: CoAttributionConversionRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Records a conversion split across multiple users with weights.

    The caller is responsible for choosing the weights — typically the
    buying-committee membership and role context drives this. We enforce
    ``Σ weights == 1`` (within 1e-3 tolerance) so commission math doesn't
    silently double- or under-bill.

    Idempotent on (target_brand, order_id) — replays return the cached
    decision.
    """
    if not req.co_attribution:
        raise HTTPException(status_code=400, detail="co_attribution_required")

    # Validate roles + weights.
    weight_sum = 0.0
    seen_users: set[str] = set()
    for entry in req.co_attribution:
        if entry.role not in CO_ATTRIBUTION_ROLES:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_role", "user_id": entry.user_id},
            )
        if entry.user_id in seen_users:
            raise HTTPException(
                status_code=400,
                detail={"error": "duplicate_user", "user_id": entry.user_id},
            )
        seen_users.add(entry.user_id)
        weight_sum += entry.weight

    if abs(weight_sum - 1.0) > WEIGHT_SUM_TOLERANCE:
        raise HTTPException(
            status_code=400,
            detail={"error": "weights_must_sum_to_one", "weight_sum": weight_sum},
        )

    # Consent — every named user must have cross_brand_tracking.
    for entry in req.co_attribution:
        await _enforce_consent(entry.user_id, r, endpoint="track_conversion_co")

    # Idempotency.
    idem_key = f"attr:order_co:{req.target_brand}:{req.order_id}"
    existing = await r.get(idem_key)
    if existing:
        cached = await r.get(f"attr:co_result:{existing}")
        if cached:
            try:
                return CoAttributionConversionResponse(**json.loads(cached))
            except (json.JSONDecodeError, TypeError):
                pass

    now = _now()
    meta = dict(req.context or {})
    meta["order_id"] = req.order_id
    meta["co_attribution"] = [
        {"user_id": e.user_id, "role": e.role, "weight": e.weight}
        for e in req.co_attribution
    ]
    if req.account_id:
        meta["account_id"] = req.account_id

    # Persist a single conversion event keyed off the *first* user (the
    # primary record). Per-user splits live in meta so any dashboard can
    # reconstruct the full picture.
    primary_user = req.co_attribution[0].user_id
    event_id, ts = await _persist_event(
        r,
        stage=STAGE_CONVERSION,
        user_id=primary_user,
        source_brand=req.source_brand,
        target_brand=req.target_brand,
        value_cents=req.amount_cents,
        meta=meta,
    )

    # Per-user split: each user gets pro-rated commission. Take rate is
    # derived from the *target* brand's tier — co-attribution is an
    # account-level concept, not a brand-acquisition one.
    target_tier = await _pick_take_rate_tier(r, req.target_brand)
    commission_rate = target_tier["commission_rate"]
    kix_fraction = target_tier["kix_take"]

    splits: list[CoAttributionUserSplit] = []
    total_commission = 0
    total_kix = 0
    pipe = r.pipeline(transaction=True)
    for entry in req.co_attribution:
        share = int(round(req.amount_cents * entry.weight))
        commission = int(round(share * commission_rate))
        kix_cents = int(round(commission * kix_fraction))
        user_cents = commission - kix_cents
        total_commission += commission
        total_kix += kix_cents

        splits.append(CoAttributionUserSplit(
            user_id=entry.user_id,
            role=entry.role,
            weight=round(entry.weight, 6),
            share_amount_cents=share,
            commission_cents=commission,
            kix_take_cents=kix_cents,
            user_take_cents=user_cents,
        ))

        # Per-user attribution rollup. Keyed by *user* (not brand) since
        # this is the user's "earned commission" ledger entry.
        pipe.hincrby(f"user:{entry.user_id}:co_attr_earned", "cents", user_cents)
        pipe.hincrby(
            f"user:{entry.user_id}:co_attr_earned", f"role:{entry.role}_cents",
            user_cents,
        )
        # Also index the event under the user's journey so cross-checks work.
        pipe.lpush(f"user:{entry.user_id}:attr_journey", event_id)
        pipe.ltrim(f"user:{entry.user_id}:attr_journey", 0, JOURNEY_MAX_LEN - 1)
        pipe.expire(f"user:{entry.user_id}:attr_journey", EVENT_TTL_SECONDS)
        # ZSET mirror for O(log N) lookups.
        zkey = f"user:{entry.user_id}:attr_journey_z"
        pipe.zadd(zkey, {event_id: ts})
        pipe.zremrangebyrank(zkey, 0, -(JOURNEY_MAX_LEN + 1))
        pipe.expire(zkey, EVENT_TTL_SECONDS)

    # Target-brand commission accounting.
    pipe.hincrby("kix:commission_collected", "cents", total_kix)
    pipe.hincrby(
        f"brand:{req.target_brand}:commission_paid", "cents", total_commission,
    )
    await pipe.execute()

    # Account-level journey rollup.
    if req.account_id:
        await _rollup_account_conversion(
            r,
            account_id=req.account_id,
            event_id=event_id,
            ts=ts,
            amount_cents=req.amount_cents,
        )

    resp = CoAttributionConversionResponse(
        attributed=True,
        event_id=event_id,
        target_brand=req.target_brand,
        account_id=req.account_id,
        total_amount_cents=req.amount_cents,
        total_commission_cents=total_commission,
        total_kix_take_cents=total_kix,
        attributed_users=splits,
    )

    # Cache the response for idempotent replay.
    await r.set(idem_key, event_id, ex=EVENT_TTL_SECONDS)
    await r.set(
        f"attr:co_result:{event_id}", resp.json(), ex=EVENT_TTL_SECONDS,
    )
    _ = now  # quell linter — we use ts from _persist_event for ordering
    return resp


class AccountJourneyEntry(BaseModel):
    event_id: str
    stage: str
    timestamp: float
    user_id: str | None = None
    source_brand: str | None = None
    target_brand: str | None = None
    value_cents: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)


class AccountJourneyResponse(BaseModel):
    account_id: str
    count: int
    total_gmv_cents: int
    member_count: int
    entries: list[AccountJourneyEntry]


@router.get(
    "/account/{account_id}/journey",
    response_model=AccountJourneyResponse,
)
async def account_journey(
    account_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    include_member_events: bool = Query(default=True),
    r: aioredis.Redis = Depends(get_redis),
):
    """All attribution events tied to an account.

    Sources:
      1. Events that explicitly carried ``account_id`` (rolled into
         ``account:{aid}:attr_journey``).
      2. (When include_member_events=True) every member's individual
         journey, merged + deduped — supports the "this exec saw 3 ads
         before procurement signed" ABM narrative.
    """
    # Resolve account members via the accounts router helper (lazy import
    # to avoid cycles at module load).
    try:
        from app.routers.accounts import get_account_members  # type: ignore
    except ImportError:
        get_account_members = None  # type: ignore[assignment]

    # 1) Direct account journey
    direct_ids = await r.zrevrange(
        f"account:{account_id}:attr_journey", 0, limit - 1,
    )
    seen: set[str] = set(direct_ids) if direct_ids else set()
    event_ids: list[str] = list(direct_ids) if direct_ids else []

    member_count = 0
    if include_member_events and get_account_members is not None:
        members = await get_account_members(r, account_id)
        member_count = len(members)
        per_user_limit = max(20, limit // max(1, member_count or 1))
        for m in members:
            uid_events = await _read_journey_recent(
                r,
                f"user:{m.user_id}:attr_journey",
                limit=per_user_limit,
            )
            for eid in uid_events:
                if eid in seen:
                    continue
                seen.add(eid)
                event_ids.append(eid)

    # Hydrate events, then sort chronologically (newest first).
    entries: list[AccountJourneyEntry] = []
    for eid in event_ids:
        raw = await r.hgetall(f"attr:{eid}")
        if not raw:
            continue
        try:
            meta = json.loads(raw.get("meta") or "{}")
            if not isinstance(meta, dict):
                meta = {}
        except json.JSONDecodeError:
            meta = {}
        try:
            ts = float(raw.get("timestamp", 0) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        entries.append(AccountJourneyEntry(
            event_id=eid,
            stage=raw.get("stage", ""),
            timestamp=ts,
            user_id=raw.get("user_id") or None,
            source_brand=raw.get("source_brand") or None,
            target_brand=raw.get("target_brand") or None,
            value_cents=int(raw.get("value_cents", 0) or 0),
            meta=meta,
        ))

    entries.sort(key=lambda e: e.timestamp, reverse=True)
    entries = entries[:limit]

    gmv_raw = await r.get(f"account:{account_id}:gmv_lifetime")
    try:
        total_gmv = int(gmv_raw) if gmv_raw else 0
    except (TypeError, ValueError):
        total_gmv = 0

    return AccountJourneyResponse(
        account_id=account_id,
        count=len(entries),
        total_gmv_cents=total_gmv,
        member_count=member_count,
        entries=entries,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Admin — ZSET backfill (LIST → ZSET migration)
#
# The journey storage migrated from LIST (O(N) LRANGE) to ZSET (O(log N)
# ZRANGEBYSCORE). New writes hit both. This endpoint lazy-migrates existing
# LIST keys by reading each event's stored timestamp and ZADD'ing under the
# parallel ``_z`` key. Safe to re-run — ZADD on an existing (member, score)
# pair is a no-op.
# ═══════════════════════════════════════════════════════════════════════════


class JourneyZSetBackfillRequest(BaseModel):
    admin_token: str = Field(..., min_length=8, max_length=512)
    batch_size: int = Field(default=100, ge=1, le=1000)
    max_users: int | None = Field(default=None, ge=1, le=1_000_000)
    # Restrict scan to "user" or "device" subjects, or both (None = both).
    subject: Literal["user", "device"] | None = None
    # Resume cursor (Redis SCAN cursor as string). 0 / None starts fresh.
    cursor: int = Field(default=0, ge=0)


class JourneyZSetBackfillResponse(BaseModel):
    scanned: int
    migrated_keys: int
    migrated_events: int
    skipped_empty: int
    errors: int
    next_cursor: int
    done: bool


def _journey_subject_patterns(
    subject: Literal["user", "device"] | None,
) -> list[str]:
    if subject == "user":
        return ["user:*:attr_journey"]
    if subject == "device":
        return ["device:*:attr_journey"]
    return ["user:*:attr_journey", "device:*:attr_journey"]


@router.post(
    "/admin/backfill-journey-zset",
    response_model=JourneyZSetBackfillResponse,
)
async def backfill_journey_zset(
    body: JourneyZSetBackfillRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Backfill ZSET journeys from existing LIST journeys.

    For each ``user:*:attr_journey`` (and/or ``device:*:attr_journey``) LIST
    key found via SCAN, read all entries + their stored ``attr:{eid}.timestamp``
    and ZADD into the parallel ``{subject_key}_z`` ZSET. Idempotent.

    Pagination: returns ``next_cursor`` and ``done=False`` when more keys
    remain; the caller is expected to re-call with the returned cursor.
    """
    # Admin auth — mirrors payouts._check_admin pattern.
    from app.security import constant_time_eq  # local import to avoid cycles
    from app.config import settings as _settings

    if not constant_time_eq(body.admin_token, _settings.jwt_secret):
        raise HTTPException(
            status_code=403, detail={"error": "admin_token_invalid"},
        )

    patterns = _journey_subject_patterns(body.subject)

    scanned = 0
    migrated_keys = 0
    migrated_events = 0
    skipped_empty = 0
    errors = 0
    cursor: int = body.cursor
    user_quota: int | None = body.max_users

    for pattern in patterns:
        # Each SCAN pass uses its own cursor — but we only honour the
        # caller-supplied cursor for the first pattern; subsequent patterns
        # start from 0. (Callers typically pin subject= to paginate cleanly.)
        local_cursor = cursor if pattern == patterns[0] else 0
        first_loop = True
        while True:
            if not first_loop and local_cursor == 0:
                # Completed full circle on this pattern.
                break
            first_loop = False
            local_cursor, keys = await r.scan(
                cursor=local_cursor,
                match=pattern,
                count=body.batch_size,
            )
            for key in keys:
                # Skip ZSET sentinels (defensive — pattern shouldn't match,
                # but keys returned by SCAN can be either bytes or str).
                if not isinstance(key, str):
                    try:
                        key = key.decode("utf-8")
                    except Exception:  # noqa: BLE001
                        errors += 1
                        continue
                if key.endswith("_z"):
                    continue
                scanned += 1
                try:
                    event_ids = await r.lrange(key, 0, JOURNEY_MAX_LEN - 1)
                except Exception as exc:  # noqa: BLE001 — never abort batch
                    logger.warning("backfill lrange failed key=%s err=%s", key, exc)
                    errors += 1
                    continue
                if not event_ids:
                    skipped_empty += 1
                    continue
                # Build score map by reading each event's timestamp.
                score_map: dict[str, float] = {}
                for eid in event_ids:
                    try:
                        raw_ts = await r.hget(f"attr:{eid}", "timestamp")
                    except Exception:  # noqa: BLE001
                        errors += 1
                        continue
                    if raw_ts is None:
                        # Event TTL'd away — drop it.
                        continue
                    try:
                        score_map[eid] = float(raw_ts)
                    except (TypeError, ValueError):
                        errors += 1
                        continue
                if not score_map:
                    skipped_empty += 1
                    continue
                zkey = f"{key}_z"
                try:
                    await r.zadd(zkey, score_map)
                    # Cap and refresh TTL to match the source LIST.
                    await r.zremrangebyrank(zkey, 0, -(JOURNEY_MAX_LEN + 1))
                    await r.expire(zkey, EVENT_TTL_SECONDS)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("backfill zadd failed key=%s err=%s", zkey, exc)
                    errors += 1
                    continue
                migrated_keys += 1
                migrated_events += len(score_map)
                if user_quota is not None:
                    user_quota -= 1
                    if user_quota <= 0:
                        # Return early so caller can decide whether to continue.
                        return JourneyZSetBackfillResponse(
                            scanned=scanned,
                            migrated_keys=migrated_keys,
                            migrated_events=migrated_events,
                            skipped_empty=skipped_empty,
                            errors=errors,
                            next_cursor=int(local_cursor),
                            done=False,
                        )
            if local_cursor == 0:
                break
        # Only the first pattern carries the caller cursor; reset for next.
        cursor = 0

    return JourneyZSetBackfillResponse(
        scanned=scanned,
        migrated_keys=migrated_keys,
        migrated_events=migrated_events,
        skipped_empty=skipped_empty,
        errors=errors,
        next_cursor=0,
        done=True,
    )
