"""Wave F refer-friend router — BRAME-style both-win viral loop.

Endpoints::

    POST /api/v1/wavef/referral/invite
    POST /api/v1/wavef/referral/accept
    POST /api/v1/wavef/referral/complete       (system / referee callback)
    GET  /api/v1/wavef/referral/{user_id}/stats
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import redis.asyncio as aioredis

from app.deps import get_current_user
from app.redis_client import get_redis
from app.services import wavef_referral as svc


router = APIRouter()


class InviteRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)


class InviteResponse(BaseModel):
    invite_token: str
    share_url: str


class AcceptRequest(BaseModel):
    invite_token: str = Field(..., min_length=1)


class AcceptResponse(BaseModel):
    accepted: bool
    already_pending: bool
    token: str


class CompleteRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    referee_user_id: str | None = None


class CompleteResponse(BaseModel):
    vouchered: bool
    reason: str | None = None
    inviter_user_id: str | None = None
    referee_user_id: str | None = None
    brand_id: str | None = None
    token: str | None = None


class StatsResponse(BaseModel):
    user_id: str
    invited: int
    accepted: int
    completed: int
    earned_voucher_count: int


@router.post("/invite", response_model=InviteResponse)
async def invite(
    body: InviteRequest,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> InviteResponse:
    try:
        res = await svc.create_invite(r, current_user["sub"], body.brand_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return InviteResponse(**res)


@router.post("/accept", response_model=AcceptResponse)
async def accept(
    body: AcceptRequest,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> AcceptResponse:
    try:
        res = await svc.accept_invite(
            r, body.invite_token, current_user["sub"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return AcceptResponse(**res)


@router.post("/complete", response_model=CompleteResponse)
async def complete(
    body: CompleteRequest,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> CompleteResponse:
    referee_uid = body.referee_user_id or current_user["sub"]
    try:
        res = await svc.on_referee_complete(r, body.brand_id, referee_uid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return CompleteResponse(**res)


@router.get("/{user_id}/stats", response_model=StatsResponse)
async def stats(
    user_id: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> StatsResponse:
    res = await svc.stats(r, user_id)
    return StatsResponse(**res)
