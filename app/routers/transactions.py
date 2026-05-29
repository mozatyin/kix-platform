"""Transactions — universal commerce transaction primitive.

This module records *any* money-moving event between buyer, seller, and
brand: purchases, subscription renewals, service fees, deposits, refunds,
chargebacks, commissions, affiliate payments. It is the canonical ledger
that downstream systems (sims, marketplace commission, dashboards) query.

Why a separate module from ``wallet`` and ``attribution``:

* ``wallet`` is a *brand-balance* ledger (topups, charges deducted from a
  prepaid balance). It does not know about buyers, sellers, or line items.
* ``attribution`` records *marketing* conversions (last-touch credit, 7-day
  window). It does not know about commission splits or partial refunds.
* ``transactions`` is the *commerce* ledger — every C2B or C2C money flow
  on the platform. It fans out to attribution (for conversion credit) and
  wallet (for commission charging) but is itself the source of truth.

Redis schema::

    transaction:{tid}                       HASH    full transaction state
    brand:{bid}:transactions                ZSET    score=ts, member=tid
    user:{uid}:transactions:as_buyer        ZSET    score=ts, member=tid
    user:{uid}:transactions:as_seller       ZSET    score=ts, member=tid
    transaction_idem:{tx_id}                STRING  24h idempotency key
    events:refund                           LIST    JSON refund events
    events:cancellation                     LIST    JSON cancel events

Fail-soft integration: attribution / wallet / pixel side-effects never
block the transaction.record call. They are best-effort.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ══════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════

TRANSACTION_TYPES = {
    "purchase",
    "subscription_renewal",
    "service_fee",
    "deposit",
    "refund_initiate",
    "chargeback",
    "commission",
    "affiliate_payment",
    "other",
}

PAYMENT_METHODS = {
    "card", "wechat", "alipay", "bank_transfer", "wallet", "voucher",
}

IDEM_TTL_SECONDS = 24 * 3600
EVENT_STREAM_MAX = 10000
DEFAULT_COMMISSION_BPS = 0  # service must set explicitly via metadata


# ══════════════════════════════════════════════════════════════════════════
# Redis key helpers
# ══════════════════════════════════════════════════════════════════════════

def _k_tx(tid: str) -> str:
    return f"transaction:{tid}"


def _k_brand_txs(bid: str) -> str:
    return f"brand:{bid}:transactions"


def _k_user_buyer_txs(uid: str) -> str:
    return f"user:{uid}:transactions:as_buyer"


def _k_user_seller_txs(uid: str) -> str:
    return f"user:{uid}:transactions:as_seller"


def _k_idem(tx_id: str) -> str:
    return f"transaction_idem:{tx_id}"


# ══════════════════════════════════════════════════════════════════════════
# Small utilities
# ══════════════════════════════════════════════════════════════════════════

def _now() -> int:
    return int(time.time())


def _iso(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _new_tx_id() -> str:
    return uuid4().hex[:24]


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def _safe_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


async def _load_tx(r: aioredis.Redis, tid: str) -> dict[str, str]:
    data = await r.hgetall(_k_tx(tid))
    if not data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"transaction_id={tid} not found",
        )
    return data


def _tx_to_dict(state: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = dict(state)
    out["line_items"] = _safe_loads(state.get("line_items"), [])
    out["metadata"] = _safe_loads(state.get("metadata"), {})
    for int_field in ("amount_cents", "ts", "refunded_cents",
                      "commission_cents", "cancelled_at", "refunded_at"):
        if int_field in out and out[int_field] not in ("", None):
            try:
                out[int_field] = int(out[int_field])
            except (TypeError, ValueError):
                pass
    return out


# ══════════════════════════════════════════════════════════════════════════
# Pydantic models
# ══════════════════════════════════════════════════════════════════════════

class LineItem(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    qty: int = Field(..., ge=1)
    unit_price_cents: int = Field(..., ge=0)


class TransactionRecordRequest(BaseModel):
    transaction_id: str | None = Field(None, max_length=64)
    brand_id: str = Field(..., min_length=1, max_length=128)
    buyer_user_id: str = Field(..., min_length=1, max_length=128)
    seller_user_id: str | None = Field(None, max_length=128)
    amount_cents: int = Field(..., ge=0)
    currency: str = Field("CNY", min_length=3, max_length=8)
    transaction_type: str = Field(...)
    line_items: list[LineItem] | None = None
    payment_method: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("transaction_type")
    @classmethod
    def _validate_type(cls, v: str) -> str:
        if v not in TRANSACTION_TYPES:
            raise ValueError(
                f"transaction_type must be one of {sorted(TRANSACTION_TYPES)}"
            )
        return v

    @field_validator("payment_method")
    @classmethod
    def _validate_payment_method(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in PAYMENT_METHODS:
            raise ValueError(
                f"payment_method must be one of {sorted(PAYMENT_METHODS)}"
            )
        return v


class RefundRequest(BaseModel):
    amount_cents: int | None = Field(None, ge=0)
    reason: str = Field("", max_length=500)
    refund_to: Literal["buyer", "wallet"] = "buyer"


class CancelRequest(BaseModel):
    cancelled_by: Literal["buyer", "seller", "system"]
    reason: str = Field("", max_length=500)


# ══════════════════════════════════════════════════════════════════════════
# Fail-soft integration hooks
# ══════════════════════════════════════════════════════════════════════════

async def _fire_pixel(
    r: aioredis.Redis,
    *,
    brand_id: str,
    user_id: str,
    event: str,
    meta: dict[str, Any],
) -> None:
    try:
        payload = {
            "brand_id": brand_id,
            "user_id": user_id,
            "event": event,
            "meta": meta,
            "ts": _now(),
        }
        await r.rpush("pixel:events", _dumps(payload))
        await r.ltrim("pixel:events", -EVENT_STREAM_MAX, -1)
    except Exception as exc:  # pragma: no cover — analytics must not break flow
        logger.debug("pixel fire failed: %s", exc)


async def _track_conversion(
    r: aioredis.Redis,
    *,
    buyer_user_id: str,
    target_brand: str,
    order_id: str,
    amount_cents: int,
) -> None:
    """Fire attribution.track_conversion via the in-process function.

    Wrapped in try/except so attribution failures (e.g. consent denial) never
    block the transaction.record write path.
    """
    if not buyer_user_id or amount_cents <= 0:
        return
    try:
        # Import locally to avoid module-load circulars.
        from app.routers.attribution import (
            ConversionCheckRequest,
            track_conversion,
        )
        req = ConversionCheckRequest(
            user_id=buyer_user_id,
            target_brand=target_brand,
            order_id=order_id,
            amount_cents=amount_cents,
        )
        await track_conversion(req, r)  # type: ignore[arg-type]
    except HTTPException as exc:
        # Most common: 403 from consent enforcement → expected for non-consenting users.
        logger.debug(
            "attribution.track_conversion skipped order=%s code=%s detail=%s",
            order_id, exc.status_code, exc.detail,
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("attribution.track_conversion failed: %s", exc)


async def _wallet_charge_commission(
    r: aioredis.Redis,
    *,
    brand_id: str,
    tx_id: str,
    commission_cents: int,
    reason_detail: str,
) -> str | None:
    """Charge ``commission_cents`` against the brand wallet for marketplace fees.

    Returns the charge_id on success, None on failure. Never raises.
    """
    if commission_cents <= 0:
        return None
    try:
        from app.routers.wallet import ChargeRequest, charge as wallet_charge
        body = ChargeRequest(
            amount_cents=commission_cents,
            reason="commission",
            reason_detail=reason_detail[:500],
            reference_id=f"tx:{tx_id}",
        )
        resp = await wallet_charge(brand_id, body, r)  # type: ignore[arg-type]
        return getattr(resp, "charge_id", None)
    except HTTPException as exc:
        logger.debug(
            "wallet.charge commission skipped tx=%s code=%s detail=%s",
            tx_id, exc.status_code, exc.detail,
        )
        return None
    except Exception as exc:  # pragma: no cover
        logger.debug("wallet.charge commission failed tx=%s: %s", tx_id, exc)
        return None


async def _wallet_reverse_commission(
    r: aioredis.Redis,
    *,
    charge_id: str,
    amount_cents: int,
    reason: str,
) -> dict[str, Any] | None:
    if not charge_id or amount_cents <= 0:
        return None
    try:
        from app.routers.wallet import (
            ReverseCommissionRequest,
            reverse_commission,
        )
        body = ReverseCommissionRequest(
            amount_cents=amount_cents, reason=reason[:500]
        )
        resp = await reverse_commission(charge_id, body, r)  # type: ignore[arg-type]
        if hasattr(resp, "model_dump"):
            return resp.model_dump()
        return {"ok": True}
    except HTTPException as exc:
        logger.debug(
            "wallet.reverse_commission skipped charge=%s code=%s",
            charge_id, exc.status_code,
        )
        return None
    except Exception as exc:  # pragma: no cover
        logger.debug("wallet.reverse_commission failed: %s", exc)
        return None


async def _push_refund_event(
    r: aioredis.Redis, payload: dict[str, Any]
) -> None:
    try:
        await r.rpush("events:refund", _dumps(payload))
        await r.ltrim("events:refund", -EVENT_STREAM_MAX, -1)
    except Exception as exc:  # pragma: no cover
        logger.debug("events:refund push failed: %s", exc)


async def _push_cancellation_event(
    r: aioredis.Redis, payload: dict[str, Any]
) -> None:
    try:
        await r.rpush("events:cancellation", _dumps(payload))
        await r.ltrim("events:cancellation", -EVENT_STREAM_MAX, -1)
    except Exception as exc:  # pragma: no cover
        logger.debug("events:cancellation push failed: %s", exc)


# ══════════════════════════════════════════════════════════════════════════
# Endpoints
# ══════════════════════════════════════════════════════════════════════════

@router.post(
    "/record",
    status_code=status.HTTP_201_CREATED,
    summary="Record a commerce transaction (purchase, refund, commission, etc.)",
)
async def record_transaction(
    body: TransactionRecordRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Records the transaction in Redis and fans out fail-soft side-effects.

    Idempotency: when the caller supplies ``transaction_id`` we honour it as
    the dedup key for 24h. Replays return the existing record untouched.
    """
    # Idempotency check
    supplied_tid = (body.transaction_id or "").strip() or None
    if supplied_tid:
        existing_tid = await r.get(_k_idem(supplied_tid))
        if existing_tid:
            existing_state = await r.hgetall(_k_tx(existing_tid))
            if existing_state:
                return {
                    "transaction_id": existing_tid,
                    "ts": int(existing_state.get("ts") or 0),
                    "idempotent": True,
                }

    tid = supplied_tid or _new_tx_id()
    ts = _now()
    line_items_dump = _dumps(
        [li.model_dump() for li in (body.line_items or [])]
    )
    metadata_dump = _dumps(body.metadata or {})

    # Commission: optional, declared in metadata.commission_cents by caller.
    try:
        commission_cents = int(body.metadata.get("commission_cents") or 0)
    except (TypeError, ValueError):
        commission_cents = 0
    if commission_cents < 0:
        commission_cents = 0

    tx_record: dict[str, str] = {
        "transaction_id": tid,
        "brand_id": body.brand_id,
        "buyer_user_id": body.buyer_user_id,
        "seller_user_id": body.seller_user_id or "",
        "amount_cents": str(body.amount_cents),
        "currency": body.currency,
        "transaction_type": body.transaction_type,
        "payment_method": body.payment_method or "",
        "line_items": line_items_dump,
        "metadata": metadata_dump,
        "status": "recorded",
        "ts": str(ts),
        "refunded_cents": "0",
        "commission_cents": str(commission_cents),
    }

    pipe = r.pipeline()
    pipe.hset(_k_tx(tid), mapping=tx_record)
    pipe.zadd(_k_brand_txs(body.brand_id), {tid: ts})
    pipe.zadd(_k_user_buyer_txs(body.buyer_user_id), {tid: ts})
    if body.seller_user_id:
        pipe.zadd(_k_user_seller_txs(body.seller_user_id), {tid: ts})
    if supplied_tid:
        pipe.set(_k_idem(supplied_tid), tid, ex=IDEM_TTL_SECONDS)
    await pipe.execute()

    logger.info(
        "transaction recorded tid=%s brand=%s buyer=%s seller=%s "
        "type=%s amount=%d %s",
        tid, body.brand_id, body.buyer_user_id, body.seller_user_id or "-",
        body.transaction_type, body.amount_cents, body.currency,
    )

    # ── Fan-out side-effects ───────────────────────────────────────────
    # 1. Attribution conversion credit (purchases / renewals only).
    if body.transaction_type in ("purchase", "subscription_renewal"):
        await _track_conversion(
            r,
            buyer_user_id=body.buyer_user_id,
            target_brand=body.brand_id,
            order_id=tid,
            amount_cents=body.amount_cents,
        )

    # 2. Commission charge against brand wallet (if declared).
    commission_charge_id: str | None = None
    if commission_cents > 0 and body.transaction_type != "commission":
        commission_charge_id = await _wallet_charge_commission(
            r,
            brand_id=body.brand_id,
            tx_id=tid,
            commission_cents=commission_cents,
            reason_detail=f"tx:{tid}:{body.transaction_type}",
        )
        if commission_charge_id:
            await r.hset(
                _k_tx(tid),
                mapping={"commission_charge_id": commission_charge_id},
            )

    # 3. Pixel event mirror (purchases / renewals only).
    if body.transaction_type in ("purchase", "subscription_renewal"):
        await _fire_pixel(
            r,
            brand_id=body.brand_id,
            user_id=body.buyer_user_id,
            event="purchase",
            meta={
                "transaction_id": tid,
                "amount_cents": body.amount_cents,
                "currency": body.currency,
                "transaction_type": body.transaction_type,
            },
        )

    return {
        "transaction_id": tid,
        "ts": ts,
        "ts_iso": _iso(ts),
        "idempotent": False,
        "commission_charge_id": commission_charge_id,
    }


@router.get(
    "/{tx_id}",
    summary="Get a transaction by id",
)
async def get_transaction(
    tx_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    state = await _load_tx(r, tx_id)
    return _tx_to_dict(state)


# ── List helpers ─────────────────────────────────────────────────────────

async def _list_from_zset(
    r: aioredis.Redis,
    *,
    zset_key: str,
    from_ts: int | None,
    to_ts: int | None,
    limit: int,
    type_filter: str | None = None,
    brand_filter: str | None = None,
) -> list[dict[str, Any]]:
    lo = from_ts if from_ts is not None else "-inf"
    hi = to_ts if to_ts is not None else "+inf"
    tids = await r.zrevrangebyscore(zset_key, hi, lo, start=0, num=limit)
    out: list[dict[str, Any]] = []
    for tid in tids or []:
        state = await r.hgetall(_k_tx(tid))
        if not state:
            continue
        if type_filter and state.get("transaction_type") != type_filter:
            continue
        if brand_filter and state.get("brand_id") != brand_filter:
            continue
        out.append(_tx_to_dict(state))
    return out


@router.get(
    "/buyer/{user_id}",
    summary="List transactions where user is the buyer",
)
async def list_buyer_transactions(
    user_id: str,
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
    type_filter: str | None = Query(None, alias="type"),
    brand_id: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    out = await _list_from_zset(
        r,
        zset_key=_k_user_buyer_txs(user_id),
        from_ts=from_ts, to_ts=to_ts, limit=limit,
        type_filter=type_filter, brand_filter=brand_id,
    )
    return {"user_id": user_id, "role": "buyer",
            "count": len(out), "transactions": out}


@router.get(
    "/seller/{user_id}",
    summary="List transactions where user is the seller (C2C)",
)
async def list_seller_transactions(
    user_id: str,
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
    limit: int = Query(100, ge=1, le=1000),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    out = await _list_from_zset(
        r,
        zset_key=_k_user_seller_txs(user_id),
        from_ts=from_ts, to_ts=to_ts, limit=limit,
    )
    return {"user_id": user_id, "role": "seller",
            "count": len(out), "transactions": out}


@router.get(
    "/brand/{brand_id}",
    summary="List transactions for a brand",
)
async def list_brand_transactions(
    brand_id: str,
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
    type_filter: str | None = Query(None, alias="type"),
    limit: int = Query(100, ge=1, le=1000),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    out = await _list_from_zset(
        r,
        zset_key=_k_brand_txs(brand_id),
        from_ts=from_ts, to_ts=to_ts, limit=limit,
        type_filter=type_filter,
    )
    return {"brand_id": brand_id, "count": len(out), "transactions": out}


# ── Refund ───────────────────────────────────────────────────────────────

@router.post(
    "/{tx_id}/refund",
    summary="Reverse a transaction (full or partial)",
)
async def refund_transaction(
    tx_id: str,
    body: RefundRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Reverses a transaction. Cascades:

    * reverses commission charge against the brand wallet (if any)
    * reverses attribution credit (best-effort)
    * fires reverse pixel event
    * pushes ``events:refund`` stream entry
    """
    key = _k_tx(tx_id)

    # Optimistic loop for refunded_cents accounting.
    for _attempt in range(5):
        async with r.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                state = await pipe.hgetall(key)
                if not state:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=404,
                        detail=f"transaction_id={tx_id} not found",
                    )

                tx_status = state.get("status", "")
                if tx_status in ("cancelled", "fully_refunded"):
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "ok": False, "reason": "invalid_status",
                            "current_status": tx_status,
                        },
                    )

                amount_cents = int(state.get("amount_cents") or 0)
                already_refunded = int(state.get("refunded_cents") or 0)
                requested = body.amount_cents
                if requested is None:
                    refund_amt = amount_cents - already_refunded
                else:
                    refund_amt = requested

                if refund_amt <= 0:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "ok": False, "reason": "refund_amount_zero_or_negative",
                            "amount_cents": refund_amt,
                        },
                    )
                if already_refunded + refund_amt > amount_cents:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "ok": False,
                            "reason": "refund_exceeds_remaining",
                            "already_refunded": already_refunded,
                            "requested": refund_amt,
                            "amount_cents": amount_cents,
                        },
                    )

                new_refunded = already_refunded + refund_amt
                new_status = (
                    "fully_refunded" if new_refunded >= amount_cents
                    else "partially_refunded"
                )
                refund_ts = _now()

                pipe.multi()
                pipe.hset(
                    key,
                    mapping={
                        "status": new_status,
                        "refunded_cents": str(new_refunded),
                        "refunded_at": str(refund_ts),
                        "last_refund_reason": body.reason,
                        "last_refund_to": body.refund_to,
                    },
                )
                await pipe.execute()
                break
            except aioredis.WatchError:
                continue
            except HTTPException:
                raise
    else:
        raise HTTPException(
            status_code=503, detail="refund_contention_exceeded_retries"
        )

    # ── Cascading side-effects (after atomic commit) ───────────────────

    # 1. Reverse commission if any was charged on the original tx.
    clawback_result: dict[str, Any] | None = None
    commission_charge_id = state.get("commission_charge_id") or ""
    commission_cents = int(state.get("commission_cents") or 0)
    if commission_charge_id and commission_cents > 0:
        # Pro-rate clawback proportional to refund_amt / amount_cents.
        if amount_cents > 0:
            clawback_amt = (commission_cents * refund_amt) // amount_cents
        else:
            clawback_amt = 0
        clawback_result = await _wallet_reverse_commission(
            r,
            charge_id=commission_charge_id,
            amount_cents=clawback_amt,
            reason=f"transaction_refund:{tx_id}",
        )

    # 2. Reverse pixel + best-effort attribution clawback signal.
    await _fire_pixel(
        r,
        brand_id=state.get("brand_id", ""),
        user_id=state.get("buyer_user_id", ""),
        event="refund",
        meta={
            "transaction_id": tx_id,
            "refund_amount_cents": refund_amt,
            "remaining_cents": amount_cents - new_refunded,
            "refund_to": body.refund_to,
            "reason": body.reason,
        },
    )

    # 3. Refund event stream.
    refund_event = {
        "type": "refund",
        "transaction_id": tx_id,
        "brand_id": state.get("brand_id"),
        "buyer_user_id": state.get("buyer_user_id"),
        "seller_user_id": state.get("seller_user_id") or "",
        "refund_amount_cents": refund_amt,
        "refund_to": body.refund_to,
        "reason": body.reason,
        "new_status": new_status,
        "commission_clawback": clawback_result,
        "ts": _now(),
    }
    await _push_refund_event(r, refund_event)

    return {
        "ok": True,
        "transaction_id": tx_id,
        "status": new_status,
        "refund_amount_cents": refund_amt,
        "total_refunded_cents": new_refunded,
        "remaining_cents": amount_cents - new_refunded,
        "commission_clawback": clawback_result,
    }


# ── Cancel ───────────────────────────────────────────────────────────────

@router.post(
    "/{tx_id}/cancel",
    summary="Soft-cancel a transaction (no refund triggered if unpaid)",
)
async def cancel_transaction(
    tx_id: str,
    body: CancelRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Mark transaction cancelled. Does **not** trigger refund.

    If a refund is also required, callers should invoke ``/refund`` first
    (or in tandem). Cancel is a *state* transition for unpaid or in-flight
    transactions (e.g. customer abandons checkout, seller pulls listing).
    """
    key = _k_tx(tx_id)
    state = await r.hgetall(key)
    if not state:
        raise HTTPException(
            status_code=404, detail=f"transaction_id={tx_id} not found"
        )
    current_status = state.get("status", "")
    if current_status in ("cancelled", "fully_refunded"):
        raise HTTPException(
            status_code=409,
            detail={
                "ok": False, "reason": "invalid_status",
                "current_status": current_status,
            },
        )

    cancel_ts = _now()
    pipe = r.pipeline()
    pipe.hset(
        key,
        mapping={
            "status": "cancelled",
            "cancelled_at": str(cancel_ts),
            "cancelled_by": body.cancelled_by,
            "cancel_reason": body.reason,
            "previous_status": current_status,
        },
    )
    await pipe.execute()

    cancel_event = {
        "type": "cancellation",
        "transaction_id": tx_id,
        "brand_id": state.get("brand_id"),
        "buyer_user_id": state.get("buyer_user_id"),
        "seller_user_id": state.get("seller_user_id") or "",
        "cancelled_by": body.cancelled_by,
        "reason": body.reason,
        "previous_status": current_status,
        "ts": cancel_ts,
    }
    await _push_cancellation_event(r, cancel_event)

    return {
        "ok": True,
        "transaction_id": tx_id,
        "status": "cancelled",
        "previous_status": current_status,
        "cancelled_by": body.cancelled_by,
        "ts": cancel_ts,
    }
