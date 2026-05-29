"""Subscriptions — first-class SaaS / membership / streaming primitive.

PROBLEM: KiX doesn't model recurring revenue. Every "subscription" today
is faked with one-shot ``transactions`` events, which means NDR (Net
Dollar Retention) and GRR (Gross Revenue Retention) — the two KPIs every
SaaS investor asks for — are uncomputable. Seat-based B2B plans have
nowhere to store seat counts; upgrade / downgrade / proration logic
lives in merchants' heads.

This router introduces the ``subscription`` aggregate, supports both
user-owned and account-owned (B2B) subscriptions, tracks the lifecycle
events (create / upgrade / downgrade / seat-change / cancel / renew),
and rolls those events into ARR / NDR / GRR metrics on a per-brand
basis.

Key schema
----------
    subscription:{sid}                       HASH   — subscription record
    subscription:{sid}:history               LIST   — JSON audit events (LPUSH)
    user:{uid}:subscriptions                 SET
    account:{aid}:subscriptions              SET
    brand:{bid}:subscriptions:active         ZSET   — score = mrr_cents
    brand:{bid}:subscriptions:cancelled      SET
    brand:{bid}:metrics:{period}:{bucket}    HASH   — ARR movements bucket
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Constants ──────────────────────────────────────────────────────────────

SUB_ID_PREFIX = "sub_"
SUB_ID_NIBBLES = 16

VALID_CYCLES = {"monthly", "quarterly", "annual"}
CYCLE_MONTHS = {"monthly": 1, "quarterly": 3, "annual": 12}

VALID_STATUSES = {
    "active",     # billing & in good standing
    "cancelled",  # terminated (immediate or end_of_period)
    "expired",    # past expires_at, didn't renew
    "paused",     # voluntary pause, not billing
}

HISTORY_MAX_LEN = 500

# Metrics movement labels — these are what every SaaS dashboard renders.
MOVEMENT_NEW = "new"
MOVEMENT_EXPANSION = "expansion"
MOVEMENT_CONTRACTION = "contraction"
MOVEMENT_CHURN = "churn"


# ── Pydantic models ────────────────────────────────────────────────────────


class SubscriptionCreateRequest(BaseModel):
    user_id: str | None = None
    account_id: str | None = None
    brand_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    monthly_amount_cents: int = Field(ge=0)
    currency: str = Field(default="CNY", min_length=3, max_length=8)
    seats: int = Field(default=1, ge=1)
    billing_cycle: Literal["monthly", "quarterly", "annual"] = "monthly"
    starts_at: float
    expires_at: float | None = None
    auto_renew: bool = True
    payment_method_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_owner(self) -> "SubscriptionCreateRequest":
        if not self.user_id and not self.account_id:
            raise ValueError("user_id_or_account_id_required")
        return self


class SubscriptionResponse(BaseModel):
    subscription_id: str
    user_id: str | None
    account_id: str | None
    brand_id: str
    plan_id: str
    monthly_amount_cents: int
    currency: str
    seats: int
    billing_cycle: str
    status: str
    starts_at: float
    expires_at: float | None
    auto_renew: bool
    payment_method_id: str | None
    mrr_cents: int
    created_at: float
    updated_at: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpgradeRequest(BaseModel):
    new_plan_id: str = Field(min_length=1)
    new_monthly_amount_cents: int = Field(ge=0)
    prorated: bool = True


class DowngradeRequest(BaseModel):
    new_plan_id: str = Field(min_length=1)
    new_monthly_amount_cents: int = Field(ge=0)
    effective: Literal["immediate", "end_of_period"] = "end_of_period"


class CancelRequest(BaseModel):
    effective: Literal["immediate", "end_of_period"] = "end_of_period"
    reason: str = Field(default="unspecified", max_length=200)


class SeatChangeRequest(BaseModel):
    new_seat_count: int = Field(ge=1)


class RenewRequest(BaseModel):
    payment_method_id: str | None = None


class SubscriptionMutationResponse(BaseModel):
    subscription_id: str
    status: str
    mrr_cents: int
    delta_mrr_cents: int
    effective_at: float
    movement: str | None = None


class SubscriptionListResponse(BaseModel):
    count: int
    subscriptions: list[SubscriptionResponse]


class BrandMetricsResponse(BaseModel):
    brand_id: str
    period: str
    arr_start_cents: int
    arr_end_cents: int
    new_arr_cents: int
    expansion_arr_cents: int
    contraction_arr_cents: int
    churn_arr_cents: int
    ndr: float                # net dollar retention (1.0 = flat)
    grr: float                # gross revenue retention (≤ 1.0)
    customer_count: int
    logo_churn_rate: float
    expansion_rate: float


# ── Helpers ────────────────────────────────────────────────────────────────


def _now() -> float:
    return time.time()


def _new_sub_id() -> str:
    return f"{SUB_ID_PREFIX}{secrets.token_hex(SUB_ID_NIBBLES // 2)}"


def _mrr_from_cycle(monthly_amount_cents: int, cycle: str, seats: int) -> int:
    """MRR is normalized to monthly regardless of billing cycle."""
    return int(monthly_amount_cents) * max(int(seats), 1)


def _period_bucket(period: str, ts: float) -> str:
    """Bucket a timestamp for ARR rollups. Day-resolution is enough."""
    import datetime as _dt
    dt = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    if period == "monthly":
        return dt.strftime("%Y-%m")
    if period == "quarterly":
        q = (dt.month - 1) // 3 + 1
        return f"{dt.year}-Q{q}"
    if period == "annual":
        return dt.strftime("%Y")
    return dt.strftime("%Y-%m")


async def _require_subscription(
    r: aioredis.Redis, subscription_id: str
) -> dict[str, str]:
    raw = await r.hgetall(f"subscription:{subscription_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="subscription_not_found")
    return raw


def _decode_sub(sid: str, raw: dict[str, str]) -> SubscriptionResponse:
    def _f(k: str, default: float = 0.0) -> float:
        try:
            return float(raw.get(k, default) or default)
        except (TypeError, ValueError):
            return default

    def _i(k: str, default: int = 0) -> int:
        try:
            return int(raw.get(k, default) or default)
        except (TypeError, ValueError):
            return default

    try:
        metadata = json.loads(raw.get("metadata") or "{}")
        if not isinstance(metadata, dict):
            metadata = {}
    except json.JSONDecodeError:
        metadata = {}

    return SubscriptionResponse(
        subscription_id=sid,
        user_id=raw.get("user_id") or None,
        account_id=raw.get("account_id") or None,
        brand_id=raw.get("brand_id", ""),
        plan_id=raw.get("plan_id", ""),
        monthly_amount_cents=_i("monthly_amount_cents"),
        currency=raw.get("currency", "CNY"),
        seats=_i("seats", 1),
        billing_cycle=raw.get("billing_cycle", "monthly"),
        status=raw.get("status", "active"),
        starts_at=_f("starts_at"),
        expires_at=_f("expires_at") or None,
        auto_renew=(raw.get("auto_renew", "true") in ("1", "true", "True")),
        payment_method_id=raw.get("payment_method_id") or None,
        mrr_cents=_i("mrr_cents"),
        created_at=_f("created_at"),
        updated_at=_f("updated_at"),
        metadata=metadata,
    )


async def _append_history(
    r: aioredis.Redis,
    subscription_id: str,
    event: str,
    payload: dict[str, Any],
) -> None:
    entry = json.dumps(
        {"ts": _now(), "event": event, **payload}, separators=(",", ":"),
    )
    key = f"subscription:{subscription_id}:history"
    pipe = r.pipeline(transaction=True)
    pipe.lpush(key, entry)
    pipe.ltrim(key, 0, HISTORY_MAX_LEN - 1)
    await pipe.execute()


async def _record_movement(
    r: aioredis.Redis,
    brand_id: str,
    movement: str,
    delta_cents: int,
    ts: float,
    *,
    logo_delta: int = 0,
) -> None:
    """Bucket a single movement (new/expansion/contraction/churn) into
    monthly + quarterly + annual rollups. Delta is MRR cents; ARR is
    derived at query time as MRR × 12."""
    if delta_cents == 0 and logo_delta == 0:
        return
    for period in ("monthly", "quarterly", "annual"):
        bucket = _period_bucket(period, ts)
        key = f"brand:{brand_id}:metrics:{period}:{bucket}"
        pipe = r.pipeline(transaction=True)
        if delta_cents:
            pipe.hincrby(key, f"{movement}_mrr_cents", delta_cents)
        if logo_delta:
            pipe.hincrby(key, f"{movement}_logos", logo_delta)
        await pipe.execute()


# ── Endpoints — lifecycle ──────────────────────────────────────────────────


@router.post("/create", response_model=SubscriptionResponse)
async def create_subscription(
    req: SubscriptionCreateRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Mint a subscription. Either user_id or account_id is required.

    The MRR is materialized on the record so ARR rollups don't need to
    recompute amount × seats on every read.
    """
    if req.billing_cycle not in VALID_CYCLES:
        raise HTTPException(status_code=400, detail="invalid_billing_cycle")

    sid = _new_sub_id()
    now = _now()
    mrr = _mrr_from_cycle(req.monthly_amount_cents, req.billing_cycle, req.seats)

    mapping = {
        "subscription_id": sid,
        "user_id": req.user_id or "",
        "account_id": req.account_id or "",
        "brand_id": req.brand_id,
        "plan_id": req.plan_id,
        "monthly_amount_cents": str(int(req.monthly_amount_cents)),
        "currency": req.currency,
        "seats": str(int(req.seats)),
        "billing_cycle": req.billing_cycle,
        "status": "active",
        "starts_at": f"{req.starts_at:.6f}",
        "expires_at": f"{req.expires_at:.6f}" if req.expires_at else "",
        "auto_renew": "true" if req.auto_renew else "false",
        "payment_method_id": req.payment_method_id or "",
        "mrr_cents": str(mrr),
        "created_at": f"{now:.6f}",
        "updated_at": f"{now:.6f}",
        "metadata": json.dumps(req.metadata or {}, separators=(",", ":")),
    }

    pipe = r.pipeline(transaction=True)
    pipe.hset(f"subscription:{sid}", mapping=mapping)
    if req.user_id:
        pipe.sadd(f"user:{req.user_id}:subscriptions", sid)
    if req.account_id:
        pipe.sadd(f"account:{req.account_id}:subscriptions", sid)
    pipe.zadd(f"brand:{req.brand_id}:subscriptions:active", {sid: mrr})
    await pipe.execute()

    await _append_history(r, sid, "create", {
        "plan_id": req.plan_id, "mrr_cents": mrr, "seats": req.seats,
    })
    await _record_movement(
        r, req.brand_id, MOVEMENT_NEW, mrr, now, logo_delta=1,
    )

    return _decode_sub(sid, mapping)


@router.get("/{sub_id}", response_model=SubscriptionResponse)
async def get_subscription(
    sub_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await _require_subscription(r, sub_id)
    return _decode_sub(sub_id, raw)


@router.post("/{sub_id}/upgrade", response_model=SubscriptionMutationResponse)
async def upgrade_subscription(
    sub_id: str,
    req: UpgradeRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Plan-level expansion. Records the positive MRR delta as ``expansion``."""
    raw = await _require_subscription(r, sub_id)
    if raw.get("status") != "active":
        raise HTTPException(status_code=409, detail="subscription_not_active")

    old_mrr = int(raw.get("mrr_cents", 0) or 0)
    seats = int(raw.get("seats", 1) or 1)
    cycle = raw.get("billing_cycle", "monthly")
    new_mrr = _mrr_from_cycle(req.new_monthly_amount_cents, cycle, seats)
    if new_mrr < old_mrr:
        raise HTTPException(status_code=400, detail="upgrade_must_increase_mrr")
    delta = new_mrr - old_mrr
    now = _now()
    brand_id = raw.get("brand_id", "")

    await r.hset(f"subscription:{sub_id}", mapping={
        "plan_id": req.new_plan_id,
        "monthly_amount_cents": str(int(req.new_monthly_amount_cents)),
        "mrr_cents": str(new_mrr),
        "updated_at": f"{now:.6f}",
    })
    await r.zadd(f"brand:{brand_id}:subscriptions:active", {sub_id: new_mrr})
    await _append_history(r, sub_id, "upgrade", {
        "old_plan": raw.get("plan_id"), "new_plan": req.new_plan_id,
        "delta_mrr_cents": delta, "prorated": req.prorated,
    })
    if delta > 0:
        await _record_movement(r, brand_id, MOVEMENT_EXPANSION, delta, now)

    return SubscriptionMutationResponse(
        subscription_id=sub_id, status="active", mrr_cents=new_mrr,
        delta_mrr_cents=delta, effective_at=now, movement=MOVEMENT_EXPANSION,
    )


@router.post("/{sub_id}/downgrade", response_model=SubscriptionMutationResponse)
async def downgrade_subscription(
    sub_id: str,
    req: DowngradeRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await _require_subscription(r, sub_id)
    if raw.get("status") != "active":
        raise HTTPException(status_code=409, detail="subscription_not_active")

    old_mrr = int(raw.get("mrr_cents", 0) or 0)
    seats = int(raw.get("seats", 1) or 1)
    cycle = raw.get("billing_cycle", "monthly")
    new_mrr = _mrr_from_cycle(req.new_monthly_amount_cents, cycle, seats)
    if new_mrr > old_mrr:
        raise HTTPException(status_code=400, detail="downgrade_must_decrease_mrr")
    delta = new_mrr - old_mrr  # negative or zero
    now = _now()
    brand_id = raw.get("brand_id", "")

    # Scheduled (end_of_period) downgrades are *queued* — the record stays
    # at the old MRR until renewal time, but we stash the pending plan.
    if req.effective == "end_of_period":
        await r.hset(f"subscription:{sub_id}", mapping={
            "pending_plan_id": req.new_plan_id,
            "pending_monthly_amount_cents": str(int(req.new_monthly_amount_cents)),
            "pending_effective": "end_of_period",
            "updated_at": f"{now:.6f}",
        })
        await _append_history(r, sub_id, "downgrade_scheduled", {
            "old_plan": raw.get("plan_id"), "new_plan": req.new_plan_id,
            "delta_mrr_cents": delta, "effective": "end_of_period",
        })
        return SubscriptionMutationResponse(
            subscription_id=sub_id, status="active", mrr_cents=old_mrr,
            delta_mrr_cents=0, effective_at=now, movement=None,
        )

    # Immediate: contract MRR right away.
    await r.hset(f"subscription:{sub_id}", mapping={
        "plan_id": req.new_plan_id,
        "monthly_amount_cents": str(int(req.new_monthly_amount_cents)),
        "mrr_cents": str(new_mrr),
        "updated_at": f"{now:.6f}",
    })
    await r.zadd(f"brand:{brand_id}:subscriptions:active", {sub_id: new_mrr})
    await _append_history(r, sub_id, "downgrade", {
        "old_plan": raw.get("plan_id"), "new_plan": req.new_plan_id,
        "delta_mrr_cents": delta, "effective": "immediate",
    })
    if delta < 0:
        await _record_movement(r, brand_id, MOVEMENT_CONTRACTION, abs(delta), now)
    return SubscriptionMutationResponse(
        subscription_id=sub_id, status="active", mrr_cents=new_mrr,
        delta_mrr_cents=delta, effective_at=now,
        movement=MOVEMENT_CONTRACTION if delta < 0 else None,
    )


@router.post("/{sub_id}/seat-change", response_model=SubscriptionMutationResponse)
async def seat_change(
    sub_id: str,
    req: SeatChangeRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Seat-based expansion/contraction. Per-seat price stays constant;
    new MRR = monthly_amount × new_seat_count."""
    raw = await _require_subscription(r, sub_id)
    if raw.get("status") != "active":
        raise HTTPException(status_code=409, detail="subscription_not_active")

    old_seats = int(raw.get("seats", 1) or 1)
    if req.new_seat_count == old_seats:
        return SubscriptionMutationResponse(
            subscription_id=sub_id,
            status="active",
            mrr_cents=int(raw.get("mrr_cents", 0) or 0),
            delta_mrr_cents=0,
            effective_at=_now(),
            movement=None,
        )
    per_seat = int(raw.get("monthly_amount_cents", 0) or 0)
    cycle = raw.get("billing_cycle", "monthly")
    new_mrr = _mrr_from_cycle(per_seat, cycle, req.new_seat_count)
    old_mrr = int(raw.get("mrr_cents", 0) or 0)
    delta = new_mrr - old_mrr
    now = _now()
    brand_id = raw.get("brand_id", "")

    await r.hset(f"subscription:{sub_id}", mapping={
        "seats": str(int(req.new_seat_count)),
        "mrr_cents": str(new_mrr),
        "updated_at": f"{now:.6f}",
    })
    await r.zadd(f"brand:{brand_id}:subscriptions:active", {sub_id: new_mrr})
    await _append_history(r, sub_id, "seat_change", {
        "old_seats": old_seats, "new_seats": req.new_seat_count,
        "delta_mrr_cents": delta,
    })
    movement: str | None = None
    if delta > 0:
        movement = MOVEMENT_EXPANSION
        await _record_movement(r, brand_id, MOVEMENT_EXPANSION, delta, now)
    elif delta < 0:
        movement = MOVEMENT_CONTRACTION
        await _record_movement(r, brand_id, MOVEMENT_CONTRACTION, abs(delta), now)

    return SubscriptionMutationResponse(
        subscription_id=sub_id, status="active", mrr_cents=new_mrr,
        delta_mrr_cents=delta, effective_at=now, movement=movement,
    )


@router.post("/{sub_id}/cancel", response_model=SubscriptionMutationResponse)
async def cancel_subscription(
    sub_id: str,
    req: CancelRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await _require_subscription(r, sub_id)
    if raw.get("status") not in ("active", "paused"):
        raise HTTPException(status_code=409, detail="subscription_not_cancellable")

    mrr = int(raw.get("mrr_cents", 0) or 0)
    now = _now()
    brand_id = raw.get("brand_id", "")

    if req.effective == "end_of_period":
        # Don't touch MRR — they keep paying through period end. We just
        # flip auto_renew off and stash the cancellation intent. Churn is
        # only recorded at actual termination (renewal worker handles it).
        await r.hset(f"subscription:{sub_id}", mapping={
            "auto_renew": "false",
            "pending_cancel": "true",
            "cancel_reason": req.reason,
            "updated_at": f"{now:.6f}",
        })
        await _append_history(r, sub_id, "cancel_scheduled", {
            "reason": req.reason, "effective": "end_of_period",
        })
        return SubscriptionMutationResponse(
            subscription_id=sub_id, status="active", mrr_cents=mrr,
            delta_mrr_cents=0, effective_at=now, movement=None,
        )

    # Immediate cancel — strip MRR, record churn.
    pipe = r.pipeline(transaction=True)
    pipe.hset(f"subscription:{sub_id}", mapping={
        "status": "cancelled",
        "auto_renew": "false",
        "cancel_reason": req.reason,
        "mrr_cents": "0",
        "updated_at": f"{now:.6f}",
    })
    pipe.zrem(f"brand:{brand_id}:subscriptions:active", sub_id)
    pipe.sadd(f"brand:{brand_id}:subscriptions:cancelled", sub_id)
    await pipe.execute()
    await _append_history(r, sub_id, "cancel", {
        "reason": req.reason, "effective": "immediate", "churned_mrr_cents": mrr,
    })
    if mrr > 0:
        await _record_movement(
            r, brand_id, MOVEMENT_CHURN, mrr, now, logo_delta=1,
        )
    return SubscriptionMutationResponse(
        subscription_id=sub_id, status="cancelled", mrr_cents=0,
        delta_mrr_cents=-mrr, effective_at=now, movement=MOVEMENT_CHURN,
    )


@router.post("/{sub_id}/renew", response_model=SubscriptionMutationResponse)
async def renew_subscription(
    sub_id: str,
    req: RenewRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Extend ``expires_at`` by one cycle. If a downgrade was scheduled
    for end_of_period, apply it now. If cancel was scheduled, terminate."""
    raw = await _require_subscription(r, sub_id)
    if raw.get("status") not in ("active",):
        raise HTTPException(status_code=409, detail="subscription_not_renewable")

    now = _now()
    brand_id = raw.get("brand_id", "")

    # Apply scheduled cancel.
    if raw.get("pending_cancel") == "true":
        mrr = int(raw.get("mrr_cents", 0) or 0)
        pipe = r.pipeline(transaction=True)
        pipe.hset(f"subscription:{sub_id}", mapping={
            "status": "cancelled", "mrr_cents": "0",
            "updated_at": f"{now:.6f}",
        })
        pipe.zrem(f"brand:{brand_id}:subscriptions:active", sub_id)
        pipe.sadd(f"brand:{brand_id}:subscriptions:cancelled", sub_id)
        await pipe.execute()
        await _append_history(r, sub_id, "cancel_applied", {
            "churned_mrr_cents": mrr,
        })
        if mrr > 0:
            await _record_movement(
                r, brand_id, MOVEMENT_CHURN, mrr, now, logo_delta=1,
            )
        return SubscriptionMutationResponse(
            subscription_id=sub_id, status="cancelled", mrr_cents=0,
            delta_mrr_cents=-mrr, effective_at=now, movement=MOVEMENT_CHURN,
        )

    # Apply pending downgrade at the period boundary.
    old_mrr = int(raw.get("mrr_cents", 0) or 0)
    delta = 0
    movement: str | None = None
    new_mrr = old_mrr
    if raw.get("pending_plan_id"):
        seats = int(raw.get("seats", 1) or 1)
        cycle = raw.get("billing_cycle", "monthly")
        per_seat = int(raw.get("pending_monthly_amount_cents", 0) or 0)
        new_mrr = _mrr_from_cycle(per_seat, cycle, seats)
        delta = new_mrr - old_mrr
        await r.hset(f"subscription:{sub_id}", mapping={
            "plan_id": raw["pending_plan_id"],
            "monthly_amount_cents": str(per_seat),
            "mrr_cents": str(new_mrr),
            "pending_plan_id": "",
            "pending_monthly_amount_cents": "",
            "pending_effective": "",
        })
        await r.zadd(f"brand:{brand_id}:subscriptions:active", {sub_id: new_mrr})
        if delta < 0:
            movement = MOVEMENT_CONTRACTION
            await _record_movement(
                r, brand_id, MOVEMENT_CONTRACTION, abs(delta), now,
            )

    # Push expires_at forward by one cycle.
    cycle = raw.get("billing_cycle", "monthly")
    months = CYCLE_MONTHS.get(cycle, 1)
    seconds = months * 30 * 86400
    try:
        old_expires = float(raw.get("expires_at") or now)
    except (TypeError, ValueError):
        old_expires = now
    new_expires = max(old_expires, now) + seconds
    updates: dict[str, str] = {
        "expires_at": f"{new_expires:.6f}",
        "updated_at": f"{now:.6f}",
    }
    if req.payment_method_id:
        updates["payment_method_id"] = req.payment_method_id
    await r.hset(f"subscription:{sub_id}", mapping=updates)
    await _append_history(r, sub_id, "renew", {
        "new_expires_at": new_expires, "delta_mrr_cents": delta,
    })

    return SubscriptionMutationResponse(
        subscription_id=sub_id, status="active", mrr_cents=new_mrr,
        delta_mrr_cents=delta, effective_at=now, movement=movement,
    )


# ── Endpoints — listing ────────────────────────────────────────────────────


async def _hydrate(r: aioredis.Redis, sids: list[str]) -> list[SubscriptionResponse]:
    out: list[SubscriptionResponse] = []
    for sid in sids:
        raw = await r.hgetall(f"subscription:{sid}")
        if raw:
            out.append(_decode_sub(sid, raw))
    return out


@router.get("/user/{user_id}", response_model=SubscriptionListResponse)
async def list_user_subscriptions(
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    sids = list(await r.smembers(f"user:{user_id}:subscriptions"))
    subs = await _hydrate(r, sids)
    subs.sort(key=lambda s: s.created_at, reverse=True)
    return SubscriptionListResponse(count=len(subs), subscriptions=subs)


@router.get("/account/{account_id}", response_model=SubscriptionListResponse)
async def list_account_subscriptions(
    account_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    sids = list(await r.smembers(f"account:{account_id}:subscriptions"))
    subs = await _hydrate(r, sids)
    subs.sort(key=lambda s: s.created_at, reverse=True)
    return SubscriptionListResponse(count=len(subs), subscriptions=subs)


@router.get("/brand/{brand_id}/active", response_model=SubscriptionListResponse)
async def list_brand_active_subscriptions(
    brand_id: str,
    limit: int = Query(default=200, ge=1, le=2000),
    r: aioredis.Redis = Depends(get_redis),
):
    """Active subscriptions sorted by MRR descending — top accounts first."""
    sids = await r.zrevrange(
        f"brand:{brand_id}:subscriptions:active", 0, limit - 1,
    )
    subs = await _hydrate(r, sids)
    return SubscriptionListResponse(count=len(subs), subscriptions=subs)


# ── Endpoints — metrics ────────────────────────────────────────────────────


def _ndr_from_movements(
    arr_start: int, expansion: int, contraction: int, churn: int
) -> float:
    """NDR = (start_ARR + expansion − contraction − churn) / start_ARR.

    >= 1.0 means net expansion (best-in-class SaaS lives at 1.20+).
    """
    if arr_start <= 0:
        return 0.0
    return (arr_start + expansion - contraction - churn) / arr_start


def _grr_from_movements(arr_start: int, contraction: int, churn: int) -> float:
    """GRR = (start_ARR − contraction − churn) / start_ARR. Always ≤ 1.0."""
    if arr_start <= 0:
        return 0.0
    return max(0.0, (arr_start - contraction - churn) / arr_start)


@router.get(
    "/brand/{brand_id}/metrics",
    response_model=BrandMetricsResponse,
)
async def brand_metrics(
    brand_id: str,
    period: Literal["monthly", "quarterly", "annual"] = Query(default="monthly"),
    from_ts: float | None = Query(default=None, alias="from"),
    r: aioredis.Redis = Depends(get_redis),
):
    """NDR / GRR for a brand over the requested period.

    Bucket is derived from ``from_ts`` (defaults to *now* → current bucket).
    ARR is MRR × 12; the live "ARR end" is the current sum of active-set MRRs,
    while "ARR start" is reconstructed as ARR_end − movements_in_bucket.
    """
    now = _now()
    ts = from_ts if from_ts is not None else now
    bucket = _period_bucket(period, ts)
    key = f"brand:{brand_id}:metrics:{period}:{bucket}"
    raw = await r.hgetall(key)

    def _g(field: str) -> int:
        try:
            return int(raw.get(field, 0) or 0)
        except (TypeError, ValueError):
            return 0

    new_mrr = _g("new_mrr_cents")
    expansion_mrr = _g("expansion_mrr_cents")
    contraction_mrr = _g("contraction_mrr_cents")
    churn_mrr = _g("churn_mrr_cents")

    # ARR end ≈ live MRR sum × 12 (current snapshot).
    active_set = f"brand:{brand_id}:subscriptions:active"
    customer_count = await r.zcard(active_set)
    members = await r.zrange(active_set, 0, -1, withscores=True)
    live_mrr = sum(int(score) for _, score in members) if members else 0
    arr_end = live_mrr * 12

    # ARR start = ARR end − (new + expansion) + (contraction + churn)
    arr_start = max(
        0,
        arr_end
        - new_mrr * 12
        - expansion_mrr * 12
        + contraction_mrr * 12
        + churn_mrr * 12,
    )

    ndr = _ndr_from_movements(
        arr_start, expansion_mrr * 12, contraction_mrr * 12, churn_mrr * 12,
    )
    grr = _grr_from_movements(
        arr_start, contraction_mrr * 12, churn_mrr * 12,
    )

    new_logos = _g("new_logos")
    churn_logos = _g("churn_logos")
    starting_logos = max(0, customer_count - new_logos + churn_logos)
    logo_churn_rate = (churn_logos / starting_logos) if starting_logos > 0 else 0.0
    expansion_rate = (expansion_mrr / arr_start * 12.0) if arr_start > 0 else 0.0

    return BrandMetricsResponse(
        brand_id=brand_id,
        period=f"{period}:{bucket}",
        arr_start_cents=arr_start,
        arr_end_cents=arr_end,
        new_arr_cents=new_mrr * 12,
        expansion_arr_cents=expansion_mrr * 12,
        contraction_arr_cents=contraction_mrr * 12,
        churn_arr_cents=churn_mrr * 12,
        ndr=round(ndr, 6),
        grr=round(grr, 6),
        customer_count=customer_count,
        logo_churn_rate=round(logo_churn_rate, 6),
        expansion_rate=round(expansion_rate, 6),
    )


@router.get("/health")
async def subscriptions_health(r: aioredis.Redis = Depends(get_redis)):
    pong = await r.ping()
    return {"ok": bool(pong), "module": "subscriptions"}
