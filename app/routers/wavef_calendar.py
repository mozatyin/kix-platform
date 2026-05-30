"""Wave F calendar daily-reveal router — MONOPOLY-style return-visit driver.

Endpoints::

    POST /api/v1/wavef/calendar/campaigns
    GET  /api/v1/wavef/calendar/campaigns/{campaign_id}
    GET  /api/v1/wavef/calendar/campaigns/{campaign_id}/today
    POST /api/v1/wavef/calendar/campaigns/{campaign_id}/claim
    GET  /api/v1/wavef/calendar/campaigns/{campaign_id}/timeline
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import redis.asyncio as aioredis

from app.deps import get_current_user
from app.redis_client import get_redis
from app.services import wavef_calendar as svc


router = APIRouter()


class DayEntry(BaseModel):
    day: int = Field(..., ge=1)
    item_type: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class CreateCampaignRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1, max_length=120)
    start_date: str = Field(..., min_length=10, max_length=10)  # YYYY-MM-DD
    days: list[DayEntry] = Field(..., min_length=1, max_length=366)


class CampaignMeta(BaseModel):
    campaign_id: str
    brand_id: str
    name: str
    start_date: str
    ttl_days: int
    days: list[DayEntry]


class TodayPiece(BaseModel):
    campaign_id: str
    day: int
    date: str
    item_type: str
    payload: dict[str, Any]


class ClaimResponse(BaseModel):
    claimed: bool
    day: int
    item_type: str
    payload: dict[str, Any]


class TimelineDay(BaseModel):
    day: int
    item_type: str
    payload: dict[str, Any]
    claimed: bool


class TimelineResponse(BaseModel):
    campaign_id: str
    brand_id: str
    today_day_index: int
    ttl_days: int
    revealed: list[TimelineDay]


@router.post("/campaigns", response_model=CampaignMeta)
async def create_campaign(
    body: CreateCampaignRequest,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> CampaignMeta:
    try:
        res = await svc.create_campaign(
            r,
            body.brand_id,
            body.name,
            body.start_date,
            [d.model_dump() for d in body.days],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return CampaignMeta(**res)


@router.get("/campaigns/{campaign_id}", response_model=CampaignMeta)
async def read_campaign(
    campaign_id: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> CampaignMeta:
    res = await svc.get_campaign(r, campaign_id)
    if res is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return CampaignMeta(**res)


@router.get("/campaigns/{campaign_id}/today", response_model=TodayPiece)
async def today(
    campaign_id: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> TodayPiece:
    res = await svc.today_piece(r, campaign_id)
    if res is None:
        raise HTTPException(
            status_code=404, detail="no piece available today",
        )
    return TodayPiece(**res)


@router.post("/campaigns/{campaign_id}/claim", response_model=ClaimResponse)
async def claim(
    campaign_id: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> ClaimResponse:
    try:
        res = await svc.claim_today(r, campaign_id, current_user["sub"])
    except ValueError as exc:
        msg = str(exc)
        if msg == "already_claimed":
            raise HTTPException(status_code=409, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    return ClaimResponse(**res)


@router.get(
    "/campaigns/{campaign_id}/timeline", response_model=TimelineResponse,
)
async def timeline(
    campaign_id: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> TimelineResponse:
    res = await svc.timeline(r, campaign_id, current_user["sub"])
    if res is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return TimelineResponse(**res)
