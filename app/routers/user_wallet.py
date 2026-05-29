"""Consumer (user) Wallet router.

Mirrors the merchant wallet but keyed on ``user_id`` instead of ``brand_id``.
Powers:

* Deposits (老田 ¥99 bike rental hold)
* Pre-paid credits (老田 ¥50 ride credit)
* Refunds back to user
* Marketplace seller payouts (老胡)
* Subscription credits (老石)

All amounts are integer cents. Charges and freezes are atomic via Redis
WATCH/MULTI. Topups / charges / freeze-releases are idempotent on
``reference_id`` (24h window).

Redis schema
------------
    user_wallet:{uid}                  HASH (balance, frozen, currency,
                                            created_at, total_topup,
                                            total_charge)
    user_wallet:{uid}:tx               LIST  (chronological tx_ids)
    user_wallet_tx:{tx_id}             HASH  (per-tx record)
    user_wallet_idem:{ref}             STRING (24h tx_id pointer)
    user_wallet:{uid}:freeze:{ref}     HASH  (active freeze record)
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
DEFAULT_CURRENCY = "CNY"
MAX_WATCH_RETRIES = 8
TX_LIST_MAX = 10_000
IDEM_TTL_SECONDS = 24 * 3600

SUPPORTED_TOPUP_SOURCES = {
    "card",
    "wechat",
    "alipay",
    "bank_transfer",
    "refund",
    "reward",
    "deposit_release",
    "marketplace_payout",
    "subscription_credit",
}
SUPPORTED_WITHDRAW_DESTS = {
    "card_refund",
    "bank_transfer",
    "vendor",
    "deposit_freeze",
}
SUPPORTED_CHARGE_REASONS = {
    "purchase",
    "deposit",
    "subscription",
    "service",
    "ride",
    "rental",
    "marketplace_fee",
    "other",
}
SUPPORTED_FREEZE_REASONS = {
    "deposit",
    "reservation",
    "pending_settlement",
    "other",
}


# ── Key helpers ──────────────────────────────────────────────────────────
def _k_wallet(uid: str) -> str:
    return f"user_wallet:{uid}"


def _k_tx_list(uid: str) -> str:
    return f"user_wallet:{uid}:tx"


def _k_tx(tx_id: str) -> str:
    return f"user_wallet_tx:{tx_id}"


def _k_idem(ref: str) -> str:
    return f"user_wallet_idem:{ref}"


def _k_freeze(uid: str, ref: str) -> str:
    return f"user_wallet:{uid}:freeze:{ref}"


def _k_daily_spent(uid: str, brand_id: str) -> str:
    """Per-(user, brand) daily-spent counter. TTL 86400s."""
    day = _today()
    return f"user_wallet:{uid}:daily_spent:{brand_id}:{day}"


def _now() -> float:
    return time.time()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Pydantic models ──────────────────────────────────────────────────────
class CreateWalletRequest(BaseModel):
    currency: str = Field(DEFAULT_CURRENCY, min_length=3, max_length=3)
    initial_amount_cents: int = Field(0, ge=0, le=100_000_000)

    @field_validator("currency")
    @classmethod
    def _cur(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 3 or not v.isalpha():
            raise ValueError("currency must be a 3-letter ISO code")
        return v


class WalletStatus(BaseModel):
    user_id: str
    balance_cents: int
    frozen_cents: int
    available_cents: int
    currency: str
    created_at: float
    total_topup_cents: int
    total_charge_cents: int


class TopupRequest(BaseModel):
    amount_cents: int = Field(..., gt=0, le=100_000_000)
    source: Literal[
        "card", "wechat", "alipay", "bank_transfer",
        "refund", "reward", "deposit_release",
        "marketplace_payout", "subscription_credit",
    ]
    reference_id: str = Field(..., min_length=1, max_length=128)
    brand_id: str | None = Field(default=None, max_length=128)
    note: str | None = Field(default=None, max_length=256)


class TopupResponse(BaseModel):
    ok: bool
    tx_id: str
    new_balance_cents: int
    amount_cents: int
    idempotent: bool = False


class WithdrawRequest(BaseModel):
    amount_cents: int = Field(..., gt=0, le=100_000_000)
    destination: Literal[
        "card_refund", "bank_transfer", "vendor", "deposit_freeze",
    ]
    reference_id: str = Field(..., min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=256)


class WithdrawResponse(BaseModel):
    ok: bool
    tx_id: str
    new_balance_cents: int
    amount_cents: int
    idempotent: bool = False


class ChargeRequest(BaseModel):
    amount_cents: int = Field(..., gt=0, le=100_000_000)
    reason: Literal[
        "purchase", "deposit", "subscription", "service",
        "ride", "rental", "marketplace_fee", "other",
    ]
    brand_id: str = Field(..., min_length=1, max_length=128)
    reference_id: str = Field(..., min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=256)


class ChargeResponse(BaseModel):
    ok: bool
    tx_id: str
    new_balance_cents: int
    amount_cents: int
    idempotent: bool = False


class FreezeRequest(BaseModel):
    amount_cents: int = Field(..., gt=0, le=100_000_000)
    reason: Literal["deposit", "reservation", "pending_settlement", "other"]
    reference_id: str = Field(..., min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=256)


class FreezeResponse(BaseModel):
    ok: bool
    reference_id: str
    frozen_cents: int  # total frozen on this user
    available_cents: int
    idempotent: bool = False


class ReleaseFreezeRequest(BaseModel):
    reference_id: str = Field(..., min_length=1, max_length=128)
    action: Literal["release_to_available", "convert_to_charge"]
    brand_id: str | None = Field(default=None, max_length=128)
    reason: str | None = Field(default=None, max_length=256)


class ReleaseFreezeResponse(BaseModel):
    ok: bool
    action: str
    released_amount_cents: int
    balance_cents: int
    frozen_cents: int
    available_cents: int
    charge_tx_id: str | None = None


class Transaction(BaseModel):
    tx_id: str
    type: str  # topup / withdraw / charge / freeze / release / freeze_charge
    amount_cents: int
    ts: float
    status: str
    source: str | None = None
    destination: str | None = None
    reason: str | None = None
    brand_id: str | None = None
    reference_id: str | None = None
    note: str | None = None


# ── Internal helpers ─────────────────────────────────────────────────────
async def _ensure_exists(r: aioredis.Redis, uid: str) -> dict:
    raw = await r.hgetall(_k_wallet(uid))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "wallet_not_found", "user_id": uid},
        )
    return raw


async def _append_tx(
    pipe: aioredis.client.Pipeline,
    uid: str,
    tx_id: str,
    payload: dict,
) -> None:
    pipe.hset(_k_tx(tx_id), mapping=payload)
    pipe.rpush(_k_tx_list(uid), tx_id)
    pipe.ltrim(_k_tx_list(uid), -TX_LIST_MAX, -1)


async def _check_idem(
    r: aioredis.Redis, ref: str
) -> tuple[str | None, dict | None]:
    """Return (tx_id, tx_payload) if reference_id was used before."""
    tx_id = await r.get(_k_idem(ref))
    if not tx_id:
        return None, None
    payload = await r.hgetall(_k_tx(tx_id))
    return tx_id, (payload or None)


# ── POST /create ─────────────────────────────────────────────────────────
@router.post("/{user_id}/create", response_model=WalletStatus)
async def create_wallet(
    user_id: str,
    body: CreateWalletRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> WalletStatus:
    """Create a new consumer wallet. Idempotent — re-create returns existing.

    If the wallet exists, ``currency`` must match. ``initial_amount_cents``
    is only applied on first create.
    """
    key = _k_wallet(user_id)
    existing = await r.hgetall(key)
    if existing:
        if existing.get("currency") and existing["currency"] != body.currency:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "currency_mismatch",
                    "wallet_currency": existing["currency"],
                    "requested": body.currency,
                },
            )
        return WalletStatus(
            user_id=user_id,
            balance_cents=int(existing.get("balance") or 0),
            frozen_cents=int(existing.get("frozen") or 0),
            available_cents=int(existing.get("balance") or 0)
            - int(existing.get("frozen") or 0),
            currency=existing.get("currency") or body.currency,
            created_at=float(existing.get("created_at") or _now()),
            total_topup_cents=int(existing.get("total_topup") or 0),
            total_charge_cents=int(existing.get("total_charge") or 0),
        )

    now = _now()
    await r.hset(
        key,
        mapping={
            "balance": body.initial_amount_cents,
            "frozen": 0,
            "currency": body.currency,
            "created_at": now,
            "total_topup": body.initial_amount_cents,
            "total_charge": 0,
        },
    )
    if body.initial_amount_cents > 0:
        # Audit-log the initial credit as a synthetic topup tx.
        tx_id = uuid4().hex
        await r.hset(
            _k_tx(tx_id),
            mapping={
                "tx_id": tx_id,
                "user_id": user_id,
                "type": "topup",
                "amount": body.initial_amount_cents,
                "source": "initial_grant",
                "reference_id": f"create:{user_id}",
                "ts": now,
                "status": "completed",
            },
        )
        await r.rpush(_k_tx_list(user_id), tx_id)

    logger.info(
        "user_wallet_created uid=%s currency=%s initial=%s",
        user_id, body.currency, body.initial_amount_cents,
    )
    return WalletStatus(
        user_id=user_id,
        balance_cents=body.initial_amount_cents,
        frozen_cents=0,
        available_cents=body.initial_amount_cents,
        currency=body.currency,
        created_at=now,
        total_topup_cents=body.initial_amount_cents,
        total_charge_cents=0,
    )


# ── GET /{user_id} ───────────────────────────────────────────────────────
@router.get("/{user_id}", response_model=WalletStatus)
async def get_wallet(
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> WalletStatus:
    raw = await _ensure_exists(r, user_id)
    balance = int(raw.get("balance") or 0)
    frozen = int(raw.get("frozen") or 0)
    return WalletStatus(
        user_id=user_id,
        balance_cents=balance,
        frozen_cents=frozen,
        available_cents=balance - frozen,
        currency=raw.get("currency") or DEFAULT_CURRENCY,
        created_at=float(raw.get("created_at") or 0.0),
        total_topup_cents=int(raw.get("total_topup") or 0),
        total_charge_cents=int(raw.get("total_charge") or 0),
    )


# ── POST /{user_id}/topup ────────────────────────────────────────────────
@router.post("/{user_id}/topup", response_model=TopupResponse)
async def topup(
    user_id: str,
    body: TopupRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> TopupResponse:
    """Credit the wallet. Idempotent on ``reference_id``.

    Auto-creates the wallet if missing so callers (refund flows, marketplace
    payouts) don't have to pre-provision.
    """
    # Idempotency check first.
    existing_tx_id, existing_tx = await _check_idem(r, body.reference_id)
    if existing_tx_id and existing_tx:
        balance = int((await r.hgetall(_k_wallet(user_id))).get("balance") or 0)
        return TopupResponse(
            ok=True,
            tx_id=existing_tx_id,
            new_balance_cents=balance,
            amount_cents=int(existing_tx.get("amount") or body.amount_cents),
            idempotent=True,
        )

    wkey = _k_wallet(user_id)
    # Auto-create if missing (currency defaults to CNY).
    if not await r.exists(wkey):
        await r.hset(
            wkey,
            mapping={
                "balance": 0,
                "frozen": 0,
                "currency": DEFAULT_CURRENCY,
                "created_at": _now(),
                "total_topup": 0,
                "total_charge": 0,
            },
        )

    tx_id = uuid4().hex
    now = _now()

    attempts = 0
    while attempts < MAX_WATCH_RETRIES:
        attempts += 1
        try:
            async with r.pipeline(transaction=True) as pipe:
                await pipe.watch(wkey, _k_idem(body.reference_id))
                # Re-check idem inside WATCH window.
                existing = await pipe.get(_k_idem(body.reference_id))
                if existing:
                    await pipe.unwatch()
                    payload = await r.hgetall(_k_tx(existing))
                    balance = int(
                        (await r.hgetall(wkey)).get("balance") or 0
                    )
                    return TopupResponse(
                        ok=True,
                        tx_id=existing,
                        new_balance_cents=balance,
                        amount_cents=int(
                            payload.get("amount") or body.amount_cents
                        ),
                        idempotent=True,
                    )

                pipe.multi()
                pipe.hincrby(wkey, "balance", body.amount_cents)
                pipe.hincrby(wkey, "total_topup", body.amount_cents)
                await _append_tx(
                    pipe, user_id, tx_id,
                    {
                        "tx_id": tx_id,
                        "user_id": user_id,
                        "type": "topup",
                        "amount": body.amount_cents,
                        "source": body.source,
                        "brand_id": body.brand_id or "",
                        "reference_id": body.reference_id,
                        "note": body.note or "",
                        "ts": now,
                        "status": "completed",
                    },
                )
                pipe.set(
                    _k_idem(body.reference_id),
                    tx_id,
                    ex=IDEM_TTL_SECONDS,
                )
                results = await pipe.execute()
                new_balance = int(results[0])
                logger.info(
                    "user_wallet_topup uid=%s amount=%s source=%s ref=%s",
                    user_id, body.amount_cents, body.source, body.reference_id,
                )
                return TopupResponse(
                    ok=True,
                    tx_id=tx_id,
                    new_balance_cents=new_balance,
                    amount_cents=body.amount_cents,
                )
        except aioredis.WatchError:
            continue

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "topup_contention", "attempts": attempts},
    )


# ── POST /{user_id}/withdraw ─────────────────────────────────────────────
@router.post("/{user_id}/withdraw", response_model=WithdrawResponse)
async def withdraw(
    user_id: str,
    body: WithdrawRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> WithdrawResponse:
    """Debit the wallet (e.g. refund to card, vendor settlement).

    Insufficient available balance → 402. Idempotent on ``reference_id``.
    """
    existing_tx_id, existing_tx = await _check_idem(r, body.reference_id)
    if existing_tx_id and existing_tx:
        balance = int((await r.hgetall(_k_wallet(user_id))).get("balance") or 0)
        return WithdrawResponse(
            ok=True,
            tx_id=existing_tx_id,
            new_balance_cents=balance,
            amount_cents=int(existing_tx.get("amount") or body.amount_cents),
            idempotent=True,
        )

    wkey = _k_wallet(user_id)
    await _ensure_exists(r, user_id)
    tx_id = uuid4().hex
    now = _now()

    attempts = 0
    while attempts < MAX_WATCH_RETRIES:
        attempts += 1
        try:
            async with r.pipeline(transaction=True) as pipe:
                await pipe.watch(wkey, _k_idem(body.reference_id))
                existing = await pipe.get(_k_idem(body.reference_id))
                if existing:
                    await pipe.unwatch()
                    payload = await r.hgetall(_k_tx(existing))
                    bal = int((await r.hgetall(wkey)).get("balance") or 0)
                    return WithdrawResponse(
                        ok=True,
                        tx_id=existing,
                        new_balance_cents=bal,
                        amount_cents=int(
                            payload.get("amount") or body.amount_cents
                        ),
                        idempotent=True,
                    )

                wallet = await pipe.hgetall(wkey)
                balance = int(wallet.get("balance") or 0)
                frozen = int(wallet.get("frozen") or 0)
                available = balance - frozen
                if available < body.amount_cents:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_402_PAYMENT_REQUIRED,
                        detail={
                            "error": "insufficient_funds",
                            "available_cents": available,
                            "amount_cents": body.amount_cents,
                        },
                    )

                pipe.multi()
                pipe.hincrby(wkey, "balance", -body.amount_cents)
                await _append_tx(
                    pipe, user_id, tx_id,
                    {
                        "tx_id": tx_id,
                        "user_id": user_id,
                        "type": "withdraw",
                        "amount": body.amount_cents,
                        "destination": body.destination,
                        "reference_id": body.reference_id,
                        "note": body.note or "",
                        "ts": now,
                        "status": "completed",
                    },
                )
                pipe.set(
                    _k_idem(body.reference_id), tx_id, ex=IDEM_TTL_SECONDS
                )
                results = await pipe.execute()
                new_balance = int(results[0])
                logger.info(
                    "user_wallet_withdraw uid=%s amount=%s dest=%s",
                    user_id, body.amount_cents, body.destination,
                )
                return WithdrawResponse(
                    ok=True,
                    tx_id=tx_id,
                    new_balance_cents=new_balance,
                    amount_cents=body.amount_cents,
                )
        except aioredis.WatchError:
            continue

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "withdraw_contention", "attempts": attempts},
    )


# ── POST /{user_id}/charge ───────────────────────────────────────────────
@router.post("/{user_id}/charge", response_model=ChargeResponse)
async def charge(
    user_id: str,
    body: ChargeRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> ChargeResponse:
    """Atomic charge: deduct from available balance.

    Different from withdraw in that it always points at a ``brand_id`` —
    used for in-app purchases, subscriptions, ride/rental charges.
    Idempotent on ``reference_id``.
    """
    existing_tx_id, existing_tx = await _check_idem(r, body.reference_id)
    if existing_tx_id and existing_tx:
        bal = int((await r.hgetall(_k_wallet(user_id))).get("balance") or 0)
        return ChargeResponse(
            ok=True,
            tx_id=existing_tx_id,
            new_balance_cents=bal,
            amount_cents=int(existing_tx.get("amount") or body.amount_cents),
            idempotent=True,
        )

    wkey = _k_wallet(user_id)
    await _ensure_exists(r, user_id)
    tx_id = uuid4().hex
    now = _now()

    attempts = 0
    while attempts < MAX_WATCH_RETRIES:
        attempts += 1
        try:
            async with r.pipeline(transaction=True) as pipe:
                await pipe.watch(wkey, _k_idem(body.reference_id))
                existing = await pipe.get(_k_idem(body.reference_id))
                if existing:
                    await pipe.unwatch()
                    payload = await r.hgetall(_k_tx(existing))
                    bal = int((await r.hgetall(wkey)).get("balance") or 0)
                    return ChargeResponse(
                        ok=True,
                        tx_id=existing,
                        new_balance_cents=bal,
                        amount_cents=int(
                            payload.get("amount") or body.amount_cents
                        ),
                        idempotent=True,
                    )

                wallet = await pipe.hgetall(wkey)
                balance = int(wallet.get("balance") or 0)
                frozen = int(wallet.get("frozen") or 0)
                available = balance - frozen
                if available < body.amount_cents:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_402_PAYMENT_REQUIRED,
                        detail={
                            "error": "insufficient_funds",
                            "available_cents": available,
                            "amount_cents": body.amount_cents,
                        },
                    )

                # Bug 9 fix: refuse to mutate near-midnight when the
                # daily-spent TTL is about to roll. The daily_spent counter
                # belongs to the same key (wkey) but a separate brand-scoped
                # daily counter has its own TTL — check it before committing
                # so we don't end up with a negative leftover after expiry.
                ds_key = _k_daily_spent(user_id, body.brand_id)
                ds_ttl = await pipe.ttl(ds_key)
                # ttl returns -2 if no key, -1 if no TTL set, else seconds.
                if isinstance(ds_ttl, int) and 0 < ds_ttl < 60:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={
                            "error": "daily_counter_rollover",
                            "retry_after_seconds": ds_ttl + 1,
                        },
                    )

                pipe.multi()
                # Bug 3 fix: wkey is already in WATCH set (see watch() call
                # above) so any concurrent mutation to balance/frozen aborts
                # this transaction via WatchError and we retry. We additionally
                # assert the post-decrement balance is non-negative below.
                pipe.hincrby(wkey, "balance", -body.amount_cents)
                pipe.hincrby(wkey, "total_charge", body.amount_cents)
                # Daily-spent counter for cap enforcement / analytics.
                pipe.hincrby(ds_key, "spent_cents", body.amount_cents)
                pipe.expire(ds_key, 86400)
                await _append_tx(
                    pipe, user_id, tx_id,
                    {
                        "tx_id": tx_id,
                        "user_id": user_id,
                        "type": "charge",
                        "amount": body.amount_cents,
                        "reason": body.reason,
                        "brand_id": body.brand_id,
                        "reference_id": body.reference_id,
                        "note": body.note or "",
                        "ts": now,
                        "status": "completed",
                    },
                )
                pipe.set(
                    _k_idem(body.reference_id), tx_id, ex=IDEM_TTL_SECONDS
                )
                results = await pipe.execute()
                new_balance = int(results[0])
                # Defensive: WATCH should have prevented this, but if a
                # concurrent path somehow drove balance negative, log loudly.
                if new_balance < 0:
                    logger.error(
                        "user_wallet_charge produced negative balance uid=%s new=%s",
                        user_id, new_balance,
                    )
                logger.info(
                    "user_wallet_charge uid=%s amount=%s reason=%s brand=%s",
                    user_id, body.amount_cents, body.reason, body.brand_id,
                )
                return ChargeResponse(
                    ok=True,
                    tx_id=tx_id,
                    new_balance_cents=new_balance,
                    amount_cents=body.amount_cents,
                )
        except aioredis.WatchError:
            continue

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "charge_contention", "attempts": attempts},
    )


# ── POST /{user_id}/freeze ───────────────────────────────────────────────
@router.post("/{user_id}/freeze", response_model=FreezeResponse)
async def freeze(
    user_id: str,
    body: FreezeRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> FreezeResponse:
    """Reserve funds — increments frozen counter, decreasing available.

    Idempotent on ``reference_id``. The freeze record persists until
    /release-freeze is called.
    """
    fkey = _k_freeze(user_id, body.reference_id)
    wkey = _k_wallet(user_id)
    await _ensure_exists(r, user_id)

    # Idempotent: existing freeze with the same ref → return current state.
    existing_freeze = await r.hgetall(fkey)
    if existing_freeze:
        wallet = await r.hgetall(wkey)
        balance = int(wallet.get("balance") or 0)
        frozen = int(wallet.get("frozen") or 0)
        return FreezeResponse(
            ok=True,
            reference_id=body.reference_id,
            frozen_cents=frozen,
            available_cents=balance - frozen,
            idempotent=True,
        )

    now = _now()
    attempts = 0
    while attempts < MAX_WATCH_RETRIES:
        attempts += 1
        try:
            async with r.pipeline(transaction=True) as pipe:
                await pipe.watch(wkey, fkey)
                if await pipe.exists(fkey):
                    await pipe.unwatch()
                    wallet = await r.hgetall(wkey)
                    balance = int(wallet.get("balance") or 0)
                    frozen = int(wallet.get("frozen") or 0)
                    return FreezeResponse(
                        ok=True,
                        reference_id=body.reference_id,
                        frozen_cents=frozen,
                        available_cents=balance - frozen,
                        idempotent=True,
                    )

                wallet = await pipe.hgetall(wkey)
                balance = int(wallet.get("balance") or 0)
                frozen = int(wallet.get("frozen") or 0)
                available = balance - frozen
                if available < body.amount_cents:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_402_PAYMENT_REQUIRED,
                        detail={
                            "error": "insufficient_funds",
                            "available_cents": available,
                            "amount_cents": body.amount_cents,
                        },
                    )

                tx_id = uuid4().hex
                pipe.multi()
                pipe.hincrby(wkey, "frozen", body.amount_cents)
                pipe.hset(
                    fkey,
                    mapping={
                        "user_id": user_id,
                        "reference_id": body.reference_id,
                        "amount": body.amount_cents,
                        "reason": body.reason,
                        "note": body.note or "",
                        "ts": now,
                        "status": "active",
                    },
                )
                await _append_tx(
                    pipe, user_id, tx_id,
                    {
                        "tx_id": tx_id,
                        "user_id": user_id,
                        "type": "freeze",
                        "amount": body.amount_cents,
                        "reason": body.reason,
                        "reference_id": body.reference_id,
                        "note": body.note or "",
                        "ts": now,
                        "status": "active",
                    },
                )
                results = await pipe.execute()
                new_frozen = int(results[0])
                logger.info(
                    "user_wallet_freeze uid=%s amount=%s reason=%s ref=%s",
                    user_id, body.amount_cents, body.reason, body.reference_id,
                )
                return FreezeResponse(
                    ok=True,
                    reference_id=body.reference_id,
                    frozen_cents=new_frozen,
                    available_cents=balance - new_frozen,
                )
        except aioredis.WatchError:
            continue

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "freeze_contention", "attempts": attempts},
    )


# ── POST /{user_id}/release-freeze ───────────────────────────────────────
@router.post(
    "/{user_id}/release-freeze", response_model=ReleaseFreezeResponse
)
async def release_freeze(
    user_id: str,
    body: ReleaseFreezeRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> ReleaseFreezeResponse:
    """Release an active freeze.

    * ``release_to_available``: simply un-freezes (decrements frozen counter).
    * ``convert_to_charge``: un-freezes AND debits balance (deposit forfeit /
      partial deduction). Requires ``brand_id``.
    """
    fkey = _k_freeze(user_id, body.reference_id)
    wkey = _k_wallet(user_id)
    await _ensure_exists(r, user_id)

    attempts = 0
    while attempts < MAX_WATCH_RETRIES:
        attempts += 1
        try:
            async with r.pipeline(transaction=True) as pipe:
                await pipe.watch(wkey, fkey)
                fr = await pipe.hgetall(fkey)
                if not fr:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail={
                            "error": "freeze_not_found",
                            "reference_id": body.reference_id,
                        },
                    )
                if fr.get("status") != "active":
                    # Idempotent: already released → return current wallet.
                    await pipe.unwatch()
                    wallet = await r.hgetall(wkey)
                    bal = int(wallet.get("balance") or 0)
                    fz = int(wallet.get("frozen") or 0)
                    return ReleaseFreezeResponse(
                        ok=True,
                        action=fr.get("status") or body.action,
                        released_amount_cents=int(fr.get("amount") or 0),
                        balance_cents=bal,
                        frozen_cents=fz,
                        available_cents=bal - fz,
                    )

                amount = int(fr.get("amount") or 0)
                now = _now()
                charge_tx_id: str | None = None
                tx_id = uuid4().hex

                pipe.multi()
                pipe.hincrby(wkey, "frozen", -amount)
                if body.action == "release_to_available":
                    pipe.hset(
                        fkey,
                        mapping={
                            "status": "released",
                            "released_at": now,
                            "release_reason": body.reason or "",
                        },
                    )
                    await _append_tx(
                        pipe, user_id, tx_id,
                        {
                            "tx_id": tx_id,
                            "user_id": user_id,
                            "type": "release",
                            "amount": amount,
                            "reason": body.reason or "",
                            "reference_id": body.reference_id,
                            "ts": now,
                            "status": "completed",
                        },
                    )
                else:  # convert_to_charge
                    if not body.brand_id:
                        await pipe.unwatch()
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail={
                                "error": "brand_id_required",
                                "hint": "convert_to_charge needs brand_id",
                            },
                        )
                    pipe.hincrby(wkey, "balance", -amount)
                    pipe.hincrby(wkey, "total_charge", amount)
                    charge_tx_id = tx_id
                    pipe.hset(
                        fkey,
                        mapping={
                            "status": "converted_to_charge",
                            "released_at": now,
                            "charge_tx_id": charge_tx_id,
                            "release_reason": body.reason or "",
                        },
                    )
                    await _append_tx(
                        pipe, user_id, tx_id,
                        {
                            "tx_id": tx_id,
                            "user_id": user_id,
                            "type": "freeze_charge",
                            "amount": amount,
                            "reason": fr.get("reason") or "deposit",
                            "brand_id": body.brand_id,
                            "reference_id": body.reference_id,
                            "note": body.reason or "",
                            "ts": now,
                            "status": "completed",
                        },
                    )
                await pipe.execute()
                wallet = await r.hgetall(wkey)
                bal = int(wallet.get("balance") or 0)
                fz = int(wallet.get("frozen") or 0)
                logger.info(
                    "user_wallet_release uid=%s ref=%s action=%s amount=%s",
                    user_id, body.reference_id, body.action, amount,
                )
                return ReleaseFreezeResponse(
                    ok=True,
                    action=body.action,
                    released_amount_cents=amount,
                    balance_cents=bal,
                    frozen_cents=fz,
                    available_cents=bal - fz,
                    charge_tx_id=charge_tx_id,
                )
        except aioredis.WatchError:
            continue

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "release_contention", "attempts": attempts},
    )


# ── GET /{user_id}/transactions ──────────────────────────────────────────
@router.get("/{user_id}/transactions", response_model=list[Transaction])
async def list_transactions(
    user_id: str,
    from_ts: float | None = Query(None, alias="from"),
    to_ts: float | None = Query(None, alias="to"),
    limit: int = Query(100, ge=1, le=1000),
    r: aioredis.Redis = Depends(get_redis),
) -> list[Transaction]:
    await _ensure_exists(r, user_id)
    slab = min(limit * 5, TX_LIST_MAX)
    ids = await r.lrange(_k_tx_list(user_id), -slab, -1)
    ids.reverse()  # newest first

    results: list[Transaction] = []
    for tx_id in ids:
        if len(results) >= limit:
            break
        tx = await r.hgetall(_k_tx(tx_id))
        if not tx:
            continue
        ts = float(tx.get("ts") or 0.0)
        if from_ts is not None and ts < from_ts:
            continue
        if to_ts is not None and ts > to_ts:
            continue
        results.append(
            Transaction(
                tx_id=tx_id,
                type=tx.get("type") or "unknown",
                amount_cents=int(tx.get("amount") or 0),
                ts=ts,
                status=tx.get("status") or "unknown",
                source=tx.get("source") or None,
                destination=tx.get("destination") or None,
                reason=tx.get("reason") or None,
                brand_id=tx.get("brand_id") or None,
                reference_id=tx.get("reference_id") or None,
                note=tx.get("note") or None,
            )
        )
    return results
