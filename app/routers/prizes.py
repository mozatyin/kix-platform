"""Prize fulfillment HTTP surface — instant-win, sweepstakes, claim flow.

Endpoints
---------
User / brand:
  POST /api/v1/prizes/create                  — brand creates a prize pool
  GET  /api/v1/prizes/{prize_id}              — read one prize
  POST /api/v1/prizes/instant-win-roll        — user rolls for instant-win
  POST /api/v1/prizes/{prize_id}/enter        — user enters a sweepstakes
  GET  /api/v1/prizes/winners/{user_id}       — list a user's wins
  GET  /api/v1/prizes/winners/by-id/{winner_id}
                                              — fetch one winner record
  POST /api/v1/prizes/winners/{winner_id}/claim
                                              — initiate fulfillment
  POST /api/v1/prizes/winners/{winner_id}/verify-contact
                                              — verify email/phone
  POST /api/v1/prizes/winners/{winner_id}/legal-ack
                                              — record T&C acknowledgment
  POST /api/v1/prizes/winners/{winner_id}/close
                                              — mark shipped / delivered

Admin:
  POST /api/v1/admin/prizes/{prize_id}/draw   — sweepstakes draw
  GET  /api/v1/admin/prizes/winners/queue     — fulfillment queue
  GET  /api/v1/admin/prizes/winners/review    — anti-fraud review queue
  POST /api/v1/admin/prizes/winners/{winner_id}/review
                                              — approve / reject review
  POST /api/v1/admin/prizes/expire-unclaimed  — sweep expired

This router only orchestrates HTTP request/response — all business logic
lives in ``app/services/prize_fulfillment.py`` so the same surface is
unit-testable without an ASGI client. Admin endpoints require
``X-Admin-Token`` (or ``admin_token`` query) gated through
``app.security.check_admin_token`` — same pattern as audit_log /
disputes.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

import redis.asyncio as aioredis

from app.redis_client import get_redis
from app.security import check_admin_token
from app.services import prize_fulfillment as svc

logger = logging.getLogger(__name__)

router = APIRouter()
admin_router = APIRouter()


# ── Auth helper ──────────────────────────────────────────────────────────


def _require_admin(token: str | None) -> None:
    if not check_admin_token(token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "admin_token_invalid"},
        )


def _service_error(exc: svc.PrizeError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"error": exc.code, "message": exc.message},
    )


# ── Pydantic models ──────────────────────────────────────────────────────


class PrizeSpec(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10_000)
    prize_type: str = Field(..., min_length=1, max_length=32)
    value_cents: int | None = Field(default=None, ge=0)
    inventory_count: int | None = Field(default=None, ge=0)
    win_probability_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    instant_win: bool = False
    sweepstakes_draw_at: int | None = Field(default=None, ge=0)
    fulfillment_method: str | None = Field(default=None, max_length=32)
    legal_disclaimer: str | None = Field(default=None, max_length=10_000)
    jurisdiction: str | None = Field(default=None, max_length=8)
    campaign_id: str | None = Field(default=None, max_length=64)


class CreatePoolRequest(BaseModel):
    brand_id: str = Field(..., min_length=1, max_length=64)
    prizes: list[PrizeSpec] = Field(..., min_length=1, max_length=100)


class InstantWinRollRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    campaign_id: str | None = Field(default=None, max_length=64)
    brand_id: str | None = Field(default=None, max_length=64)
    user_age: int | None = Field(default=None, ge=0, le=150)
    jurisdiction: str | None = Field(default=None, max_length=8)


class VerifyContactRequest(BaseModel):
    contact_method: str = Field(..., min_length=1, max_length=32)
    contact_value: str = Field(..., min_length=1, max_length=500)
    verification_token: str | None = Field(default=None, max_length=500)


class ClaimRequest(BaseModel):
    locale: str = Field(default="en-SG", max_length=16)


class CloseRequest(BaseModel):
    evidence: dict[str, Any] = Field(default_factory=dict)
    new_status: str = Field(default="delivered", max_length=32)


class EnterSweepstakesRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    user_age: int | None = Field(default=None, ge=0, le=150)
    jurisdiction: str | None = Field(default=None, max_length=8)


class DrawRequest(BaseModel):
    n_winners: int | None = Field(default=None, ge=1, le=1000)


class ResolveReviewRequest(BaseModel):
    decision: str = Field(..., min_length=1, max_length=16)
    note: str = Field(default="", max_length=500)


class ExpireRequest(BaseModel):
    days: int = Field(default=svc.DEFAULT_CLAIM_DEADLINE_DAYS, ge=1, le=3650)
    return_to_pool: bool = True


# ══════════════════════════════════════════════════════════════════════════
# Public / brand endpoints
# ══════════════════════════════════════════════════════════════════════════


@router.post("/create", status_code=status.HTTP_201_CREATED)
async def create_prize_pool(
    body: CreatePoolRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    try:
        out = await svc.create_prize_pool(
            r,
            brand_id=body.brand_id,
            prizes=[p.model_dump() for p in body.prizes],
        )
    except svc.PrizeError as exc:
        raise _service_error(exc)
    return {"ok": True, "brand_id": body.brand_id, "prizes": out}


@router.get("/{prize_id}")
async def get_prize(
    prize_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    prize = await svc.get_prize(r, prize_id)
    if prize is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "prize_not_found", "prize_id": prize_id},
        )
    return prize


@router.post("/instant-win-roll")
async def instant_win_roll(
    body: InstantWinRollRequest,
    request: Request,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    user_ip = ""
    try:
        user_ip = request.client.host if request.client else ""
    except Exception:
        user_ip = ""
    try:
        result = await svc.try_instant_win(
            r,
            user_id=body.user_id,
            campaign_id=body.campaign_id,
            brand_id=body.brand_id,
            user_age=body.user_age,
            user_ip=user_ip,
            jurisdiction=body.jurisdiction,
        )
    except svc.PrizeError as exc:
        raise _service_error(exc)
    return {
        "won": result.won,
        "prize_id": result.prize_id,
        "winner_id": result.winner_id,
        "reason": result.reason,
        "rolled": result.rolled,
        "review_required": result.review_required,
    }


@router.post("/{prize_id}/enter")
async def enter_sweepstakes(
    prize_id: str,
    body: EnterSweepstakesRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    try:
        return await svc.enter_sweepstakes(
            r,
            prize_id=prize_id,
            user_id=body.user_id,
            jurisdiction=body.jurisdiction,
            user_age=body.user_age,
        )
    except svc.PrizeError as exc:
        raise _service_error(exc)


@router.get("/winners/{user_id}")
async def get_user_winners(
    user_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    winners = await svc.list_user_winners(r, user_id, limit=limit)
    return {"user_id": user_id, "winners": winners, "count": len(winners)}


@router.get("/winners/by-id/{winner_id}")
async def get_winner_by_id(
    winner_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    winner = await svc.get_winner(r, winner_id)
    if winner is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "winner_not_found", "winner_id": winner_id},
        )
    return winner


@router.post("/winners/{winner_id}/claim")
async def claim_winner(
    winner_id: str,
    body: ClaimRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    try:
        return await svc.initiate_fulfillment(
            r, winner_id=winner_id, locale=body.locale
        )
    except svc.PrizeError as exc:
        raise _service_error(exc)


@router.post("/winners/{winner_id}/verify-contact")
async def verify_contact(
    winner_id: str,
    body: VerifyContactRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    try:
        return await svc.verify_contact_info(
            r,
            winner_id=winner_id,
            contact_method=body.contact_method,
            contact_value=body.contact_value,
            verification_token=body.verification_token,
        )
    except svc.PrizeError as exc:
        raise _service_error(exc)


@router.post("/winners/{winner_id}/legal-ack")
async def legal_ack(
    winner_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    try:
        return await svc.record_legal_acknowledgment(r, winner_id=winner_id)
    except svc.PrizeError as exc:
        raise _service_error(exc)


@router.post("/winners/{winner_id}/close")
async def close_winner(
    winner_id: str,
    body: CloseRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    try:
        return await svc.mark_claimed(
            r,
            winner_id=winner_id,
            evidence=body.evidence,
            new_status=body.new_status,
        )
    except svc.PrizeError as exc:
        raise _service_error(exc)


# ══════════════════════════════════════════════════════════════════════════
# Admin endpoints
# ══════════════════════════════════════════════════════════════════════════


@admin_router.post("/{prize_id}/draw")
async def draw_sweepstakes(
    prize_id: str,
    body: DrawRequest = Body(default_factory=DrawRequest),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    admin_token: str | None = Query(default=None),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _require_admin(x_admin_token or admin_token)
    try:
        winner_ids = await svc.draw_sweepstakes(
            r, prize_id=prize_id, n_winners=body.n_winners
        )
    except svc.PrizeError as exc:
        raise _service_error(exc)
    return {
        "ok": True,
        "prize_id": prize_id,
        "winner_ids": winner_ids,
        "count": len(winner_ids),
    }


@admin_router.get("/winners/queue")
async def get_fulfillment_queue(
    limit: int = Query(default=100, ge=1, le=1000),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    admin_token: str | None = Query(default=None),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _require_admin(x_admin_token or admin_token)
    winners = await svc.list_fulfillment_queue(r, limit=limit)
    return {"queue": winners, "count": len(winners)}


@admin_router.get("/winners/review")
async def get_review_queue(
    limit: int = Query(default=100, ge=1, le=1000),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    admin_token: str | None = Query(default=None),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _require_admin(x_admin_token or admin_token)
    winners = await svc.list_review_queue(r, limit=limit)
    return {"queue": winners, "count": len(winners)}


@admin_router.post("/winners/{winner_id}/review")
async def resolve_review(
    winner_id: str,
    body: ResolveReviewRequest,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    admin_token: str | None = Query(default=None),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _require_admin(x_admin_token or admin_token)
    try:
        return await svc.resolve_review(
            r, winner_id=winner_id, decision=body.decision, note=body.note
        )
    except svc.PrizeError as exc:
        raise _service_error(exc)


@admin_router.post("/expire-unclaimed")
async def expire_unclaimed(
    body: ExpireRequest = Body(default_factory=ExpireRequest),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    admin_token: str | None = Query(default=None),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _require_admin(x_admin_token or admin_token)
    return await svc.expire_unclaimed(
        r, days=body.days, return_to_pool=body.return_to_pool
    )
