"""Wave F daily check-in router — Bunchball-style daily-active driver.

Endpoints:
    POST /api/v1/wavef/daily-checkin            check in for today
    GET  /api/v1/wavef/daily-checkin/status     read state without check-in

Body for POST: {brand_id}.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, Field

import redis.asyncio as aioredis

from app.deps import get_current_user
from app.redis_client import get_redis
from app.services import wavef_daily_checkin as svc


router = APIRouter()


class CheckInRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)


class CheckInResponse(BaseModel):
    checked_in_today: bool
    day: str
    total_checkins: int
    reward_eligible: bool


class StatusResponse(BaseModel):
    checked_in_today: bool
    day: str
    total_checkins: int
    last_check_in: str | None = None


@router.post("/", response_model=CheckInResponse)
async def post_checkin(
    body: CheckInRequest,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> CheckInResponse:
    user_id = current_user["sub"]
    res = await svc.check_in(r, body.brand_id, user_id)
    return CheckInResponse(**res)


@router.get("/status", response_model=StatusResponse)
async def get_status(
    brand_id: str = Query(..., min_length=1),
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> StatusResponse:
    user_id = current_user["sub"]
    res = await svc.status(r, brand_id, user_id)
    return StatusResponse(**res)
