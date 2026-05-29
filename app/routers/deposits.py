"""Deposit Lifecycle router.

Tracks bike-rental holds (老田 ¥99), power-bank deposits, reservation locks,
event registration deposits and similar refundable holds.

Backed by :mod:`user_wallet` freeze/release primitives — a deposit is just
a typed wrapper around a freeze with its own lifecycle FSM:

    placed → released   (action=full_refund)
    placed → released   (action=partial_deduct, partial returned)
    placed → forfeited  (action=forfeit, full amount converts to brand revenue)
    placed → expired    (passive — for accounting visibility only)

Redis schema
------------
    deposit:{deposit_id}                 HASH
    user:{uid}:deposits                  SET of deposit_ids
    brand:{bid}:deposits:active          SET of deposit_ids (currently held)
    brand:{bid}:deposits:all             SET of deposit_ids (lifetime)
"""

from __future__ import annotations

import logging
import time
from typing import Literal
from uuid import uuid4

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.redis_client import get_redis
from app.routers.user_wallet import (
    FreezeRequest,
    ReleaseFreezeRequest,
    freeze as wallet_freeze,
    release_freeze as wallet_release_freeze,
)

logger = logging.getLogger(__name__)

router = APIRouter()


SUPPORTED_PURPOSES = {
    "bike_rental",
    "powerbank",
    "reservation_hold",
    "event_deposit",
    "rental",
    "other",
}


# ── Key helpers ──────────────────────────────────────────────────────────
def _k_deposit(did: str) -> str:
    return f"deposit:{did}"


def _k_user_deposits(uid: str) -> str:
    return f"user:{uid}:deposits"


def _k_brand_active(bid: str) -> str:
    return f"brand:{bid}:deposits:active"


def _k_brand_all(bid: str) -> str:
    return f"brand:{bid}:deposits:all"


def _k_ref_to_did(brand_id: str, ref: str) -> str:
    """Idempotency pointer: brand+reference_id → deposit_id."""
    return f"deposit_idem:{brand_id}:{ref}"


# ── Pydantic models ──────────────────────────────────────────────────────
class PlaceDepositRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    brand_id: str = Field(..., min_length=1, max_length=128)
    amount_cents: int = Field(..., gt=0, le=100_000_000)
    purpose: Literal[
        "bike_rental", "powerbank", "reservation_hold",
        "event_deposit", "rental", "other",
    ]
    reference_id: str = Field(..., min_length=1, max_length=128)
    expires_at: float | None = Field(default=None, ge=0)
    refundable: bool = True
    note: str | None = Field(default=None, max_length=256)


class DepositRecord(BaseModel):
    deposit_id: str
    user_id: str
    brand_id: str
    amount_cents: int
    purpose: str
    reference_id: str
    status: str  # placed / released / partially_refunded / forfeited / expired
    refundable: bool
    placed_at: float
    expires_at: float | None
    released_at: float | None = None
    released_amount_cents: int = 0
    retained_amount_cents: int = 0
    note: str | None = None


class PlaceDepositResponse(BaseModel):
    deposit_id: str
    user_id: str
    brand_id: str
    frozen_amount_cents: int
    purpose: str
    expires_at: float | None
    status: str
    idempotent: bool = False


class ReleaseDepositRequest(BaseModel):
    action: Literal["full_refund", "partial_deduct", "forfeit"]
    deduct_amount_cents: int | None = Field(default=None, ge=0)
    reason: str = Field(..., min_length=1, max_length=500)


class ReleaseDepositResponse(BaseModel):
    deposit_id: str
    action: str
    released_amount_cents: int
    retained_amount_cents: int
    status: str


# ── Internal helpers ─────────────────────────────────────────────────────
def _hydrate(raw: dict) -> DepositRecord:
    return DepositRecord(
        deposit_id=raw["deposit_id"],
        user_id=raw["user_id"],
        brand_id=raw["brand_id"],
        amount_cents=int(raw.get("amount") or 0),
        purpose=raw.get("purpose") or "other",
        reference_id=raw.get("reference_id") or "",
        status=raw.get("status") or "placed",
        refundable=raw.get("refundable", "1") == "1",
        placed_at=float(raw.get("placed_at") or 0.0),
        expires_at=(
            float(raw["expires_at"])
            if raw.get("expires_at") not in (None, "", "None")
            else None
        ),
        released_at=(
            float(raw["released_at"])
            if raw.get("released_at") not in (None, "", "None")
            else None
        ),
        released_amount_cents=int(raw.get("released_amount") or 0),
        retained_amount_cents=int(raw.get("retained_amount") or 0),
        note=raw.get("note") or None,
    )


# ── POST /place ──────────────────────────────────────────────────────────
@router.post("/place", response_model=PlaceDepositResponse)
async def place_deposit(
    body: PlaceDepositRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> PlaceDepositResponse:
    """Place a refundable deposit — freezes funds in the user's wallet.

    Idempotent on (brand_id, reference_id): a replay returns the original
    deposit record without double-freezing.
    """
    idem_key = _k_ref_to_did(body.brand_id, body.reference_id)
    existing_did = await r.get(idem_key)
    if existing_did:
        existing = await r.hgetall(_k_deposit(existing_did))
        if existing:
            return PlaceDepositResponse(
                deposit_id=existing_did,
                user_id=existing.get("user_id") or body.user_id,
                brand_id=existing.get("brand_id") or body.brand_id,
                frozen_amount_cents=int(existing.get("amount") or 0),
                purpose=existing.get("purpose") or body.purpose,
                expires_at=(
                    float(existing["expires_at"])
                    if existing.get("expires_at")
                    not in (None, "", "None")
                    else None
                ),
                status=existing.get("status") or "placed",
                idempotent=True,
            )

    # Freeze via user_wallet. We piggyback on its WATCH/MULTI atomicity for
    # available-funds check + 402 raise.
    freeze_body = FreezeRequest(
        amount_cents=body.amount_cents,
        reason="deposit",
        reference_id=f"deposit:{body.reference_id}",
        note=f"{body.purpose}:{body.note or ''}",
    )
    try:
        await wallet_freeze(body.user_id, freeze_body, r)
    except HTTPException:
        raise

    deposit_id = uuid4().hex
    now = time.time()

    expires_str = "" if body.expires_at is None else str(body.expires_at)
    pipe = r.pipeline()
    pipe.hset(
        _k_deposit(deposit_id),
        mapping={
            "deposit_id": deposit_id,
            "user_id": body.user_id,
            "brand_id": body.brand_id,
            "amount": body.amount_cents,
            "purpose": body.purpose,
            "reference_id": body.reference_id,
            "freeze_ref": f"deposit:{body.reference_id}",
            "refundable": "1" if body.refundable else "0",
            "status": "placed",
            "placed_at": now,
            "expires_at": expires_str,
            "released_amount": 0,
            "retained_amount": 0,
            "note": body.note or "",
        },
    )
    pipe.sadd(_k_user_deposits(body.user_id), deposit_id)
    pipe.sadd(_k_brand_active(body.brand_id), deposit_id)
    pipe.sadd(_k_brand_all(body.brand_id), deposit_id)
    pipe.set(idem_key, deposit_id, ex=30 * 24 * 3600)  # 30d
    await pipe.execute()

    logger.info(
        "deposit_placed did=%s uid=%s brand=%s amount=%s purpose=%s",
        deposit_id, body.user_id, body.brand_id,
        body.amount_cents, body.purpose,
    )
    return PlaceDepositResponse(
        deposit_id=deposit_id,
        user_id=body.user_id,
        brand_id=body.brand_id,
        frozen_amount_cents=body.amount_cents,
        purpose=body.purpose,
        expires_at=body.expires_at,
        status="placed",
    )


# ── GET /{deposit_id} ────────────────────────────────────────────────────
@router.get("/{deposit_id}", response_model=DepositRecord)
async def get_deposit(
    deposit_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> DepositRecord:
    raw = await r.hgetall(_k_deposit(deposit_id))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "deposit_not_found", "deposit_id": deposit_id},
        )
    return _hydrate(raw)


# ── POST /{deposit_id}/release ───────────────────────────────────────────
@router.post(
    "/{deposit_id}/release", response_model=ReleaseDepositResponse
)
async def release_deposit(
    deposit_id: str,
    body: ReleaseDepositRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> ReleaseDepositResponse:
    """Resolve the deposit lifecycle.

    * ``full_refund``       → unfreeze 100% back to user wallet
    * ``partial_deduct``    → unfreeze (amount - deduct), forfeit `deduct`
    * ``forfeit``           → entire amount converts to brand revenue
    """
    dkey = _k_deposit(deposit_id)
    raw = await r.hgetall(dkey)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "deposit_not_found", "deposit_id": deposit_id},
        )

    cur_status = raw.get("status") or "placed"
    if cur_status != "placed":
        # Idempotent: already settled → return last record.
        return ReleaseDepositResponse(
            deposit_id=deposit_id,
            action=cur_status,
            released_amount_cents=int(raw.get("released_amount") or 0),
            retained_amount_cents=int(raw.get("retained_amount") or 0),
            status=cur_status,
        )

    user_id = raw["user_id"]
    brand_id = raw["brand_id"]
    amount = int(raw.get("amount") or 0)
    freeze_ref = raw.get("freeze_ref") or f"deposit:{raw.get('reference_id')}"

    if amount <= 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "invalid_deposit_amount"},
        )

    refundable = raw.get("refundable", "1") == "1"

    released_amount = 0
    retained_amount = 0
    final_status = "placed"

    if body.action == "full_refund":
        if not refundable:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "deposit_not_refundable"},
            )
        await wallet_release_freeze(
            user_id,
            ReleaseFreezeRequest(
                reference_id=freeze_ref,
                action="release_to_available",
                reason=body.reason,
            ),
            r,
        )
        released_amount = amount
        retained_amount = 0
        final_status = "released"

    elif body.action == "partial_deduct":
        deduct = body.deduct_amount_cents or 0
        if deduct <= 0 or deduct > amount:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "invalid_deduct_amount",
                    "amount_cents": amount,
                    "deduct_amount_cents": deduct,
                },
            )
        # Partial = un-freeze the full amount and then charge back `deduct`.
        # Implementation: convert_to_charge on `deduct` portion, refund rest.
        # The user_wallet primitive operates on the full freeze record, so
        # we split into two synthetic ops: (1) release full to available,
        # (2) charge `deduct` against the user.
        await wallet_release_freeze(
            user_id,
            ReleaseFreezeRequest(
                reference_id=freeze_ref,
                action="release_to_available",
                reason=body.reason,
            ),
            r,
        )
        if deduct > 0:
            # Reuse user_wallet charge primitive.
            from app.routers.user_wallet import (
                ChargeRequest as _Charge,
                charge as _charge_fn,
            )
            await _charge_fn(
                user_id,
                _Charge(
                    amount_cents=deduct,
                    reason="deposit",
                    brand_id=brand_id,
                    reference_id=f"deposit_deduct:{deposit_id}",
                    note=body.reason,
                ),
                r,
            )
        released_amount = amount - deduct
        retained_amount = deduct
        final_status = "partially_refunded"

    else:  # forfeit
        await wallet_release_freeze(
            user_id,
            ReleaseFreezeRequest(
                reference_id=freeze_ref,
                action="convert_to_charge",
                brand_id=brand_id,
                reason=body.reason,
            ),
            r,
        )
        released_amount = 0
        retained_amount = amount
        final_status = "forfeited"

    now = time.time()
    pipe = r.pipeline()
    pipe.hset(
        dkey,
        mapping={
            "status": final_status,
            "released_at": now,
            "released_amount": released_amount,
            "retained_amount": retained_amount,
            "release_action": body.action,
            "release_reason": body.reason,
        },
    )
    pipe.srem(_k_brand_active(brand_id), deposit_id)
    await pipe.execute()

    logger.info(
        "deposit_released did=%s action=%s released=%s retained=%s",
        deposit_id, body.action, released_amount, retained_amount,
    )
    return ReleaseDepositResponse(
        deposit_id=deposit_id,
        action=body.action,
        released_amount_cents=released_amount,
        retained_amount_cents=retained_amount,
        status=final_status,
    )


# ── GET /user/{user_id} ──────────────────────────────────────────────────
@router.get("/user/{user_id}", response_model=list[DepositRecord])
async def list_user_deposits(
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> list[DepositRecord]:
    ids = await r.smembers(_k_user_deposits(user_id))
    out: list[DepositRecord] = []
    for did in ids:
        raw = await r.hgetall(_k_deposit(did))
        if raw:
            out.append(_hydrate(raw))
    out.sort(key=lambda d: d.placed_at, reverse=True)
    return out


# ── GET /brand/{brand_id}/active ─────────────────────────────────────────
@router.get(
    "/brand/{brand_id}/active", response_model=list[DepositRecord]
)
async def list_brand_active_deposits(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> list[DepositRecord]:
    """Currently-held deposits for a brand (for accounting / liability)."""
    ids = await r.smembers(_k_brand_active(brand_id))
    out: list[DepositRecord] = []
    for did in ids:
        raw = await r.hgetall(_k_deposit(did))
        if raw and raw.get("status") == "placed":
            out.append(_hydrate(raw))
    out.sort(key=lambda d: d.placed_at, reverse=True)
    return out
