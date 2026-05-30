"""Wave F spin-the-wheel router — CataBoom-style prize wheel.

Endpoints::

    POST /api/v1/wavef/spin/configs
    GET  /api/v1/wavef/spin/configs/{config_id}
    POST /api/v1/wavef/spin/configs/{config_id}/spin
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import redis.asyncio as aioredis

from app.deps import get_current_user
from app.redis_client import get_redis
from app.services import wavef_spin as svc


router = APIRouter()


class SliceIn(BaseModel):
    label: str = Field(..., min_length=1, max_length=64)
    weight: float = Field(..., ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class SliceOut(SliceIn):
    id: str


class CreateConfigRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    slices: list[SliceIn] = Field(..., min_length=2, max_length=16)
    daily_limit: int = Field(default=1, ge=1, le=100)


class ConfigOut(BaseModel):
    config_id: str
    brand_id: str
    daily_limit: int
    slices: list[SliceOut]


class SpinResponse(BaseModel):
    config_id: str
    slice_id: str
    label: str
    payload: dict[str, Any]
    spins_used_today: int
    daily_limit: int


@router.post("/configs", response_model=ConfigOut)
async def create_config(
    body: CreateConfigRequest,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> ConfigOut:
    try:
        res = await svc.create_config(
            r,
            body.brand_id,
            [s.model_dump() for s in body.slices],
            daily_limit=body.daily_limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ConfigOut(**res)


@router.get("/configs/{config_id}", response_model=ConfigOut)
async def read_config(
    config_id: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> ConfigOut:
    res = await svc.get_config(r, config_id)
    if res is None:
        raise HTTPException(status_code=404, detail="config not found")
    return ConfigOut(**res)


@router.post("/configs/{config_id}/spin", response_model=SpinResponse)
async def spin(
    config_id: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> SpinResponse:
    try:
        res = await svc.spin(r, config_id, current_user["sub"])
    except ValueError as exc:
        msg = str(exc)
        if msg == "daily_limit_exceeded":
            raise HTTPException(status_code=429, detail=msg) from exc
        if msg == "config not found":
            raise HTTPException(status_code=404, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    return SpinResponse(**res)
