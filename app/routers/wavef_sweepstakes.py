"""Wave F sweepstakes router — Merkle/HelloWorld-style draw infrastructure.

Endpoints (all authenticated):
    POST /api/v1/wavef/sweepstakes/{campaign_id}/enter
    GET  /api/v1/wavef/sweepstakes/{campaign_id}/count
    POST /api/v1/wavef/sweepstakes/{campaign_id}/draw     (admin-gated)
    GET  /api/v1/wavef/sweepstakes/{campaign_id}/winners

NEW file — no existing module touched.
"""

from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

import redis.asyncio as aioredis

from app.deps import get_current_user
from app.redis_client import get_redis
from app.services import wavef_sweepstakes as svc


router = APIRouter()


class EnterRequest(BaseModel):
    method: Literal["voucher", "amoe", "purchase", "social"] = "voucher"


class EnterResponse(BaseModel):
    entry_id: str
    ts_ms: int
    method: str
    total_entries: int


class CountResponse(BaseModel):
    campaign_id: str
    total_entries: int


class DrawRequest(BaseModel):
    n_winners: int = Field(1, ge=1, le=1000)
    seed: int | None = None


class WinnerItem(BaseModel):
    entry_id: str
    user_id: str | None = None
    drawn_at_ms: int
    method: str | None = None


class DrawResponse(BaseModel):
    campaign_id: str
    winners: list[WinnerItem]


class WinnersResponse(BaseModel):
    campaign_id: str
    winners: list[WinnerItem]


def _check_admin(token: str | None) -> None:
    expected = os.environ.get("KIX_ADMIN_TOKEN", "admin-dev-token")
    if token != expected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin token required")


@router.post("/{campaign_id}/enter", response_model=EnterResponse)
async def enter_sweepstakes(
    campaign_id: str,
    body: EnterRequest = Body(default=EnterRequest()),
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> EnterResponse:
    user_id = current_user["sub"]
    res = await svc.enter(r, campaign_id, user_id, method=body.method)
    return EnterResponse(**res)


@router.get("/{campaign_id}/count", response_model=CountResponse)
async def count_entries(
    campaign_id: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> CountResponse:
    n = await svc.count(r, campaign_id)
    return CountResponse(campaign_id=campaign_id, total_entries=n)


@router.post("/{campaign_id}/draw", response_model=DrawResponse)
async def draw_winners(
    campaign_id: str,
    body: DrawRequest = Body(default=DrawRequest()),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    r: aioredis.Redis = Depends(get_redis),
) -> DrawResponse:
    _check_admin(x_admin_token)
    winners = await svc.draw(r, campaign_id, n_winners=body.n_winners, seed=body.seed)
    return DrawResponse(
        campaign_id=campaign_id,
        winners=[WinnerItem(**w) for w in winners],
    )


@router.get("/{campaign_id}/winners", response_model=WinnersResponse)
async def list_winners(
    campaign_id: str,
    limit: int = Query(100, ge=1, le=1000),
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> WinnersResponse:
    winners = await svc.winners(r, campaign_id, limit=limit)
    return WinnersResponse(
        campaign_id=campaign_id,
        winners=[WinnerItem(**w) for w in winners],
    )
