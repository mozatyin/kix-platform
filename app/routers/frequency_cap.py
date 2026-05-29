"""Frequency Cap + Pacing — protect users from ad burn-out.

Industry standard for any sustainable ad network: limit the number of
impressions a single user can see per brand and across all brands, in a
given day/week. Without caps a "winning" auction + permissive geofence
will keep firing the same brands at the same users until they uninstall.

This module exposes:

  * Public API endpoints (check / record / status / admin config).
  * ``check_internal(user_id, brand_id, slot, r)`` — in-process helper
    that auction.py / geofence.py call **before** awarding an impression.
  * ``record_internal(user_id, brand_id, slot, token, r)`` — call
    **after** awarding so the counters move.

Default policies (overridable via POST /admin/config):

  * Global cap: 10/day, 25/week per user across all brands.
  * Per-brand cap: 3/day, 7/week per (user, brand).
  * Recency: same brand can't re-surface within 30 minutes.
  * Pacing: track per-hour buckets so spend is spread evenly through
    the day instead of front-loading.

All state lives in Redis with appropriate TTLs (day = 86400s, week =
604800s) so eviction is automatic; we never need cron cleanup.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ────────────────────────────────────────────────────────────

SECONDS_DAY = 86400
SECONDS_WEEK = 604800

# Key templates.
K_GLOBAL_DAY = "freq:user:{uid}:global:day:{date}"
K_GLOBAL_WEEK = "freq:user:{uid}:global:week:{week}"
K_BRAND_DAY = "freq:user:{uid}:brand:{bid}:day:{date}"
K_BRAND_WEEK = "freq:user:{uid}:brand:{bid}:week:{week}"
K_BRAND_LAST = "freq:user:{uid}:brand:{bid}:last"
K_PAUSED = "freq:user:{uid}:paused"  # SET of brand_ids paused for today
K_PACING_HOUR = "freq:pacing:brand:{bid}:hour:{date}:{hour}"  # counter
K_CONFIG = "freq:config"  # HASH
K_USER_TIER_BRAND = "user:{uid}:tier:{bid}"  # STRING — per-brand tier
K_USER_TIER_GLOBAL = "user:{uid}:tier"  # STRING — global fallback tier
K_USER_OVERRIDE = "freq:user:{uid}:override"  # HASH — explicit per-user cap override

# Fields stored inside the HASH config that are tier-overridable per-field.
CAP_FIELDS = (
    "global_daily",
    "global_weekly",
    "per_brand_daily",
    "per_brand_weekly",
    "recency_minutes",
    "pacing_hourly_cap",
)

# Default rules — applied when ``freq:config`` is empty or missing keys.
DEFAULT_CONFIG: dict[str, int] = {
    "global_daily": 10,
    "global_weekly": 25,
    "per_brand_daily": 3,
    "per_brand_weekly": 7,
    "recency_minutes": 30,
    # Pacing: max impressions per brand per hour bucket. Set to 0 / unset
    # to disable hourly pacing.
    "pacing_hourly_cap": 0,
}

VALID_SLOTS = {"push", "feed", "interstitial", "main", "banner", "geofence"}


# ── Helpers ──────────────────────────────────────────────────────────────


def _today_str(now: float | None = None) -> str:
    ts = now if now is not None else time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _week_str(now: float | None = None) -> str:
    ts = now if now is not None else time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _hour_str(now: float | None = None) -> str:
    ts = now if now is not None else time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H")


def _resolve_user_key(user_id: str | None, device_fingerprint: str | None) -> str:
    """Return a canonical key for either user_id or device_fingerprint."""
    if user_id:
        return f"u:{user_id}"
    if device_fingerprint:
        return f"d:{device_fingerprint}"
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="user_id or device_fingerprint required",
    )


async def _load_config(r: aioredis.Redis) -> dict[str, Any]:
    """Merge stored config over defaults.

    Returns a dict where integer cap fields are ints, plus an optional
    ``tier_overrides`` key (a dict of ``{tier_name: {field: int_value}}``)
    parsed from the stored JSON blob.
    """
    cfg: dict[str, Any] = dict(DEFAULT_CONFIG)
    raw = await r.hgetall(K_CONFIG)
    for k, v in (raw or {}).items():
        if k == "tier_overrides":
            try:
                parsed = json.loads(v) if v else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = {}
            if isinstance(parsed, dict):
                # Sanitize: ensure {tier: {field: int}}
                clean: dict[str, dict[str, int]] = {}
                for tier_name, fields in parsed.items():
                    if not isinstance(fields, dict):
                        continue
                    sub: dict[str, int] = {}
                    for fk, fv in fields.items():
                        if fk not in CAP_FIELDS:
                            continue
                        try:
                            sub[fk] = int(fv)
                        except (TypeError, ValueError):
                            continue
                    if sub:
                        clean[str(tier_name)] = sub
                cfg["tier_overrides"] = clean
            continue
        try:
            cfg[k] = int(v)
        except (TypeError, ValueError):
            continue
    cfg.setdefault("tier_overrides", {})
    return cfg


async def _resolve_user_tier(
    r: aioredis.Redis,
    *,
    user_id: str | None,
    brand_id: str | None,
    user_tier: str | None,
) -> str | None:
    """Resolve the user's tier name.

    Priority: explicit ``user_tier`` arg → per-brand tier key → global tier key.
    Returns ``None`` if no tier is recorded.
    """
    if user_tier:
        return user_tier
    if not user_id:
        return None
    if brand_id:
        per_brand = await r.get(K_USER_TIER_BRAND.format(uid=user_id, bid=brand_id))
        if per_brand:
            return per_brand
    glob = await r.get(K_USER_TIER_GLOBAL.format(uid=user_id))
    return glob or None


async def _load_user_override(
    r: aioredis.Redis, user_id: str | None
) -> dict[str, int]:
    """Read the per-user override HASH (admin allowlist)."""
    if not user_id:
        return {}
    raw = await r.hgetall(K_USER_OVERRIDE.format(uid=user_id))
    out: dict[str, int] = {}
    for k, v in (raw or {}).items():
        if k not in CAP_FIELDS:
            continue
        try:
            out[k] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def _effective_caps(
    base_cfg: dict[str, Any],
    tier: str | None,
    user_override: dict[str, int] | None,
) -> dict[str, int]:
    """Merge defaults ← tier override ← per-user override (latter wins per-field).

    Returns a flat dict of just the cap fields as ints.
    """
    out: dict[str, int] = {f: int(base_cfg.get(f, DEFAULT_CONFIG.get(f, 0))) for f in CAP_FIELDS}
    tier_map = base_cfg.get("tier_overrides") or {}
    if tier and isinstance(tier_map, dict):
        tier_cfg = tier_map.get(tier) or {}
        for f, v in tier_cfg.items():
            if f in CAP_FIELDS:
                try:
                    out[f] = int(v)
                except (TypeError, ValueError):
                    continue
    if user_override:
        for f, v in user_override.items():
            if f in CAP_FIELDS:
                try:
                    out[f] = int(v)
                except (TypeError, ValueError):
                    continue
    return out


async def _get_counter(r: aioredis.Redis, key: str) -> int:
    val = await r.get(key)
    try:
        return int(val) if val is not None else 0
    except (TypeError, ValueError):
        return 0


def _reset_at_end_of_day(now: float | None = None) -> int:
    ts = now if now is not None else time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    end = dt.replace(hour=23, minute=59, second=59, microsecond=0)
    return int(end.timestamp())


# ── Pydantic Schemas ─────────────────────────────────────────────────────


class _UserIdent(BaseModel):
    user_id: str | None = None
    device_fingerprint: str | None = None

    @model_validator(mode="after")
    def _one_of(self) -> "_UserIdent":
        if not self.user_id and not self.device_fingerprint:
            raise ValueError("user_id or device_fingerprint required")
        return self


class CheckRequest(_UserIdent):
    brand_id: str
    # Accept the full VALID_SLOTS set — earlier the Literal here only
    # covered push/feed/interstitial which 422'd legitimate "main" /
    # "banner" / "geofence" slot checks before tier_overrides could apply.
    slot: Literal["push", "feed", "interstitial", "main", "banner", "geofence"]
    user_tier: str | None = None
    # Priority axis (老蔡 healthcare / 老郑 finance / 老周 fitness):
    #   - "normal"     : default — all caps apply
    #   - "high"       : 2x cap multiplier (more lenient)
    #   - "regulatory" : bypass all caps (e.g. 信披, mandatory disclosure)
    #   - "emergency"  : bypass all caps (e.g. life-saving / trainer cancel)
    priority: Literal["normal", "high", "regulatory", "emergency"] | None = None


class CheckResponse(BaseModel):
    allow: bool
    reason: str | None = None
    current_count: int
    cap: int
    reset_at: int
    # Observability: which tier resolved + the effective caps applied.
    # Lets callers / sims verify tier_overrides actually took effect.
    tier: str | None = None
    effective_caps: dict[str, int] = Field(default_factory=dict)
    # Echo back the priority axis if set — dashboards filter on this.
    priority: str | None = None


class RecordRequest(_UserIdent):
    brand_id: str
    slot: Literal["push", "feed", "interstitial", "main", "banner", "geofence"]
    impression_token: str


class RecordResponse(BaseModel):
    ok: bool
    global_today: int
    global_week: int
    brand_today: int
    brand_week: int


class PerBrandCounts(BaseModel):
    today: int
    week: int


class StatusResponse(BaseModel):
    global_today: int
    global_week: int
    per_brand: dict[str, PerBrandCounts]
    paused_brands: list[str]
    tier: str | None = None
    effective_caps: dict[str, int] = Field(default_factory=dict)
    user_override: dict[str, int] = Field(default_factory=dict)


class AdminConfigRequest(BaseModel):
    global_daily: int | None = Field(default=None, ge=0)
    global_weekly: int | None = Field(default=None, ge=0)
    per_brand_daily: int | None = Field(default=None, ge=0)
    per_brand_weekly: int | None = Field(default=None, ge=0)
    recency_minutes: int | None = Field(default=None, ge=0)
    pacing_hourly_cap: int | None = Field(default=None, ge=0)
    # Per-tier overrides — e.g. {"vip": {"per_brand_daily": 10, "global_daily": 30}, ...}
    # Each tier may set any subset of CAP_FIELDS; unset fields fall back to defaults.
    tier_overrides: dict[str, dict[str, int]] | None = None


# ── Core logic (in-process callable) ─────────────────────────────────────


K_PRIORITY_BYPASS = "freq:user:{uid}:priority_bypass:{date}"  # COUNTER per day
PRIORITY_BYPASS_LEVELS = {"regulatory", "emergency"}
PRIORITY_HIGH_MULTIPLIER = 2


async def check_internal(
    user_id: str | None,
    brand_id: str,
    slot: str,
    r: aioredis.Redis,
    *,
    device_fingerprint: str | None = None,
    user_tier: str | None = None,
    priority: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Decide whether ``user`` may see ``brand_id`` right now.

    Returns ``(allow, details)`` where ``details`` carries the reason and
    the counters that drove the decision so the caller can echo them in
    its response / logs.

    ``user_tier`` lets the caller pass an explicit tier (e.g. "vip"). If
    omitted, the tier is resolved via ``user:{uid}:tier:{brand_id}`` (or the
    global ``user:{uid}:tier``). Per-tier overrides + per-user overrides
    are merged over the base config to produce the effective caps.
    """
    if slot not in VALID_SLOTS:
        return False, {
            "reason": "invalid_slot",
            "current_count": 0,
            "cap": 0,
            "reset_at": _reset_at_end_of_day(),
            "tier": None,
            "effective_caps": {},
        }

    uid = _resolve_user_key(user_id, device_fingerprint)
    base_cfg = await _load_config(r)
    tier = await _resolve_user_tier(
        r, user_id=user_id, brand_id=brand_id, user_tier=user_tier
    )
    user_override = await _load_user_override(r, user_id)
    cfg = _effective_caps(base_cfg, tier, user_override)
    now = time.time()
    date = _today_str(now)
    week = _week_str(now)

    # ── Priority bypass (老蔡 healthcare / 老郑 finance / 老周 fitness) ──
    # Regulatory + emergency notifications skip every cap. We still record
    # the bypass for audit + dashboard ("X bypasses today").
    if priority in PRIORITY_BYPASS_LEVELS:
        bypass_key = K_PRIORITY_BYPASS.format(uid=uid, date=date)
        try:
            n = await r.incr(bypass_key)
            if n == 1:
                await r.expire(bypass_key, SECONDS_DAY * 2)
        except aioredis.RedisError as exc:
            logger.warning("priority_bypass counter failed: %s", exc)
        logger.info(
            "freq_cap priority_bypass user=%s brand=%s slot=%s priority=%s",
            uid, brand_id, slot, priority,
        )
        return True, {
            "reason": "priority_bypass",
            "current_count": 0,
            "cap": 0,
            "reset_at": _reset_at_end_of_day(now),
            "tier": tier,
            "effective_caps": dict(cfg),
            "priority": priority,
        }

    # ── High-priority leniency: 2x multiplier on numeric cap fields ──
    if priority == "high":
        cfg = dict(cfg)
        for f in ("global_daily", "global_weekly",
                  "per_brand_daily", "per_brand_weekly",
                  "pacing_hourly_cap"):
            try:
                if int(cfg.get(f, 0)) > 0:
                    cfg[f] = int(cfg[f]) * PRIORITY_HIGH_MULTIPLIER
            except (TypeError, ValueError):
                continue

    # Common debug payload added to every return.
    _debug = {"tier": tier, "effective_caps": dict(cfg)}
    if priority:
        _debug["priority"] = priority

    # Paused list — soft pause (e.g. user complained, brand backed off).
    if await r.sismember(K_PAUSED.format(uid=uid), brand_id):
        return False, {
            "reason": "brand_paused",
            "current_count": 0,
            "cap": 0,
            "reset_at": _reset_at_end_of_day(now),
            **_debug,
        }

    # Recency — same brand, < N minutes ago.
    recency_min = cfg.get("recency_minutes", 30)
    if recency_min > 0:
        last_raw = await r.get(K_BRAND_LAST.format(uid=uid, bid=brand_id))
        if last_raw:
            try:
                last_ts = float(last_raw)
                if now - last_ts < recency_min * 60:
                    return False, {
                        "reason": "recency_block",
                        "current_count": 0,
                        "cap": 0,
                        "reset_at": int(last_ts + recency_min * 60),
                        **_debug,
                    }
            except (TypeError, ValueError):
                pass

    # Per-brand daily.
    brand_day = await _get_counter(r, K_BRAND_DAY.format(uid=uid, bid=brand_id, date=date))
    if brand_day >= cfg["per_brand_daily"]:
        return False, {
            "reason": "per_brand_daily_cap",
            "current_count": brand_day,
            "cap": cfg["per_brand_daily"],
            "reset_at": _reset_at_end_of_day(now),
            **_debug,
        }

    # Per-brand weekly.
    brand_week = await _get_counter(r, K_BRAND_WEEK.format(uid=uid, bid=brand_id, week=week))
    if brand_week >= cfg["per_brand_weekly"]:
        return False, {
            "reason": "per_brand_weekly_cap",
            "current_count": brand_week,
            "cap": cfg["per_brand_weekly"],
            "reset_at": int(now + SECONDS_WEEK),
            **_debug,
        }

    # Global daily.
    global_day = await _get_counter(r, K_GLOBAL_DAY.format(uid=uid, date=date))
    if global_day >= cfg["global_daily"]:
        return False, {
            "reason": "global_daily_cap",
            "current_count": global_day,
            "cap": cfg["global_daily"],
            "reset_at": _reset_at_end_of_day(now),
            **_debug,
        }

    # Global weekly.
    global_week = await _get_counter(r, K_GLOBAL_WEEK.format(uid=uid, week=week))
    if global_week >= cfg["global_weekly"]:
        return False, {
            "reason": "global_weekly_cap",
            "current_count": global_week,
            "cap": cfg["global_weekly"],
            "reset_at": int(now + SECONDS_WEEK),
            **_debug,
        }

    # Pacing — soft brake: if hour bucket is full, defer to next hour.
    pacing_cap = cfg.get("pacing_hourly_cap", 0)
    if pacing_cap > 0:
        hour = _hour_str(now)
        pacing_count = await _get_counter(
            r, K_PACING_HOUR.format(bid=brand_id, date=date, hour=hour)
        )
        if pacing_count >= pacing_cap:
            # Reset at start of next hour.
            dt = datetime.fromtimestamp(now, tz=timezone.utc)
            next_hour = dt.replace(minute=0, second=0, microsecond=0)
            reset_at = int(next_hour.timestamp()) + 3600
            return False, {
                "reason": "pacing_hourly_cap",
                "current_count": pacing_count,
                "cap": pacing_cap,
                "reset_at": reset_at,
                **_debug,
            }

    # All gates passed.
    return True, {
        "reason": None,
        "current_count": brand_day,
        "cap": cfg["per_brand_daily"],
        "reset_at": _reset_at_end_of_day(now),
        **_debug,
    }


async def record_internal(
    user_id: str | None,
    brand_id: str,
    slot: str,
    impression_token: str,
    r: aioredis.Redis,
    *,
    device_fingerprint: str | None = None,
) -> dict[str, int]:
    """Increment all counters after an impression has been served.

    Idempotent on ``impression_token`` — calling twice with the same
    token is a no-op so retries don't double-count.
    """
    uid = _resolve_user_key(user_id, device_fingerprint)
    now = time.time()
    date = _today_str(now)
    week = _week_str(now)
    hour = _hour_str(now)

    # Idempotency guard — claim the token (24h TTL).
    token_key = f"freq:imp:{impression_token}"
    claimed = await r.set(token_key, "1", ex=SECONDS_DAY, nx=True)
    if not claimed:
        # Already recorded — return current state.
        return {
            "global_today": await _get_counter(r, K_GLOBAL_DAY.format(uid=uid, date=date)),
            "global_week": await _get_counter(r, K_GLOBAL_WEEK.format(uid=uid, week=week)),
            "brand_today": await _get_counter(
                r, K_BRAND_DAY.format(uid=uid, bid=brand_id, date=date)
            ),
            "brand_week": await _get_counter(
                r, K_BRAND_WEEK.format(uid=uid, bid=brand_id, week=week)
            ),
        }

    gd_key = K_GLOBAL_DAY.format(uid=uid, date=date)
    gw_key = K_GLOBAL_WEEK.format(uid=uid, week=week)
    bd_key = K_BRAND_DAY.format(uid=uid, bid=brand_id, date=date)
    bw_key = K_BRAND_WEEK.format(uid=uid, bid=brand_id, week=week)
    last_key = K_BRAND_LAST.format(uid=uid, bid=brand_id)
    pacing_key = K_PACING_HOUR.format(bid=brand_id, date=date, hour=hour)

    pipe = r.pipeline()
    pipe.incr(gd_key)
    pipe.expire(gd_key, SECONDS_DAY)
    pipe.incr(gw_key)
    pipe.expire(gw_key, SECONDS_WEEK)
    pipe.incr(bd_key)
    pipe.expire(bd_key, SECONDS_DAY)
    pipe.incr(bw_key)
    pipe.expire(bw_key, SECONDS_WEEK)
    pipe.set(last_key, f"{now:.3f}", ex=SECONDS_DAY)
    pipe.incr(pacing_key)
    pipe.expire(pacing_key, 3600 * 2)  # 2h is plenty for the hour bucket
    results = await pipe.execute()

    # results positions: 0,2,4,6 are the INCR returns (counters).
    return {
        "global_today": int(results[0]),
        "global_week": int(results[2]),
        "brand_today": int(results[4]),
        "brand_week": int(results[6]),
    }


# ── HTTP Endpoints ───────────────────────────────────────────────────────


@router.post("/check", response_model=CheckResponse)
async def check_cap(
    req: CheckRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> CheckResponse:
    """Pre-impression gate: is this user/brand/slot allowed right now?"""
    allow, details = await check_internal(
        req.user_id,
        req.brand_id,
        req.slot,
        r,
        device_fingerprint=req.device_fingerprint,
        user_tier=req.user_tier,
        priority=req.priority,
    )
    return CheckResponse(
        allow=allow,
        reason=details.get("reason"),
        current_count=int(details.get("current_count", 0)),
        cap=int(details.get("cap", 0)),
        reset_at=int(details.get("reset_at", _reset_at_end_of_day())),
        tier=details.get("tier"),
        effective_caps=details.get("effective_caps") or {},
        priority=details.get("priority"),
    )


@router.post("/record", response_model=RecordResponse)
async def record_impression(
    req: RecordRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> RecordResponse:
    """Post-impression accounting: bump every counter that applies."""
    if req.slot not in VALID_SLOTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid slot: {req.slot}",
        )
    counts = await record_internal(
        req.user_id,
        req.brand_id,
        req.slot,
        req.impression_token,
        r,
        device_fingerprint=req.device_fingerprint,
    )
    return RecordResponse(ok=True, **counts)


@router.get("/user/{user_id}/status", response_model=StatusResponse)
async def user_status(
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> StatusResponse:
    """Return all current counter values for a user — useful for debug
    dashboards and the merchant Portal "why didn't my ad show?" panel.
    """
    uid = f"u:{user_id}"
    now = time.time()
    date = _today_str(now)
    week = _week_str(now)

    global_today = await _get_counter(r, K_GLOBAL_DAY.format(uid=uid, date=date))
    global_week = await _get_counter(r, K_GLOBAL_WEEK.format(uid=uid, week=week))

    # Per-brand: discover brands from existing daily/weekly keys.
    pattern_day = f"freq:user:{uid}:brand:*:day:{date}"
    pattern_week = f"freq:user:{uid}:brand:*:week:{week}"
    brand_ids: set[str] = set()
    async for key in r.scan_iter(match=pattern_day, count=100):
        try:
            brand_ids.add(key.split(":brand:")[1].split(":day:")[0])
        except (IndexError, AttributeError):
            continue
    async for key in r.scan_iter(match=pattern_week, count=100):
        try:
            brand_ids.add(key.split(":brand:")[1].split(":week:")[0])
        except (IndexError, AttributeError):
            continue

    per_brand: dict[str, PerBrandCounts] = {}
    for bid in brand_ids:
        per_brand[bid] = PerBrandCounts(
            today=await _get_counter(r, K_BRAND_DAY.format(uid=uid, bid=bid, date=date)),
            week=await _get_counter(r, K_BRAND_WEEK.format(uid=uid, bid=bid, week=week)),
        )

    paused_raw = await r.smembers(K_PAUSED.format(uid=uid))
    paused = sorted(paused_raw or [])

    # Resolve tier + effective caps so dashboards can show "why" decisions
    # were made. We resolve the *global* tier here (no brand context).
    base_cfg = await _load_config(r)
    tier = await _resolve_user_tier(
        r, user_id=user_id, brand_id=None, user_tier=None
    )
    user_override = await _load_user_override(r, user_id)
    effective = _effective_caps(base_cfg, tier, user_override)

    return StatusResponse(
        global_today=global_today,
        global_week=global_week,
        per_brand=per_brand,
        paused_brands=paused,
        tier=tier,
        effective_caps=effective,
        user_override=user_override,
    )


@router.post("/admin/config", response_model=dict)
async def update_config(
    req: AdminConfigRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Update global frequency-cap rules. Partial updates allowed —
    only fields explicitly set in the request body are written; others
    keep their previous value (or fall back to defaults).
    """
    updates: dict[str, Any] = {}
    for field in CAP_FIELDS:
        v = getattr(req, field)
        if v is not None:
            updates[field] = int(v)

    if req.tier_overrides is not None:
        # Sanitize: keep only known fields with int values.
        clean: dict[str, dict[str, int]] = {}
        for tier_name, fields in req.tier_overrides.items():
            if not isinstance(fields, dict):
                continue
            sub: dict[str, int] = {}
            for fk, fv in fields.items():
                if fk not in CAP_FIELDS:
                    continue
                try:
                    sub[fk] = int(fv)
                except (TypeError, ValueError):
                    continue
            if sub:
                clean[str(tier_name)] = sub
        updates["tier_overrides"] = json.dumps(clean, separators=(",", ":"))

    if updates:
        await r.hset(
            K_CONFIG,
            mapping={k: (v if isinstance(v, str) else str(v)) for k, v in updates.items()},
        )

    merged = await _load_config(r)
    return {"ok": True, "config": merged, "updated": list(updates.keys())}


@router.post("/admin/pause", response_model=dict)
async def pause_brand(
    user_id: str,
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Soft-pause a (user, brand) — won't surface until midnight UTC.
    Used by complaint handlers and the "mute brand" affordance.
    """
    key = K_PAUSED.format(uid=f"u:{user_id}")
    await r.sadd(key, brand_id)
    # Pause SET expires at end of day so it auto-clears.
    now = time.time()
    ttl = max(_reset_at_end_of_day(now) - int(now), 60)
    await r.expire(key, ttl)
    return {"ok": True, "user_id": user_id, "brand_id": brand_id}


@router.delete("/admin/pause", response_model=dict)
async def unpause_brand(
    user_id: str,
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Lift a soft-pause for (user, brand)."""
    key = K_PAUSED.format(uid=f"u:{user_id}")
    removed = await r.srem(key, brand_id)
    return {"ok": True, "user_id": user_id, "brand_id": brand_id, "removed": bool(removed)}


@router.get("/admin/config", response_model=dict)
async def get_config(
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Read merged (defaults + Redis-stored) cap configuration."""
    return {"ok": True, "config": await _load_config(r)}


@router.get("/effective-caps", response_model=dict)
async def get_effective_caps(
    user_id: str | None = None,
    brand_id: str | None = None,
    user_tier: str | None = None,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return the merged cap layers for a given (user, brand, tier) trio.

    This is the explicit observability hook the sims/tests demand: it
    reveals exactly which default → tier_override → user_override layers
    fed into ``check_internal``. If ``effective`` here doesn't reflect a
    just-written ``tier_overrides`` value, the bug is in writes — not in
    the check path (which calls the same helpers).

    Returns:
      * ``default_caps``     — DEFAULT_CONFIG (compile-time baseline)
      * ``stored_caps``      — what's in Redis (post-defaults overlay,
                               no tier applied)
      * ``tier``             — resolved tier name (or null)
      * ``tier_override``    — fields the resolved tier overrides (or {})
      * ``user_override``    — fields the per-user override pins (or {})
      * ``effective``        — final values check_internal will compare
                               against (same helper, no drift possible)
      * ``all_tier_overrides`` — full tier-override map (debug aid)
    """
    base_cfg = await _load_config(r)
    tier = await _resolve_user_tier(
        r, user_id=user_id, brand_id=brand_id, user_tier=user_tier
    )
    user_override = await _load_user_override(r, user_id)

    tier_map = base_cfg.get("tier_overrides") or {}
    tier_override: dict[str, int] = {}
    if tier and isinstance(tier_map, dict):
        raw_tier = tier_map.get(tier) or {}
        for fk, fv in raw_tier.items():
            if fk in CAP_FIELDS:
                try:
                    tier_override[fk] = int(fv)
                except (TypeError, ValueError):
                    continue

    stored_caps = {
        f: int(base_cfg.get(f, DEFAULT_CONFIG.get(f, 0))) for f in CAP_FIELDS
    }
    effective = _effective_caps(base_cfg, tier, user_override)

    return {
        "ok": True,
        "user_id": user_id,
        "brand_id": brand_id,
        "tier": tier,
        "default_caps": dict(DEFAULT_CONFIG),
        "stored_caps": stored_caps,
        "tier_override": tier_override,
        "user_override": user_override,
        "effective": effective,
        "all_tier_overrides": (
            tier_map if isinstance(tier_map, dict) else {}
        ),
    }
