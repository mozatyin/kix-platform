"""Merchant Wallet + Billing router.

Brands top up balance (like Google Ads) and spend on campaigns (CPA / CPS /
CPM / CPV). All amounts are integer cents — never floats — to avoid rounding
drift. Charges are atomic via Redis WATCH/MULTI with daily-budget guards and
audit trails. Auto-recharge fires an event when balance drops below a brand
threshold; the actual payment integration is stubbed for MVP.

Redis schema
------------
    wallet:{brand_id}:balance              INT (cents)
    wallet:{brand_id}:currency             STRING (CNY/USD/...)
    wallet:{brand_id}:daily_spent:{date}   INT (EX 86400)
    wallet:{brand_id}:daily_budget         INT (cents, 0 = no cap)
    wallet:{brand_id}:total_spent          INT (lifetime)
    wallet:{brand_id}:last_topup_at        FLOAT (unix ts)
    wallet:{brand_id}:auto_recharge        HASH  (config)
    wallet:{brand_id}:transactions         LIST  (chronological tx_ids)
    wallet:topup:{topup_id}                HASH
    wallet:charge:{charge_id}              HASH
    wallet:refund:{refund_id}              HASH
    wallet:auto_recharge_needed            LIST  (events for worker)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ────────────────────────────────────────────────────────────
SUPPORTED_PAYMENT_METHODS = {"alipay", "wechat", "stripe", "paypal"}
SUPPORTED_CHARGE_REASONS = {
    "cpa_conversion",
    "cps_commission",
    "cpm_impression",
    "cpv_visit",
}
DEFAULT_CURRENCY = "CNY"
REFUND_WINDOW_SECONDS = 30 * 24 * 3600  # 30 days
MAX_WATCH_RETRIES = 8
TX_LIST_MAX = 10_000  # cap to bound memory


# ── Key helpers ──────────────────────────────────────────────────────────
def _k_balance(b: str) -> str:
    return f"wallet:{b}:balance"


def _k_currency(b: str) -> str:
    return f"wallet:{b}:currency"


def _k_daily_spent(b: str, day: str) -> str:
    return f"wallet:{b}:daily_spent:{day}"


def _k_daily_budget(b: str) -> str:
    return f"wallet:{b}:daily_budget"


def _k_total_spent(b: str) -> str:
    return f"wallet:{b}:total_spent"


def _k_last_topup(b: str) -> str:
    return f"wallet:{b}:last_topup_at"


def _k_auto_recharge(b: str) -> str:
    return f"wallet:{b}:auto_recharge"


def _k_tx_list(b: str) -> str:
    return f"wallet:{b}:transactions"


def _k_topup(topup_id: str) -> str:
    return f"wallet:topup:{topup_id}"


def _k_charge(charge_id: str) -> str:
    return f"wallet:charge:{charge_id}"


def _k_refund(refund_id: str) -> str:
    return f"wallet:refund:{refund_id}"


_AUTO_RECHARGE_EVENT_LIST = "wallet:auto_recharge_needed"


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Pydantic models ──────────────────────────────────────────────────────
class TopupRequest(BaseModel):
    amount_cents: int = Field(..., gt=0, le=100_000_000)  # ≤ ¥1M / $1M
    payment_method: Literal["alipay", "wechat", "stripe", "paypal"]
    payment_token: str | None = None
    currency: str | None = None  # defaults to existing wallet currency

    @field_validator("currency")
    @classmethod
    def _cur(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().upper()
        if len(v) != 3 or not v.isalpha():
            raise ValueError("currency must be a 3-letter ISO code")
        return v


class TopupResponse(BaseModel):
    topup_id: str
    status: Literal["pending", "confirmed"]
    new_balance_cents: int
    amount_cents: int
    currency: str


class TopupConfirmRequest(BaseModel):
    payment_gateway_response: dict = Field(default_factory=dict)


class ChargeRequest(BaseModel):
    amount_cents: int = Field(..., gt=0, le=10_000_000)
    reason: Literal[
        "cpa_conversion", "cps_commission", "cpm_impression", "cpv_visit"
    ]
    reference_id: str = Field(..., min_length=1, max_length=128)
    campaign_id: str | None = None


class ChargeResponse(BaseModel):
    ok: bool
    new_balance_cents: int
    charge_id: str | None = None
    reason: str | None = None  # e.g. "insufficient_funds"


class RefundRequest(BaseModel):
    charge_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=512)


class RefundResponse(BaseModel):
    ok: bool
    new_balance_cents: int
    refund_id: str | None = None


class AutoRechargeConfig(BaseModel):
    enabled: bool
    threshold_cents: int = Field(50_000, ge=0)
    recharge_amount_cents: int = Field(500_000, gt=0)
    payment_method: Literal["alipay", "wechat", "stripe", "paypal"] | None = None
    payment_token: str | None = None


class WalletStatus(BaseModel):
    brand_id: str
    balance_cents: int
    currency: str
    daily_spent_cents: int
    total_spent_cents: int
    last_topup_at: float | None
    auto_recharge_config: AutoRechargeConfig | None


class DailyBudgetRequest(BaseModel):
    daily_budget_cents: int = Field(..., ge=0)  # 0 disables the cap


class DailyBudgetStatus(BaseModel):
    today_spent_cents: int
    today_budget_cents: int
    remaining_cents: int
    paused: bool


class Transaction(BaseModel):
    id: str
    type: Literal["topup", "charge", "refund"]
    amount_cents: int
    ts: float
    status: str
    reason: str | None = None
    reference_id: str | None = None
    campaign_id: str | None = None
    payment_method: str | None = None


class Forecast(BaseModel):
    avg_daily_spend_cents: int
    days_until_empty: float | None
    recommendation: Literal["topup_now", "reduce_bids", "add_campaign", "healthy"]


# ── Internal helpers ─────────────────────────────────────────────────────
async def _get_currency(brand_id: str, r: aioredis.Redis) -> str:
    cur = await r.get(_k_currency(brand_id))
    return cur or DEFAULT_CURRENCY


async def _get_auto_recharge_config(
    brand_id: str, r: aioredis.Redis
) -> AutoRechargeConfig | None:
    raw = await r.hgetall(_k_auto_recharge(brand_id))
    if not raw:
        return None
    try:
        return AutoRechargeConfig(
            enabled=raw.get("enabled", "0") == "1",
            threshold_cents=int(raw.get("threshold_cents", "0")),
            recharge_amount_cents=int(raw.get("recharge_amount_cents", "0") or 1),
            payment_method=raw.get("payment_method") or None,
            payment_token=raw.get("payment_token") or None,
        )
    except (ValueError, TypeError):
        logger.warning("Malformed auto_recharge config for brand=%s", brand_id)
        return None


async def _maybe_auto_recharge(
    brand_id: str, new_balance: int, r: aioredis.Redis
) -> None:
    """Emit an auto_recharge event if balance dropped below threshold.

    For MVP we just enqueue; a worker handles the real payment provider.
    """
    cfg = await _get_auto_recharge_config(brand_id, r)
    if cfg is None or not cfg.enabled:
        return
    if new_balance >= cfg.threshold_cents:
        return
    if not cfg.payment_token or not cfg.payment_method:
        return

    # De-dupe: only fire if no pending event already exists for this brand.
    flag = f"wallet:{brand_id}:auto_recharge_pending"
    set_ok = await r.set(flag, "1", nx=True, ex=600)
    if not set_ok:
        return

    event = {
        "brand_id": brand_id,
        "balance_cents": str(new_balance),
        "threshold_cents": str(cfg.threshold_cents),
        "recharge_amount_cents": str(cfg.recharge_amount_cents),
        "payment_method": cfg.payment_method,
        "ts": str(time.time()),
    }
    import json as _json

    await r.rpush(_AUTO_RECHARGE_EVENT_LIST, _json.dumps(event))
    logger.info(
        "auto_recharge_needed emitted brand=%s balance=%s threshold=%s",
        brand_id,
        new_balance,
        cfg.threshold_cents,
    )


async def _append_tx(
    r: aioredis.Redis, brand_id: str, tx_id: str, pipe: aioredis.client.Pipeline | None = None
) -> None:
    """Push a tx id to the brand transactions list (chronological)."""
    target = pipe if pipe is not None else r
    target.rpush(_k_tx_list(brand_id), tx_id) if pipe is not None else await r.rpush(
        _k_tx_list(brand_id), tx_id
    )
    # Trim from the left to keep most recent TX_LIST_MAX entries.
    if pipe is not None:
        pipe.ltrim(_k_tx_list(brand_id), -TX_LIST_MAX, -1)
    else:
        await r.ltrim(_k_tx_list(brand_id), -TX_LIST_MAX, -1)


# ── GET /{brand_id} ──────────────────────────────────────────────────────
@router.get("/{brand_id}", response_model=WalletStatus)
async def get_wallet(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> WalletStatus:
    balance = int(await r.get(_k_balance(brand_id)) or 0)
    currency = await _get_currency(brand_id, r)
    daily_spent = int(await r.get(_k_daily_spent(brand_id, _today_str())) or 0)
    total_spent = int(await r.get(_k_total_spent(brand_id)) or 0)
    last_topup = await r.get(_k_last_topup(brand_id))
    last_topup_ts = float(last_topup) if last_topup else None
    auto_cfg = await _get_auto_recharge_config(brand_id, r)

    return WalletStatus(
        brand_id=brand_id,
        balance_cents=balance,
        currency=currency,
        daily_spent_cents=daily_spent,
        total_spent_cents=total_spent,
        last_topup_at=last_topup_ts,
        auto_recharge_config=auto_cfg,
    )


# ── POST /{brand_id}/topup ───────────────────────────────────────────────
@router.post("/{brand_id}/topup", response_model=TopupResponse)
async def create_topup(
    brand_id: str,
    body: TopupRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> TopupResponse:
    """Initiate a top-up. Records as `pending` until /confirm is called.

    In production this is where you'd hit the payment provider to create
    the actual checkout intent and return its client_secret. For MVP we
    just persist the record and let the caller drive confirmation.
    """
    # Lock in the wallet's currency on first ever topup.
    cur_existing = await r.get(_k_currency(brand_id))
    currency = (body.currency or cur_existing or DEFAULT_CURRENCY).upper()
    if cur_existing is None:
        await r.set(_k_currency(brand_id), currency)
    elif body.currency and body.currency != cur_existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "currency_mismatch",
                "wallet_currency": cur_existing,
                "requested": body.currency,
            },
        )

    topup_id = uuid4().hex
    now = time.time()
    await r.hset(
        _k_topup(topup_id),
        mapping={
            "topup_id": topup_id,
            "brand_id": brand_id,
            "amount": body.amount_cents,
            "currency": currency,
            "payment_method": body.payment_method,
            "payment_token": body.payment_token or "",
            "status": "pending",
            "created_at": now,
            "confirmed_at": "",
        },
    )

    balance = int(await r.get(_k_balance(brand_id)) or 0)
    return TopupResponse(
        topup_id=topup_id,
        status="pending",
        new_balance_cents=balance,
        amount_cents=body.amount_cents,
        currency=currency,
    )


# ── POST /{brand_id}/topup/{topup_id}/confirm ────────────────────────────
@router.post(
    "/{brand_id}/topup/{topup_id}/confirm", response_model=TopupResponse
)
async def confirm_topup(
    brand_id: str,
    topup_id: str,
    body: TopupConfirmRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> TopupResponse:
    """Confirm a pending top-up — idempotent on `topup_id`.

    Called by the payment provider webhook (or after a redirect). The
    second call returns the same record without double-crediting.
    """
    key = _k_topup(topup_id)

    while True:
        try:
            async with r.pipeline(transaction=True) as pipe:
                await pipe.watch(key, _k_balance(brand_id))
                tu = await pipe.hgetall(key)
                if not tu:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail={"error": "topup_not_found", "topup_id": topup_id},
                    )
                if tu.get("brand_id") != brand_id:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail={"error": "brand_mismatch"},
                    )

                amount = int(tu.get("amount") or 0)
                currency = tu.get("currency", DEFAULT_CURRENCY)
                existing_status = tu.get("status", "pending")

                # Idempotent: if already confirmed, just return state.
                if existing_status == "confirmed":
                    await pipe.unwatch()
                    balance = int(await r.get(_k_balance(brand_id)) or 0)
                    return TopupResponse(
                        topup_id=topup_id,
                        status="confirmed",
                        new_balance_cents=balance,
                        amount_cents=amount,
                        currency=currency,
                    )

                if existing_status == "failed":
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={"error": "topup_failed"},
                    )

                now = time.time()
                pipe.multi()
                pipe.incrby(_k_balance(brand_id), amount)
                pipe.set(_k_last_topup(brand_id), now)
                pipe.hset(
                    key,
                    mapping={
                        "status": "confirmed",
                        "confirmed_at": now,
                        "gateway_response": _safe_json(body.payment_gateway_response),
                    },
                )
                pipe.rpush(_k_tx_list(brand_id), topup_id)
                pipe.ltrim(_k_tx_list(brand_id), -TX_LIST_MAX, -1)
                results = await pipe.execute()

                new_balance = int(results[0])
                logger.info(
                    "topup confirmed brand=%s topup_id=%s amount=%s new_balance=%s",
                    brand_id,
                    topup_id,
                    amount,
                    new_balance,
                )
                return TopupResponse(
                    topup_id=topup_id,
                    status="confirmed",
                    new_balance_cents=new_balance,
                    amount_cents=amount,
                    currency=currency,
                )
        except aioredis.WatchError:
            continue


def _safe_json(o: dict) -> str:
    import json as _json

    try:
        return _json.dumps(o, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


# ── POST /{brand_id}/charge ──────────────────────────────────────────────
@router.post("/{brand_id}/charge")
async def charge(
    brand_id: str,
    body: ChargeRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Atomically deduct from balance for a campaign event.

    Returns 402 if balance is insufficient or the brand-wide daily cap is
    exceeded. The Redis transaction uses WATCH on all keys we read so a
    concurrent topup/charge re-runs the optimistic loop.
    """
    today = _today_str()
    balance_key = _k_balance(brand_id)
    daily_key = _k_daily_spent(brand_id, today)
    daily_budget_key = _k_daily_budget(brand_id)
    total_key = _k_total_spent(brand_id)

    attempts = 0
    while attempts < MAX_WATCH_RETRIES:
        attempts += 1
        try:
            async with r.pipeline(transaction=True) as pipe:
                await pipe.watch(balance_key, daily_key, daily_budget_key)

                balance = int(await pipe.get(balance_key) or 0)
                if balance < body.amount_cents:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_402_PAYMENT_REQUIRED,
                        detail={
                            "ok": False,
                            "reason": "insufficient_funds",
                            "balance_cents": balance,
                            "amount_cents": body.amount_cents,
                        },
                    )

                daily_spent = int(await pipe.get(daily_key) or 0)
                daily_budget = int(await pipe.get(daily_budget_key) or 0)
                if (
                    daily_budget > 0
                    and daily_spent + body.amount_cents > daily_budget
                ):
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_402_PAYMENT_REQUIRED,
                        detail={
                            "ok": False,
                            "reason": "daily_budget_exceeded",
                            "daily_spent_cents": daily_spent,
                            "daily_budget_cents": daily_budget,
                        },
                    )

                charge_id = uuid4().hex
                now = time.time()

                pipe.multi()
                pipe.decrby(balance_key, body.amount_cents)
                pipe.incrby(daily_key, body.amount_cents)
                pipe.expire(daily_key, 86400 + 3600)  # +1h safety overlap
                pipe.incrby(total_key, body.amount_cents)
                pipe.hset(
                    _k_charge(charge_id),
                    mapping={
                        "charge_id": charge_id,
                        "brand_id": brand_id,
                        "amount": body.amount_cents,
                        "reason": body.reason,
                        "reference_id": body.reference_id,
                        "campaign_id": body.campaign_id or "",
                        "ts": now,
                        "status": "completed",
                    },
                )
                pipe.rpush(_k_tx_list(brand_id), charge_id)
                pipe.ltrim(_k_tx_list(brand_id), -TX_LIST_MAX, -1)
                await pipe.execute()

                new_balance = balance - body.amount_cents
                logger.info(
                    "charge brand=%s amount=%s reason=%s new_balance=%s",
                    brand_id,
                    body.amount_cents,
                    body.reason,
                    new_balance,
                )

                # Best-effort: fire auto-recharge check outside the txn.
                try:
                    await _maybe_auto_recharge(brand_id, new_balance, r)
                except Exception as exc:  # never break the charge path
                    logger.warning("auto_recharge check failed: %s", exc)

                return ChargeResponse(
                    ok=True,
                    new_balance_cents=new_balance,
                    charge_id=charge_id,
                )
        except aioredis.WatchError:
            continue

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "charge_contention", "attempts": attempts},
    )


# ── POST /{brand_id}/refund ──────────────────────────────────────────────
@router.post("/{brand_id}/refund", response_model=RefundResponse)
async def refund(
    brand_id: str,
    body: RefundRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> RefundResponse:
    """Reverse a charge — credits balance back and reduces daily/total spend.

    Within the refund window. Idempotent on `charge_id` (a second refund
    on the same charge returns 409).
    """
    ckey = _k_charge(body.charge_id)
    balance_key = _k_balance(brand_id)
    today = _today_str()
    daily_key = _k_daily_spent(brand_id, today)
    total_key = _k_total_spent(brand_id)

    attempts = 0
    while attempts < MAX_WATCH_RETRIES:
        attempts += 1
        try:
            async with r.pipeline(transaction=True) as pipe:
                await pipe.watch(ckey, balance_key, daily_key, total_key)

                ch = await pipe.hgetall(ckey)
                if not ch:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail={"error": "charge_not_found"},
                    )
                if ch.get("brand_id") != brand_id:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail={"error": "brand_mismatch"},
                    )
                if ch.get("status") != "completed":
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "error": "charge_not_refundable",
                            "status": ch.get("status"),
                        },
                    )

                ch_ts = float(ch.get("ts") or 0.0)
                if time.time() - ch_ts > REFUND_WINDOW_SECONDS:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "error": "refund_window_expired",
                            "charge_ts": ch_ts,
                            "window_seconds": REFUND_WINDOW_SECONDS,
                        },
                    )

                amount = int(ch.get("amount") or 0)
                refund_id = uuid4().hex
                now = time.time()

                # Refund landed on a different UTC day than the charge?
                # We still credit back to today's daily_spent so brand-day
                # accounting matches actual cash flow today. If you'd rather
                # keep historical days clean, flip this to skip the daily
                # decrement when day-of-charge != today.
                pipe.multi()
                pipe.incrby(balance_key, amount)
                pipe.decrby(daily_key, amount)
                pipe.decrby(total_key, amount)
                pipe.hset(
                    ckey,
                    mapping={
                        "status": "refunded",
                        "refund_id": refund_id,
                        "refunded_at": now,
                    },
                )
                pipe.hset(
                    _k_refund(refund_id),
                    mapping={
                        "refund_id": refund_id,
                        "brand_id": brand_id,
                        "charge_id": body.charge_id,
                        "amount": amount,
                        "reason": body.reason,
                        "ts": now,
                        "status": "completed",
                    },
                )
                pipe.rpush(_k_tx_list(brand_id), refund_id)
                pipe.ltrim(_k_tx_list(brand_id), -TX_LIST_MAX, -1)
                results = await pipe.execute()

                new_balance = int(results[0])
                logger.info(
                    "refund brand=%s charge_id=%s amount=%s new_balance=%s",
                    brand_id,
                    body.charge_id,
                    amount,
                    new_balance,
                )
                return RefundResponse(
                    ok=True,
                    new_balance_cents=new_balance,
                    refund_id=refund_id,
                )
        except aioredis.WatchError:
            continue

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "refund_contention", "attempts": attempts},
    )


# ── POST /{brand_id}/auto-recharge/configure ─────────────────────────────
@router.post(
    "/{brand_id}/auto-recharge/configure", response_model=AutoRechargeConfig
)
async def configure_auto_recharge(
    brand_id: str,
    body: AutoRechargeConfig,
    r: aioredis.Redis = Depends(get_redis),
) -> AutoRechargeConfig:
    if body.enabled and (not body.payment_method or not body.payment_token):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_payment_method_or_token",
                "hint": "enabled=true requires payment_method and payment_token",
            },
        )

    await r.hset(
        _k_auto_recharge(brand_id),
        mapping={
            "enabled": "1" if body.enabled else "0",
            "threshold_cents": body.threshold_cents,
            "recharge_amount_cents": body.recharge_amount_cents,
            "payment_method": body.payment_method or "",
            "payment_token": body.payment_token or "",
            "updated_at": time.time(),
        },
    )
    return body


# ── GET /{brand_id}/transactions ─────────────────────────────────────────
@router.get("/{brand_id}/transactions", response_model=list[Transaction])
async def list_transactions(
    brand_id: str,
    from_ts: float | None = Query(None, alias="from"),
    to_ts: float | None = Query(None, alias="to"),
    tx_type: Literal["topup", "charge", "refund"] | None = Query(
        None, alias="type"
    ),
    limit: int = Query(100, ge=1, le=1000),
    r: aioredis.Redis = Depends(get_redis),
) -> list[Transaction]:
    """List transactions sorted by time desc, optionally filtered.

    We over-fetch from the brand list (newest first) and walk until we've
    collected `limit` matches in window. This avoids needing a secondary
    sorted-set index.
    """
    # Pull a slab from the tail (newest). 5× limit headroom for filter
    # misses, capped at TX_LIST_MAX.
    slab = min(limit * 5, TX_LIST_MAX)
    ids = await r.lrange(_k_tx_list(brand_id), -slab, -1)
    ids.reverse()  # newest first

    results: list[Transaction] = []
    for tx_id in ids:
        if len(results) >= limit:
            break

        # Resolve type by probing in order: charge → topup → refund.
        # Cheap because keys are O(1) and most lookups hit on first try.
        tx, kind = await _load_tx(r, tx_id)
        if tx is None:
            continue

        if tx_type and kind != tx_type:
            continue

        ts = float(tx.get("ts") or tx.get("created_at") or 0.0)
        if from_ts is not None and ts < from_ts:
            continue
        if to_ts is not None and ts > to_ts:
            continue

        results.append(
            Transaction(
                id=tx_id,
                type=kind,  # type: ignore[arg-type]
                amount_cents=int(tx.get("amount") or 0),
                ts=ts,
                status=tx.get("status", "unknown"),
                reason=tx.get("reason"),
                reference_id=tx.get("reference_id"),
                campaign_id=tx.get("campaign_id") or None,
                payment_method=tx.get("payment_method") or None,
            )
        )

    return results


async def _load_tx(
    r: aioredis.Redis, tx_id: str
) -> tuple[dict | None, str]:
    """Resolve a tx id to its hash + kind. Returns (None, '') if missing."""
    for kind, k in (
        ("charge", _k_charge(tx_id)),
        ("topup", _k_topup(tx_id)),
        ("refund", _k_refund(tx_id)),
    ):
        h = await r.hgetall(k)
        if h:
            return h, kind
    return None, ""


# ── GET /{brand_id}/daily-budget-status ──────────────────────────────────
@router.get("/{brand_id}/daily-budget-status", response_model=DailyBudgetStatus)
async def daily_budget_status(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> DailyBudgetStatus:
    today = _today_str()
    spent = int(await r.get(_k_daily_spent(brand_id, today)) or 0)
    budget = int(await r.get(_k_daily_budget(brand_id)) or 0)
    remaining = max(0, budget - spent) if budget > 0 else 0
    paused = budget > 0 and spent >= budget
    return DailyBudgetStatus(
        today_spent_cents=spent,
        today_budget_cents=budget,
        remaining_cents=remaining,
        paused=paused,
    )


# ── POST /{brand_id}/daily-budget ────────────────────────────────────────
@router.post("/{brand_id}/daily-budget", response_model=DailyBudgetStatus)
async def set_daily_budget(
    brand_id: str,
    body: DailyBudgetRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> DailyBudgetStatus:
    """Brand-wide daily cap (cents). 0 disables the cap.

    Independent of per-campaign budgets — this is the brand-level circuit
    breaker.
    """
    await r.set(_k_daily_budget(brand_id), body.daily_budget_cents)
    return await daily_budget_status(brand_id, r)


# ── GET /{brand_id}/forecast ─────────────────────────────────────────────
@router.get("/{brand_id}/forecast", response_model=Forecast)
async def forecast(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> Forecast:
    """Naive 7-day-avg forecast — good enough for a dashboard widget.

    Walks the recent transactions list, sums charge amounts in the trailing
    7-day window, divides by 7. days_until_empty = balance / avg.
    """
    balance = int(await r.get(_k_balance(brand_id)) or 0)
    now = time.time()
    window_start = now - 7 * 86400

    # Pull a generous slab — most brands will have << 5000 tx/week.
    ids = await r.lrange(_k_tx_list(brand_id), -5000, -1)
    total_charged = 0
    for tx_id in reversed(ids):
        ch = await r.hgetall(_k_charge(tx_id))
        if not ch:
            continue
        ts = float(ch.get("ts") or 0.0)
        if ts < window_start:
            break  # list is roughly time-ordered; stop scanning old data
        if ch.get("status") == "completed":
            total_charged += int(ch.get("amount") or 0)

    avg_daily = total_charged // 7 if total_charged else 0
    days_left: float | None
    if avg_daily <= 0:
        days_left = None
        rec: Literal[
            "topup_now", "reduce_bids", "add_campaign", "healthy"
        ] = "add_campaign" if balance > 0 else "topup_now"
    else:
        days_left = round(balance / avg_daily, 2)
        if days_left < 3:
            rec = "topup_now"
        elif days_left < 7:
            rec = "reduce_bids"
        else:
            rec = "healthy"

    return Forecast(
        avg_daily_spend_cents=avg_daily,
        days_until_empty=days_left,
        recommendation=rec,
    )
