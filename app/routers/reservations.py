"""Reservation / Booking primitive — future-dated commitments + no-show
trigger module for KiX.

This unlocks restaurants, fitness studios, salons, travel, events — any
brand whose product is a *promise to show up*. The no-show flow is the
critical recovery surface: when a user misses a booking, we auto-issue
a recovery voucher and emit an event the existing triggers/conditions
engine can react to.

Pipeline:
    POST /create                      → reservation:{rid} HASH
                                        brand:{bid}:reservations ZSET (score=ts)
                                        user:{uid}:reservations ZSET
                                        emit reservation.created
    POST /{rid}/check-in              → status=honored, fire attribution
                                        emit reservation.honored
    POST /{rid}/cancel                → policy-based status transition
                                        emit reservation.cancelled_by_{by}
    POST /{rid}/reschedule            → updates scheduled_at + ZSCORE
                                        emit reservation.rescheduled
    GET  /{rid}                       → single reservation
    GET  /brand/{bid}                 → filterable brand list
    GET  /user/{uid}                  → filterable user list
    POST /scan-no-shows               → admin cron-like: marks no_show +
                                        issues recovery vouchers
    POST /admin/policy/configure      → brand-level defaults
    GET  /brand/{bid}/stats           → counters + no-show rate
    POST /triggers/register           → subscribe to reservation events

Redis schema:
    reservation:{rid}                 HASH (full state, see _hash_to_dict)
    brand:{bid}:reservations          ZSET score=scheduled_at
    user:{uid}:reservations           ZSET score=scheduled_at
    brand:{bid}:reservation_stats     HASH counters
    brand:{bid}:reservation_policy    HASH brand defaults
    brand:{bid}:reservation_triggers  LIST of trigger config JSON blobs
    events:reservation                STREAM (XADD) — generic event bus

Integrations:
    * Geofence: when user enters store geofence AND has confirmed reservation
      within next 30 min → auto check-in (helper exposed: maybe_auto_check_in).
    * Attribution: honored reservation emits a visit + completed conversion
      event via the pixel hook (best-effort).
    * Frequency cap: pre-arrival pushes from this module are tagged
      ``exempt_from_cap=True`` so callers know to bypass caps.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ─────────────────────────────────────────────────────────────

_DEFAULT_GRACE_MINUTES = 15
_DEFAULT_FREE_BEFORE_HOURS = 24
_DEFAULT_PARTIAL_REFUND_PCT = 50
_EVENT_STREAM = "events:reservation"
_EVENT_STREAM_MAXLEN = 50_000  # approximate trim

_RESERVATION_TYPES = (
    "dining",
    "fitness_class",
    "appointment",
    "event",
    "tour",
    "service",
)
_STATUSES = (
    "confirmed",
    "honored",
    "no_show",
    "cancelled_by_user",
    "cancelled_by_brand",
    "cancelled_with_penalty",
    "rescheduled",
)
_ACTION_TYPES = ("issue_voucher", "send_push", "award_xp", "webhook")
_EVENT_TYPES = (
    "reservation.created",
    "reservation.confirmed",
    "reservation.honored",
    "reservation.no_show",
    "reservation.cancelled_by_user",
    "reservation.cancelled_by_brand",
    "reservation.rescheduled",
)


# ── Redis key helpers ─────────────────────────────────────────────────────


def _k_res(rid: str) -> str:
    return f"reservation:{rid}"


def _k_brand_res(bid: str) -> str:
    return f"brand:{bid}:reservations"


def _k_user_res(uid: str) -> str:
    return f"user:{uid}:reservations"


def _k_brand_stats(bid: str) -> str:
    return f"brand:{bid}:reservation_stats"


def _k_brand_policy(bid: str) -> str:
    return f"brand:{bid}:reservation_policy"


def _k_brand_triggers(bid: str) -> str:
    return f"brand:{bid}:reservation_triggers"


# ── Utils ─────────────────────────────────────────────────────────────────


def _now() -> int:
    return int(time.time())


def _new_rid() -> str:
    return f"res_{uuid4().hex[:16]}"


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)


def _safe_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def _hash_to_dict(state: dict[str, str]) -> dict[str, Any]:
    """Convert raw Redis HASH (all-str) into a typed dict."""
    if not state:
        return {}
    return {
        "reservation_id": state.get("reservation_id", ""),
        "brand_id": state.get("brand_id", ""),
        "user_id": state.get("user_id", ""),
        "scheduled_at": int(state.get("scheduled_at") or 0),
        "party_size": int(state.get("party_size") or 1),
        "type": state.get("type", "appointment"),
        "status": state.get("status", "confirmed"),
        "created_at": int(state.get("created_at") or 0),
        "updated_at": int(state.get("updated_at") or 0),
        "honored_at": int(state["honored_at"]) if state.get("honored_at") else None,
        "cancelled_at": int(state["cancelled_at"]) if state.get("cancelled_at") else None,
        "check_in_grace_minutes": int(
            state.get("check_in_grace_minutes") or _DEFAULT_GRACE_MINUTES
        ),
        "metadata": _safe_loads(state.get("metadata"), {}),
        "cancellation_policy": _safe_loads(state.get("cancellation_policy"), {}),
        "recovery_voucher_template_id": state.get("recovery_voucher_template_id") or None,
        "recovery_voucher_id": state.get("recovery_voucher_id") or None,
        "cancellation_reason": state.get("cancellation_reason") or None,
        "cancellation_by": state.get("cancellation_by") or None,
    }


# ── Pydantic models ───────────────────────────────────────────────────────


class CancellationPolicy(BaseModel):
    free_before_hours: int = Field(default=_DEFAULT_FREE_BEFORE_HOURS, ge=0, le=24 * 365)
    partial_refund_pct: int = Field(default=_DEFAULT_PARTIAL_REFUND_PCT, ge=0, le=100)
    full_charge_at_no_show: bool = True


class CreateReservationRequest(BaseModel):
    brand_id: str = Field(..., min_length=1, max_length=128)
    user_id: str = Field(..., min_length=1, max_length=128)
    scheduled_at: int = Field(..., gt=0, description="Future epoch seconds (UTC)")
    party_size: int = Field(1, ge=1, le=1000)
    type: Literal[
        "dining", "fitness_class", "appointment", "event", "tour", "service"
    ] = "appointment"
    metadata: dict[str, Any] = Field(default_factory=dict)
    recovery_voucher_template_id: str | None = Field(None, max_length=128)
    cancellation_policy: CancellationPolicy | None = None
    check_in_grace_minutes: int | None = Field(None, ge=0, le=24 * 60)


class CreateReservationResponse(BaseModel):
    reservation_id: str
    status: str
    scheduled_at: int


class CheckInRequest(BaseModel):
    at_brand_id: str | None = Field(None, max_length=128)
    evidence: Literal["qr", "manual", "geo"] = "manual"


class CancelRequest(BaseModel):
    by: Literal["user", "brand"] = "user"
    reason: str = Field("", max_length=500)


class RescheduleRequest(BaseModel):
    new_scheduled_at: int = Field(..., gt=0)


class ScanNoShowsRequest(BaseModel):
    admin_token: str = Field(..., min_length=1)
    dry_run: bool = False
    cutoff_seconds: int = Field(1800, ge=0, le=24 * 3600)
    limit: int = Field(5000, ge=1, le=100_000)


class PolicyConfigureRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    default_grace_minutes: int | None = Field(None, ge=0, le=24 * 60)
    default_cancellation_policy: CancellationPolicy | None = None
    default_recovery_voucher_template_id: str | None = Field(None, max_length=128)


class TriggerRegisterRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    event_type: Literal[
        "reservation.created",
        "reservation.confirmed",
        "reservation.honored",
        "reservation.no_show",
        "reservation.cancelled_by_user",
        "reservation.cancelled_by_brand",
        "reservation.rescheduled",
    ]
    action_type: Literal["issue_voucher", "send_push", "award_xp", "webhook"]
    action_config: dict[str, Any] = Field(default_factory=dict)


# ── Brand policy ──────────────────────────────────────────────────────────


async def _load_brand_policy(r: aioredis.Redis, brand_id: str) -> dict[str, Any]:
    raw = await r.hgetall(_k_brand_policy(brand_id))
    if not raw:
        return {}
    out: dict[str, Any] = {}
    if raw.get("default_grace_minutes"):
        out["default_grace_minutes"] = int(raw["default_grace_minutes"])
    if raw.get("default_cancellation_policy"):
        out["default_cancellation_policy"] = _safe_loads(
            raw["default_cancellation_policy"], {}
        )
    if raw.get("default_recovery_voucher_template_id"):
        out["default_recovery_voucher_template_id"] = raw[
            "default_recovery_voucher_template_id"
        ]
    return out


# ── Event emission ────────────────────────────────────────────────────────


async def _emit_event(
    r: aioredis.Redis,
    *,
    event_type: str,
    reservation_id: str,
    brand_id: str,
    user_id: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append to events:reservation stream and dispatch registered triggers.

    Never fails the caller — wrapped in try/except.
    """
    payload = {
        "event_type": event_type,
        "reservation_id": reservation_id,
        "brand_id": brand_id,
        "user_id": user_id,
        "at": str(_now()),
    }
    if extra:
        payload["extra"] = _dumps(extra)
    try:
        await r.xadd(
            _EVENT_STREAM,
            payload,
            maxlen=_EVENT_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("reservation event xadd failed: %s", exc)

    # Dispatch registered triggers (best-effort, non-blocking semantics).
    try:
        await _dispatch_triggers(
            r,
            brand_id=brand_id,
            event_type=event_type,
            reservation_id=reservation_id,
            user_id=user_id,
            extra=extra or {},
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("reservation trigger dispatch failed: %s", exc)


async def _dispatch_triggers(
    r: aioredis.Redis,
    *,
    brand_id: str,
    event_type: str,
    reservation_id: str,
    user_id: str,
    extra: dict[str, Any],
) -> None:
    raw_list = await r.lrange(_k_brand_triggers(brand_id), 0, -1)
    if not raw_list:
        return
    for raw in raw_list:
        cfg = _safe_loads(raw, None)
        if not cfg or cfg.get("event_type") != event_type:
            continue
        action_type = cfg.get("action_type")
        action_config = cfg.get("action_config") or {}
        try:
            await _execute_action(
                r,
                action_type=action_type,
                action_config=action_config,
                brand_id=brand_id,
                user_id=user_id,
                reservation_id=reservation_id,
                event_type=event_type,
                extra=extra,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "trigger action %s failed for %s: %s", action_type, event_type, exc
            )


async def _execute_action(
    r: aioredis.Redis,
    *,
    action_type: str,
    action_config: dict[str, Any],
    brand_id: str,
    user_id: str,
    reservation_id: str,
    event_type: str,
    extra: dict[str, Any],
) -> None:
    if action_type == "issue_voucher":
        template_id = action_config.get("template_id")
        if not template_id:
            return
        await _issue_recovery_voucher(
            r,
            brand_id=brand_id,
            user_id=user_id,
            template_id=template_id,
            reservation_id=reservation_id,
            source=action_config.get("source", "reservation_trigger"),
            expires_in_days=int(action_config.get("expires_in_days", 30)),
        )
    elif action_type == "send_push":
        # Enqueue a notification with an exempt-from-cap flag (callers honor).
        payload = {
            "user_id": user_id,
            "brand_id": brand_id,
            "reservation_id": reservation_id,
            "event_type": event_type,
            "template": action_config.get("template", ""),
            "exempt_from_cap": True,
        }
        try:
            await r.lpush(
                f"user:{user_id}:notifications", _dumps(payload)
            )
            await r.ltrim(f"user:{user_id}:notifications", 0, 199)
        except Exception:  # pragma: no cover
            pass
    elif action_type == "award_xp":
        amount = int(action_config.get("amount", 0))
        if amount <= 0:
            return
        try:
            await r.hincrby(f"user:{user_id}:xp", brand_id, amount)
        except Exception:  # pragma: no cover
            pass
    elif action_type == "webhook":
        # Queue webhook delivery; actual HTTP fan-out lives outside this module.
        url = action_config.get("url")
        if not url:
            return
        try:
            await r.lpush(
                "webhook:outbound",
                _dumps(
                    {
                        "url": url,
                        "event_type": event_type,
                        "brand_id": brand_id,
                        "user_id": user_id,
                        "reservation_id": reservation_id,
                        "extra": extra,
                    }
                ),
            )
            await r.ltrim("webhook:outbound", 0, 9999)
        except Exception:  # pragma: no cover
            pass


# ── Recovery voucher (best-effort) ────────────────────────────────────────


async def _issue_recovery_voucher(
    r: aioredis.Redis,
    *,
    brand_id: str,
    user_id: str,
    template_id: str,
    reservation_id: str,
    source: str = "reservation_recovery",
    expires_in_days: int = 30,
) -> str | None:
    """Best-effort voucher issuance. Never raises."""
    try:
        # Late import to avoid circular at module load time.
        from app.routers.vouchers import IssueVoucherRequest, _do_issue  # type: ignore
    except Exception as exc:  # pragma: no cover
        logger.warning("voucher module unavailable: %s", exc)
        return None

    try:
        expires_at = _now() + expires_in_days * 24 * 3600
        body = IssueVoucherRequest(
            template_id=template_id,
            user_id=user_id,
            redeemable_at="issuer_only",
            value_cents=None,
            expires_at=expires_at,
            conditions={"reservation_id": reservation_id},
            source="promo",  # closest enum match — UI surfaces "source" tag
            transferable=False,
            max_uses=1,
        )
        result = await _do_issue(r, issuer_brand_id=brand_id, body=body)
        vid = result.get("voucher_id")
        if vid:
            # Audit trail: stamp voucher_id back on the reservation hash.
            try:
                await r.hset(_k_res(reservation_id), "recovery_voucher_id", vid)
            except Exception:  # pragma: no cover
                pass
        logger.info(
            "recovery voucher issued: rid=%s brand=%s user=%s template=%s vid=%s",
            reservation_id, brand_id, user_id, template_id, vid,
        )
        return vid
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "recovery voucher issue failed: rid=%s brand=%s err=%s",
            reservation_id, brand_id, exc,
        )
        return None


# ── Stats helpers ─────────────────────────────────────────────────────────


async def _incr_stat(r: aioredis.Redis, brand_id: str, field: str, by: int = 1) -> None:
    try:
        await r.hincrby(_k_brand_stats(brand_id), field, by)
    except Exception:  # pragma: no cover
        pass


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post(
    "/create",
    response_model=CreateReservationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a future-dated reservation",
)
async def create_reservation(
    body: CreateReservationRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> CreateReservationResponse:
    now = _now()
    if body.scheduled_at <= now:
        raise HTTPException(
            status_code=400, detail="scheduled_at must be in the future"
        )

    # Merge brand defaults under the explicit request fields.
    policy = await _load_brand_policy(r, body.brand_id)
    grace = (
        body.check_in_grace_minutes
        if body.check_in_grace_minutes is not None
        else int(policy.get("default_grace_minutes") or _DEFAULT_GRACE_MINUTES)
    )
    cancellation_policy = (
        body.cancellation_policy.model_dump()
        if body.cancellation_policy
        else policy.get("default_cancellation_policy")
        or CancellationPolicy().model_dump()
    )
    recovery_template = (
        body.recovery_voucher_template_id
        or policy.get("default_recovery_voucher_template_id")
        or ""
    )

    rid = _new_rid()
    state: dict[str, str] = {
        "reservation_id": rid,
        "brand_id": body.brand_id,
        "user_id": body.user_id,
        "scheduled_at": str(body.scheduled_at),
        "party_size": str(body.party_size),
        "type": body.type,
        "status": "confirmed",
        "created_at": str(now),
        "updated_at": str(now),
        "check_in_grace_minutes": str(grace),
        "metadata": _dumps(body.metadata),
        "cancellation_policy": _dumps(cancellation_policy),
        "recovery_voucher_template_id": recovery_template,
    }

    pipe = r.pipeline()
    pipe.hset(_k_res(rid), mapping=state)
    pipe.zadd(_k_brand_res(body.brand_id), {rid: body.scheduled_at})
    pipe.zadd(_k_user_res(body.user_id), {rid: body.scheduled_at})
    pipe.hincrby(_k_brand_stats(body.brand_id), "total_confirmed", 1)
    pipe.hincrby(_k_brand_stats(body.brand_id), "party_size_sum", body.party_size)
    pipe.hincrby(_k_brand_stats(body.brand_id), "party_size_count", 1)
    await pipe.execute()

    await _emit_event(
        r,
        event_type="reservation.created",
        reservation_id=rid,
        brand_id=body.brand_id,
        user_id=body.user_id,
        extra={
            "scheduled_at": body.scheduled_at,
            "type": body.type,
            "party_size": body.party_size,
        },
    )
    # Distinct "confirmed" event for downstream subscribers that only react
    # to confirmed state (e.g. send a "see you Friday" push).
    await _emit_event(
        r,
        event_type="reservation.confirmed",
        reservation_id=rid,
        brand_id=body.brand_id,
        user_id=body.user_id,
        extra={"scheduled_at": body.scheduled_at},
    )

    logger.info(
        "reservation created: rid=%s brand=%s user=%s at=%s type=%s",
        rid, body.brand_id, body.user_id, body.scheduled_at, body.type,
    )

    return CreateReservationResponse(
        reservation_id=rid, status="confirmed", scheduled_at=body.scheduled_at
    )


@router.post("/{rid}/check-in", summary="Mark reservation honored (user arrived)")
async def check_in(
    rid: str,
    body: CheckInRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    state = await r.hgetall(_k_res(rid))
    if not state:
        raise HTTPException(status_code=404, detail="reservation not found")
    if state.get("status") != "confirmed":
        raise HTTPException(
            status_code=409,
            detail=f"cannot check-in from status={state.get('status')}",
        )

    scheduled_at = int(state.get("scheduled_at") or 0)
    grace_minutes = int(
        state.get("check_in_grace_minutes") or _DEFAULT_GRACE_MINUTES
    )
    grace_seconds = grace_minutes * 60
    now = _now()
    if now < scheduled_at - grace_seconds:
        raise HTTPException(
            status_code=409,
            detail="too early to check in",
        )
    if now > scheduled_at + grace_seconds:
        raise HTTPException(
            status_code=409,
            detail="grace window expired — run scan-no-shows or contact brand",
        )

    brand_id = state.get("brand_id", "")
    user_id = state.get("user_id", "")
    if body.at_brand_id and body.at_brand_id != brand_id:
        raise HTTPException(
            status_code=409, detail="at_brand_id does not match reservation brand"
        )

    pipe = r.pipeline()
    pipe.hset(
        _k_res(rid),
        mapping={
            "status": "honored",
            "honored_at": str(now),
            "updated_at": str(now),
            "check_in_evidence": body.evidence,
        },
    )
    pipe.hincrby(_k_brand_stats(brand_id), "total_honored", 1)
    await pipe.execute()

    # Fire attribution conversion (best-effort).
    try:
        await r.xadd(
            "events:attribution",
            {
                "event_type": "visit_completed",
                "brand_id": brand_id,
                "user_id": user_id,
                "source": "reservation",
                "reservation_id": rid,
                "at": str(now),
            },
            maxlen=10_000,
            approximate=True,
        )
    except Exception:  # pragma: no cover
        pass

    await _emit_event(
        r,
        event_type="reservation.honored",
        reservation_id=rid,
        brand_id=brand_id,
        user_id=user_id,
        extra={"evidence": body.evidence, "honored_at": now},
    )

    return {
        "reservation_id": rid,
        "status": "honored",
        "honored_at": now,
        "evidence": body.evidence,
    }


@router.post("/{rid}/cancel", summary="Cancel a reservation (user or brand)")
async def cancel(
    rid: str,
    body: CancelRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    state = await r.hgetall(_k_res(rid))
    if not state:
        raise HTTPException(status_code=404, detail="reservation not found")
    if state.get("status") not in ("confirmed", "rescheduled"):
        raise HTTPException(
            status_code=409,
            detail=f"cannot cancel from status={state.get('status')}",
        )

    scheduled_at = int(state.get("scheduled_at") or 0)
    brand_id = state.get("brand_id", "")
    user_id = state.get("user_id", "")
    policy = _safe_loads(state.get("cancellation_policy"), {}) or {}
    free_before_hours = int(policy.get("free_before_hours", _DEFAULT_FREE_BEFORE_HOURS))
    partial_refund_pct = int(
        policy.get("partial_refund_pct", _DEFAULT_PARTIAL_REFUND_PCT)
    )
    now = _now()
    hours_until = max(0, (scheduled_at - now) / 3600.0)

    # Compute resulting status + penalty surface.
    penalty = False
    if body.by == "brand":
        new_status = "cancelled_by_brand"
        refund_pct = 100
    else:
        if hours_until >= free_before_hours:
            new_status = "cancelled_by_user"
            refund_pct = 100
        else:
            new_status = "cancelled_with_penalty"
            refund_pct = partial_refund_pct
            penalty = True

    pipe = r.pipeline()
    pipe.hset(
        _k_res(rid),
        mapping={
            "status": new_status,
            "cancelled_at": str(now),
            "updated_at": str(now),
            "cancellation_reason": body.reason,
            "cancellation_by": body.by,
            "refund_pct": str(refund_pct),
        },
    )
    pipe.zrem(_k_brand_res(brand_id), rid)
    pipe.zrem(_k_user_res(user_id), rid)
    pipe.hincrby(_k_brand_stats(brand_id), f"total_{new_status}", 1)
    await pipe.execute()

    # Emit a normalized "cancelled_by_{user|brand}" event regardless of the
    # penalty bucket so subscribers don't need to enumerate sub-states.
    canon_event = (
        "reservation.cancelled_by_brand"
        if body.by == "brand"
        else "reservation.cancelled_by_user"
    )
    await _emit_event(
        r,
        event_type=canon_event,
        reservation_id=rid,
        brand_id=brand_id,
        user_id=user_id,
        extra={
            "status": new_status,
            "refund_pct": refund_pct,
            "penalty": penalty,
            "reason": body.reason,
            "hours_until": round(hours_until, 2),
        },
    )

    return {
        "reservation_id": rid,
        "status": new_status,
        "refund_pct": refund_pct,
        "penalty": penalty,
    }


@router.post("/{rid}/reschedule", summary="Move a reservation to a new time")
async def reschedule(
    rid: str,
    body: RescheduleRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    state = await r.hgetall(_k_res(rid))
    if not state:
        raise HTTPException(status_code=404, detail="reservation not found")
    if state.get("status") not in ("confirmed", "rescheduled"):
        raise HTTPException(
            status_code=409,
            detail=f"cannot reschedule from status={state.get('status')}",
        )

    now = _now()
    if body.new_scheduled_at <= now:
        raise HTTPException(
            status_code=400, detail="new_scheduled_at must be in the future"
        )

    brand_id = state.get("brand_id", "")
    user_id = state.get("user_id", "")
    old_scheduled_at = int(state.get("scheduled_at") or 0)

    pipe = r.pipeline()
    pipe.hset(
        _k_res(rid),
        mapping={
            "scheduled_at": str(body.new_scheduled_at),
            "status": "confirmed",  # rescheduled collapses back to confirmed
            "updated_at": str(now),
        },
    )
    pipe.zadd(_k_brand_res(brand_id), {rid: body.new_scheduled_at})
    pipe.zadd(_k_user_res(user_id), {rid: body.new_scheduled_at})
    pipe.hincrby(_k_brand_stats(brand_id), "total_rescheduled", 1)
    await pipe.execute()

    await _emit_event(
        r,
        event_type="reservation.rescheduled",
        reservation_id=rid,
        brand_id=brand_id,
        user_id=user_id,
        extra={
            "old_scheduled_at": old_scheduled_at,
            "new_scheduled_at": body.new_scheduled_at,
        },
    )

    return {
        "reservation_id": rid,
        "status": "confirmed",
        "scheduled_at": body.new_scheduled_at,
    }


@router.get("/{rid}", summary="Get a single reservation by id")
async def get_reservation(
    rid: str, r: aioredis.Redis = Depends(get_redis)
) -> dict[str, Any]:
    state = await r.hgetall(_k_res(rid))
    if not state:
        raise HTTPException(status_code=404, detail="reservation not found")
    return _hash_to_dict(state)


async def _list_by_index(
    r: aioredis.Redis,
    *,
    index_key: str,
    status_filter: str | None,
    from_ts: int | None,
    to_ts: int | None,
    limit: int,
) -> list[dict[str, Any]]:
    lo = from_ts if from_ts is not None else "-inf"
    hi = to_ts if to_ts is not None else "+inf"
    rids = await r.zrangebyscore(index_key, lo, hi, start=0, num=limit)
    if not rids:
        return []
    out: list[dict[str, Any]] = []
    # Pipeline HGETALL for the page.
    pipe = r.pipeline()
    for rid in rids:
        pipe.hgetall(_k_res(rid))
    rows = await pipe.execute()
    for state in rows:
        if not state:
            continue
        if status_filter and state.get("status") != status_filter:
            continue
        out.append(_hash_to_dict(state))
    return out


@router.get("/brand/{brand_id}", summary="List reservations for a brand")
async def list_brand_reservations(
    brand_id: str,
    status: str | None = Query(None),
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
    limit: int = Query(200, ge=1, le=2000),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    if status and status not in _STATUSES:
        raise HTTPException(status_code=400, detail=f"unknown status: {status}")
    items = await _list_by_index(
        r,
        index_key=_k_brand_res(brand_id),
        status_filter=status,
        from_ts=from_ts,
        to_ts=to_ts,
        limit=limit,
    )
    return {"brand_id": brand_id, "count": len(items), "reservations": items}


@router.get("/user/{user_id}", summary="List reservations for a user")
async def list_user_reservations(
    user_id: str,
    status: str | None = Query(None),
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
    limit: int = Query(200, ge=1, le=2000),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    if status and status not in _STATUSES:
        raise HTTPException(status_code=400, detail=f"unknown status: {status}")
    items = await _list_by_index(
        r,
        index_key=_k_user_res(user_id),
        status_filter=status,
        from_ts=from_ts,
        to_ts=to_ts,
        limit=limit,
    )
    return {"user_id": user_id, "count": len(items), "reservations": items}


@router.post(
    "/scan-no-shows",
    summary="Cron-like: mark overdue confirmed reservations as no_show",
)
async def scan_no_shows(
    body: ScanNoShowsRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    # Admin gate (matches vouchers.cleanup pattern).
    try:
        from app.config import settings
        expected = getattr(settings, "admin_token", None)
    except Exception:
        expected = None
    if expected and body.admin_token != expected:
        raise HTTPException(status_code=403, detail="invalid admin_token")

    now = _now()
    cutoff = now - int(body.cutoff_seconds)

    scanned = 0
    marked_no_show = 0
    vouchers_issued = 0

    cursor = 0
    while True:
        cursor, keys = await r.scan(
            cursor=cursor, match="reservation:*", count=200
        )
        for k in keys:
            # reservation:{rid} only — skip any future sub-keys.
            if k.count(":") != 1:
                continue
            scanned += 1
            if scanned > body.limit:
                cursor = 0
                break

            state = await r.hgetall(k)
            if not state:
                continue
            if state.get("status") != "confirmed":
                continue
            scheduled_at = int(state.get("scheduled_at") or 0)
            grace_minutes = int(
                state.get("check_in_grace_minutes") or _DEFAULT_GRACE_MINUTES
            )
            grace_seconds = grace_minutes * 60
            # No-show iff scheduled_at + grace + cutoff_seconds < now.
            # ``cutoff`` already accounts for `now - cutoff_seconds` so any
            # reservation whose grace window ended *before* `cutoff` is
            # confidently past.
            if scheduled_at + grace_seconds > cutoff:
                continue

            rid = state.get("reservation_id") or k.split(":", 1)[1]
            brand_id = state.get("brand_id", "")
            user_id = state.get("user_id", "")

            if body.dry_run:
                marked_no_show += 1
                continue

            pipe = r.pipeline()
            pipe.hset(
                _k_res(rid),
                mapping={
                    "status": "no_show",
                    "updated_at": str(now),
                    "no_show_at": str(now),
                },
            )
            pipe.zrem(_k_brand_res(brand_id), rid)
            pipe.zrem(_k_user_res(user_id), rid)
            pipe.hincrby(_k_brand_stats(brand_id), "total_no_show", 1)
            await pipe.execute()
            marked_no_show += 1

            # Auto-issue recovery voucher if template configured.
            template_id = state.get("recovery_voucher_template_id") or ""
            if template_id:
                vid = await _issue_recovery_voucher(
                    r,
                    brand_id=brand_id,
                    user_id=user_id,
                    template_id=template_id,
                    reservation_id=rid,
                    source="reservation_recovery",
                    expires_in_days=30,
                )
                if vid:
                    vouchers_issued += 1

            await _emit_event(
                r,
                event_type="reservation.no_show",
                reservation_id=rid,
                brand_id=brand_id,
                user_id=user_id,
                extra={
                    "scheduled_at": scheduled_at,
                    "grace_minutes": grace_minutes,
                },
            )

        if cursor == 0 or scanned >= body.limit:
            break

    return {
        "scanned": scanned,
        "marked_no_show": marked_no_show,
        "vouchers_issued": vouchers_issued,
        "dry_run": body.dry_run,
    }


@router.post(
    "/admin/policy/configure",
    summary="Configure brand-level reservation defaults",
)
async def configure_policy(
    body: PolicyConfigureRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    mapping: dict[str, str] = {}
    if body.default_grace_minutes is not None:
        mapping["default_grace_minutes"] = str(body.default_grace_minutes)
    if body.default_cancellation_policy is not None:
        mapping["default_cancellation_policy"] = _dumps(
            body.default_cancellation_policy.model_dump()
        )
    if body.default_recovery_voucher_template_id is not None:
        mapping["default_recovery_voucher_template_id"] = (
            body.default_recovery_voucher_template_id
        )

    if not mapping:
        raise HTTPException(status_code=400, detail="no policy fields supplied")

    await r.hset(_k_brand_policy(body.brand_id), mapping=mapping)
    policy = await _load_brand_policy(r, body.brand_id)
    return {"brand_id": body.brand_id, "policy": policy}


@router.get(
    "/brand/{brand_id}/stats",
    summary="Aggregate reservation counters for a brand",
)
async def brand_stats(
    brand_id: str, r: aioredis.Redis = Depends(get_redis)
) -> dict[str, Any]:
    raw = await r.hgetall(_k_brand_stats(brand_id))
    total_confirmed = int(raw.get("total_confirmed") or 0)
    total_honored = int(raw.get("total_honored") or 0)
    total_no_show = int(raw.get("total_no_show") or 0)
    party_sum = int(raw.get("party_size_sum") or 0)
    party_count = int(raw.get("party_size_count") or 0)

    # no_show_rate is over reservations whose outcome is resolved
    # (honored + no_show). Cancellations are excluded — they're not a
    # measure of brand experience quality.
    resolved = total_honored + total_no_show
    no_show_rate = (total_no_show / resolved) if resolved > 0 else 0.0
    avg_party_size = (party_sum / party_count) if party_count > 0 else 0.0

    return {
        "brand_id": brand_id,
        "total_confirmed": total_confirmed,
        "total_honored": total_honored,
        "total_no_show": total_no_show,
        "total_cancelled_by_user": int(raw.get("total_cancelled_by_user") or 0),
        "total_cancelled_by_brand": int(raw.get("total_cancelled_by_brand") or 0),
        "total_cancelled_with_penalty": int(
            raw.get("total_cancelled_with_penalty") or 0
        ),
        "total_rescheduled": int(raw.get("total_rescheduled") or 0),
        "no_show_rate": round(no_show_rate, 4),
        "avg_party_size": round(avg_party_size, 2),
    }


@router.post(
    "/triggers/register",
    summary="Register a trigger that fires on a reservation event",
    status_code=status.HTTP_201_CREATED,
)
async def register_trigger(
    body: TriggerRegisterRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    trigger_id = f"trg_{uuid4().hex[:12]}"
    cfg = {
        "trigger_id": trigger_id,
        "brand_id": body.brand_id,
        "event_type": body.event_type,
        "action_type": body.action_type,
        "action_config": body.action_config,
        "created_at": _now(),
    }
    await r.lpush(_k_brand_triggers(body.brand_id), _dumps(cfg))
    # Cap to last 200 triggers per brand.
    await r.ltrim(_k_brand_triggers(body.brand_id), 0, 199)
    return {
        "trigger_id": trigger_id,
        "brand_id": body.brand_id,
        "event_type": body.event_type,
        "action_type": body.action_type,
    }


# ── Geofence integration helper ───────────────────────────────────────────


async def maybe_auto_check_in(
    r: aioredis.Redis,
    *,
    user_id: str,
    brand_id: str,
    window_seconds: int = 30 * 60,
) -> str | None:
    """Auto check-in helper called by the geofence module.

    Returns the reservation_id that was honored, or None if no upcoming
    reservation falls in [now - grace, now + window]. Safe to call from
    any caller — never raises.
    """
    try:
        now = _now()
        # We don't know grace per-reservation up front, so widen the lower
        # bound conservatively using the platform default.
        lo = now - (_DEFAULT_GRACE_MINUTES * 60)
        hi = now + window_seconds
        rids = await r.zrangebyscore(_k_user_res(user_id), lo, hi, start=0, num=10)
        for rid in rids:
            state = await r.hgetall(_k_res(rid))
            if not state or state.get("brand_id") != brand_id:
                continue
            if state.get("status") != "confirmed":
                continue
            scheduled_at = int(state.get("scheduled_at") or 0)
            grace = int(
                state.get("check_in_grace_minutes") or _DEFAULT_GRACE_MINUTES
            ) * 60
            if scheduled_at - grace <= now <= scheduled_at + grace:
                pipe = r.pipeline()
                pipe.hset(
                    _k_res(rid),
                    mapping={
                        "status": "honored",
                        "honored_at": str(now),
                        "updated_at": str(now),
                        "check_in_evidence": "geo",
                    },
                )
                pipe.hincrby(_k_brand_stats(brand_id), "total_honored", 1)
                await pipe.execute()
                await _emit_event(
                    r,
                    event_type="reservation.honored",
                    reservation_id=rid,
                    brand_id=brand_id,
                    user_id=user_id,
                    extra={"evidence": "geo", "honored_at": now, "auto": True},
                )
                return rid
        return None
    except Exception as exc:  # pragma: no cover
        logger.warning("maybe_auto_check_in failed: %s", exc)
        return None
