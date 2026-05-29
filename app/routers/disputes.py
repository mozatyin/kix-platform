"""Disputes + Refund router.

Formal dispute flow for merchants who challenge a KiX commission charge —
e.g. they suspect the conversion was a fake user, an existing customer, or
fraud. Opening a dispute *freezes* the underlying wallet charge so it can't
be collected against until an admin (or auto-policy) resolves it.

Resolution paths
----------------
    refund_full     → wallet credit back full amount + remove the
                      attribution event from the user journey
    refund_partial  → wallet credit back a portion (charge stays in audit
                      trail but marked partially_refunded)
    reject          → dispute closed, charge becomes collectable again
    withdrawn       → merchant pulls their own dispute

Auto-approve
------------
If the disputed amount is below `auto_refund_under_cents` AND the merchant's
30-day dispute rate is under 5%, we auto-approve a `refund_full` without
admin intervention. Everything else queues for admin review with an SLA.

Redis schema
------------
    dispute:{dispute_id}              HASH   (state + metadata)
    dispute:{dispute_id}:timeline     LIST   (JSON event records)
    brand:{bid}:disputes              ZSET   (score=opened_at, member=did)
    disputes:queue:pending            ZSET   (score=sla_breach_at, member=did)
    disputes:policy                   HASH   (global config)
    disputes:stats                    HASH   (rolling counters)
    disputes:by_charge:{charge_id}    STRING (dispute_id — uniqueness guard)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal
from uuid import uuid4

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, HttpUrl, model_validator

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ────────────────────────────────────────────────────────────
DISPUTE_CATEGORIES = {
    "fake_user",
    "existing_customer",
    "fraud_suspected",
    "wrong_attribution",
    "other",
}

VALID_OPEN_STATES = {"pending_review", "under_investigation"}
TERMINAL_STATES = {
    "resolved_refund_full",
    "resolved_refund_partial",
    "resolved_reject",
    "withdrawn",
}

DEFAULT_AUTO_REFUND_UNDER_CENTS = 1000        # ¥10
DEFAULT_SLA_RESPONSE_HOURS = 48
DEFAULT_MAX_DISPUTES_PER_MONTH = 200
GOOD_STANDING_DISPUTE_RATE = 0.05             # < 5% → auto-approve eligible
DISPUTE_RATE_WINDOW_SECONDS = 30 * 86400

MAX_EVIDENCE_TEXT = 4096
MAX_COMMENT_LEN = 2048
MAX_WATCH_RETRIES = 8
TIMELINE_MAX_ENTRIES = 200
QUEUE_DEFAULT_LIMIT = 50
QUEUE_MAX_LIMIT = 500


# ── Key helpers ──────────────────────────────────────────────────────────
def _k_dispute(did: str) -> str:
    return f"dispute:{did}"


def _k_timeline(did: str) -> str:
    return f"dispute:{did}:timeline"


def _k_brand_disputes(bid: str) -> str:
    return f"brand:{bid}:disputes"


def _k_by_charge(charge_id: str) -> str:
    return f"disputes:by_charge:{charge_id}"


_K_QUEUE_PENDING = "disputes:queue:pending"
_K_POLICY = "disputes:policy"
_K_STATS = "disputes:stats"


# ── Wallet keys we touch (mirror wallet.py) ──────────────────────────────
def _k_wallet_charge(charge_id: str) -> str:
    return f"wallet:charge:{charge_id}"


# ── Pydantic models ──────────────────────────────────────────────────────
class OpenDisputeRequest(BaseModel):
    brand_id: str = Field(..., min_length=1, max_length=128)
    charge_id: str | None = None
    conversion_id: str | None = None
    impression_token: str | None = None
    category: Literal[
        "fake_user",
        "existing_customer",
        "fraud_suspected",
        "wrong_attribution",
        "other",
    ]
    evidence_text: str = Field(..., min_length=1, max_length=MAX_EVIDENCE_TEXT)
    evidence_url: HttpUrl | None = None

    @model_validator(mode="after")
    def _at_least_one_ref(self) -> "OpenDisputeRequest":
        if not any([self.charge_id, self.conversion_id, self.impression_token]):
            raise ValueError(
                "one of charge_id, conversion_id, impression_token is required"
            )
        return self


class OpenDisputeResponse(BaseModel):
    dispute_id: str
    status: str = "pending_review"
    auto_pause_charge: bool = True
    auto_resolved: bool = False
    refund_id: str | None = None


class TimelineEvent(BaseModel):
    ts: float
    actor: str  # "merchant" | "admin" | "system"
    kind: str  # "opened" | "comment" | "state_change" | "resolved" | ...
    payload: dict[str, Any] = Field(default_factory=dict)


class DisputeDetail(BaseModel):
    dispute_id: str
    brand_id: str
    charge_id: str | None
    conversion_id: str | None
    impression_token: str | None
    category: str
    status: str
    evidence_text: str
    evidence_url: str | None
    amount_cents: int
    opened_at: float
    sla_breach_at: float
    resolved_at: float | None
    decision: str | None
    refund_id: str | None
    refund_cents: int | None
    reason: str | None
    timeline: list[TimelineEvent]


class DisputeSummary(BaseModel):
    dispute_id: str
    brand_id: str
    charge_id: str | None
    category: str
    status: str
    amount_cents: int
    opened_at: float
    sla_breach_at: float


class AdminCommentRequest(BaseModel):
    comment: str = Field(..., min_length=1, max_length=MAX_COMMENT_LEN)
    internal: bool = True


class AdminResolveRequest(BaseModel):
    decision: Literal["refund_full", "refund_partial", "reject"]
    refund_cents: int | None = Field(None, ge=1, le=10_000_000)
    reason: str = Field(..., min_length=1, max_length=MAX_COMMENT_LEN)

    @model_validator(mode="after")
    def _partial_needs_amount(self) -> "AdminResolveRequest":
        if self.decision == "refund_partial" and self.refund_cents is None:
            raise ValueError("refund_partial requires refund_cents")
        if self.decision == "reject" and self.refund_cents is not None:
            raise ValueError("reject must not include refund_cents")
        return self


class WithdrawRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=MAX_COMMENT_LEN)


class PolicyUpdate(BaseModel):
    auto_refund_under_cents: int | None = Field(None, ge=0, le=10_000_000)
    sla_response_hours: int | None = Field(None, ge=1, le=720)
    max_disputes_per_brand_per_month: int | None = Field(None, ge=1, le=10_000)


class Policy(BaseModel):
    auto_refund_under_cents: int = DEFAULT_AUTO_REFUND_UNDER_CENTS
    sla_response_hours: int = DEFAULT_SLA_RESPONSE_HOURS
    max_disputes_per_brand_per_month: int = DEFAULT_MAX_DISPUTES_PER_MONTH


class Stats(BaseModel):
    total_open: int
    total_resolved: int
    refund_rate: float
    avg_resolution_hours: float
    top_categories: list[dict[str, Any]]


# ── Policy helpers ───────────────────────────────────────────────────────
async def _get_policy(r: aioredis.Redis) -> Policy:
    raw = await r.hgetall(_K_POLICY)
    if not raw:
        return Policy()
    try:
        return Policy(
            auto_refund_under_cents=int(
                raw.get("auto_refund_under_cents", DEFAULT_AUTO_REFUND_UNDER_CENTS)
            ),
            sla_response_hours=int(
                raw.get("sla_response_hours", DEFAULT_SLA_RESPONSE_HOURS)
            ),
            max_disputes_per_brand_per_month=int(
                raw.get(
                    "max_disputes_per_brand_per_month",
                    DEFAULT_MAX_DISPUTES_PER_MONTH,
                )
            ),
        )
    except (ValueError, TypeError):
        logger.warning("Malformed disputes policy, using defaults")
        return Policy()


# ── Timeline helper ──────────────────────────────────────────────────────
async def _append_timeline(
    r: aioredis.Redis,
    dispute_id: str,
    actor: str,
    kind: str,
    payload: dict[str, Any] | None = None,
    pipe: aioredis.client.Pipeline | None = None,
) -> None:
    event = {
        "ts": time.time(),
        "actor": actor,
        "kind": kind,
        "payload": payload or {},
    }
    encoded = json.dumps(event, ensure_ascii=False)
    target = pipe if pipe is not None else r
    if pipe is not None:
        pipe.rpush(_k_timeline(dispute_id), encoded)
        pipe.ltrim(_k_timeline(dispute_id), -TIMELINE_MAX_ENTRIES, -1)
    else:
        await target.rpush(_k_timeline(dispute_id), encoded)
        await target.ltrim(_k_timeline(dispute_id), -TIMELINE_MAX_ENTRIES, -1)


async def _load_timeline(
    r: aioredis.Redis, dispute_id: str
) -> list[TimelineEvent]:
    raw = await r.lrange(_k_timeline(dispute_id), 0, -1)
    out: list[TimelineEvent] = []
    for entry in raw:
        try:
            d = json.loads(entry)
            out.append(
                TimelineEvent(
                    ts=float(d.get("ts", 0)),
                    actor=str(d.get("actor", "system")),
                    kind=str(d.get("kind", "unknown")),
                    payload=d.get("payload") or {},
                )
            )
        except (ValueError, TypeError):
            continue
    return out


# ── Dispute rate helper (for auto-approve gating) ────────────────────────
async def _brand_dispute_rate(brand_id: str, r: aioredis.Redis) -> float:
    """Disputes opened in last 30d / charges in last 30d.

    Both numerators and denominators are estimates; we walk the brand's
    dispute ZSET (cheap) and use total_spent + a tx-list scan only when
    necessary. For MVP we use disputes-per-30d normalised by a cap of 100
    historic events so brands without much charge history don't get
    branded "high dispute rate" from a single dispute.
    """
    now = time.time()
    window_start = now - DISPUTE_RATE_WINDOW_SECONDS

    recent_disputes = await r.zcount(
        _k_brand_disputes(brand_id), window_start, now
    )

    # Use total transactions as a rough denominator. Scan a slab — we just
    # need an order-of-magnitude estimate, not perfect accuracy.
    tx_slab = await r.lrange(f"wallet:{brand_id}:transactions", -500, -1)
    denom = max(len(tx_slab), 20)  # floor avoids divide-by-tiny
    return recent_disputes / denom


# ── Stats helpers ────────────────────────────────────────────────────────
async def _stats_incr(
    r: aioredis.Redis,
    field: str,
    delta: int = 1,
    pipe: aioredis.client.Pipeline | None = None,
) -> None:
    target = pipe if pipe is not None else r
    if pipe is not None:
        pipe.hincrby(_K_STATS, field, delta)
    else:
        await target.hincrby(_K_STATS, field, delta)


# ── Wallet integration (in-process, not HTTP) ─────────────────────────────
async def _freeze_charge(
    r: aioredis.Redis, charge_id: str, dispute_id: str
) -> tuple[bool, int, str | None]:
    """Mark a wallet charge as disputed so collection halts.

    Returns (ok, amount_cents, error). Uses WATCH so a concurrent refund
    can't slip past us.
    """
    key = _k_wallet_charge(charge_id)
    attempts = 0
    while attempts < MAX_WATCH_RETRIES:
        attempts += 1
        try:
            async with r.pipeline(transaction=True) as pipe:
                await pipe.watch(key)
                ch = await pipe.hgetall(key)
                if not ch:
                    await pipe.unwatch()
                    return False, 0, "charge_not_found"
                cur_status = ch.get("status")
                if cur_status == "disputed":
                    # Already disputed — return amount + idempotent ok.
                    await pipe.unwatch()
                    return True, int(ch.get("amount") or 0), None
                if cur_status not in ("completed",):
                    await pipe.unwatch()
                    return (
                        False,
                        int(ch.get("amount") or 0),
                        f"charge_not_disputable:{cur_status}",
                    )
                amount = int(ch.get("amount") or 0)
                pipe.multi()
                pipe.hset(
                    key,
                    mapping={
                        "status": "disputed",
                        "disputed_at": time.time(),
                        "dispute_id": dispute_id,
                    },
                )
                await pipe.execute()
                return True, amount, None
        except aioredis.WatchError:
            continue
    return False, 0, "freeze_contention"


async def _unfreeze_charge(r: aioredis.Redis, charge_id: str) -> None:
    """Revert a disputed charge back to collectable. Used on reject/withdraw."""
    key = _k_wallet_charge(charge_id)
    attempts = 0
    while attempts < MAX_WATCH_RETRIES:
        attempts += 1
        try:
            async with r.pipeline(transaction=True) as pipe:
                await pipe.watch(key)
                ch = await pipe.hgetall(key)
                if not ch or ch.get("status") != "disputed":
                    await pipe.unwatch()
                    return
                pipe.multi()
                pipe.hset(key, "status", "completed")
                pipe.hdel(key, "disputed_at", "dispute_id")
                await pipe.execute()
                return
        except aioredis.WatchError:
            continue


async def _wallet_refund_internal(
    r: aioredis.Redis, brand_id: str, charge_id: str, amount_cents: int, reason: str
) -> tuple[bool, str | None, str | None]:
    """Call the wallet's refund logic in-process.

    We import lazily to avoid a circular import at module load. The wallet
    refund() endpoint is the canonical implementation — we just synthesise
    the request object.
    """
    try:
        from app.routers.wallet import (
            RefundRequest as WalletRefundRequest,
            _k_charge as _wk_charge,
            _k_balance as _wk_balance,
            _k_daily_spent as _wk_daily,
            _k_total_spent as _wk_total,
            _k_refund as _wk_refund,
            _k_tx_list as _wk_tx_list,
            TX_LIST_MAX,
        )
    except Exception as exc:  # pragma: no cover
        logger.error("wallet import failed: %s", exc)
        return False, None, "wallet_unavailable"

    # We mirror wallet.refund() but support partial amounts AND accept a
    # charge that is in "disputed" state (the wallet's own endpoint only
    # allows "completed"). This is exactly the controlled bypass that the
    # disputes flow exists to provide.
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ckey = _wk_charge(charge_id)
    balance_key = _wk_balance(brand_id)
    daily_key = _wk_daily(brand_id, today)
    total_key = _wk_total(brand_id)

    attempts = 0
    while attempts < MAX_WATCH_RETRIES:
        attempts += 1
        try:
            async with r.pipeline(transaction=True) as pipe:
                await pipe.watch(ckey, balance_key, daily_key, total_key)
                ch = await pipe.hgetall(ckey)
                if not ch:
                    await pipe.unwatch()
                    return False, None, "charge_not_found"
                if ch.get("brand_id") != brand_id:
                    await pipe.unwatch()
                    return False, None, "brand_mismatch"
                ch_status = ch.get("status")
                if ch_status not in ("completed", "disputed", "partially_refunded"):
                    await pipe.unwatch()
                    return False, None, f"charge_not_refundable:{ch_status}"

                already = int(ch.get("refunded_amount") or 0)
                ch_amount = int(ch.get("amount") or 0)
                remaining = ch_amount - already
                if amount_cents > remaining:
                    await pipe.unwatch()
                    return False, None, "refund_exceeds_remaining"

                refund_id = uuid4().hex
                now = time.time()
                new_refunded = already + amount_cents
                final_status = (
                    "refunded" if new_refunded >= ch_amount else "partially_refunded"
                )

                pipe.multi()
                pipe.incrby(balance_key, amount_cents)
                pipe.decrby(daily_key, amount_cents)
                pipe.decrby(total_key, amount_cents)
                pipe.hset(
                    ckey,
                    mapping={
                        "status": final_status,
                        "refunded_amount": new_refunded,
                        "last_refund_id": refund_id,
                        "last_refunded_at": now,
                    },
                )
                pipe.hset(
                    _wk_refund(refund_id),
                    mapping={
                        "refund_id": refund_id,
                        "brand_id": brand_id,
                        "charge_id": charge_id,
                        "amount": amount_cents,
                        "reason": reason,
                        "ts": now,
                        "status": "completed",
                        "source": "dispute",
                    },
                )
                pipe.rpush(_wk_tx_list(brand_id), refund_id)
                pipe.ltrim(_wk_tx_list(brand_id), -TX_LIST_MAX, -1)
                await pipe.execute()
                return True, refund_id, None
        except aioredis.WatchError:
            continue
    return False, None, "refund_contention"


# ── Attribution rollback ─────────────────────────────────────────────────
async def _cascade_remove_attribution(
    r: aioredis.Redis, event_id: str | None
) -> None:
    """On a full refund, scrub the attribution event from user journey.

    No-op if the event_id is unknown or the record is already gone.
    """
    if not event_id:
        return
    try:
        ev = await r.hgetall(f"attr:{event_id}")
        if not ev:
            return
        user_id = ev.get("user_id")
        device_fp = ev.get("device_fingerprint")
        async with r.pipeline(transaction=False) as pipe:
            if user_id:
                pipe.lrem(f"user:{user_id}:attr_journey", 0, event_id)
            if device_fp:
                pipe.lrem(f"device:{device_fp}:attr_journey", 0, event_id)
            pipe.delete(f"attr:{event_id}")
            await pipe.execute()
        logger.info(
            "attribution rollback event=%s user=%s device=%s",
            event_id,
            user_id,
            device_fp,
        )
    except Exception as exc:
        # Attribution rollback is best-effort: wallet refund already won.
        logger.warning("attribution rollback failed event=%s: %s", event_id, exc)


# ── POST /open ────────────────────────────────────────────────────────────
@router.post("/open", response_model=OpenDisputeResponse)
async def open_dispute(
    body: OpenDisputeRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> OpenDisputeResponse:
    """Merchant raises a dispute. Freezes the charge, queues for review.

    If the amount is small AND the brand has a clean track record, we
    auto-approve a full refund immediately — saves admin toil on the long
    tail of micro-disputes.
    """
    policy = await _get_policy(r)
    now = time.time()

    # Monthly cap — protects KiX from a single bad-faith brand spamming.
    month_window_start = now - 30 * 86400
    monthly_count = await r.zcount(
        _k_brand_disputes(body.brand_id), month_window_start, now
    )
    if monthly_count >= policy.max_disputes_per_brand_per_month:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "brand_monthly_dispute_limit_reached",
                "limit": policy.max_disputes_per_brand_per_month,
                "current": monthly_count,
            },
        )

    # Uniqueness guard: one open dispute per charge_id.
    if body.charge_id:
        prev = await r.get(_k_by_charge(body.charge_id))
        if prev:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "dispute_already_exists_for_charge",
                    "dispute_id": prev,
                },
            )

    # Freeze the wallet charge first — if it fails we abort without creating
    # the dispute record (avoids orphan disputes pointing at nothing).
    amount_cents = 0
    if body.charge_id:
        ok, amount_cents, err = await _freeze_charge(r, body.charge_id, "_pending_")
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "charge_freeze_failed",
                    "reason": err,
                    "charge_id": body.charge_id,
                },
            )

    dispute_id = uuid4().hex
    sla_breach_at = now + policy.sla_response_hours * 3600

    record = {
        "dispute_id": dispute_id,
        "brand_id": body.brand_id,
        "charge_id": body.charge_id or "",
        "conversion_id": body.conversion_id or "",
        "impression_token": body.impression_token or "",
        "category": body.category,
        "status": "pending_review",
        "evidence_text": body.evidence_text,
        "evidence_url": str(body.evidence_url) if body.evidence_url else "",
        "amount_cents": amount_cents,
        "opened_at": now,
        "sla_breach_at": sla_breach_at,
    }
    await r.hset(_k_dispute(dispute_id), mapping=record)
    await r.zadd(_k_brand_disputes(body.brand_id), {dispute_id: now})
    await r.zadd(_K_QUEUE_PENDING, {dispute_id: sla_breach_at})
    if body.charge_id:
        await r.set(_k_by_charge(body.charge_id), dispute_id)
        # Patch the freeze record with the real dispute_id (the freeze used
        # a placeholder so we could short-circuit on failure without a UUID
        # leaking out).
        await r.hset(
            _k_wallet_charge(body.charge_id), "dispute_id", dispute_id
        )
    await _stats_incr(r, "total_open")
    await _stats_incr(r, f"category:{body.category}")

    await _append_timeline(
        r,
        dispute_id,
        actor="merchant",
        kind="opened",
        payload={
            "category": body.category,
            "charge_id": body.charge_id,
            "amount_cents": amount_cents,
        },
    )

    logger.info(
        "dispute opened id=%s brand=%s category=%s amount=%s charge=%s",
        dispute_id,
        body.brand_id,
        body.category,
        amount_cents,
        body.charge_id,
    )

    # ── Auto-approve gate ────────────────────────────────────────────────
    auto_resolved = False
    refund_id: str | None = None
    if (
        body.charge_id
        and amount_cents > 0
        and amount_cents < policy.auto_refund_under_cents
    ):
        rate = await _brand_dispute_rate(body.brand_id, r)
        if rate < GOOD_STANDING_DISPUTE_RATE:
            logger.info(
                "auto-approve eligible id=%s amount=%s rate=%.3f",
                dispute_id,
                amount_cents,
                rate,
            )
            ok, ref_id, err = await _wallet_refund_internal(
                r,
                body.brand_id,
                body.charge_id,
                amount_cents,
                reason=f"auto_refund_dispute:{dispute_id}",
            )
            if ok:
                refund_id = ref_id
                auto_resolved = True
                await _finalize_resolution(
                    r,
                    dispute_id=dispute_id,
                    brand_id=body.brand_id,
                    charge_id=body.charge_id,
                    new_status="resolved_refund_full",
                    decision="refund_full",
                    refund_id=ref_id,
                    refund_cents=amount_cents,
                    reason="auto_approved_small_amount_good_standing",
                    actor="system",
                    conversion_id=body.conversion_id,
                )
            else:
                logger.warning(
                    "auto-approve refund failed id=%s err=%s — falling back to admin queue",
                    dispute_id,
                    err,
                )

    return OpenDisputeResponse(
        dispute_id=dispute_id,
        status="resolved_refund_full" if auto_resolved else "pending_review",
        auto_pause_charge=bool(body.charge_id),
        auto_resolved=auto_resolved,
        refund_id=refund_id,
    )


# ── Internal: finalize a resolution (shared by admin/auto/withdraw) ──────
async def _finalize_resolution(
    r: aioredis.Redis,
    *,
    dispute_id: str,
    brand_id: str,
    charge_id: str | None,
    new_status: str,
    decision: str | None,
    refund_id: str | None,
    refund_cents: int | None,
    reason: str,
    actor: str,
    conversion_id: str | None = None,
) -> None:
    """Common tail for any terminal transition.

    Removes from pending queue, updates the dispute hash, pushes a timeline
    event, bumps stats counters. Caller is responsible for the wallet/attr
    side-effects (refund or unfreeze) BEFORE calling us so audit ordering
    reads cleanly.
    """
    now = time.time()
    fields: dict[str, Any] = {
        "status": new_status,
        "resolved_at": now,
        "reason": reason,
    }
    if decision is not None:
        fields["decision"] = decision
    if refund_id is not None:
        fields["refund_id"] = refund_id
    if refund_cents is not None:
        fields["refund_cents"] = refund_cents

    async with r.pipeline(transaction=False) as pipe:
        pipe.hset(_k_dispute(dispute_id), mapping=fields)
        pipe.zrem(_K_QUEUE_PENDING, dispute_id)
        pipe.hincrby(_K_STATS, "total_open", -1)
        pipe.hincrby(_K_STATS, "total_resolved", 1)
        pipe.hincrby(_K_STATS, f"outcome:{new_status}", 1)
        if refund_cents:
            pipe.hincrby(_K_STATS, "refund_count", 1)
            pipe.hincrby(_K_STATS, "refund_cents_total", refund_cents)
        # resolution duration accumulator for avg_resolution_hours
        await pipe.execute()

    # Track resolution time on the dispute record for stats aggregation.
    opened_at = await r.hget(_k_dispute(dispute_id), "opened_at")
    if opened_at:
        try:
            elapsed_h = (now - float(opened_at)) / 3600.0
            await r.hincrbyfloat(_K_STATS, "resolution_hours_total", elapsed_h)
        except (ValueError, TypeError):
            pass

    await _append_timeline(
        r,
        dispute_id,
        actor=actor,
        kind="resolved",
        payload={
            "decision": decision,
            "status": new_status,
            "refund_id": refund_id,
            "refund_cents": refund_cents,
            "reason": reason,
        },
    )

    # Charge state side-effect AFTER the dispute record is settled.
    if charge_id:
        if decision == "reject":
            await _unfreeze_charge(r, charge_id)
        # On full refund: cascade-remove the attribution event.
        if decision == "refund_full":
            ev_id = conversion_id
            if not ev_id:
                ev_id = await r.hget(_k_dispute(dispute_id), "conversion_id")
            await _cascade_remove_attribution(r, ev_id or None)

    # Clear by-charge index when terminal (allows the brand to dispute a
    # *future* charge with the same id, though in practice charge_ids are
    # uuid4 so this is paranoia).
    if charge_id and new_status != "pending_review":
        await r.delete(_k_by_charge(charge_id))


# ── GET /{dispute_id} ─────────────────────────────────────────────────────
@router.get("/{dispute_id}", response_model=DisputeDetail)
async def get_dispute(
    dispute_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> DisputeDetail:
    raw = await r.hgetall(_k_dispute(dispute_id))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "dispute_not_found", "dispute_id": dispute_id},
        )
    timeline = await _load_timeline(r, dispute_id)
    return _hydrate_detail(raw, timeline)


def _hydrate_detail(
    raw: dict[str, Any], timeline: list[TimelineEvent]
) -> DisputeDetail:
    def _f(key: str, default: float = 0.0) -> float:
        try:
            return float(raw.get(key) or default)
        except (ValueError, TypeError):
            return default

    def _i(key: str, default: int = 0) -> int:
        try:
            return int(raw.get(key) or default)
        except (ValueError, TypeError):
            return default

    resolved_at = _f("resolved_at", 0.0) or None
    refund_cents = _i("refund_cents", 0) or None

    return DisputeDetail(
        dispute_id=raw.get("dispute_id", ""),
        brand_id=raw.get("brand_id", ""),
        charge_id=raw.get("charge_id") or None,
        conversion_id=raw.get("conversion_id") or None,
        impression_token=raw.get("impression_token") or None,
        category=raw.get("category", "other"),
        status=raw.get("status", "unknown"),
        evidence_text=raw.get("evidence_text", ""),
        evidence_url=raw.get("evidence_url") or None,
        amount_cents=_i("amount_cents"),
        opened_at=_f("opened_at"),
        sla_breach_at=_f("sla_breach_at"),
        resolved_at=resolved_at,
        decision=raw.get("decision") or None,
        refund_id=raw.get("refund_id") or None,
        refund_cents=refund_cents,
        reason=raw.get("reason") or None,
        timeline=timeline,
    )


# ── GET /brand/{brand_id} ─────────────────────────────────────────────────
@router.get("/brand/{brand_id}", response_model=list[DisputeSummary])
async def list_brand_disputes(
    brand_id: str,
    status_filter: Literal["pending", "resolved", "rejected"] | None = Query(
        None, alias="status"
    ),
    limit: int = Query(100, ge=1, le=500),
    r: aioredis.Redis = Depends(get_redis),
) -> list[DisputeSummary]:
    # Newest first.
    ids = await r.zrevrange(_k_brand_disputes(brand_id), 0, limit * 3 - 1)
    out: list[DisputeSummary] = []
    for did in ids:
        if len(out) >= limit:
            break
        raw = await r.hgetall(_k_dispute(did))
        if not raw:
            continue
        cur_status = raw.get("status", "unknown")
        if status_filter == "pending" and cur_status not in VALID_OPEN_STATES:
            continue
        if status_filter == "resolved" and not cur_status.startswith("resolved_"):
            continue
        if status_filter == "rejected" and cur_status != "resolved_reject":
            continue
        try:
            out.append(
                DisputeSummary(
                    dispute_id=raw.get("dispute_id", did),
                    brand_id=raw.get("brand_id", brand_id),
                    charge_id=raw.get("charge_id") or None,
                    category=raw.get("category", "other"),
                    status=cur_status,
                    amount_cents=int(raw.get("amount_cents") or 0),
                    opened_at=float(raw.get("opened_at") or 0),
                    sla_breach_at=float(raw.get("sla_breach_at") or 0),
                )
            )
        except (ValueError, TypeError):
            continue
    return out


# ── POST /{dispute_id}/admin/comment ─────────────────────────────────────
@router.post("/{dispute_id}/admin/comment")
async def admin_comment(
    dispute_id: str,
    body: AdminCommentRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await r.hgetall(_k_dispute(dispute_id))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "dispute_not_found"},
        )
    # Mark dispute as under_investigation on first admin touch.
    if raw.get("status") == "pending_review":
        await r.hset(_k_dispute(dispute_id), "status", "under_investigation")
        await _append_timeline(
            r,
            dispute_id,
            actor="system",
            kind="state_change",
            payload={"from": "pending_review", "to": "under_investigation"},
        )

    await _append_timeline(
        r,
        dispute_id,
        actor="admin",
        kind="comment",
        payload={"comment": body.comment, "internal": body.internal},
    )
    return {"ok": True}


# ── POST /{dispute_id}/admin/resolve ─────────────────────────────────────
@router.post("/{dispute_id}/admin/resolve")
async def admin_resolve(
    dispute_id: str,
    body: AdminResolveRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await r.hgetall(_k_dispute(dispute_id))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "dispute_not_found"},
        )
    if raw.get("status") in TERMINAL_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "dispute_already_resolved",
                "status": raw.get("status"),
            },
        )

    brand_id = raw.get("brand_id", "")
    charge_id = raw.get("charge_id") or None
    conversion_id = raw.get("conversion_id") or None
    amount_cents = int(raw.get("amount_cents") or 0)

    refund_id: str | None = None
    refund_cents: int | None = None
    new_status: str

    if body.decision == "reject":
        new_status = "resolved_reject"
    elif body.decision == "refund_full":
        if not charge_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "refund_requires_charge_id"},
            )
        ok, ref_id, err = await _wallet_refund_internal(
            r, brand_id, charge_id, amount_cents, reason=f"dispute:{dispute_id}"
        )
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error": "wallet_refund_failed", "reason": err},
            )
        refund_id = ref_id
        refund_cents = amount_cents
        new_status = "resolved_refund_full"
    else:  # refund_partial
        if not charge_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "refund_requires_charge_id"},
            )
        assert body.refund_cents is not None  # guaranteed by validator
        if body.refund_cents > amount_cents:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "partial_exceeds_charge",
                    "refund_cents": body.refund_cents,
                    "charge_amount_cents": amount_cents,
                },
            )
        ok, ref_id, err = await _wallet_refund_internal(
            r,
            brand_id,
            charge_id,
            body.refund_cents,
            reason=f"dispute:{dispute_id}",
        )
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error": "wallet_refund_failed", "reason": err},
            )
        refund_id = ref_id
        refund_cents = body.refund_cents
        new_status = "resolved_refund_partial"
        # Partial refund leaves the residue collectable — unfreeze.
        await _unfreeze_charge(r, charge_id)

    await _finalize_resolution(
        r,
        dispute_id=dispute_id,
        brand_id=brand_id,
        charge_id=charge_id,
        new_status=new_status,
        decision=body.decision,
        refund_id=refund_id,
        refund_cents=refund_cents,
        reason=body.reason,
        actor="admin",
        conversion_id=conversion_id,
    )

    return {
        "ok": True,
        "dispute_id": dispute_id,
        "status": new_status,
        "refund_id": refund_id,
        "refund_cents": refund_cents,
    }


# ── POST /{dispute_id}/merchant/withdraw ─────────────────────────────────
@router.post("/{dispute_id}/merchant/withdraw")
async def merchant_withdraw(
    dispute_id: str,
    body: WithdrawRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await r.hgetall(_k_dispute(dispute_id))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "dispute_not_found"},
        )
    if raw.get("status") in TERMINAL_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "dispute_already_resolved",
                "status": raw.get("status"),
            },
        )

    charge_id = raw.get("charge_id") or None
    brand_id = raw.get("brand_id", "")

    # Merchant pulled the dispute — re-open the charge for collection.
    if charge_id:
        await _unfreeze_charge(r, charge_id)

    await _finalize_resolution(
        r,
        dispute_id=dispute_id,
        brand_id=brand_id,
        charge_id=charge_id,
        new_status="withdrawn",
        decision=None,
        refund_id=None,
        refund_cents=None,
        reason=body.reason,
        actor="merchant",
    )
    return {"ok": True, "dispute_id": dispute_id, "status": "withdrawn"}


# ── GET /admin/queue ─────────────────────────────────────────────────────
@router.get("/admin/queue", response_model=list[DisputeSummary])
async def admin_queue(
    priority: Literal["high", "normal"] | None = Query(None),
    limit: int = Query(QUEUE_DEFAULT_LIMIT, ge=1, le=QUEUE_MAX_LIMIT),
    r: aioredis.Redis = Depends(get_redis),
) -> list[DisputeSummary]:
    """Pending disputes sorted by SLA breach risk (earliest deadline first).

    `priority=high` filters to disputes whose SLA breach is within 6h, or
    already breached.
    """
    # ZSET is scored by sla_breach_at — ascending = most urgent first.
    ids = await r.zrange(_K_QUEUE_PENDING, 0, limit * 3 - 1, withscores=True)
    now = time.time()
    high_cutoff = now + 6 * 3600

    out: list[DisputeSummary] = []
    for did, score in ids:
        if len(out) >= limit:
            break
        if priority == "high" and score > high_cutoff:
            # ZSET is sorted asc so once we pass high_cutoff we're done.
            break
        if priority == "normal" and score <= high_cutoff:
            continue
        raw = await r.hgetall(_k_dispute(did))
        if not raw:
            # Stale queue entry — clean up.
            await r.zrem(_K_QUEUE_PENDING, did)
            continue
        try:
            out.append(
                DisputeSummary(
                    dispute_id=raw.get("dispute_id", did),
                    brand_id=raw.get("brand_id", ""),
                    charge_id=raw.get("charge_id") or None,
                    category=raw.get("category", "other"),
                    status=raw.get("status", "unknown"),
                    amount_cents=int(raw.get("amount_cents") or 0),
                    opened_at=float(raw.get("opened_at") or 0),
                    sla_breach_at=float(score),
                )
            )
        except (ValueError, TypeError):
            continue
    return out


# ── POST /admin/policy ───────────────────────────────────────────────────
@router.post("/admin/policy", response_model=Policy)
async def update_policy(
    body: PolicyUpdate,
    r: aioredis.Redis = Depends(get_redis),
) -> Policy:
    cur = await _get_policy(r)
    merged = Policy(
        auto_refund_under_cents=(
            body.auto_refund_under_cents
            if body.auto_refund_under_cents is not None
            else cur.auto_refund_under_cents
        ),
        sla_response_hours=(
            body.sla_response_hours
            if body.sla_response_hours is not None
            else cur.sla_response_hours
        ),
        max_disputes_per_brand_per_month=(
            body.max_disputes_per_brand_per_month
            if body.max_disputes_per_brand_per_month is not None
            else cur.max_disputes_per_brand_per_month
        ),
    )
    await r.hset(
        _K_POLICY,
        mapping={
            "auto_refund_under_cents": merged.auto_refund_under_cents,
            "sla_response_hours": merged.sla_response_hours,
            "max_disputes_per_brand_per_month": merged.max_disputes_per_brand_per_month,
            "updated_at": time.time(),
        },
    )
    return merged


# ── GET /stats ────────────────────────────────────────────────────────────
@router.get("/stats", response_model=Stats)
async def get_stats(r: aioredis.Redis = Depends(get_redis)) -> Stats:
    raw = await r.hgetall(_K_STATS)

    def _i(key: str) -> int:
        try:
            return int(raw.get(key) or 0)
        except (ValueError, TypeError):
            return 0

    def _f(key: str) -> float:
        try:
            return float(raw.get(key) or 0.0)
        except (ValueError, TypeError):
            return 0.0

    total_open = _i("total_open")
    total_resolved = _i("total_resolved")
    refund_count = _i("refund_count")
    refund_rate = (refund_count / total_resolved) if total_resolved else 0.0
    res_hours_total = _f("resolution_hours_total")
    avg_resolution_hours = (
        (res_hours_total / total_resolved) if total_resolved else 0.0
    )

    # Top categories by open volume.
    cats = []
    for cat in DISPUTE_CATEGORIES:
        cats.append({"category": cat, "count": _i(f"category:{cat}")})
    cats.sort(key=lambda d: d["count"], reverse=True)

    return Stats(
        total_open=total_open,
        total_resolved=total_resolved,
        refund_rate=round(refund_rate, 4),
        avg_resolution_hours=round(avg_resolution_hours, 2),
        top_categories=cats[:5],
    )
