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
from pydantic import BaseModel, Field, model_validator
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
    # Core appointment-like
    "dining",
    "fitness_class",
    "appointment",
    "event",
    "tour",
    "service",
    # Property / real-estate (老陆)
    "property_viewing",
    "legal",  # signing / legal consultations (老陆)
    # Logistics (老贾)
    "pickup",
    "delivery",
    # Vehicle / asset (老田)
    "vehicle_rental",
    "asset_hold",  # deposit / lock
    # Group travel (老梁)
    "group_tour",
    # Healthcare (老蔡)
    "specialist",
    "gp",
    "lab_test",
    "vaccination",
    "dental",
    # Pet (老韩)
    "pet_grooming",
    "pet_medical",
    "grooming",  # legacy alias used by series module
    # Beauty (老钱)
    "stylist",
    # Generic consult (老沈)
    "consultation",
    # Education / fitness cohort (老周)
    "group_class",
    # Open fallback — pair with type_label for arbitrary domains
    "custom",
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


def _k_brand_res_by_resource(bid: str, resource_id: str) -> str:
    return f"brand:{bid}:reservation:by_resource:{resource_id}"


def _k_user_res_as_fulfiller(uid: str) -> str:
    """ZSET of reservations this user is the fulfiller for (courier/doctor/stylist)."""
    return f"user:{uid}:reservations_as_fulfiller"


def _k_user_res_as_beneficiary(uid: str) -> str:
    """ZSET of reservations this user is the beneficiary/recipient for."""
    return f"user:{uid}:reservations_as_beneficiary"


def _k_res_travelers(rid: str) -> str:
    """LIST of traveler manifest JSON entries for a (group) reservation."""
    return f"reservation:{rid}:travelers"


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


def _normalize_type(
    raw_type: str | None, raw_label: str | None
) -> tuple[str, str | None]:
    """Coerce arbitrary type strings into (curated_type, type_label).

    Unknown values fall back to ``"custom"`` with the original preserved in
    ``type_label``. Empty input becomes ``("appointment", None)``.
    """
    t = (raw_type or "").strip() or "appointment"
    label = (raw_label or "").strip() or None
    if t in _RESERVATION_TYPES:
        return t, label
    # Unknown value: keep as custom + remember the raw label.
    return "custom", label or t


def _hash_to_dict(state: dict[str, str]) -> dict[str, Any]:
    """Convert raw Redis HASH (all-str) into a typed dict."""
    if not state:
        return {}
    return {
        "reservation_id": state.get("reservation_id", ""),
        "brand_id": state.get("brand_id", ""),
        "user_id": state.get("user_id", ""),
        "beneficiary_user_id": state.get("beneficiary_user_id") or None,
        "fulfiller_user_id": state.get("fulfiller_user_id") or None,
        "recipient_user_id": state.get("recipient_user_id") or None,
        "scheduled_at": int(state.get("scheduled_at") or 0),
        "party_size": int(state.get("party_size") or 1),
        "type": state.get("type", "appointment"),
        "type_label": state.get("type_label") or None,
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
        "resource_id": state.get("resource_id") or None,
        "duration_minutes": (
            int(state.get("duration_minutes") or 0) or None
        ),
    }


# ── Pydantic models ───────────────────────────────────────────────────────


class CancellationPolicy(BaseModel):
    free_before_hours: int = Field(default=_DEFAULT_FREE_BEFORE_HOURS, ge=0, le=24 * 365)
    partial_refund_pct: int = Field(default=_DEFAULT_PARTIAL_REFUND_PCT, ge=0, le=100)
    full_charge_at_no_show: bool = True


class CreateReservationRequest(BaseModel):
    brand_id: str = Field(..., min_length=1, max_length=128)
    user_id: str = Field(
        ..., min_length=1, max_length=128,
        description="Who books / pays for the reservation.",
    )
    beneficiary_user_id: str | None = Field(
        None,
        max_length=128,
        description=(
            "Who receives the service (different from booker — e.g. parent books "
            "for child, sender books delivery for recipient). Indexed for "
            "cross-id lookup via /recipient/{user_id}."
        ),
    )
    fulfiller_user_id: str | None = Field(
        None,
        max_length=128,
        description=(
            "Who performs the service (courier/doctor/stylist). Indexed for "
            "fulfiller-side calendar via /fulfiller/{user_id}."
        ),
    )
    recipient_user_id: str | None = Field(
        None,
        max_length=128,
        description=(
            "Explicit recipient for logistics flows (老贾). When supplied, "
            "treated as the beneficiary if beneficiary_user_id is not set."
        ),
    )
    scheduled_at: int = Field(..., gt=0, description="Future epoch seconds (UTC)")
    party_size: int = Field(1, ge=1, le=1000)
    # Open type — accept the curated enum OR any string when type_label set.
    # Validation happens at runtime: unknown type values are coerced to
    # "custom" and the raw value is preserved in type_label.
    type: str = Field(
        "appointment",
        min_length=1,
        max_length=64,
        description=(
            "Reservation domain. Curated enum values are validated; unknown "
            "values are coerced to 'custom' with the original preserved in "
            "type_label."
        ),
    )
    type_label: str | None = Field(
        None,
        max_length=128,
        description=(
            "Free-form label for arbitrary reservation types. When `type` is "
            "'custom' or an unknown string, the original label is stored here."
        ),
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
    recovery_voucher_template_id: str | None = Field(None, max_length=128)
    cancellation_policy: CancellationPolicy | None = None
    check_in_grace_minutes: int | None = Field(None, ge=0, le=24 * 60)
    resource_id: str | None = Field(
        None,
        max_length=128,
        description=(
            "Optional resource handle (stylist/doctor/room/property/agent/...) "
            "that this reservation books — indexes into brand resource calendars."
        ),
    )
    duration_minutes: int | None = Field(
        None, ge=1, le=24 * 60 * 30,
        description="Optional service length; used by resource availability views.",
    )


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

    # Normalize the open type — unknown values are coerced to "custom" and
    # the raw label is preserved on `type_label`.
    norm_type, norm_label = _normalize_type(body.type, body.type_label)

    # Resource availability check: refuse if an active reservation overlaps
    # the requested window on the same brand resource. Uses the by_resource
    # ZSET maintained on create/cancel/no_show/reschedule.
    if body.resource_id:
        dur_seconds = (body.duration_minutes or 30) * 60
        window_lo = body.scheduled_at - 1800
        window_hi = body.scheduled_at + dur_seconds
        try:
            # Bounded conflict check: a resource window cannot legitimately
            # have more than 1000 overlapping holds.
            conflict_rids = await r.zrangebyscore(
                _k_brand_res_by_resource(body.brand_id, body.resource_id),
                window_lo,
                window_hi,
                start=0,
                num=1000,
            )
        except Exception:  # pragma: no cover
            conflict_rids = []
        if conflict_rids:
            # Only fail on *active* (confirmed/rescheduled) holds — the
            # by_resource index already prunes cancelled/no_show, but verify
            # defensively against stale entries.
            active_conflict_rid: str | None = None
            for c_rid in conflict_rids:
                try:
                    c_state = await r.hgetall(_k_res(c_rid))
                except Exception:  # pragma: no cover
                    c_state = {}
                if not c_state:
                    continue
                if c_state.get("status") in ("confirmed", "rescheduled"):
                    active_conflict_rid = c_state.get("reservation_id") or c_rid
                    break
            if active_conflict_rid:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "resource_conflict",
                        "resource_id": body.resource_id,
                        "conflict_rid": active_conflict_rid,
                        "window": [window_lo, window_hi],
                    },
                )

    # Resolve beneficiary: explicit beneficiary wins, otherwise fall back
    # to the recipient field (老贾 delivery flow).
    beneficiary_uid = body.beneficiary_user_id or body.recipient_user_id

    rid = _new_rid()
    state: dict[str, str] = {
        "reservation_id": rid,
        "brand_id": body.brand_id,
        "user_id": body.user_id,
        "scheduled_at": str(body.scheduled_at),
        "party_size": str(body.party_size),
        "type": norm_type,
        "status": "confirmed",
        "created_at": str(now),
        "updated_at": str(now),
        "check_in_grace_minutes": str(grace),
        "metadata": _dumps(body.metadata),
        "cancellation_policy": _dumps(cancellation_policy),
        "recovery_voucher_template_id": recovery_template,
    }
    if norm_label:
        state["type_label"] = norm_label
    if beneficiary_uid:
        state["beneficiary_user_id"] = beneficiary_uid
    if body.recipient_user_id:
        state["recipient_user_id"] = body.recipient_user_id
    if body.fulfiller_user_id:
        state["fulfiller_user_id"] = body.fulfiller_user_id
    if body.resource_id:
        state["resource_id"] = body.resource_id
    if body.duration_minutes is not None:
        state["duration_minutes"] = str(body.duration_minutes)

    pipe = r.pipeline()
    pipe.hset(_k_res(rid), mapping=state)
    pipe.zadd(_k_brand_res(body.brand_id), {rid: body.scheduled_at})
    pipe.zadd(_k_user_res(body.user_id), {rid: body.scheduled_at})
    if beneficiary_uid and beneficiary_uid != body.user_id:
        pipe.zadd(
            _k_user_res_as_beneficiary(beneficiary_uid),
            {rid: body.scheduled_at},
        )
    if body.fulfiller_user_id:
        pipe.zadd(
            _k_user_res_as_fulfiller(body.fulfiller_user_id),
            {rid: body.scheduled_at},
        )
    if body.resource_id:
        pipe.zadd(
            _k_brand_res_by_resource(body.brand_id, body.resource_id),
            {rid: body.scheduled_at},
        )
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
            "type": norm_type,
            "type_label": norm_label,
            "party_size": body.party_size,
            "beneficiary_user_id": beneficiary_uid,
            "fulfiller_user_id": body.fulfiller_user_id,
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
        "reservation created: rid=%s brand=%s user=%s at=%s type=%s label=%s",
        rid, body.brand_id, body.user_id, body.scheduled_at, norm_type, norm_label,
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
    resource_id = state.get("resource_id") or ""
    if resource_id:
        pipe.zrem(_k_brand_res_by_resource(brand_id, resource_id), rid)
    beneficiary_uid = state.get("beneficiary_user_id") or ""
    if beneficiary_uid and beneficiary_uid != user_id:
        pipe.zrem(_k_user_res_as_beneficiary(beneficiary_uid), rid)
    fulfiller_uid = state.get("fulfiller_user_id") or ""
    if fulfiller_uid:
        pipe.zrem(_k_user_res_as_fulfiller(fulfiller_uid), rid)
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
    resource_id = state.get("resource_id") or ""
    if resource_id:
        pipe.zadd(
            _k_brand_res_by_resource(brand_id, resource_id),
            {rid: body.new_scheduled_at},
        )
    beneficiary_uid = state.get("beneficiary_user_id") or ""
    if beneficiary_uid and beneficiary_uid != user_id:
        pipe.zadd(
            _k_user_res_as_beneficiary(beneficiary_uid),
            {rid: body.new_scheduled_at},
        )
    fulfiller_uid = state.get("fulfiller_user_id") or ""
    if fulfiller_uid:
        pipe.zadd(
            _k_user_res_as_fulfiller(fulfiller_uid),
            {rid: body.new_scheduled_at},
        )
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
    resource_id: str | None = Query(
        None,
        description=(
            "If set, results are restricted to a single resource's calendar "
            "(uses the brand:{bid}:reservation:by_resource:{rid} index)."
        ),
    ),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    if status and status not in _STATUSES:
        raise HTTPException(status_code=400, detail=f"unknown status: {status}")
    index_key = (
        _k_brand_res_by_resource(brand_id, resource_id)
        if resource_id
        else _k_brand_res(brand_id)
    )
    items = await _list_by_index(
        r,
        index_key=index_key,
        status_filter=status,
        from_ts=from_ts,
        to_ts=to_ts,
        limit=limit,
    )
    return {
        "brand_id": brand_id,
        "resource_id": resource_id,
        "count": len(items),
        "reservations": items,
    }


@router.get(
    "/brand/{brand_id}/resources/{resource_id}/availability",
    summary="Calendar view: time slots booked for a specific resource",
)
async def resource_availability(
    brand_id: str,
    resource_id: str,
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
    limit: int = Query(500, ge=1, le=5000),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return time slots already booked against a resource.

    Each slot has start_ts (= scheduled_at), end_ts (= scheduled_at +
    duration_minutes if available, else scheduled_at), the reservation_id,
    and current status. Cancelled / no_show entries are pruned from the
    by-resource index, so what you see here are live commitments.
    """
    lo = from_ts if from_ts is not None else "-inf"
    hi = to_ts if to_ts is not None else "+inf"
    rids = await r.zrangebyscore(
        _k_brand_res_by_resource(brand_id, resource_id),
        lo, hi, start=0, num=limit,
    )
    slots: list[dict[str, Any]] = []
    if rids:
        pipe = r.pipeline()
        for rid in rids:
            pipe.hgetall(_k_res(rid))
        rows = await pipe.execute()
        for state in rows:
            if not state:
                continue
            start_ts = int(state.get("scheduled_at") or 0)
            dur_min = int(state.get("duration_minutes") or 0)
            end_ts = start_ts + dur_min * 60 if dur_min > 0 else start_ts
            slots.append(
                {
                    "reservation_id": state.get("reservation_id", ""),
                    "user_id": state.get("user_id", ""),
                    "status": state.get("status", "confirmed"),
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "duration_minutes": dur_min or None,
                    "party_size": int(state.get("party_size") or 1),
                    "type": state.get("type", "appointment"),
                }
            )
    # Group by YYYY-MM-DD (UTC) for callers that want a date-keyed view.
    import datetime as _dt
    by_date: dict[str, list[dict[str, Any]]] = {}
    for s in slots:
        date_str = _dt.datetime.utcfromtimestamp(s["start_ts"]).strftime("%Y-%m-%d")
        by_date.setdefault(date_str, []).append(s)
    days = [
        {"date": d, "slots": sorted(v, key=lambda x: x["start_ts"])}
        for d, v in sorted(by_date.items())
    ]
    return {
        "brand_id": brand_id,
        "resource_id": resource_id,
        "count": len(slots),
        "slots": slots,
        "days": days,
    }


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


@router.get(
    "/fulfiller/{user_id}",
    summary="List reservations where this user is the fulfiller (courier/doctor/stylist)",
)
async def list_fulfiller_reservations(
    user_id: str,
    status: str | None = Query(None),
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
    limit: int = Query(200, ge=1, le=2000),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Fulfiller-side calendar: jobs assigned to this user.

    Powers courier route boards (老贾), doctor day-views (老蔡), and stylist
    chair calendars (老钱) — anywhere the booking user differs from the
    person actually performing the work.
    """
    if status and status not in _STATUSES:
        raise HTTPException(status_code=400, detail=f"unknown status: {status}")
    items = await _list_by_index(
        r,
        index_key=_k_user_res_as_fulfiller(user_id),
        status_filter=status,
        from_ts=from_ts,
        to_ts=to_ts,
        limit=limit,
    )
    return {
        "fulfiller_user_id": user_id,
        "count": len(items),
        "reservations": items,
    }


@router.get(
    "/recipient/{user_id}",
    summary="List reservations where this user is the beneficiary / recipient",
)
async def list_recipient_reservations(
    user_id: str,
    status: str | None = Query(None),
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
    limit: int = Query(200, ge=1, le=2000),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Beneficiary-side view: services someone else booked *for* this user.

    Logistics deliveries (老贾), parent-books-for-child appointments (老蔡),
    and gifted services all surface here even though the booker differs
    from the recipient.
    """
    if status and status not in _STATUSES:
        raise HTTPException(status_code=400, detail=f"unknown status: {status}")
    items = await _list_by_index(
        r,
        index_key=_k_user_res_as_beneficiary(user_id),
        status_filter=status,
        from_ts=from_ts,
        to_ts=to_ts,
        limit=limit,
    )
    return {
        "beneficiary_user_id": user_id,
        "count": len(items),
        "reservations": items,
    }


# ── Travelers manifest (老梁 group tour) ─────────────────────────────────


def _hash_passport(raw: str) -> str:
    """SHA-256 hex digest of a passport number; never store plaintext."""
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TravelerEntry(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    passport_number: str | None = Field(
        None,
        min_length=1,
        max_length=64,
        description=(
            "Plaintext passport number — never persisted. Hashed via SHA-256 "
            "at write-time; the digest is stored as passport_number_hash."
        ),
    )
    passport_number_hash: str | None = Field(
        None,
        min_length=8,
        max_length=128,
        description=(
            "Pre-hashed passport identifier. Supply this when the client "
            "already hashed the number (preferred — avoids plaintext over "
            "the wire)."
        ),
    )
    dob_year: int | None = Field(None, ge=1900, le=3000)
    dietary: str | None = Field(None, max_length=256)
    visa_status: str | None = Field(None, max_length=128)
    role: Literal["primary", "companion", "child"] | None = None
    notes: str | None = Field(None, max_length=512)


class TravelersUpsertRequest(BaseModel):
    travelers: list[TravelerEntry] = Field(..., min_length=1, max_length=200)
    replace: bool = Field(
        True,
        description=(
            "When True (default), the existing manifest is replaced. When "
            "False, entries are appended to the existing list."
        ),
    )


def _sanitize_traveler(entry: TravelerEntry) -> dict[str, Any]:
    """Strip plaintext PII, hash passport, return persistable dict."""
    out: dict[str, Any] = {"name": entry.name}
    digest = entry.passport_number_hash
    if entry.passport_number and not digest:
        digest = _hash_passport(entry.passport_number)
    if digest:
        out["passport_number_hash"] = digest
    if entry.dob_year is not None:
        out["dob_year"] = entry.dob_year
    if entry.dietary:
        out["dietary"] = entry.dietary
    if entry.visa_status:
        out["visa_status"] = entry.visa_status
    if entry.role:
        out["role"] = entry.role
    if entry.notes:
        out["notes"] = entry.notes
    return out


async def _log_sensitive_pi(
    *,
    reservation_id: str,
    brand_id: str,
    user_id: str,
    field: str,
    count: int,
) -> None:
    """Best-effort sensitive PI audit hook for compliance modules."""
    try:
        from app.routers import compliance as _compliance  # type: ignore

        logger_fn = getattr(_compliance, "log_sensitive_pi_access", None)
        if logger_fn is None:
            return
        await logger_fn(
            actor_user_id=user_id,
            subject_user_id=user_id,
            brand_id=brand_id,
            resource=f"reservation:{reservation_id}:travelers",
            field=field,
            count=count,
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("sensitive_pi log skipped: %s", exc)


@router.post(
    "/{rid}/travelers",
    summary="Attach a traveler manifest to a (group) reservation (老梁)",
    status_code=status.HTTP_201_CREATED,
)
async def upsert_travelers(
    rid: str,
    body: TravelersUpsertRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    state = await r.hgetall(_k_res(rid))
    if not state:
        raise HTTPException(status_code=404, detail="reservation not found")

    brand_id = state.get("brand_id", "")
    user_id = state.get("user_id", "")

    sanitized = [_sanitize_traveler(e) for e in body.travelers]
    passport_hits = sum(1 for s in sanitized if s.get("passport_number_hash"))

    pipe = r.pipeline()
    if body.replace:
        pipe.delete(_k_res_travelers(rid))
    for entry in sanitized:
        pipe.rpush(_k_res_travelers(rid), _dumps(entry))
    # Stamp manifest size on the reservation header so list views show it.
    if body.replace:
        manifest_count = len(sanitized)
    else:
        # Approximate; the precise count requires LLEN post-write.
        existing = int(state.get("travelers_count") or 0)
        manifest_count = existing + len(sanitized)
    pipe.hset(
        _k_res(rid),
        mapping={
            "travelers_count": str(manifest_count),
            "updated_at": str(_now()),
        },
    )
    await pipe.execute()

    if passport_hits > 0:
        await _log_sensitive_pi(
            reservation_id=rid,
            brand_id=brand_id,
            user_id=user_id,
            field="passport_number_hash",
            count=passport_hits,
        )

    return {
        "reservation_id": rid,
        "travelers_count": manifest_count,
        "replaced": body.replace,
        "added": len(sanitized),
    }


@router.get(
    "/{rid}/travelers",
    summary="Read the traveler manifest for a (group) reservation",
)
async def list_travelers(
    rid: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    state = await r.hgetall(_k_res(rid))
    if not state:
        raise HTTPException(status_code=404, detail="reservation not found")
    raw = await r.lrange(_k_res_travelers(rid), 0, -1)
    travelers = [_safe_loads(item, {}) for item in raw]
    return {
        "reservation_id": rid,
        "count": len(travelers),
        "travelers": travelers,
    }


@router.post(
    "/scan-no-shows",
    summary="Cron-like: mark overdue confirmed reservations as no_show",
)
async def scan_no_shows(
    body: ScanNoShowsRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    # Admin gate (matches vouchers.cleanup pattern). Constant-time compare.
    from app.security import constant_time_eq

    try:
        from app.config import settings
        expected = getattr(settings, "admin_token", None)
    except Exception:
        expected = None
    if expected and not constant_time_eq(body.admin_token, expected):
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
            no_show_resource = state.get("resource_id") or ""
            if no_show_resource:
                pipe.zrem(
                    _k_brand_res_by_resource(brand_id, no_show_resource), rid
                )
            ns_beneficiary = state.get("beneficiary_user_id") or ""
            if ns_beneficiary and ns_beneficiary != user_id:
                pipe.zrem(_k_user_res_as_beneficiary(ns_beneficiary), rid)
            ns_fulfiller = state.get("fulfiller_user_id") or ""
            if ns_fulfiller:
                pipe.zrem(_k_user_res_as_fulfiller(ns_fulfiller), rid)
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


# ── Recurring reservation series (老韩 P0) ───────────────────────────────
#
# Many brands need the same user to come back at a regular cadence: a
# 4-week haircut, a weekly fitness class, a monthly grooming appointment.
# `/series/create` expands a single declaration into N concrete
# reservations + a parent series record. The series can be cancelled or
# re-cadenced wholesale via `/series/{sid}/cancel` and
# `/series/{sid}/reschedule`.


_K_SERIES_PREFIX = "reservation_series"
_MAX_SERIES_COUNT = 104  # 2 years of weekly cadence, hard upper bound

_CADENCE_PATTERNS: dict[str, int] = {
    "weekly": 7,
    "biweekly": 14,
    "monthly": 30,
}


def _k_series(sid: str) -> str:
    return f"{_K_SERIES_PREFIX}:{sid}"


def _k_series_rids(sid: str) -> str:
    return f"{_K_SERIES_PREFIX}:{sid}:reservations"


def _k_brand_series(bid: str) -> str:
    return f"brand:{bid}:reservation_series"


def _new_sid() -> str:
    return f"rsvs_{uuid4().hex[:14]}"


def _resolve_cadence_days(
    cadence_days: int | None, cadence_pattern: str | None
) -> int:
    if cadence_days is not None and cadence_days > 0:
        return cadence_days
    if cadence_pattern and cadence_pattern in _CADENCE_PATTERNS:
        return _CADENCE_PATTERNS[cadence_pattern]
    raise HTTPException(
        status_code=400,
        detail=(
            "must supply either cadence_days>0 or "
            f"cadence_pattern in {list(_CADENCE_PATTERNS)}"
        ),
    )


class CreateSeriesRequest(BaseModel):
    brand_id: str = Field(..., min_length=1, max_length=128)
    user_id: str = Field(..., min_length=1, max_length=128)
    resource_id: str | None = Field(None, max_length=128)
    # Open type: same normalization rules as CreateReservationRequest.
    type: str = Field("appointment", min_length=1, max_length=64)
    type_label: str | None = Field(None, max_length=128)
    fulfiller_user_id: str | None = Field(None, max_length=128)
    beneficiary_user_id: str | None = Field(None, max_length=128)
    first_scheduled_at: int = Field(..., gt=0)

    @model_validator(mode="before")
    @classmethod
    def _alias_scheduled_at(cls, data: Any) -> Any:
        """Merchant-intuitive alias: accept ``scheduled_at`` (used by /create)
        as a synonym for ``first_scheduled_at`` (the canonical series name).
        """
        if isinstance(data, dict):
            if "scheduled_at" in data and "first_scheduled_at" not in data:
                data = {**data, "first_scheduled_at": data["scheduled_at"]}
        return data
    cadence_days: int | None = Field(None, ge=1, le=365)
    cadence_pattern: Literal["weekly", "biweekly", "monthly", "custom"] | None = None
    count: int = Field(..., ge=1, le=_MAX_SERIES_COUNT)
    party_size: int = Field(1, ge=1, le=1000)
    duration_minutes: int | None = Field(None, ge=1, le=24 * 60 * 30)
    recovery_voucher_template_id: str | None = Field(None, max_length=128)
    cancellation_policy: CancellationPolicy | None = None
    check_in_grace_minutes: int | None = Field(None, ge=0, le=24 * 60)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateSeriesResponse(BaseModel):
    series_id: str
    reservation_ids: list[str]
    cadence_days: int
    count: int


class SeriesRescheduleRequest(BaseModel):
    from_index: int = Field(..., ge=0)
    new_cadence_days: int | None = Field(None, ge=1, le=365)
    new_first_at: int | None = Field(None, gt=0)


class PreRemindersRequest(BaseModel):
    hours_before: list[int] = Field(..., min_length=1, max_length=10)
    push_template_id: str = Field(..., min_length=1, max_length=128)
    auction_max_cents: int | None = Field(None, ge=0, le=10_000_000)


async def _series_create_one(
    r: aioredis.Redis,
    *,
    brand_id: str,
    user_id: str,
    scheduled_at: int,
    party_size: int,
    res_type: str,
    metadata: dict[str, Any],
    recovery_voucher_template_id: str | None,
    cancellation_policy: dict[str, Any],
    check_in_grace_minutes: int,
    resource_id: str | None,
    duration_minutes: int | None,
    series_id: str,
    series_index: int,
    type_label: str | None = None,
    fulfiller_user_id: str | None = None,
    beneficiary_user_id: str | None = None,
) -> str:
    """Internal helper — create a single reservation belonging to a series.

    Returns the new reservation_id. Does not emit `reservation.confirmed`
    (the parent series fires a series.created event instead).
    """
    rid = _new_rid()
    now = _now()
    state: dict[str, str] = {
        "reservation_id": rid,
        "brand_id": brand_id,
        "user_id": user_id,
        "scheduled_at": str(scheduled_at),
        "party_size": str(party_size),
        "type": res_type,
        "status": "confirmed",
        "created_at": str(now),
        "updated_at": str(now),
        "check_in_grace_minutes": str(check_in_grace_minutes),
        "metadata": _dumps(metadata),
        "cancellation_policy": _dumps(cancellation_policy),
        "recovery_voucher_template_id": recovery_voucher_template_id or "",
        "series_id": series_id,
        "series_index": str(series_index),
    }
    if resource_id:
        state["resource_id"] = resource_id
    if duration_minutes is not None:
        state["duration_minutes"] = str(duration_minutes)
    if type_label:
        state["type_label"] = type_label
    if fulfiller_user_id:
        state["fulfiller_user_id"] = fulfiller_user_id
    if beneficiary_user_id:
        state["beneficiary_user_id"] = beneficiary_user_id

    pipe = r.pipeline()
    pipe.hset(_k_res(rid), mapping=state)
    pipe.zadd(_k_brand_res(brand_id), {rid: scheduled_at})
    pipe.zadd(_k_user_res(user_id), {rid: scheduled_at})
    if resource_id:
        pipe.zadd(
            _k_brand_res_by_resource(brand_id, resource_id),
            {rid: scheduled_at},
        )
    if beneficiary_user_id and beneficiary_user_id != user_id:
        pipe.zadd(
            _k_user_res_as_beneficiary(beneficiary_user_id),
            {rid: scheduled_at},
        )
    if fulfiller_user_id:
        pipe.zadd(
            _k_user_res_as_fulfiller(fulfiller_user_id),
            {rid: scheduled_at},
        )
    pipe.hincrby(_k_brand_stats(brand_id), "total_confirmed", 1)
    pipe.hincrby(_k_brand_stats(brand_id), "party_size_sum", party_size)
    pipe.hincrby(_k_brand_stats(brand_id), "party_size_count", 1)
    await pipe.execute()

    await _emit_event(
        r,
        event_type="reservation.created",
        reservation_id=rid,
        brand_id=brand_id,
        user_id=user_id,
        extra={
            "scheduled_at": scheduled_at,
            "type": res_type,
            "party_size": party_size,
            "series_id": series_id,
            "series_index": series_index,
        },
    )
    return rid


@router.post(
    "/series/create",
    response_model=CreateSeriesResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a recurring reservation series (e.g. 12 monthly haircuts)",
)
async def create_series(
    body: CreateSeriesRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> CreateSeriesResponse:
    now = _now()
    if body.first_scheduled_at <= now:
        raise HTTPException(
            status_code=400, detail="first_scheduled_at must be in the future"
        )
    cadence_days = _resolve_cadence_days(body.cadence_days, body.cadence_pattern)

    # Merge brand defaults for grace + policy + recovery voucher.
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

    norm_type, norm_label = _normalize_type(body.type, body.type_label)

    sid = _new_sid()
    cadence_seconds = cadence_days * 24 * 3600
    reservation_ids: list[str] = []

    # Persist series header before fan-out so partial failures still leave
    # the parent record discoverable.
    await r.hset(
        _k_series(sid),
        mapping={
            "series_id": sid,
            "brand_id": body.brand_id,
            "user_id": body.user_id,
            "resource_id": body.resource_id or "",
            "type": norm_type,
            "type_label": norm_label or "",
            "fulfiller_user_id": body.fulfiller_user_id or "",
            "beneficiary_user_id": body.beneficiary_user_id or "",
            "cadence_days": str(cadence_days),
            "cadence_pattern": body.cadence_pattern or "custom",
            "first_scheduled_at": str(body.first_scheduled_at),
            "count": str(body.count),
            "party_size": str(body.party_size),
            "duration_minutes": str(body.duration_minutes or 0),
            "recovery_voucher_template_id": recovery_template,
            "status": "active",
            "created_at": str(now),
            "updated_at": str(now),
            "metadata": _dumps(body.metadata),
        },
    )
    await r.sadd(_k_brand_series(body.brand_id), sid)

    for i in range(body.count):
        scheduled_at = body.first_scheduled_at + i * cadence_seconds
        rid = await _series_create_one(
            r,
            brand_id=body.brand_id,
            user_id=body.user_id,
            scheduled_at=scheduled_at,
            party_size=body.party_size,
            res_type=norm_type,
            metadata=body.metadata,
            recovery_voucher_template_id=recovery_template or None,
            cancellation_policy=cancellation_policy,
            check_in_grace_minutes=grace,
            resource_id=body.resource_id,
            duration_minutes=body.duration_minutes,
            series_id=sid,
            series_index=i,
            type_label=norm_label,
            fulfiller_user_id=body.fulfiller_user_id,
            beneficiary_user_id=body.beneficiary_user_id,
        )
        reservation_ids.append(rid)
        await r.rpush(_k_series_rids(sid), rid)

    logger.info(
        "reservation series created sid=%s brand=%s user=%s count=%s cadence=%sd",
        sid, body.brand_id, body.user_id, body.count, cadence_days,
    )

    return CreateSeriesResponse(
        series_id=sid,
        reservation_ids=reservation_ids,
        cadence_days=cadence_days,
        count=body.count,
    )


@router.get("/series/{series_id}", summary="Get a reservation series header + child rids")
async def get_series(
    series_id: str, r: aioredis.Redis = Depends(get_redis)
) -> dict[str, Any]:
    state = await r.hgetall(_k_series(series_id))
    if not state:
        raise HTTPException(status_code=404, detail="series not found")
    rids = await r.lrange(_k_series_rids(series_id), 0, -1)
    return {
        "series_id": series_id,
        "brand_id": state.get("brand_id", ""),
        "user_id": state.get("user_id", ""),
        "resource_id": state.get("resource_id") or None,
        "type": state.get("type", "appointment"),
        "cadence_days": int(state.get("cadence_days") or 0),
        "cadence_pattern": state.get("cadence_pattern") or None,
        "first_scheduled_at": int(state.get("first_scheduled_at") or 0),
        "count": int(state.get("count") or 0),
        "party_size": int(state.get("party_size") or 1),
        "duration_minutes": int(state.get("duration_minutes") or 0) or None,
        "status": state.get("status", "active"),
        "created_at": int(state.get("created_at") or 0),
        "updated_at": int(state.get("updated_at") or 0),
        "recovery_voucher_template_id": (
            state.get("recovery_voucher_template_id") or None
        ),
        "metadata": _safe_loads(state.get("metadata"), {}),
        "reservation_ids": rids,
    }


@router.post(
    "/series/{series_id}/cancel",
    summary="Cancel every future reservation in a series",
)
async def cancel_series(
    series_id: str, r: aioredis.Redis = Depends(get_redis)
) -> dict[str, Any]:
    state = await r.hgetall(_k_series(series_id))
    if not state:
        raise HTTPException(status_code=404, detail="series not found")
    if state.get("status") in ("cancelled",):
        raise HTTPException(status_code=409, detail="series already cancelled")

    rids = await r.lrange(_k_series_rids(series_id), 0, -1)
    now = _now()
    cancelled_rids: list[str] = []
    skipped_rids: list[str] = []

    for rid in rids:
        rstate = await r.hgetall(_k_res(rid))
        if not rstate:
            skipped_rids.append(rid)
            continue
        if rstate.get("status") not in ("confirmed", "rescheduled"):
            skipped_rids.append(rid)
            continue
        scheduled_at = int(rstate.get("scheduled_at") or 0)
        if scheduled_at <= now:
            # Past reservations stay where they are — let scan-no-shows
            # decide. Only cancel forward-dated commitments.
            skipped_rids.append(rid)
            continue

        brand_id = rstate.get("brand_id", "")
        user_id = rstate.get("user_id", "")
        resource_id = rstate.get("resource_id") or ""

        pipe = r.pipeline()
        pipe.hset(
            _k_res(rid),
            mapping={
                "status": "cancelled_by_user",
                "cancelled_at": str(now),
                "updated_at": str(now),
                "cancellation_reason": "series_cancelled",
                "cancellation_by": "user",
                "refund_pct": "100",
            },
        )
        pipe.zrem(_k_brand_res(brand_id), rid)
        pipe.zrem(_k_user_res(user_id), rid)
        if resource_id:
            pipe.zrem(_k_brand_res_by_resource(brand_id, resource_id), rid)
        beneficiary_uid = rstate.get("beneficiary_user_id") or ""
        if beneficiary_uid and beneficiary_uid != user_id:
            pipe.zrem(_k_user_res_as_beneficiary(beneficiary_uid), rid)
        fulfiller_uid = rstate.get("fulfiller_user_id") or ""
        if fulfiller_uid:
            pipe.zrem(_k_user_res_as_fulfiller(fulfiller_uid), rid)
        pipe.hincrby(_k_brand_stats(brand_id), "total_cancelled_by_user", 1)
        await pipe.execute()

        await _emit_event(
            r,
            event_type="reservation.cancelled_by_user",
            reservation_id=rid,
            brand_id=brand_id,
            user_id=user_id,
            extra={
                "series_id": series_id,
                "reason": "series_cancelled",
                "penalty": False,
                "refund_pct": 100,
            },
        )
        cancelled_rids.append(rid)

    await r.hset(
        _k_series(series_id),
        mapping={
            "status": "cancelled",
            "cancelled_at": str(now),
            "updated_at": str(now),
        },
    )
    return {
        "series_id": series_id,
        "status": "cancelled",
        "cancelled_reservations": cancelled_rids,
        "skipped_reservations": skipped_rids,
    }


@router.post(
    "/series/{series_id}/reschedule",
    summary="Re-cadence the upcoming portion of a series",
)
async def reschedule_series(
    series_id: str,
    body: SeriesRescheduleRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    state = await r.hgetall(_k_series(series_id))
    if not state:
        raise HTTPException(status_code=404, detail="series not found")
    if state.get("status") != "active":
        raise HTTPException(
            status_code=409,
            detail=f"cannot reschedule series in status={state.get('status')}",
        )

    rids = await r.lrange(_k_series_rids(series_id), 0, -1)
    if body.from_index >= len(rids):
        raise HTTPException(
            status_code=400, detail="from_index >= series length"
        )

    now = _now()
    old_cadence_days = int(state.get("cadence_days") or 7)
    new_cadence_days = body.new_cadence_days or old_cadence_days
    cadence_seconds = new_cadence_days * 24 * 3600

    # Determine the anchor — first reservation's new time.
    if body.new_first_at is not None:
        if body.new_first_at <= now:
            raise HTTPException(
                status_code=400, detail="new_first_at must be in the future"
            )
        anchor = body.new_first_at
    else:
        # Keep first affected reservation's scheduled_at; only re-cadence rest.
        first_rid = rids[body.from_index]
        first_state = await r.hgetall(_k_res(first_rid))
        anchor = int(first_state.get("scheduled_at") or now)

    rescheduled: list[dict[str, Any]] = []
    for offset_idx, rid in enumerate(rids[body.from_index :]):
        rstate = await r.hgetall(_k_res(rid))
        if not rstate:
            continue
        if rstate.get("status") not in ("confirmed", "rescheduled"):
            continue
        new_sched = anchor + offset_idx * cadence_seconds
        if new_sched <= now:
            # Skip past slots after re-anchoring.
            continue

        brand_id = rstate.get("brand_id", "")
        user_id = rstate.get("user_id", "")
        resource_id = rstate.get("resource_id") or ""
        old_sched = int(rstate.get("scheduled_at") or 0)

        pipe = r.pipeline()
        pipe.hset(
            _k_res(rid),
            mapping={
                "scheduled_at": str(new_sched),
                "status": "confirmed",
                "updated_at": str(now),
            },
        )
        pipe.zadd(_k_brand_res(brand_id), {rid: new_sched})
        pipe.zadd(_k_user_res(user_id), {rid: new_sched})
        if resource_id:
            pipe.zadd(
                _k_brand_res_by_resource(brand_id, resource_id),
                {rid: new_sched},
            )
        beneficiary_uid = rstate.get("beneficiary_user_id") or ""
        if beneficiary_uid and beneficiary_uid != user_id:
            pipe.zadd(
                _k_user_res_as_beneficiary(beneficiary_uid),
                {rid: new_sched},
            )
        fulfiller_uid = rstate.get("fulfiller_user_id") or ""
        if fulfiller_uid:
            pipe.zadd(
                _k_user_res_as_fulfiller(fulfiller_uid),
                {rid: new_sched},
            )
        pipe.hincrby(_k_brand_stats(brand_id), "total_rescheduled", 1)
        await pipe.execute()

        await _emit_event(
            r,
            event_type="reservation.rescheduled",
            reservation_id=rid,
            brand_id=brand_id,
            user_id=user_id,
            extra={
                "old_scheduled_at": old_sched,
                "new_scheduled_at": new_sched,
                "series_id": series_id,
            },
        )
        rescheduled.append(
            {
                "reservation_id": rid,
                "old_scheduled_at": old_sched,
                "new_scheduled_at": new_sched,
            }
        )

    await r.hset(
        _k_series(series_id),
        mapping={
            "cadence_days": str(new_cadence_days),
            "updated_at": str(now),
        },
    )
    return {
        "series_id": series_id,
        "from_index": body.from_index,
        "new_cadence_days": new_cadence_days,
        "anchor": anchor,
        "rescheduled_count": len(rescheduled),
        "rescheduled": rescheduled,
    }


# ── Pre-event reminders ───────────────────────────────────────────────────


@router.post(
    "/{rid}/pre-reminders",
    summary="Schedule pre-event push reminders for a reservation",
)
async def schedule_pre_reminders(
    rid: str,
    body: PreRemindersRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Schedule one push per ``hours_before`` entry, anchored on scheduled_at.

    Uses the push_engine schedule primitive when available; falls back to
    a queued record under ``reservation:{rid}:reminders`` so an
    out-of-band worker can pick them up.
    """
    state = await r.hgetall(_k_res(rid))
    if not state:
        raise HTTPException(status_code=404, detail="reservation not found")
    if state.get("status") not in ("confirmed", "rescheduled"):
        raise HTTPException(
            status_code=409,
            detail=f"cannot schedule reminders for status={state.get('status')}",
        )

    scheduled_at = int(state.get("scheduled_at") or 0)
    brand_id = state.get("brand_id", "")
    user_id = state.get("user_id", "")
    now = _now()

    # Attempt to use the push_engine for proper delivery.
    push_schedule = None
    try:
        from app.routers.push_engine import (
            AuctionParams,
            ContextPredicate,
            ScheduleRequest,
            schedule_push,
        )
        push_schedule = (schedule_push, ScheduleRequest, ContextPredicate, AuctionParams)
    except Exception as exc:  # pragma: no cover
        logger.warning("push_engine unavailable for pre-reminders: %s", exc)

    scheduled: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []

    for hours in sorted(set(body.hours_before), reverse=True):
        fire_at = scheduled_at - int(hours) * 3600
        if fire_at <= now:
            # Skip reminders whose window already closed.
            continue

        if push_schedule is not None:
            schedule_push_fn, ScheduleReq, _CtxP, _Auct = push_schedule
            try:
                req = ScheduleReq(
                    kid=user_id,
                    fire_at_ts=float(fire_at),
                )
                resp = await schedule_push_fn(req, r)
                scheduled.append(
                    {
                        "hours_before": hours,
                        "fire_at": fire_at,
                        "schedule_id": getattr(resp, "schedule_id", None),
                        "push_template_id": body.push_template_id,
                    }
                )
                # Persist association so the worker knows what to render.
                try:
                    await r.hset(
                        f"reservation:{rid}:reminder:{resp.schedule_id}",
                        mapping={
                            "reservation_id": rid,
                            "brand_id": brand_id,
                            "user_id": user_id,
                            "push_template_id": body.push_template_id,
                            "hours_before": str(hours),
                            "fire_at": str(fire_at),
                            "exempt_from_cap": "1",
                        },
                    )
                    await r.expire(
                        f"reservation:{rid}:reminder:{resp.schedule_id}",
                        max(86400, fire_at - now + 86400),
                    )
                except Exception:  # pragma: no cover
                    pass
                continue
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "push_engine schedule failed for rid=%s hours=%s: %s",
                    rid, hours, exc,
                )

        # Fallback: queue the reminder under the reservation; a worker
        # consumes ``reservation:{rid}:reminders`` to drive delivery.
        payload = {
            "reservation_id": rid,
            "brand_id": brand_id,
            "user_id": user_id,
            "push_template_id": body.push_template_id,
            "hours_before": hours,
            "fire_at": fire_at,
            "exempt_from_cap": True,
        }
        try:
            await r.rpush(f"reservation:{rid}:reminders", _dumps(payload))
        except Exception:  # pragma: no cover
            pass
        fallback.append(payload)

    return {
        "reservation_id": rid,
        "scheduled_via_push_engine": scheduled,
        "scheduled_via_fallback": fallback,
        "skipped_past_count": len(set(body.hours_before)) - len(scheduled) - len(fallback),
    }


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
