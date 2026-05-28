"""Group Actions — Pinduoduo-style viral mechanics for KiX.

Three modules, all brand-isolated via Redis key namespacing:

    1. GroupBuy      — N people buy together for a discount (拼团)
    2. GroupAtomic   — Generic N-of-N: N people complete action X in
                       window Y, or all fail
    3. PriceCut      — 砍一刀: each friend click reduces the price; if
                       target reached within window, initiator wins

The most powerful viral mechanic in the world: a group MUST complete
the same action within a time window, OR all members fail together.
This creates a positive-sum incentive to recruit, because every member
of the group is now selling for you.

Pure state machine + Redis. No LLM. Idempotent joins. Atomic check-
and-set on group full / time expired.
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Constants ──────────────────────────────────────────────────────────────

DEFAULT_LANDING_BASE = "https://kix.app"
DEFAULT_GROUP_SIZE = 5
DEFAULT_BUY_WINDOW_MINUTES = 24 * 60
DEFAULT_ATOMIC_WINDOW_MINUTES = 60
DEFAULT_PRICECUT_WINDOW_HOURS = 24
DEFAULT_MAX_CUTS = 20
MAX_GROUP_SIZE = 100
MAX_WINDOW_MINUTES = 7 * 24 * 60  # 7 days

STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed_timeout"

KIND_BUY = "buy"
KIND_ATOMIC = "atomic"
KIND_PRICECUT = "pricecut"


# ── Pydantic models ────────────────────────────────────────────────────────


class GroupBuyCreate(BaseModel):
    brand_id: str
    sku_id: str
    group_size: int = Field(default=DEFAULT_GROUP_SIZE, ge=2, le=MAX_GROUP_SIZE)
    discount_percent: int = Field(default=50, ge=1, le=99)
    window_minutes: int = Field(
        default=DEFAULT_BUY_WINDOW_MINUTES, ge=1, le=MAX_WINDOW_MINUTES
    )
    initiator_user_id: str
    base_url: str | None = None


class GroupBuyJoin(BaseModel):
    user_id: str


class GroupAtomicCreate(BaseModel):
    brand_id: str
    action_name: str
    group_size: int = Field(default=DEFAULT_GROUP_SIZE, ge=2, le=MAX_GROUP_SIZE)
    window_minutes: int = Field(
        default=DEFAULT_ATOMIC_WINDOW_MINUTES, ge=1, le=MAX_WINDOW_MINUTES
    )
    reward: dict[str, Any] = Field(default_factory=dict)
    initiator: str
    base_url: str | None = None


class GroupAtomicJoin(BaseModel):
    user_id: str
    action_proof: dict[str, Any] = Field(default_factory=dict)


class PriceCutCreate(BaseModel):
    brand_id: str
    sku_id: str
    original_price_cents: int = Field(..., ge=1)
    target_price_cents: int = Field(default=0, ge=0)
    max_cuts: int = Field(default=DEFAULT_MAX_CUTS, ge=1, le=100)
    window_hours: int = Field(default=DEFAULT_PRICECUT_WINDOW_HOURS, ge=1, le=24 * 14)
    initiator: str
    base_url: str | None = None


class PriceCutHelp(BaseModel):
    helper_user_id: str


# ── Redis key helpers ──────────────────────────────────────────────────────


def _k_group(kind: str, group_id: str) -> str:
    return f"group:{kind}:{group_id}"


def _k_brand_active(brand_id: str) -> str:
    return f"brand:{brand_id}:groups:active"


def _k_user_groups(user_id: str) -> str:
    return f"user:{user_id}:groups"


def _k_pending(user_id: str, brand_id: str) -> str:
    """Pending action ops queue for (user, brand)."""
    return f"brand:{brand_id}:user:{user_id}:pending_actions"


# ── Core helpers ───────────────────────────────────────────────────────────


def _now() -> int:
    return int(time.time())


def _new_group_id() -> str:
    return uuid4().hex[:14]


def _resolve_base(base_url: str | None) -> str:
    return (base_url or DEFAULT_LANDING_BASE).rstrip("/")


def _share_url(base_url: str | None, brand_id: str, group_id: str) -> str:
    base = _resolve_base(base_url)
    return f"{base}/landing/play.html?brand={brand_id}&group={group_id}"


def _decode_members(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return list(v) if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []


def _decode_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}


async def _load_group(
    r: aioredis.Redis, kind: str, group_id: str
) -> dict[str, str]:
    """Load the raw HASH for a group, raising 404 if missing."""
    if not group_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="group_id is required",
        )
    data = await r.hgetall(_k_group(kind, group_id))
    if not data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Group {group_id} not found",
        )
    return data


def _time_left(expires_at: int) -> int:
    return max(0, expires_at - _now())


async def _mark_expired_if_needed(
    r: aioredis.Redis, kind: str, group_id: str, data: dict[str, str]
) -> dict[str, str]:
    """If active but past expiry, mark failed_timeout in place."""
    if data.get("status") != STATUS_ACTIVE:
        return data
    expires_at = int(data.get("expires_at", "0") or 0)
    if expires_at and _now() >= expires_at:
        # Atomically transition to failed (only if still active)
        pipe = r.pipeline()
        pipe.hget(_k_group(kind, group_id), "status")
        cur = await pipe.execute()
        if cur and cur[0] == STATUS_ACTIVE:
            await r.hset(_k_group(kind, group_id), "status", STATUS_FAILED)
            data["status"] = STATUS_FAILED
            # Remove from brand active set
            brand_id = data.get("brand_id", "")
            if brand_id:
                await r.srem(_k_brand_active(brand_id), group_id)
    return data


async def _push_action(
    r: aioredis.Redis,
    *,
    user_id: str,
    brand_id: str,
    action: dict[str, Any],
    source: str,
    group_id: str,
) -> None:
    """Append an action payload to the user's pending_actions list."""
    payload = {
        "source": source,
        "group_id": group_id,
        "granted_at": _now(),
        "action": action,
    }
    await r.rpush(_k_pending(user_id, brand_id), json.dumps(payload))


# ──────────────────────────────────────────────────────────────────────────
# 1. GroupBuy
# ──────────────────────────────────────────────────────────────────────────


@router.post("/buy/create")
async def group_buy_create(
    body: GroupBuyCreate,
    r: aioredis.Redis = Depends(get_redis),
):
    """Create a new GroupBuy. Initiator is the first member."""
    group_id = _new_group_id()
    now = _now()
    ttl = body.window_minutes * 60
    expires_at = now + ttl
    members = [body.initiator_user_id]

    record = {
        "kind": KIND_BUY,
        "brand_id": body.brand_id,
        "sku_id": body.sku_id,
        "initiator": body.initiator_user_id,
        "members": json.dumps(members),
        "status": STATUS_ACTIVE,
        "group_size": str(body.group_size),
        "window_minutes": str(body.window_minutes),
        "discount_percent": str(body.discount_percent),
        "created_at": str(now),
        "expires_at": str(expires_at),
    }
    key = _k_group(KIND_BUY, group_id)
    pipe = r.pipeline()
    pipe.hset(key, mapping=record)
    pipe.expire(key, ttl + 86400)  # keep a day past expiry for status queries
    pipe.sadd(_k_brand_active(body.brand_id), group_id)
    pipe.expire(_k_brand_active(body.brand_id), ttl + 86400)
    pipe.sadd(_k_user_groups(body.initiator_user_id), group_id)
    await pipe.execute()

    return {
        "group_id": group_id,
        "share_url": _share_url(body.base_url, body.brand_id, group_id),
        "expires_at": expires_at,
        "members_count": 1,
        "members_needed": body.group_size,
        "status": STATUS_ACTIVE,
    }


@router.post("/buy/{group_id}/join")
async def group_buy_join(
    group_id: str,
    body: GroupBuyJoin,
    r: aioredis.Redis = Depends(get_redis),
):
    """Join an existing GroupBuy. Idempotent — same user can't join twice."""
    data = await _load_group(r, KIND_BUY, group_id)
    data = await _mark_expired_if_needed(r, KIND_BUY, group_id, data)

    status_now = data.get("status", STATUS_ACTIVE)
    members = _decode_members(data.get("members"))
    group_size = int(data.get("group_size", str(DEFAULT_GROUP_SIZE)))
    expires_at = int(data.get("expires_at", "0") or 0)

    if status_now == STATUS_FAILED:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Group expired before completion",
        )
    if status_now == STATUS_COMPLETED:
        # Idempotent: re-joining a completed group as an existing member is OK.
        if body.user_id in members:
            return {
                "ok": True,
                "members_count": len(members),
                "members_needed": group_size,
                "time_left": _time_left(expires_at),
                "status": status_now,
                "already_member": True,
            }
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Group already completed",
        )

    # Idempotent join: existing member returns current state, not an error.
    if body.user_id in members:
        return {
            "ok": True,
            "members_count": len(members),
            "members_needed": group_size,
            "time_left": _time_left(expires_at),
            "status": status_now,
            "already_member": True,
        }

    if len(members) >= group_size:
        # Race condition: full but not yet flipped to completed.
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Group is already full",
        )

    members.append(body.user_id)
    new_count = len(members)
    new_status = STATUS_COMPLETED if new_count >= group_size else STATUS_ACTIVE

    pipe = r.pipeline()
    pipe.hset(
        _k_group(KIND_BUY, group_id),
        mapping={"members": json.dumps(members), "status": new_status},
    )
    pipe.sadd(_k_user_groups(body.user_id), group_id)
    if new_status == STATUS_COMPLETED:
        brand_id = data.get("brand_id", "")
        if brand_id:
            pipe.srem(_k_brand_active(brand_id), group_id)
    await pipe.execute()

    return {
        "ok": True,
        "members_count": new_count,
        "members_needed": group_size,
        "time_left": _time_left(expires_at),
        "status": new_status,
    }


@router.get("/buy/{group_id}")
async def group_buy_get(
    group_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Return current state of a GroupBuy."""
    data = await _load_group(r, KIND_BUY, group_id)
    data = await _mark_expired_if_needed(r, KIND_BUY, group_id, data)
    members = _decode_members(data.get("members"))
    group_size = int(data.get("group_size", str(DEFAULT_GROUP_SIZE)))
    expires_at = int(data.get("expires_at", "0") or 0)
    return {
        "group_id": group_id,
        "brand_id": data.get("brand_id", ""),
        "sku_id": data.get("sku_id", ""),
        "initiator": data.get("initiator", ""),
        "status": data.get("status", STATUS_ACTIVE),
        "members": members,
        "members_count": len(members),
        "members_needed": group_size,
        "discount_percent": int(data.get("discount_percent", "0") or 0),
        "expires_at": expires_at,
        "time_left": _time_left(expires_at),
    }


@router.post("/buy/{group_id}/checkout")
async def group_buy_checkout(
    group_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Finalise a GroupBuy. If members met → grant discount to all and
    mark completed. If timer expired → mark failed.

    Idempotent: re-checking out a completed group returns the same payload
    but does not re-grant vouchers (we use a checkout_done flag).
    """
    data = await _load_group(r, KIND_BUY, group_id)
    data = await _mark_expired_if_needed(r, KIND_BUY, group_id, data)

    status_now = data.get("status", STATUS_ACTIVE)
    members = _decode_members(data.get("members"))
    group_size = int(data.get("group_size", str(DEFAULT_GROUP_SIZE)))
    discount = int(data.get("discount_percent", "0") or 0)
    brand_id = data.get("brand_id", "")
    sku_id = data.get("sku_id", "")
    expires_at = int(data.get("expires_at", "0") or 0)
    already = data.get("checkout_done") == "1"

    if status_now == STATUS_FAILED:
        return {
            "ok": False,
            "status": STATUS_FAILED,
            "reason": "timeout",
            "members_count": len(members),
            "members_needed": group_size,
            "time_left": 0,
        }

    if len(members) < group_size:
        # Not yet met. If still active and not expired, signal "needs more".
        return {
            "ok": False,
            "status": status_now,
            "reason": "incomplete",
            "members_count": len(members),
            "members_needed": group_size,
            "time_left": _time_left(expires_at),
        }

    # Group is full → complete (idempotent)
    if not already:
        # Grant voucher to each member
        for uid in members:
            await _push_action(
                r,
                user_id=uid,
                brand_id=brand_id,
                action={
                    "module": "voucher",
                    "method": "grant",
                    "voucher": {
                        "type": "group_buy_discount",
                        "discount_percent": discount,
                        "sku_id": sku_id,
                    },
                },
                source=f"group_buy:{group_id}",
                group_id=group_id,
            )
        # Mark checkout_done + completed
        pipe = r.pipeline()
        pipe.hset(
            _k_group(KIND_BUY, group_id),
            mapping={"status": STATUS_COMPLETED, "checkout_done": "1"},
        )
        if brand_id:
            pipe.srem(_k_brand_active(brand_id), group_id)
        await pipe.execute()

    return {
        "ok": True,
        "status": STATUS_COMPLETED,
        "members_count": len(members),
        "members_needed": group_size,
        "discount_percent": discount,
        "voucher_granted_to": members,
        "idempotent": already,
    }


# ──────────────────────────────────────────────────────────────────────────
# 2. GroupAtomic — generic N-of-N
# ──────────────────────────────────────────────────────────────────────────


@router.post("/atomic/create")
async def group_atomic_create(
    body: GroupAtomicCreate,
    r: aioredis.Redis = Depends(get_redis),
):
    """Create a generic atomic group: N people complete action X within
    window Y or all fail.

    Initiator counts as a participant once they record their own proof via
    /join (initiator can call /join with their own user_id).
    """
    group_id = _new_group_id()
    now = _now()
    ttl = body.window_minutes * 60
    expires_at = now + ttl

    record = {
        "kind": KIND_ATOMIC,
        "brand_id": body.brand_id,
        "action_name": body.action_name,
        "initiator": body.initiator,
        "members": json.dumps([body.initiator]),
        "proofs": json.dumps({}),  # user_id → proof dict
        "status": STATUS_ACTIVE,
        "group_size": str(body.group_size),
        "window_minutes": str(body.window_minutes),
        "reward": json.dumps(body.reward or {}),
        "created_at": str(now),
        "expires_at": str(expires_at),
    }
    key = _k_group(KIND_ATOMIC, group_id)
    pipe = r.pipeline()
    pipe.hset(key, mapping=record)
    pipe.expire(key, ttl + 86400)
    pipe.sadd(_k_brand_active(body.brand_id), group_id)
    pipe.expire(_k_brand_active(body.brand_id), ttl + 86400)
    pipe.sadd(_k_user_groups(body.initiator), group_id)
    await pipe.execute()

    return {
        "group_id": group_id,
        "share_url": _share_url(body.base_url, body.brand_id, group_id),
        "expires_at": expires_at,
        "action_name": body.action_name,
        "current": 1,
        "target": body.group_size,
        "status": STATUS_ACTIVE,
    }


@router.post("/atomic/{group_id}/join")
async def group_atomic_join(
    group_id: str,
    body: GroupAtomicJoin,
    r: aioredis.Redis = Depends(get_redis),
):
    """Record a user's action proof toward an atomic group's target."""
    data = await _load_group(r, KIND_ATOMIC, group_id)
    data = await _mark_expired_if_needed(r, KIND_ATOMIC, group_id, data)

    status_now = data.get("status", STATUS_ACTIVE)
    members = _decode_members(data.get("members"))
    proofs = _decode_dict(data.get("proofs"))
    group_size = int(data.get("group_size", str(DEFAULT_GROUP_SIZE)))
    expires_at = int(data.get("expires_at", "0") or 0)
    brand_id = data.get("brand_id", "")

    if status_now == STATUS_FAILED:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Atomic group expired before completion",
        )
    if status_now == STATUS_COMPLETED:
        if body.user_id in members:
            return {
                "ok": True,
                "current": len(members),
                "target": group_size,
                "time_left": _time_left(expires_at),
                "status": status_now,
                "already_member": True,
            }
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Atomic group already completed",
        )

    if body.user_id in members:
        # Idempotent: same user joining again returns state, no double-count.
        # Optionally update their proof (latest wins).
        if body.action_proof:
            proofs[body.user_id] = body.action_proof
            await r.hset(
                _k_group(KIND_ATOMIC, group_id),
                "proofs",
                json.dumps(proofs),
            )
        return {
            "ok": True,
            "current": len(members),
            "target": group_size,
            "time_left": _time_left(expires_at),
            "status": status_now,
            "already_member": True,
        }

    if len(members) >= group_size:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Atomic group already full",
        )

    members.append(body.user_id)
    if body.action_proof:
        proofs[body.user_id] = body.action_proof

    current = len(members)
    new_status = STATUS_COMPLETED if current >= group_size else STATUS_ACTIVE

    pipe = r.pipeline()
    pipe.hset(
        _k_group(KIND_ATOMIC, group_id),
        mapping={
            "members": json.dumps(members),
            "proofs": json.dumps(proofs),
            "status": new_status,
        },
    )
    pipe.sadd(_k_user_groups(body.user_id), group_id)
    if new_status == STATUS_COMPLETED and brand_id:
        pipe.srem(_k_brand_active(brand_id), group_id)
    await pipe.execute()

    # On completion, distribute the configured reward to every member.
    if new_status == STATUS_COMPLETED:
        reward = _decode_dict(data.get("reward"))
        # If the reward dict declares its own module/method, pass it through.
        # Otherwise wrap it as a generic "atomic_reward" payload.
        if reward.get("module"):
            action = reward
        else:
            action = {
                "module": "atomic_reward",
                "method": "grant",
                "payload": reward,
            }
        for uid in members:
            await _push_action(
                r,
                user_id=uid,
                brand_id=brand_id,
                action=action,
                source=f"group_atomic:{group_id}",
                group_id=group_id,
            )
        # Mark distribution complete for idempotency
        await r.hset(
            _k_group(KIND_ATOMIC, group_id), "reward_granted", "1"
        )

    return {
        "ok": True,
        "current": current,
        "target": group_size,
        "time_left": _time_left(expires_at),
        "status": new_status,
    }


@router.get("/atomic/{group_id}")
async def group_atomic_get(
    group_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Return current state of an atomic group."""
    data = await _load_group(r, KIND_ATOMIC, group_id)
    data = await _mark_expired_if_needed(r, KIND_ATOMIC, group_id, data)
    members = _decode_members(data.get("members"))
    proofs = _decode_dict(data.get("proofs"))
    group_size = int(data.get("group_size", str(DEFAULT_GROUP_SIZE)))
    expires_at = int(data.get("expires_at", "0") or 0)
    return {
        "group_id": group_id,
        "brand_id": data.get("brand_id", ""),
        "action_name": data.get("action_name", ""),
        "initiator": data.get("initiator", ""),
        "status": data.get("status", STATUS_ACTIVE),
        "members": members,
        "proofs": proofs,
        "current": len(members),
        "target": group_size,
        "reward": _decode_dict(data.get("reward")),
        "expires_at": expires_at,
        "time_left": _time_left(expires_at),
    }


# ──────────────────────────────────────────────────────────────────────────
# 3. PriceCut (砍一刀)
# ──────────────────────────────────────────────────────────────────────────


def _generate_cut_amounts(total: int, n_cuts: int) -> list[int]:
    """Generate n_cuts random positive integers that sum exactly to total.

    Front-loaded so first cuts feel large (Pinduoduo psychology).
    """
    if n_cuts <= 0 or total <= 0:
        return []
    if n_cuts == 1:
        return [total]
    # Use a falling weight curve: weight[i] = (n_cuts - i)^1.5
    weights = [max(1.0, (n_cuts - i) ** 1.5) for i in range(n_cuts)]
    s = sum(weights)
    raw = [w / s * total for w in weights]
    cuts = [max(1, int(x)) for x in raw]
    # Reconcile rounding drift by adjusting the last cut
    drift = total - sum(cuts)
    if drift != 0:
        cuts[-1] = max(1, cuts[-1] + drift)
    # Final safety: total must equal sum
    diff = total - sum(cuts)
    if diff != 0:
        cuts[-1] += diff
    return cuts


@router.post("/pricecut/create")
async def pricecut_create(
    body: PriceCutCreate,
    r: aioredis.Redis = Depends(get_redis),
):
    """Create a 砍一刀 group: each helper click reduces price toward target."""
    if body.target_price_cents >= body.original_price_cents:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="target_price_cents must be less than original_price_cents",
        )
    group_id = _new_group_id()
    now = _now()
    ttl = body.window_hours * 3600
    expires_at = now + ttl
    discount_total = body.original_price_cents - body.target_price_cents
    # Pre-compute the schedule of cut amounts. Each helper draws the next one.
    schedule = _generate_cut_amounts(discount_total, body.max_cuts)

    record = {
        "kind": KIND_PRICECUT,
        "brand_id": body.brand_id,
        "sku_id": body.sku_id,
        "initiator": body.initiator,
        "original_price_cents": str(body.original_price_cents),
        "target_price_cents": str(body.target_price_cents),
        "current_price_cents": str(body.original_price_cents),
        "max_cuts": str(body.max_cuts),
        "cuts_done": "0",
        "window_hours": str(body.window_hours),
        "schedule": json.dumps(schedule),
        "helpers": json.dumps([]),
        "status": STATUS_ACTIVE,
        "created_at": str(now),
        "expires_at": str(expires_at),
    }
    key = _k_group(KIND_PRICECUT, group_id)
    pipe = r.pipeline()
    pipe.hset(key, mapping=record)
    pipe.expire(key, ttl + 86400)
    pipe.sadd(_k_brand_active(body.brand_id), group_id)
    pipe.expire(_k_brand_active(body.brand_id), ttl + 86400)
    pipe.sadd(_k_user_groups(body.initiator), group_id)
    await pipe.execute()

    return {
        "group_id": group_id,
        "share_url": _share_url(body.base_url, body.brand_id, group_id),
        "original_price_cents": body.original_price_cents,
        "target_price_cents": body.target_price_cents,
        "current_price_cents": body.original_price_cents,
        "max_cuts": body.max_cuts,
        "cuts_remaining": body.max_cuts,
        "expires_at": expires_at,
        "status": STATUS_ACTIVE,
    }


@router.post("/pricecut/{group_id}/cut")
async def pricecut_help(
    group_id: str,
    body: PriceCutHelp,
    r: aioredis.Redis = Depends(get_redis),
):
    """Apply one cut by a helper. Idempotent — same helper can't double-cut."""
    data = await _load_group(r, KIND_PRICECUT, group_id)
    data = await _mark_expired_if_needed(r, KIND_PRICECUT, group_id, data)

    status_now = data.get("status", STATUS_ACTIVE)
    cuts_done = int(data.get("cuts_done", "0") or 0)
    max_cuts = int(data.get("max_cuts", str(DEFAULT_MAX_CUTS)))
    current_price = int(data.get("current_price_cents", "0") or 0)
    target_price = int(data.get("target_price_cents", "0") or 0)
    helpers = _decode_members(data.get("helpers"))
    schedule = _decode_members(data.get("schedule"))  # list[int]
    expires_at = int(data.get("expires_at", "0") or 0)
    initiator = data.get("initiator", "")
    brand_id = data.get("brand_id", "")
    sku_id = data.get("sku_id", "")

    if status_now == STATUS_FAILED:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="PriceCut group expired before target reached",
        )
    if status_now == STATUS_COMPLETED:
        # Idempotent: helper already in helpers gets a no-op success
        return {
            "ok": True,
            "new_price_cents": current_price,
            "cuts_remaining": max(0, max_cuts - cuts_done),
            "target_reached": True,
            "status": status_now,
            "already_cut": body.helper_user_id in helpers,
        }

    if body.helper_user_id == initiator:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Initiator cannot cut their own price",
        )
    if body.helper_user_id in helpers:
        return {
            "ok": True,
            "new_price_cents": current_price,
            "cuts_remaining": max(0, max_cuts - cuts_done),
            "target_reached": current_price <= target_price,
            "status": status_now,
            "already_cut": True,
        }
    if cuts_done >= max_cuts:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="No cuts remaining",
        )

    # Draw next cut from the schedule (or fallback random small amount).
    if cuts_done < len(schedule):
        cut_amount = int(schedule[cuts_done])
    else:
        # Defensive: small random fallback
        cut_amount = max(1, random.randint(1, max(1, current_price - target_price)))

    new_price = max(target_price, current_price - cut_amount)
    cuts_done += 1
    helpers.append(body.helper_user_id)
    target_reached = new_price <= target_price
    new_status = STATUS_COMPLETED if target_reached else STATUS_ACTIVE

    pipe = r.pipeline()
    pipe.hset(
        _k_group(KIND_PRICECUT, group_id),
        mapping={
            "current_price_cents": str(new_price),
            "cuts_done": str(cuts_done),
            "helpers": json.dumps(helpers),
            "status": new_status,
        },
    )
    pipe.sadd(_k_user_groups(body.helper_user_id), group_id)
    if new_status == STATUS_COMPLETED and brand_id:
        pipe.srem(_k_brand_active(brand_id), group_id)
    await pipe.execute()

    # When target reached: grant the discounted voucher to the initiator.
    if new_status == STATUS_COMPLETED and not data.get("reward_granted"):
        await _push_action(
            r,
            user_id=initiator,
            brand_id=brand_id,
            action={
                "module": "voucher",
                "method": "grant",
                "voucher": {
                    "type": "pricecut_reward",
                    "sku_id": sku_id,
                    "final_price_cents": new_price,
                    "original_price_cents": int(
                        data.get("original_price_cents", "0") or 0
                    ),
                    "cuts_used": cuts_done,
                },
            },
            source=f"pricecut:{group_id}",
            group_id=group_id,
        )
        # Bonus: helpers get a small thank-you XP / energy ping
        for uid in helpers:
            await _push_action(
                r,
                user_id=uid,
                brand_id=brand_id,
                action={
                    "module": "xp",
                    "method": "grant",
                    "amount": 10,
                    "reason": "pricecut_help_success",
                },
                source=f"pricecut:{group_id}",
                group_id=group_id,
            )
        await r.hset(
            _k_group(KIND_PRICECUT, group_id), "reward_granted", "1"
        )

    return {
        "ok": True,
        "new_price_cents": new_price,
        "cut_amount_cents": cut_amount,
        "cuts_done": cuts_done,
        "cuts_remaining": max(0, max_cuts - cuts_done),
        "target_reached": target_reached,
        "status": new_status,
        "time_left": _time_left(expires_at),
    }


@router.get("/pricecut/{group_id}")
async def pricecut_get(
    group_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Return current state of a PriceCut group."""
    data = await _load_group(r, KIND_PRICECUT, group_id)
    data = await _mark_expired_if_needed(r, KIND_PRICECUT, group_id, data)
    helpers = _decode_members(data.get("helpers"))
    max_cuts = int(data.get("max_cuts", str(DEFAULT_MAX_CUTS)))
    cuts_done = int(data.get("cuts_done", "0") or 0)
    expires_at = int(data.get("expires_at", "0") or 0)
    current_price = int(data.get("current_price_cents", "0") or 0)
    target_price = int(data.get("target_price_cents", "0") or 0)
    return {
        "group_id": group_id,
        "brand_id": data.get("brand_id", ""),
        "sku_id": data.get("sku_id", ""),
        "initiator": data.get("initiator", ""),
        "status": data.get("status", STATUS_ACTIVE),
        "original_price_cents": int(data.get("original_price_cents", "0") or 0),
        "current_price_cents": current_price,
        "target_price_cents": target_price,
        "target_reached": current_price <= target_price,
        "max_cuts": max_cuts,
        "cuts_done": cuts_done,
        "cuts_remaining": max(0, max_cuts - cuts_done),
        "helpers": helpers,
        "expires_at": expires_at,
        "time_left": _time_left(expires_at),
    }


# ──────────────────────────────────────────────────────────────────────────
# Listings / utilities
# ──────────────────────────────────────────────────────────────────────────


@router.get("/brand/{brand_id}/active")
async def list_brand_active(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """List all active group_ids for a brand (any kind)."""
    members = await r.smembers(_k_brand_active(brand_id))
    return {
        "brand_id": brand_id,
        "active_group_ids": sorted(members),
        "count": len(members),
    }


@router.get("/user/{user_id}/groups")
async def list_user_groups(
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """List all group_ids a user is participating in (any kind)."""
    members = await r.smembers(_k_user_groups(user_id))
    return {
        "user_id": user_id,
        "group_ids": sorted(members),
        "count": len(members),
    }


@router.post("/sweep-expired")
async def sweep_expired(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Scan a brand's active set and transition any past-expiry groups to
    failed_timeout. Safe to call repeatedly (idempotent).

    This is the optional cron job hook — call it periodically (e.g. every
    minute) to release stuck "active" rows whose timer has already passed.
    """
    active = await r.smembers(_k_brand_active(brand_id))
    swept = []
    for gid in active:
        # We don't know the kind from the set; try each kind.
        for kind in (KIND_BUY, KIND_ATOMIC, KIND_PRICECUT):
            key = _k_group(kind, gid)
            data = await r.hgetall(key)
            if not data:
                continue
            before = data.get("status")
            await _mark_expired_if_needed(r, kind, gid, data)
            after = data.get("status")
            if before != after:
                swept.append({"group_id": gid, "kind": kind, "new_status": after})
            break  # found the right kind
    return {"brand_id": brand_id, "swept": swept, "count": len(swept)}
