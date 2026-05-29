"""Payouts & Settlement — KiX merchant withdrawal spine.

The attribution router (`attribution.py`) accrues commission to
`brand:{bid}:commission_owed` whenever a cross-brand conversion is
attributed. That money is *owed* to the source merchant but locked in
Redis until they pull it out to a real bank account.

This module is the withdrawal pipe:

    commission_owed ──► payout_request (pending)
                              │
                              ▼ admin /process
                              ├──► paid              ──► paid_lifetime
                              └──► failed            ──► refund commission_owed

Plus invoice generation (PDF metadata stub) and an auto-payout cron
(`/cron/run`) that flushes eligible brands on weekly / biweekly /
monthly schedules.

All state lives in Redis. Atomic debit uses WATCH/MULTI mirroring the
pattern in `wallet.charge`. Bank account numbers are SHA-256 hashed at
the edge — only the last 4 are stored in cleartext.

Key schema
----------
    brand:{bid}:bank_accounts          SET   — bank_account_ids
    bank_account:{id}                  HASH  — account metadata (hashed)
    brand:{bid}:commission_owed        HASH  — field "cents" (from attribution)
    brand:{bid}:pending_payouts        STRING (cents counter)
    brand:{bid}:paid_lifetime          STRING (cents counter)
    payout:{payout_id}                 HASH  — full payout record
    brand:{bid}:payouts                ZSET  — score=requested_at
    invoice:{invoice_id}               HASH  — invoice metadata
    brand:{bid}:invoices               LIST  — invoice_ids (newest first)
    brand:{bid}:payout_schedule        HASH  — frequency / min / auto / last_run_at
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.config import settings
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ──────────────────────────────────────────────────────────────

MAX_WATCH_RETRIES = 8
PAYOUT_ETA_DAYS = 5  # 3–7 day band, surface midpoint
DEFAULT_MIN_PAYOUT_CENTS = 10_000  # $100 / ¥100
DEFAULT_CURRENCY = "CNY"

# Status machine
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_PAID = "paid"
STATUS_FAILED = "failed"

VALID_TRANSITIONS = {
    STATUS_PENDING: {STATUS_PROCESSING, STATUS_FAILED},
    STATUS_PROCESSING: {STATUS_PAID, STATUS_FAILED},
    STATUS_PAID: set(),
    STATUS_FAILED: set(),
}

# Source of funds
SRC_COMMISSION = "commission"
SRC_WALLET_REFUND = "wallet_refund"
VALID_SOURCES = {SRC_COMMISSION, SRC_WALLET_REFUND}

# Schedule cadences
FREQ_WEEKLY = "weekly"
FREQ_BIWEEKLY = "biweekly"
FREQ_MONTHLY = "monthly"
FREQ_SECONDS = {
    FREQ_WEEKLY: 7 * 86400,
    FREQ_BIWEEKLY: 14 * 86400,
    FREQ_MONTHLY: 30 * 86400,
}

# Invoice file stub — in production this would be a signed S3/OSS URL.
INVOICE_URL_TEMPLATE = "https://invoices.kix.app/{invoice_id}.pdf"

# Admin token — in production a JWT with role=admin; for now share the JWT
# secret as a coarse pre-shared key, mirroring the project's other internal
# trust boundaries.
_ADMIN_TOKEN_FALLBACK = settings.jwt_secret


# ── Redis key helpers ──────────────────────────────────────────────────────

def _k_bank_accounts(b: str) -> str:
    return f"brand:{b}:bank_accounts"


def _k_bank_account(bank_account_id: str) -> str:
    return f"bank_account:{bank_account_id}"


def _k_commission_owed(b: str) -> str:
    return f"brand:{b}:commission_owed"


def _k_wallet_balance(b: str) -> str:
    return f"wallet:{b}:balance"


def _k_pending_payouts(b: str) -> str:
    return f"brand:{b}:pending_payouts"


def _k_paid_lifetime(b: str) -> str:
    return f"brand:{b}:paid_lifetime"


def _k_payout(payout_id: str) -> str:
    return f"payout:{payout_id}"


def _k_brand_payouts(b: str) -> str:
    return f"brand:{b}:payouts"


def _k_invoice(invoice_id: str) -> str:
    return f"invoice:{invoice_id}"


def _k_brand_invoices(b: str) -> str:
    return f"brand:{b}:invoices"


def _k_payout_schedule(b: str) -> str:
    return f"brand:{b}:payout_schedule"


def _k_wallet_currency(b: str) -> str:
    return f"wallet:{b}:currency"


# ── Pydantic models ────────────────────────────────────────────────────────

class BankAccountAddRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    account_holder: str = Field(..., min_length=1, max_length=120)
    bank_name: str = Field(..., min_length=1, max_length=120)
    account_number_hash: str = Field(
        ...,
        min_length=4,
        max_length=128,
        description="Either a SHA-256 hex hash, or the raw number — we re-hash.",
    )
    routing: str | None = Field(default=None, max_length=64)
    swift: str | None = Field(default=None, max_length=32)
    country: str = Field(..., min_length=2, max_length=3)
    currency: str = Field(default=DEFAULT_CURRENCY, min_length=3, max_length=3)


class BankAccountAddResponse(BaseModel):
    bank_account_id: str
    verification_required: bool


class BankAccountVerifyRequest(BaseModel):
    brand_id: str
    micro_deposit_amounts: list[int] | None = Field(
        default=None,
        description="Two small cent amounts the bank wired; e.g. [11, 23].",
    )


class BankAccountVerifyResponse(BaseModel):
    verified: bool


class BalanceResponse(BaseModel):
    brand_id: str
    currency: str
    commission_owed_cents: int
    wallet_balance_cents: int
    pending_payouts_cents: int
    paid_lifetime_cents: int
    next_payout_eligible_at: float | None


class PayoutRequest(BaseModel):
    brand_id: str
    amount_cents: int = Field(..., gt=0)
    bank_account_id: str
    source: Literal["commission", "wallet_refund"] = SRC_COMMISSION


class PayoutResponse(BaseModel):
    payout_id: str
    brand_id: str
    amount_cents: int
    currency: str
    bank_account_id: str
    bank_account_masked: str | None = None
    source: str
    status: str
    eta_days: int
    requested_at: float
    processed_at: float | None = None
    failed_at: float | None = None
    failure_reason: str | None = None


class AdminProcessRequest(BaseModel):
    admin_token: str
    payment_provider_ref: str | None = None


class AdminFailRequest(BaseModel):
    admin_token: str
    reason: str = Field(..., min_length=1, max_length=500)


class InvoiceGenerateRequest(BaseModel):
    brand_id: str
    period_start: float
    period_end: float


class InvoiceLine(BaseModel):
    counterparty_brand: str  # the target brand whose orders generated the commission
    orders: int
    gross_cents: int
    commission_cents: int


class InvoiceResponse(BaseModel):
    invoice_id: str
    invoice_number: str
    brand_id: str
    period_start: float
    period_end: float
    currency: str
    lines: list[InvoiceLine]
    commission_earned_cents: int
    kix_take_cents: int
    net_payable_cents: int
    bank_account_masked: str | None = None
    status: str
    url: str
    generated_at: float


class ScheduleConfigureRequest(BaseModel):
    brand_id: str
    frequency: Literal["weekly", "biweekly", "monthly"] = FREQ_WEEKLY
    min_payout_cents: int = Field(default=DEFAULT_MIN_PAYOUT_CENTS, ge=100)
    auto: bool = True
    bank_account_id: str | None = None


class ScheduleResponse(BaseModel):
    brand_id: str
    frequency: str
    min_payout_cents: int
    auto: bool
    bank_account_id: str | None = None
    last_run_at: float | None = None
    next_run_at: float | None = None


class CronRunRequest(BaseModel):
    admin_token: str
    dry_run: bool = False


class CronRunResponse(BaseModel):
    scanned_brands: int
    payouts_created: int
    skipped: int
    payout_ids: list[str]


# ── Helpers ────────────────────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid4().hex}"


def _check_admin(token: str) -> None:
    """Reject if the admin token does not match.

    Production swap: validate a signed JWT with `role=admin`. The
    pre-shared key path stays as a controlled-environment fallback.
    """
    if not token or not secrets.compare_digest(token, _ADMIN_TOKEN_FALLBACK):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "admin_token_invalid"},
        )


def _hash_account_number(raw: str) -> str:
    """Idempotent hash: if input is already a 64-char hex digest, return as-is.

    Otherwise SHA-256 it. Clients SHOULD hash on their side; this is
    defense-in-depth so we never persist a raw number.
    """
    s = (raw or "").strip()
    if len(s) == 64 and all(c in "0123456789abcdef" for c in s.lower()):
        return s.lower()
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _last4(raw: str) -> str:
    """Return last 4 digits of a raw account number for masking.

    If the input is already a hash (no clear last 4), return '****'.
    """
    s = (raw or "").strip()
    if len(s) == 64 and all(c in "0123456789abcdef" for c in s.lower()):
        return "****"
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) >= 4:
        return digits[-4:]
    return "****"


def _mask_account(bank_name: str, last4: str) -> str:
    return f"{bank_name} ****{last4}"


async def _get_currency(brand_id: str, r: aioredis.Redis) -> str:
    cur = await r.get(_k_wallet_currency(brand_id))
    return (cur or DEFAULT_CURRENCY).upper()


async def _load_bank_account(
    r: aioredis.Redis, bank_account_id: str
) -> dict[str, str] | None:
    raw = await r.hgetall(_k_bank_account(bank_account_id))
    return raw or None


async def _commission_owed_cents(r: aioredis.Redis, brand_id: str) -> int:
    raw = await r.hget(_k_commission_owed(brand_id), "cents")
    return int(raw or 0)


async def _wallet_balance_cents(r: aioredis.Redis, brand_id: str) -> int:
    raw = await r.get(_k_wallet_balance(brand_id))
    return int(raw or 0)


def _serialize_payout(payout: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in payout.items():
        if v is None:
            continue
        out[k] = str(v) if not isinstance(v, str) else v
    return out


def _deserialize_payout(raw: dict[str, str]) -> PayoutResponse | None:
    if not raw:
        return None
    return PayoutResponse(
        payout_id=raw.get("payout_id", ""),
        brand_id=raw.get("brand_id", ""),
        amount_cents=int(raw.get("amount_cents", 0) or 0),
        currency=raw.get("currency", DEFAULT_CURRENCY),
        bank_account_id=raw.get("bank_account_id", ""),
        bank_account_masked=raw.get("bank_account_masked") or None,
        source=raw.get("source", SRC_COMMISSION),
        status=raw.get("status", STATUS_PENDING),
        eta_days=int(raw.get("eta_days", PAYOUT_ETA_DAYS) or PAYOUT_ETA_DAYS),
        requested_at=float(raw.get("requested_at", 0) or 0),
        processed_at=float(raw["processed_at"]) if raw.get("processed_at") else None,
        failed_at=float(raw["failed_at"]) if raw.get("failed_at") else None,
        failure_reason=raw.get("failure_reason") or None,
    )


def _next_run_at(frequency: str, last_run_at: float | None) -> float:
    base = last_run_at if last_run_at else _now()
    return base + FREQ_SECONDS.get(frequency, FREQ_SECONDS[FREQ_WEEKLY])


def _invoice_number(brand_id: str, ts: float) -> str:
    d = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")
    return f"INV-{d}-{brand_id}"


# ── Bank-account endpoints ─────────────────────────────────────────────────

@router.post("/bank-account/add", response_model=BankAccountAddResponse)
async def add_bank_account(
    req: BankAccountAddRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Register a destination bank account for a brand.

    The account number is SHA-256 hashed before storage and only the
    last 4 digits are retained for masking in the UI. New accounts
    require micro-deposit verification before a payout can target them.
    """
    bank_account_id = _new_id("ba_")
    account_hash = _hash_account_number(req.account_number_hash)
    last4 = _last4(req.account_number_hash)
    now = _now()

    # Two random micro-deposit amounts (1–99 cents) for verification.
    md1 = secrets.randbelow(99) + 1
    md2 = secrets.randbelow(99) + 1

    mapping = {
        "bank_account_id": bank_account_id,
        "brand_id": req.brand_id,
        "account_holder": req.account_holder,
        "bank_name": req.bank_name,
        "account_hash": account_hash,
        "last4": last4,
        "routing": req.routing or "",
        "swift": req.swift or "",
        "country": req.country.upper(),
        "currency": req.currency.upper(),
        "verified": "0",
        "micro_deposit_1": str(md1),
        "micro_deposit_2": str(md2),
        "created_at": f"{now:.6f}",
    }
    pipe = r.pipeline(transaction=True)
    pipe.hset(_k_bank_account(bank_account_id), mapping=mapping)
    pipe.sadd(_k_bank_accounts(req.brand_id), bank_account_id)
    await pipe.execute()

    logger.info(
        "bank_account added brand=%s id=%s bank=%s last4=%s",
        req.brand_id,
        bank_account_id,
        req.bank_name,
        last4,
    )
    return BankAccountAddResponse(
        bank_account_id=bank_account_id,
        verification_required=True,
    )


@router.post(
    "/bank-account/{bank_account_id}/verify",
    response_model=BankAccountVerifyResponse,
)
async def verify_bank_account(
    bank_account_id: str,
    req: BankAccountVerifyRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Confirm ownership via two micro-deposit amounts the bank wired.

    Verification is required before any payout can be sent. If the
    amounts mismatch, the account stays unverified; clients can retry.
    """
    ba = await _load_bank_account(r, bank_account_id)
    if not ba:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "bank_account_not_found"},
        )
    if ba.get("brand_id") != req.brand_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "brand_mismatch"},
        )
    if ba.get("verified") == "1":
        return BankAccountVerifyResponse(verified=True)

    if not req.micro_deposit_amounts or len(req.micro_deposit_amounts) != 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "two_micro_deposit_amounts_required"},
        )

    expected = sorted([int(ba.get("micro_deposit_1", 0)), int(ba.get("micro_deposit_2", 0))])
    provided = sorted([int(x) for x in req.micro_deposit_amounts])
    if expected != provided:
        return BankAccountVerifyResponse(verified=False)

    await r.hset(
        _k_bank_account(bank_account_id),
        mapping={"verified": "1", "verified_at": f"{_now():.6f}"},
    )
    logger.info("bank_account verified brand=%s id=%s", req.brand_id, bank_account_id)
    return BankAccountVerifyResponse(verified=True)


# ── Balance endpoint ───────────────────────────────────────────────────────

@router.get("/brand/{brand_id}/balance", response_model=BalanceResponse)
async def brand_balance(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """One-call summary: owed, wallet, in-flight, paid-lifetime."""
    pipe = r.pipeline(transaction=False)
    pipe.hget(_k_commission_owed(brand_id), "cents")
    pipe.get(_k_wallet_balance(brand_id))
    pipe.get(_k_pending_payouts(brand_id))
    pipe.get(_k_paid_lifetime(brand_id))
    pipe.hgetall(_k_payout_schedule(brand_id))
    pipe.get(_k_wallet_currency(brand_id))
    owed, wallet, pending, paid, sched, cur = await pipe.execute()

    next_at: float | None = None
    if sched:
        freq = sched.get("frequency", FREQ_WEEKLY)
        last = float(sched.get("last_run_at") or 0) or None
        next_at = _next_run_at(freq, last)

    return BalanceResponse(
        brand_id=brand_id,
        currency=(cur or DEFAULT_CURRENCY).upper(),
        commission_owed_cents=int(owed or 0),
        wallet_balance_cents=int(wallet or 0),
        pending_payouts_cents=int(pending or 0),
        paid_lifetime_cents=int(paid or 0),
        next_payout_eligible_at=next_at,
    )


# ── Payout request (atomic debit) ──────────────────────────────────────────

async def _create_payout_locked(
    r: aioredis.Redis,
    *,
    brand_id: str,
    amount_cents: int,
    bank_account_id: str,
    source: str,
    bank_masked: str | None,
    currency: str,
) -> PayoutResponse:
    """Atomically debit source-of-funds and create a pending payout.

    Mirrors `wallet.charge`: WATCH on the funds key, optimistic retry
    loop, single MULTI to debit + bump pending + persist record. The
    moment this returns, the funds are reserved.
    """
    if source == SRC_COMMISSION:
        fund_key = _k_commission_owed(brand_id)
        fund_is_hash = True
        fund_field = "cents"
    elif source == SRC_WALLET_REFUND:
        fund_key = _k_wallet_balance(brand_id)
        fund_is_hash = False
        fund_field = None
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_source", "source": source},
        )

    pending_key = _k_pending_payouts(brand_id)
    payouts_zset = _k_brand_payouts(brand_id)

    for _ in range(MAX_WATCH_RETRIES):
        try:
            async with r.pipeline(transaction=True) as pipe:
                await pipe.watch(fund_key, pending_key)
                if fund_is_hash:
                    current_raw = await pipe.hget(fund_key, fund_field)
                else:
                    current_raw = await pipe.get(fund_key)
                current = int(current_raw or 0)

                if current < amount_cents:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_402_PAYMENT_REQUIRED,
                        detail={
                            "error": "insufficient_funds",
                            "source": source,
                            "available_cents": current,
                            "requested_cents": amount_cents,
                        },
                    )

                payout_id = _new_id("po_")
                now = _now()
                record = {
                    "payout_id": payout_id,
                    "brand_id": brand_id,
                    "amount_cents": amount_cents,
                    "currency": currency,
                    "bank_account_id": bank_account_id,
                    "bank_account_masked": bank_masked or "",
                    "source": source,
                    "status": STATUS_PENDING,
                    "eta_days": PAYOUT_ETA_DAYS,
                    "requested_at": f"{now:.6f}",
                }

                pipe.multi()
                if fund_is_hash:
                    pipe.hincrby(fund_key, fund_field, -amount_cents)
                else:
                    pipe.decrby(fund_key, amount_cents)
                pipe.incrby(pending_key, amount_cents)
                pipe.hset(_k_payout(payout_id), mapping=_serialize_payout(record))
                pipe.zadd(payouts_zset, {payout_id: now})
                await pipe.execute()

                logger.info(
                    "payout requested brand=%s payout_id=%s amount=%s source=%s",
                    brand_id,
                    payout_id,
                    amount_cents,
                    source,
                )
                return PayoutResponse(
                    payout_id=payout_id,
                    brand_id=brand_id,
                    amount_cents=amount_cents,
                    currency=currency,
                    bank_account_id=bank_account_id,
                    bank_account_masked=bank_masked,
                    source=source,
                    status=STATUS_PENDING,
                    eta_days=PAYOUT_ETA_DAYS,
                    requested_at=now,
                )
        except aioredis.WatchError:
            continue

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "contention", "retries": MAX_WATCH_RETRIES},
    )


@router.post("/request", response_model=PayoutResponse)
async def request_payout(
    req: PayoutRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Reserve `amount_cents` from commission/wallet into a pending payout.

    The bank account must belong to this brand and be verified. The
    debit is atomic — partial states are not possible.
    """
    if req.source not in VALID_SOURCES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_source"},
        )

    ba = await _load_bank_account(r, req.bank_account_id)
    if not ba:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "bank_account_not_found"},
        )
    if ba.get("brand_id") != req.brand_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "brand_mismatch"},
        )
    if ba.get("verified") != "1":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "bank_account_unverified"},
        )

    bank_masked = _mask_account(ba.get("bank_name", "bank"), ba.get("last4", "****"))
    currency = ba.get("currency") or await _get_currency(req.brand_id, r)

    return await _create_payout_locked(
        r,
        brand_id=req.brand_id,
        amount_cents=req.amount_cents,
        bank_account_id=req.bank_account_id,
        source=req.source,
        bank_masked=bank_masked,
        currency=currency,
    )


# ── Status machine: process / fail ─────────────────────────────────────────

async def _transition(
    r: aioredis.Redis,
    payout_id: str,
    *,
    to_status: str,
    extra: dict[str, Any] | None = None,
) -> PayoutResponse:
    """Validate + apply a status transition atomically.

    On failure-from-pending|processing: rolls back the reserved funds
    by crediting the source-of-funds key and decrementing pending. On
    success-to-paid: clears pending and bumps paid_lifetime.
    """
    key = _k_payout(payout_id)
    for _ in range(MAX_WATCH_RETRIES):
        try:
            async with r.pipeline(transaction=True) as pipe:
                await pipe.watch(key)
                raw = await pipe.hgetall(key)
                if not raw:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail={"error": "payout_not_found"},
                    )
                cur = raw.get("status", STATUS_PENDING)
                if to_status not in VALID_TRANSITIONS.get(cur, set()):
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "error": "invalid_transition",
                            "from": cur,
                            "to": to_status,
                        },
                    )

                brand_id = raw.get("brand_id", "")
                amount = int(raw.get("amount_cents", 0) or 0)
                source = raw.get("source", SRC_COMMISSION)
                now = _now()

                mapping: dict[str, Any] = {"status": to_status}
                if extra:
                    mapping.update(extra)

                pipe.multi()
                pipe.hset(key, mapping=_serialize_payout(mapping))

                if to_status == STATUS_PAID:
                    pipe.decrby(_k_pending_payouts(brand_id), amount)
                    pipe.incrby(_k_paid_lifetime(brand_id), amount)
                    pipe.hset(key, "processed_at", f"{now:.6f}")
                elif to_status == STATUS_FAILED and cur in (STATUS_PENDING, STATUS_PROCESSING):
                    # Roll back: pending → 0, refund funding source.
                    pipe.decrby(_k_pending_payouts(brand_id), amount)
                    if source == SRC_COMMISSION:
                        pipe.hincrby(_k_commission_owed(brand_id), "cents", amount)
                    else:
                        pipe.incrby(_k_wallet_balance(brand_id), amount)
                    pipe.hset(key, "failed_at", f"{now:.6f}")

                await pipe.execute()
                logger.info(
                    "payout transition payout_id=%s %s → %s amount=%s",
                    payout_id,
                    cur,
                    to_status,
                    amount,
                )
                # Reload final state.
                final = await r.hgetall(key)
                return _deserialize_payout(final)  # type: ignore[return-value]
        except aioredis.WatchError:
            continue

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "contention"},
    )


@router.post("/{payout_id}/process", response_model=PayoutResponse)
async def process_payout(
    payout_id: str,
    body: AdminProcessRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Admin marks a pending payout as paid (after wiring funds).

    In production this is the spot to call out to Stripe Payouts /
    Adyen / Airwallex etc. For now it's a two-step manual flow:
    pending → processing (with provider ref) → paid is collapsed into
    one call if the gateway confirms synchronously.
    """
    _check_admin(body.admin_token)
    extra: dict[str, Any] = {}
    if body.payment_provider_ref:
        extra["payment_provider_ref"] = body.payment_provider_ref

    # Drive pending → processing → paid in one admin call.
    raw = await r.hgetall(_k_payout(payout_id))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "payout_not_found"},
        )
    cur = raw.get("status", STATUS_PENDING)
    if cur == STATUS_PENDING:
        await _transition(r, payout_id, to_status=STATUS_PROCESSING, extra=extra)
    return await _transition(r, payout_id, to_status=STATUS_PAID, extra=extra)


@router.post("/{payout_id}/fail", response_model=PayoutResponse)
async def fail_payout(
    payout_id: str,
    body: AdminFailRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Admin marks a payout as failed and refunds the source-of-funds."""
    _check_admin(body.admin_token)
    return await _transition(
        r,
        payout_id,
        to_status=STATUS_FAILED,
        extra={"failure_reason": body.reason},
    )


# ── Read endpoints ─────────────────────────────────────────────────────────

@router.get("/{payout_id}", response_model=PayoutResponse)
async def get_payout(
    payout_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await r.hgetall(_k_payout(payout_id))
    payout = _deserialize_payout(raw)
    if not payout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "payout_not_found"},
        )
    return payout


@router.get("/brand/{brand_id}/history", response_model=list[PayoutResponse])
async def payout_history(
    brand_id: str,
    from_ts: float | None = Query(default=None, alias="from"),
    to_ts: float | None = Query(default=None, alias="to"),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    r: aioredis.Redis = Depends(get_redis),
):
    """Reverse-chronological payouts for a brand, with optional filters."""
    now = _now()
    start = from_ts if from_ts is not None else 0.0
    end = to_ts if to_ts is not None else now
    ids = await r.zrevrangebyscore(
        _k_brand_payouts(brand_id), end, start, start=0, num=limit
    )
    out: list[PayoutResponse] = []
    for pid in ids:
        raw = await r.hgetall(_k_payout(pid))
        payout = _deserialize_payout(raw)
        if not payout:
            continue
        if status_filter and payout.status != status_filter:
            continue
        out.append(payout)
    return out


# ── Invoicing ──────────────────────────────────────────────────────────────

async def _build_invoice_lines(
    r: aioredis.Redis,
    brand_id: str,
    period_start: float,
    period_end: float,
) -> tuple[list[InvoiceLine], int]:
    """Aggregate cross-brand conversions where this brand was the source.

    Reads `brand:{bid}:attr_outgoing` (written by attribution.py) and
    sums per target-brand. The figures here are *the conversion side*;
    the actual commission accrual already happened in attribution and
    is what ends up in `commission_owed`. We surface both views so an
    invoice line item is auditable end-to-end.
    """
    event_ids = await r.zrangebyscore(
        f"brand:{brand_id}:attr_outgoing",
        period_start,
        period_end,
        start=0,
        num=10_000,
    )
    by_target: dict[str, dict[str, int]] = {}
    for eid in event_ids:
        ev = await r.hgetall(f"attr:{eid}")
        if not ev:
            continue
        if ev.get("stage") != "conversion":
            continue
        target = ev.get("target_brand") or "unknown"
        agg = by_target.setdefault(target, {"orders": 0, "gross": 0, "commission": 0})
        agg["orders"] += 1
        agg["gross"] += int(ev.get("value_cents", 0) or 0)
        try:
            meta = json.loads(ev.get("meta") or "{}")
        except json.JSONDecodeError:
            meta = {}
        # commission split breakdown stored in meta isn't guaranteed — best effort.
        if isinstance(meta, dict) and "source_brand_take_cents" in meta:
            agg["commission"] += int(meta.get("source_brand_take_cents") or 0)
        else:
            # Fallback: 10% commission, source gets 70% of that.
            agg["commission"] += int(round(int(ev.get("value_cents", 0) or 0) * 0.10 * 0.70))

    lines = [
        InvoiceLine(
            counterparty_brand=tgt,
            orders=v["orders"],
            gross_cents=v["gross"],
            commission_cents=v["commission"],
        )
        for tgt, v in sorted(by_target.items(), key=lambda kv: -kv[1]["commission"])
    ]
    total = sum(line.commission_cents for line in lines)
    return lines, total


@router.post("/invoice/generate", response_model=InvoiceResponse)
async def generate_invoice(
    req: InvoiceGenerateRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Produce an invoice covering [period_start, period_end].

    The returned `url` is a stub pointing at an external PDF renderer;
    the structured payload here is the source of truth.
    """
    if req.period_end <= req.period_start:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_period"},
        )

    lines, commission_total = await _build_invoice_lines(
        r, req.brand_id, req.period_start, req.period_end
    )

    # Net payable to merchant — they already net 70% of the 10% commission;
    # what we hand back to them is the sum of source_brand_take. KiX take in
    # this invoice context is the platform retention from gross commission.
    # Using DEFAULT_KIX_TAKE_FRACTION=0.30 mirrors attribution.py defaults.
    gross_commission = int(round(commission_total / 0.70)) if commission_total else 0
    kix_take = max(0, gross_commission - commission_total)
    net_payable = commission_total

    currency = await _get_currency(req.brand_id, r)
    now = _now()
    invoice_id = _new_id("inv_")
    invoice_number = _invoice_number(req.brand_id, now)

    # Best-effort: surface a default bank account mask if one verified.
    bank_masked: str | None = None
    ba_ids = await r.smembers(_k_bank_accounts(req.brand_id))
    for bid in ba_ids:
        ba = await _load_bank_account(r, bid)
        if ba and ba.get("verified") == "1":
            bank_masked = _mask_account(ba.get("bank_name", "bank"), ba.get("last4", "****"))
            break

    mapping = {
        "invoice_id": invoice_id,
        "invoice_number": invoice_number,
        "brand_id": req.brand_id,
        "period_start": f"{req.period_start:.6f}",
        "period_end": f"{req.period_end:.6f}",
        "currency": currency,
        "lines": json.dumps([line.model_dump() for line in lines], separators=(",", ":")),
        "commission_earned_cents": str(gross_commission),
        "kix_take_cents": str(kix_take),
        "net_payable_cents": str(net_payable),
        "bank_account_masked": bank_masked or "",
        "status": "issued",
        "url": INVOICE_URL_TEMPLATE.format(invoice_id=invoice_id),
        "generated_at": f"{now:.6f}",
    }
    pipe = r.pipeline(transaction=True)
    pipe.hset(_k_invoice(invoice_id), mapping=mapping)
    pipe.lpush(_k_brand_invoices(req.brand_id), invoice_id)
    pipe.ltrim(_k_brand_invoices(req.brand_id), 0, 999)
    await pipe.execute()

    logger.info(
        "invoice generated brand=%s id=%s commission=%s net=%s",
        req.brand_id,
        invoice_id,
        gross_commission,
        net_payable,
    )

    return InvoiceResponse(
        invoice_id=invoice_id,
        invoice_number=invoice_number,
        brand_id=req.brand_id,
        period_start=req.period_start,
        period_end=req.period_end,
        currency=currency,
        lines=lines,
        commission_earned_cents=gross_commission,
        kix_take_cents=kix_take,
        net_payable_cents=net_payable,
        bank_account_masked=bank_masked,
        status="issued",
        url=mapping["url"],
        generated_at=now,
    )


@router.get("/invoice/{invoice_id}", response_model=InvoiceResponse)
async def get_invoice(
    invoice_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await r.hgetall(_k_invoice(invoice_id))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "invoice_not_found"},
        )
    try:
        lines_raw = json.loads(raw.get("lines") or "[]")
        lines = [InvoiceLine(**ln) for ln in lines_raw]
    except (json.JSONDecodeError, TypeError, ValueError):
        lines = []
    return InvoiceResponse(
        invoice_id=raw.get("invoice_id", invoice_id),
        invoice_number=raw.get("invoice_number", ""),
        brand_id=raw.get("brand_id", ""),
        period_start=float(raw.get("period_start", 0) or 0),
        period_end=float(raw.get("period_end", 0) or 0),
        currency=raw.get("currency", DEFAULT_CURRENCY),
        lines=lines,
        commission_earned_cents=int(raw.get("commission_earned_cents", 0) or 0),
        kix_take_cents=int(raw.get("kix_take_cents", 0) or 0),
        net_payable_cents=int(raw.get("net_payable_cents", 0) or 0),
        bank_account_masked=raw.get("bank_account_masked") or None,
        status=raw.get("status", "issued"),
        url=raw.get("url", ""),
        generated_at=float(raw.get("generated_at", 0) or 0),
    )


# ── Schedule + cron ────────────────────────────────────────────────────────

@router.post("/schedule/configure", response_model=ScheduleResponse)
async def configure_schedule(
    req: ScheduleConfigureRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Set up auto-payout cadence for a brand.

    `min_payout_cents` is the floor — the cron skips brands whose
    commission_owed is below it.
    """
    if req.frequency not in FREQ_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_frequency"},
        )

    mapping = {
        "brand_id": req.brand_id,
        "frequency": req.frequency,
        "min_payout_cents": str(req.min_payout_cents),
        "auto": "1" if req.auto else "0",
        "bank_account_id": req.bank_account_id or "",
        "updated_at": f"{_now():.6f}",
    }
    await r.hset(_k_payout_schedule(req.brand_id), mapping=mapping)

    sched = await r.hgetall(_k_payout_schedule(req.brand_id))
    last = float(sched.get("last_run_at") or 0) or None
    return ScheduleResponse(
        brand_id=req.brand_id,
        frequency=req.frequency,
        min_payout_cents=req.min_payout_cents,
        auto=req.auto,
        bank_account_id=req.bank_account_id,
        last_run_at=last,
        next_run_at=_next_run_at(req.frequency, last),
    )


@router.post("/cron/run", response_model=CronRunResponse)
async def cron_run(
    body: CronRunRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Scan all brand schedules; create payout requests for eligibles.

    Eligibility = auto=1 AND commission_owed >= min AND now >= next_run_at
    AND a verified bank account exists. Idempotency: a brand whose
    `last_run_at` is in this cycle is skipped.
    """
    _check_admin(body.admin_token)

    scanned = 0
    created: list[str] = []
    skipped = 0
    now = _now()

    # SCAN all schedule keys; redis-py async returns an async iterator.
    async for key in r.scan_iter(match="brand:*:payout_schedule", count=200):
        scanned += 1
        sched = await r.hgetall(key)
        if not sched or sched.get("auto") != "1":
            skipped += 1
            continue
        brand_id = sched.get("brand_id") or key.split(":")[1]
        freq = sched.get("frequency", FREQ_WEEKLY)
        min_payout = int(sched.get("min_payout_cents") or DEFAULT_MIN_PAYOUT_CENTS)
        last_run = float(sched.get("last_run_at") or 0) or None
        if last_run and now < _next_run_at(freq, last_run):
            skipped += 1
            continue

        owed = await _commission_owed_cents(r, brand_id)
        if owed < min_payout:
            skipped += 1
            continue

        # Pick a verified bank account — prefer the configured one.
        configured = sched.get("bank_account_id") or ""
        chosen: dict[str, str] | None = None
        if configured:
            ba = await _load_bank_account(r, configured)
            if ba and ba.get("verified") == "1" and ba.get("brand_id") == brand_id:
                chosen = ba
        if not chosen:
            ba_ids = await r.smembers(_k_bank_accounts(brand_id))
            for bid in ba_ids:
                ba = await _load_bank_account(r, bid)
                if ba and ba.get("verified") == "1":
                    chosen = ba
                    break
        if not chosen:
            skipped += 1
            continue

        if body.dry_run:
            skipped += 1
            continue

        bank_masked = _mask_account(chosen.get("bank_name", "bank"), chosen.get("last4", "****"))
        currency = chosen.get("currency") or await _get_currency(brand_id, r)
        try:
            payout = await _create_payout_locked(
                r,
                brand_id=brand_id,
                amount_cents=owed,
                bank_account_id=chosen.get("bank_account_id", ""),
                source=SRC_COMMISSION,
                bank_masked=bank_masked,
                currency=currency,
            )
            created.append(payout.payout_id)
            await r.hset(key, "last_run_at", f"{now:.6f}")
        except HTTPException as e:
            logger.warning(
                "cron payout skipped brand=%s reason=%s", brand_id, e.detail
            )
            skipped += 1

    return CronRunResponse(
        scanned_brands=scanned,
        payouts_created=len(created),
        skipped=skipped,
        payout_ids=created,
    )


# ── Health ─────────────────────────────────────────────────────────────────

@router.get("/health")
async def payouts_health(r: aioredis.Redis = Depends(get_redis)):
    pong = await r.ping()
    return {
        "ok": bool(pong),
        "default_min_payout_cents": DEFAULT_MIN_PAYOUT_CENTS,
        "eta_days": PAYOUT_ETA_DAYS,
        "frequencies": list(FREQ_SECONDS.keys()),
    }
