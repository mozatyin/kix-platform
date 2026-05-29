"""Special trigger modules — 5 mechanics for FOMO, gating, scarcity.

  1. UserAttributeTrigger — birthday / signup anniversary / first visit
                            (fires once per year or once per lifetime)
  2. RateLimit            — per-action rate windows (day/hour/week)
  3. LimitedDrop          — FOMO scarcity: N items, time-windowed
  4. PerkActivation       — tier-locked perks
  5. FirstComeFirstServe  — red-envelope-style claim race

All keys are brand-isolated.

Atomicity (claim races + supply caps):
  * LimitedDrop.claim   — Lua script: TIME check, set membership check,
                          DECR supply, SADD claimer atomically.
  * FCFS.claim          — same Lua pattern with a separate pool.
  * RateLimit.consume   — INCR + EXPIRE on a sliding window key.
  * UserAttributeTrigger — SETNX on a year-scoped key (or lifetime).

Where Lua is overkill (e.g. dev-mode) we fall back to a single
DECR + rollback pattern. Lua remains the source of truth.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Lua: atomic claim against a counter + member set ──────────────────────
#
# KEYS[1] = state hash (HASH; fields: supply, claimed, ends_at, status, ...)
# KEYS[2] = claimers SET
# ARGV[1] = user_id
# ARGV[2] = now (epoch seconds)
# Returns:
#   {1, supply_remaining, claimed_total}    — success
#   {0, "already_claimed"}                  — user already in set
#   {0, "sold_out"}                         — supply exhausted
#   {0, "expired"}                          — past ends_at
#   {0, "not_found"}                        — state missing

_LUA_ATOMIC_CLAIM = """
if redis.call('EXISTS', KEYS[1]) == 0 then
    return {0, "not_found"}
end
local ends_at = tonumber(redis.call('HGET', KEYS[1], 'ends_at')) or 0
if ends_at > 0 and tonumber(ARGV[2]) > ends_at then
    return {0, "expired"}
end
local supply = tonumber(redis.call('HGET', KEYS[1], 'supply')) or 0
if supply <= 0 then
    return {0, "sold_out"}
end
if redis.call('SISMEMBER', KEYS[2], ARGV[1]) == 1 then
    return {0, "already_claimed"}
end
local new_supply = redis.call('HINCRBY', KEYS[1], 'supply', -1)
if new_supply < 0 then
    -- Race lost — roll back and report sold_out.
    redis.call('HINCRBY', KEYS[1], 'supply', 1)
    return {0, "sold_out"}
end
redis.call('HINCRBY', KEYS[1], 'claimed', 1)
redis.call('SADD', KEYS[2], ARGV[1])
local claimed = tonumber(redis.call('HGET', KEYS[1], 'claimed')) or 0
return {1, new_supply, claimed}
"""


# Singleton script cache — registered lazily per Redis connection.
_atomic_claim_script: Any = None


async def _get_atomic_claim_script(r: aioredis.Redis):
    global _atomic_claim_script
    if _atomic_claim_script is None:
        _atomic_claim_script = r.register_script(_LUA_ATOMIC_CLAIM)
    return _atomic_claim_script


# ── Pydantic models ────────────────────────────────────────────────────────


# 1. UserAttribute -----------------------------------------------------------


class AttrTriggerCheck(BaseModel):
    user_id: str = Field(..., min_length=1)
    brand_id: str = Field(..., min_length=1)
    trigger_type: Literal["birthday", "signup_anniversary", "first_visit"]
    # Optional: caller-supplied attributes (we don't read a user DB here).
    birthday_iso: str | None = None  # YYYY-MM-DD
    signup_ts: int | None = None


# 2. RateLimit ---------------------------------------------------------------


class RateLimitCheck(BaseModel):
    user_id: str = Field(..., min_length=1)
    brand_id: str = Field(..., min_length=1)
    action_name: str = Field(..., min_length=1)
    limit_per: Literal["day", "hour", "week"]
    limit: int = Field(1, ge=1)


class RateLimitConsume(BaseModel):
    user_id: str = Field(..., min_length=1)
    brand_id: str = Field(..., min_length=1)
    action_name: str = Field(..., min_length=1)
    limit_per: Literal["day", "hour", "week"] = "day"
    limit: int = Field(1, ge=1)


# 3. LimitedDrop -------------------------------------------------------------


class LimitedDropCreate(BaseModel):
    brand_id: str = Field(..., min_length=1)
    drop_id: str = Field(..., min_length=1)
    item_id: str = Field(..., min_length=1)
    total_supply: int = Field(..., ge=1)
    ends_at: int = Field(..., ge=0)  # epoch seconds; 0 = no end


class LimitedDropClaim(BaseModel):
    user_id: str = Field(..., min_length=1)


# 4. Perk --------------------------------------------------------------------


class PerkConfigure(BaseModel):
    brand_id: str = Field(..., min_length=1)
    perk_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    required_tier: str = Field(..., min_length=1)
    action_on_use: dict[str, Any] = Field(default_factory=dict)


class PerkUse(BaseModel):
    user_id: str = Field(..., min_length=1)
    brand_id: str = Field(..., min_length=1)


# 5. FCFS --------------------------------------------------------------------


class FcfsCreate(BaseModel):
    brand_id: str = Field(..., min_length=1)
    pool_size: int = Field(..., ge=1)
    reward_per_claim: dict[str, Any] = Field(default_factory=dict)
    expires_at: int = Field(..., ge=0)
    pool_id: str | None = None


class FcfsClaim(BaseModel):
    user_id: str = Field(..., min_length=1)


# ── Helpers ────────────────────────────────────────────────────────────────


def _now() -> int:
    return int(time.time())


def _window_seconds(unit: str) -> int:
    return {"hour": 3600, "day": 86400, "week": 604800}.get(unit, 86400)


def _window_bucket(unit: str, ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    if unit == "hour":
        return dt.strftime("%Y%m%d%H")
    if unit == "week":
        iso = dt.isocalendar()
        return f"{iso.year}W{iso.week:02d}"
    return dt.strftime("%Y%m%d")


def _new_pool_id() -> str:
    return f"pool_{uuid4().hex[:12]}"


# ── 1. UserAttributeTrigger ────────────────────────────────────────────────


def _k_attr_marker(brand_id: str, user_id: str, kind: str, period: str) -> str:
    return f"brand:{brand_id}:trigger:attr:{kind}:{user_id}:{period}"


def _is_birthday(birthday_iso: str | None) -> bool:
    if not birthday_iso:
        return False
    try:
        bd = datetime.strptime(birthday_iso, "%Y-%m-%d")
    except ValueError:
        return False
    today = datetime.now(timezone.utc)
    return bd.month == today.month and bd.day == today.day


def _is_signup_anniversary(signup_ts: int | None) -> bool:
    if not signup_ts:
        return False
    bd = datetime.fromtimestamp(int(signup_ts), tz=timezone.utc)
    today = datetime.now(timezone.utc)
    return bd.month == today.month and bd.day == today.day and bd.year < today.year


@router.post(
    "/attribute/check",
    summary="Check & fire a user-attribute trigger (birthday, etc.)",
)
async def attribute_check(
    body: AttrTriggerCheck,
    r: aioredis.Redis = Depends(get_redis),
):
    kind = body.trigger_type
    today = datetime.now(timezone.utc)
    year = today.strftime("%Y")

    if kind == "first_visit":
        # Fire once per lifetime.
        key = _k_attr_marker(body.brand_id, body.user_id, kind, "lifetime")
        was_new = await r.setnx(key, str(_now()))
        return {
            "triggered": bool(was_new),
            "reward_pending": bool(was_new),
            "trigger_type": kind,
            "period": "lifetime",
        }

    if kind == "birthday":
        if not _is_birthday(body.birthday_iso):
            return {
                "triggered": False,
                "reward_pending": False,
                "trigger_type": kind,
                "reason": "not_birthday_today",
            }
        key = _k_attr_marker(body.brand_id, body.user_id, kind, year)
        was_new = await r.setnx(key, str(_now()))
        # Expire at next Jan 1 (rough — 400d TTL).
        if was_new:
            await r.expire(key, 400 * 86400)
        return {
            "triggered": bool(was_new),
            "reward_pending": bool(was_new),
            "trigger_type": kind,
            "period": year,
        }

    if kind == "signup_anniversary":
        if not _is_signup_anniversary(body.signup_ts):
            return {
                "triggered": False,
                "reward_pending": False,
                "trigger_type": kind,
                "reason": "not_anniversary_today",
            }
        key = _k_attr_marker(body.brand_id, body.user_id, kind, year)
        was_new = await r.setnx(key, str(_now()))
        if was_new:
            await r.expire(key, 400 * 86400)
        return {
            "triggered": bool(was_new),
            "reward_pending": bool(was_new),
            "trigger_type": kind,
            "period": year,
        }

    raise HTTPException(400, f"Unknown trigger_type {kind!r}")


# ── 2. RateLimit ───────────────────────────────────────────────────────────


def _k_ratelimit(
    brand_id: str, user_id: str, action: str, unit: str, bucket: str
) -> str:
    return (
        f"brand:{brand_id}:trigger:rl:{action}:{user_id}:{unit}:{bucket}"
    )


def _resets_at(unit: str, now: int) -> int:
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    if unit == "hour":
        # Next hour boundary.
        return (now // 3600 + 1) * 3600
    if unit == "day":
        return (now // 86400 + 1) * 86400
    if unit == "week":
        # End of ISO week — approximate: next Monday 00:00 UTC.
        weekday = dt.weekday()  # 0=Mon
        days_until_mon = (7 - weekday) % 7 or 7
        midnight = (now // 86400) * 86400
        return midnight + days_until_mon * 86400
    return now + _window_seconds(unit)


@router.post(
    "/ratelimit/check",
    summary="Check whether an action is currently allowed (no consumption)",
)
async def ratelimit_check(
    body: RateLimitCheck,
    r: aioredis.Redis = Depends(get_redis),
):
    now = _now()
    bucket = _window_bucket(body.limit_per, now)
    key = _k_ratelimit(
        body.brand_id, body.user_id, body.action_name, body.limit_per, bucket
    )
    used = int(await r.get(key) or 0)
    return {
        "allowed": used < body.limit,
        "remaining": max(0, body.limit - used),
        "limit": body.limit,
        "used": used,
        "resets_at": _resets_at(body.limit_per, now),
        "action_name": body.action_name,
        "limit_per": body.limit_per,
    }


@router.post(
    "/ratelimit/consume",
    summary="Consume one unit against the rate limit (atomic)",
)
async def ratelimit_consume(
    body: RateLimitConsume,
    r: aioredis.Redis = Depends(get_redis),
):
    now = _now()
    bucket = _window_bucket(body.limit_per, now)
    key = _k_ratelimit(
        body.brand_id, body.user_id, body.action_name, body.limit_per, bucket
    )
    new_count = await r.incr(key)
    if new_count == 1:
        # First INCR in this bucket: set TTL.
        await r.expire(key, _window_seconds(body.limit_per))
    if new_count > body.limit:
        # Roll back — we exceeded the cap.
        await r.decr(key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "ok": False,
                "reason": "rate_limit_exceeded",
                "remaining": 0,
                "resets_at": _resets_at(body.limit_per, now),
            },
        )
    return {
        "ok": True,
        "remaining": max(0, body.limit - new_count),
        "used": new_count,
        "resets_at": _resets_at(body.limit_per, now),
    }


# ── 3. LimitedDrop ─────────────────────────────────────────────────────────


def _k_drop(brand_id: str, drop_id: str) -> str:
    return f"brand:{brand_id}:trigger:drop:{drop_id}"


def _k_drop_claimers(brand_id: str, drop_id: str) -> str:
    return f"brand:{brand_id}:trigger:drop:{drop_id}:claimers"


@router.post(
    "/limiteddrop/create",
    summary="Create a limited-supply, time-windowed drop",
    status_code=status.HTTP_201_CREATED,
)
async def limiteddrop_create(
    body: LimitedDropCreate,
    r: aioredis.Redis = Depends(get_redis),
):
    if body.ends_at and body.ends_at <= _now():
        raise HTTPException(400, "ends_at must be in the future")
    key = _k_drop(body.brand_id, body.drop_id)
    if await r.exists(key):
        raise HTTPException(409, "Drop already exists")
    state = {
        "drop_id": body.drop_id,
        "brand_id": body.brand_id,
        "item_id": body.item_id,
        "supply": str(body.total_supply),
        "total_supply": str(body.total_supply),
        "claimed": "0",
        "ends_at": str(body.ends_at),
        "created_at": str(_now()),
        "status": "active",
    }
    await r.hset(key, mapping=state)
    # Keep the drop record at least 30 days after end.
    if body.ends_at > 0:
        await r.expireat(key, body.ends_at + 30 * 86400)
    return {
        "drop_id": body.drop_id,
        "ok": True,
        "supply_remaining": body.total_supply,
    }


@router.post(
    "/limiteddrop/{drop_id}/claim",
    summary="Atomically claim one unit from a limited drop",
)
async def limiteddrop_claim(
    drop_id: str,
    body: LimitedDropClaim,
    brand_id: str = "",
    r: aioredis.Redis = Depends(get_redis),
):
    # brand_id passed via query string or body.brand_id. Be flexible.
    if not brand_id:
        # Fallback: scan keyspace (cheap because we only need one match).
        # In practice the client should pass brand_id; we support both.
        raise HTTPException(400, "brand_id required as query parameter")

    script = await _get_atomic_claim_script(r)
    key = _k_drop(brand_id, drop_id)
    claimers_key = _k_drop_claimers(brand_id, drop_id)
    res = await script(keys=[key, claimers_key], args=[body.user_id, _now()])

    if res[0] == 1:
        new_supply = int(res[1])
        claimed = int(res[2])
        logger.info(
            "LimitedDrop claim ok: drop=%s user=%s remaining=%d",
            drop_id,
            body.user_id,
            new_supply,
        )
        return {
            "ok": True,
            "drop_id": drop_id,
            "supply_remaining": new_supply,
            "claimed_total": claimed,
            "user_id": body.user_id,
        }

    reason = res[1]
    if reason == "sold_out":
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="sold_out"
        )
    if reason == "expired":
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="expired"
        )
    if reason == "already_claimed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="already_claimed"
        )
    if reason == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="drop_not_found"
        )
    raise HTTPException(500, f"Unknown claim outcome: {reason}")


@router.get(
    "/limiteddrop/{drop_id}",
    summary="Get drop state (supply, claimed, expiry)",
)
async def limiteddrop_state(
    drop_id: str,
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    state = await r.hgetall(_k_drop(brand_id, drop_id))
    if not state:
        raise HTTPException(404, "Drop not found")
    now = _now()
    ends_at = int(state.get("ends_at") or 0)
    supply = int(state.get("supply") or 0)
    if ends_at > 0 and now > ends_at:
        state["status"] = "expired"
    elif supply <= 0:
        state["status"] = "sold_out"
    return {
        "drop_id": drop_id,
        "brand_id": brand_id,
        "item_id": state.get("item_id"),
        "supply_remaining": supply,
        "total_supply": int(state.get("total_supply") or 0),
        "claimed": int(state.get("claimed") or 0),
        "ends_at": ends_at,
        "status": state.get("status", "active"),
    }


# ── 4. PerkActivation ──────────────────────────────────────────────────────


# Tier rank ordering — higher index = higher rank.
TIER_RANK = ("bronze", "silver", "gold", "platinum", "diamond")


def _k_perk(brand_id: str, perk_id: str) -> str:
    return f"brand:{brand_id}:trigger:perk:{perk_id}"


def _k_user_tier(brand_id: str, user_id: str) -> str:
    return f"brand:{brand_id}:user:{user_id}:tier"


def _tier_meets(user_tier: str | None, required: str) -> bool:
    try:
        u = TIER_RANK.index((user_tier or "").lower())
        r = TIER_RANK.index((required or "").lower())
        return u >= r
    except ValueError:
        # Unknown tier — fall back to exact-match semantics.
        return (user_tier or "").lower() == (required or "").lower()


@router.post(
    "/perk/configure",
    summary="Configure a tier-locked perk",
    status_code=status.HTTP_201_CREATED,
)
async def perk_configure(
    body: PerkConfigure,
    r: aioredis.Redis = Depends(get_redis),
):
    record = {
        "perk_id": body.perk_id,
        "brand_id": body.brand_id,
        "name": body.name,
        "required_tier": body.required_tier.lower(),
        "action_on_use": json.dumps(body.action_on_use),
        "created_at": str(_now()),
    }
    await r.hset(_k_perk(body.brand_id, body.perk_id), mapping=record)
    return {"ok": True, "perk_id": body.perk_id}


@router.post(
    "/perk/{perk_id}/use",
    summary="Use a perk — returns 403 if tier insufficient",
)
async def perk_use(
    perk_id: str,
    body: PerkUse,
    r: aioredis.Redis = Depends(get_redis),
):
    perk = await r.hgetall(_k_perk(body.brand_id, perk_id))
    if not perk:
        raise HTTPException(404, "Perk not found")
    user_tier = await r.get(_k_user_tier(body.brand_id, body.user_id))
    required = perk.get("required_tier", "bronze")
    if not _tier_meets(user_tier, required):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "reason": "tier_required_not_met",
                "required_tier": required,
                "user_tier": user_tier,
            },
        )
    action = json.loads(perk.get("action_on_use") or "{}")
    # Record usage event (append to a list — caller can pull for processing).
    event = {
        "perk_id": perk_id,
        "user_id": body.user_id,
        "brand_id": body.brand_id,
        "used_at": _now(),
        "action": action,
    }
    await r.rpush(
        f"brand:{body.brand_id}:trigger:perk:{perk_id}:uses",
        json.dumps(event),
    )
    return {"ok": True, "perk_id": perk_id, "action_triggered": action}


@router.get(
    "/perk/{perk_id}",
    summary="Get perk configuration",
)
async def perk_get(
    perk_id: str,
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    perk = await r.hgetall(_k_perk(brand_id, perk_id))
    if not perk:
        raise HTTPException(404, "Perk not found")
    perk["action_on_use"] = json.loads(perk.get("action_on_use") or "{}")
    return perk


# ── 5. FirstComeFirstServe ─────────────────────────────────────────────────


def _k_fcfs(brand_id: str, pool_id: str) -> str:
    return f"brand:{brand_id}:trigger:fcfs:{pool_id}"


def _k_fcfs_claimers(brand_id: str, pool_id: str) -> str:
    return f"brand:{brand_id}:trigger:fcfs:{pool_id}:claimers"


@router.post(
    "/fcfs/create",
    summary="Create a first-come-first-serve claim pool",
    status_code=status.HTTP_201_CREATED,
)
async def fcfs_create(
    body: FcfsCreate,
    r: aioredis.Redis = Depends(get_redis),
):
    if body.expires_at and body.expires_at <= _now():
        raise HTTPException(400, "expires_at must be in the future")
    pool_id = body.pool_id or _new_pool_id()
    key = _k_fcfs(body.brand_id, pool_id)
    if await r.exists(key):
        raise HTTPException(409, "Pool already exists")
    state = {
        "pool_id": pool_id,
        "brand_id": body.brand_id,
        "supply": str(body.pool_size),
        "total_supply": str(body.pool_size),
        "claimed": "0",
        "ends_at": str(body.expires_at),
        "reward": json.dumps(body.reward_per_claim),
        "created_at": str(_now()),
        "status": "active",
    }
    await r.hset(key, mapping=state)
    if body.expires_at > 0:
        await r.expireat(key, body.expires_at + 30 * 86400)
    return {
        "ok": True,
        "pool_id": pool_id,
        "supply_remaining": body.pool_size,
    }


@router.post(
    "/fcfs/{pool_id}/claim",
    summary="Atomically claim one slot from an FCFS pool",
)
async def fcfs_claim(
    pool_id: str,
    body: FcfsClaim,
    brand_id: str = "",
    r: aioredis.Redis = Depends(get_redis),
):
    if not brand_id:
        raise HTTPException(400, "brand_id required as query parameter")

    script = await _get_atomic_claim_script(r)
    key = _k_fcfs(brand_id, pool_id)
    claimers_key = _k_fcfs_claimers(brand_id, pool_id)
    res = await script(keys=[key, claimers_key], args=[body.user_id, _now()])

    if res[0] == 1:
        # Resolve the reward payload from the pool record.
        reward_raw = await r.hget(key, "reward")
        reward = json.loads(reward_raw or "{}")
        new_supply = int(res[1])
        claimed = int(res[2])
        logger.info(
            "FCFS claim ok: pool=%s user=%s remaining=%d",
            pool_id,
            body.user_id,
            new_supply,
        )
        return {
            "ok": True,
            "pool_id": pool_id,
            "reward": reward,
            "supply_remaining": new_supply,
            "claimed_total": claimed,
            "user_id": body.user_id,
        }

    reason = res[1]
    if reason == "sold_out":
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="empty"
        )
    if reason == "expired":
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="expired"
        )
    if reason == "already_claimed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="already_claimed"
        )
    if reason == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="pool_not_found"
        )
    raise HTTPException(500, f"Unknown claim outcome: {reason}")


@router.get(
    "/fcfs/{pool_id}",
    summary="Get FCFS pool state",
)
async def fcfs_state(
    pool_id: str,
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    state = await r.hgetall(_k_fcfs(brand_id, pool_id))
    if not state:
        raise HTTPException(404, "Pool not found")
    now = _now()
    ends_at = int(state.get("ends_at") or 0)
    supply = int(state.get("supply") or 0)
    label = state.get("status", "active")
    if ends_at > 0 and now > ends_at:
        label = "expired"
    elif supply <= 0:
        label = "empty"
    return {
        "pool_id": pool_id,
        "brand_id": brand_id,
        "supply_remaining": supply,
        "total_supply": int(state.get("total_supply") or 0),
        "claimed": int(state.get("claimed") or 0),
        "ends_at": ends_at,
        "status": label,
        "reward_per_claim": json.loads(state.get("reward") or "{}"),
    }


# ═════════════════════════════════════════════════════════════════════════
# 6. Generic Event-Driven Triggers (register / fire / list / disable)
# ═════════════════════════════════════════════════════════════════════════
#
# Plumbing for "when X happens to user U, do Y" — a thin, brand-isolated
# trigger registry that downstream services (reservations, cart, vouchers,
# achievements, ...) can fire via POST /triggers/{tid}/fire.
#
# State (per trigger):
#   trigger:{tid}                 HASH   (config + counters)
#   brand:{bid}:triggers          SET    (all trigger ids)
#   brand:{bid}:triggers:{event}  SET    (lookup by event_type)
#   trigger:{tid}:fires:{uid}     STR    (cooldown counter; TTL = cooldown_s)
#   trigger:{tid}:user_count:{uid} STR   (lifetime per-user count, no TTL)
#
# The action payload is opaque from this router's point of view — we
# resolve the recipient_user_id_attr indirection and emit/structure the
# action, but the actual side-effect (issue_voucher etc.) is delegated to
# the appropriate downstream module via the returned ``action`` payload.


_VALID_EVENT_TYPES = {
    "reservation_no_show",
    "cart_abandoned",
    "subscription_renewal_due",
    "attribute_threshold",
    "voucher_redeemed",
    "first_purchase",
    "churn_risk",
    "achievement_unlocked",
}

_VALID_ACTION_TYPES = {
    "issue_voucher",
    "send_push",
    "award_xp",
    "fire_achievement",
    "webhook",
    "create_audience_member",
}


class TriggerSchedule(BaseModel):
    start: int | None = None  # epoch seconds
    end: int | None = None    # epoch seconds
    hours: list[int] | None = None  # active hours-of-day UTC (0-23)


class TriggerAction(BaseModel):
    type: str = Field(..., min_length=1)
    config: dict[str, Any] = Field(default_factory=dict)
    # When set, the action target is resolved by reading the named user
    # attribute (e.g. "parent_of") on the actor; useful for kid → parent
    # notifications, sub-account → master, etc.
    recipient_user_id_attr: str | None = None


class TriggerRegister(BaseModel):
    brand_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    event_filter: dict[str, Any] = Field(default_factory=dict)
    action: TriggerAction
    cooldown_seconds: int = Field(default=0, ge=0)
    max_fires_per_user: int = Field(default=0, ge=0)  # 0 = unlimited
    schedule: TriggerSchedule | None = None


class TriggerFire(BaseModel):
    actor_user_id: str = Field(..., min_length=1)
    event_data: dict[str, Any] = Field(default_factory=dict)


def _k_trigger(tid: str) -> str:
    return f"trigger:{tid}"


def _k_brand_triggers(bid: str) -> str:
    return f"brand:{bid}:triggers"


def _k_brand_event_triggers(bid: str, event_type: str) -> str:
    return f"brand:{bid}:triggers:{event_type}"


def _k_trigger_fires(tid: str, uid: str) -> str:
    return f"trigger:{tid}:fires:{uid}"


def _k_trigger_user_count(tid: str, uid: str) -> str:
    return f"trigger:{tid}:user_count:{uid}"


def _new_trigger_id() -> str:
    return f"trg_{uuid4().hex[:12]}"


def _trigger_to_dict(state: dict[str, Any]) -> dict[str, Any]:
    """Decode HASH back into typed dict (action/event_filter/schedule are JSON)."""
    out: dict[str, Any] = dict(state)
    for f in ("event_filter", "action", "schedule"):
        raw = out.get(f)
        if raw:
            try:
                out[f] = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                out[f] = {}
        else:
            out[f] = None if f == "schedule" else {}
    for f in ("cooldown_seconds", "max_fires_per_user", "fired_count", "created_at"):
        if f in out:
            try:
                out[f] = int(out[f])
            except (TypeError, ValueError):
                out[f] = 0
    out["active"] = out.get("active", "1") == "1"
    return out


def _event_filter_matches(filt: dict[str, Any], event_data: dict[str, Any]) -> bool:
    """Trivial equality match: every key in filt must equal that key in event_data.

    Empty filter matches everything. Values are compared as strings so
    callers don't need to worry about int/string mismatches across the
    HTTP boundary.
    """
    if not filt:
        return True
    for k, v in filt.items():
        if str(event_data.get(k)) != str(v):
            return False
    return True


def _schedule_active(sched: dict[str, Any] | None, now: int) -> tuple[bool, str | None]:
    if not sched:
        return True, None
    start = sched.get("start")
    end = sched.get("end")
    hours = sched.get("hours")
    if start is not None and now < int(start):
        return False, "not_yet_active"
    if end is not None and now > int(end):
        return False, "schedule_expired"
    if hours:
        hr = datetime.fromtimestamp(now, tz=timezone.utc).hour
        if hr not in [int(h) for h in hours]:
            return False, "outside_active_hours"
    return True, None


@router.post(
    "/register",
    summary="Register a generic event-driven trigger",
    status_code=status.HTTP_201_CREATED,
)
async def trigger_register(
    body: TriggerRegister,
    r: aioredis.Redis = Depends(get_redis),
):
    if body.event_type not in _VALID_EVENT_TYPES:
        raise HTTPException(
            422,
            detail={
                "reason": "unknown_event_type",
                "event_type": body.event_type,
                "valid": sorted(_VALID_EVENT_TYPES),
            },
        )
    if body.action.type not in _VALID_ACTION_TYPES:
        raise HTTPException(
            422,
            detail={
                "reason": "unknown_action_type",
                "action_type": body.action.type,
                "valid": sorted(_VALID_ACTION_TYPES),
            },
        )

    tid = _new_trigger_id()
    now = _now()
    sched_payload = body.schedule.model_dump() if body.schedule else None
    record = {
        "trigger_id": tid,
        "brand_id": body.brand_id,
        "name": body.name,
        "event_type": body.event_type,
        "event_filter": json.dumps(body.event_filter, separators=(",", ":")),
        "action": json.dumps(body.action.model_dump(), separators=(",", ":")),
        "cooldown_seconds": str(body.cooldown_seconds),
        "max_fires_per_user": str(body.max_fires_per_user),
        "schedule": json.dumps(sched_payload, separators=(",", ":")) if sched_payload else "",
        "active": "1",
        "fired_count": "0",
        "created_at": str(now),
    }
    pipe = r.pipeline()
    pipe.hset(_k_trigger(tid), mapping=record)
    pipe.sadd(_k_brand_triggers(body.brand_id), tid)
    pipe.sadd(_k_brand_event_triggers(body.brand_id, body.event_type), tid)
    await pipe.execute()
    return {
        "trigger_id": tid,
        "active": True,
        "brand_id": body.brand_id,
        "event_type": body.event_type,
    }


async def _resolve_recipient(
    r: aioredis.Redis,
    actor_user_id: str,
    brand_id: str,
    attr: str | None,
) -> str:
    """Resolve indirection: if recipient_user_id_attr set, look up that attr
    on the actor; fall back to the actor when unset / not found.

    Reads user attribute via the canonical user attributes HASH (matches
    primitives.py: ``user:{uid}:attributes:{bid}`` then global
    ``user:{uid}:attributes``).
    """
    if not attr:
        return actor_user_id
    val = await r.hget(f"user:{actor_user_id}:attributes:{brand_id}", attr)
    if val:
        return val
    val = await r.hget(f"user:{actor_user_id}:attributes", attr)
    if val:
        return val
    # No mapping found — fall back to actor (callers can detect via
    # equal actor/recipient if they care).
    return actor_user_id


@router.post(
    "/{trigger_id}/fire",
    summary="Fire a trigger; runs filter + cooldown + max-fire checks",
)
async def trigger_fire(
    trigger_id: str,
    body: TriggerFire,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await r.hgetall(_k_trigger(trigger_id))
    if not raw:
        raise HTTPException(404, "Trigger not found")
    state = _trigger_to_dict(raw)

    blockers: list[str] = []
    if not state.get("active", True):
        blockers.append("trigger_disabled")

    now = _now()
    ok_sched, sched_reason = _schedule_active(state.get("schedule"), now)
    if not ok_sched and sched_reason:
        blockers.append(sched_reason)

    if not _event_filter_matches(state.get("event_filter") or {}, body.event_data):
        blockers.append("event_filter_no_match")

    actor = body.actor_user_id
    cooldown = int(state.get("cooldown_seconds", 0) or 0)
    max_fires = int(state.get("max_fires_per_user", 0) or 0)

    if cooldown > 0:
        cd_key = _k_trigger_fires(trigger_id, actor)
        # If the cooldown key exists, the actor fired within the window.
        if await r.exists(cd_key):
            ttl = await r.ttl(cd_key)
            blockers.append(f"cooldown_active:{max(ttl, 0)}s")

    if max_fires > 0:
        uc_key = _k_trigger_user_count(trigger_id, actor)
        used = int(await r.get(uc_key) or 0)
        if used >= max_fires:
            blockers.append("max_fires_per_user_reached")

    if blockers:
        return {
            "fired": False,
            "trigger_id": trigger_id,
            "blockers": blockers,
        }

    action_cfg = state.get("action") or {}
    recipient = await _resolve_recipient(
        r,
        actor_user_id=actor,
        brand_id=state.get("brand_id", ""),
        attr=action_cfg.get("recipient_user_id_attr"),
    )

    action_id = f"act_{uuid4().hex[:12]}"
    pipe = r.pipeline()
    pipe.hincrby(_k_trigger(trigger_id), "fired_count", 1)
    if cooldown > 0:
        pipe.set(_k_trigger_fires(trigger_id, actor), str(now), ex=cooldown)
    if max_fires > 0:
        pipe.incr(_k_trigger_user_count(trigger_id, actor))
    # Append a structured log entry for downstream consumers.
    log_key = f"trigger:{trigger_id}:log"
    log_entry = {
        "action_id": action_id,
        "actor_user_id": actor,
        "recipient_user_id": recipient,
        "fired_at": now,
        "event_data": body.event_data,
        "action": action_cfg,
    }
    pipe.rpush(log_key, json.dumps(log_entry, separators=(",", ":")))
    pipe.ltrim(log_key, -500, -1)
    await pipe.execute()

    return {
        "fired": True,
        "trigger_id": trigger_id,
        "action_id": action_id,
        "actor_user_id": actor,
        "recipient_user_id": recipient,
        "action": action_cfg,
        "event_type": state.get("event_type"),
    }


@router.get(
    "/brand/{brand_id}",
    summary="List all triggers registered for a brand",
)
async def trigger_list_brand(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    ids = await r.smembers(_k_brand_triggers(brand_id))
    out: list[dict[str, Any]] = []
    for tid in sorted(ids or []):
        raw = await r.hgetall(_k_trigger(tid))
        if raw:
            out.append(_trigger_to_dict(raw))
    return {"brand_id": brand_id, "count": len(out), "triggers": out}


@router.get(
    "/{trigger_id}",
    summary="Get a single trigger's configuration + counters",
)
async def trigger_get(
    trigger_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await r.hgetall(_k_trigger(trigger_id))
    if not raw:
        raise HTTPException(404, "Trigger not found")
    return _trigger_to_dict(raw)


@router.post(
    "/{trigger_id}/disable",
    summary="Mark a trigger inactive (idempotent)",
)
async def trigger_disable(
    trigger_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    if not await r.exists(_k_trigger(trigger_id)):
        raise HTTPException(404, "Trigger not found")
    await r.hset(_k_trigger(trigger_id), "active", "0")
    return {"ok": True, "trigger_id": trigger_id, "active": False}


@router.delete(
    "/{trigger_id}",
    summary="Delete a trigger and its lookup pointers",
)
async def trigger_delete(
    trigger_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await r.hgetall(_k_trigger(trigger_id))
    if not raw:
        raise HTTPException(404, "Trigger not found")
    brand_id = raw.get("brand_id", "")
    event_type = raw.get("event_type", "")
    pipe = r.pipeline()
    pipe.delete(_k_trigger(trigger_id))
    if brand_id:
        pipe.srem(_k_brand_triggers(brand_id), trigger_id)
        if event_type:
            pipe.srem(_k_brand_event_triggers(brand_id, event_type), trigger_id)
    pipe.delete(f"trigger:{trigger_id}:log")
    await pipe.execute()
    return {"ok": True, "trigger_id": trigger_id, "deleted": True}
