"""Wave F scratch-card router — CataBoom-style reveal-N mechanic.

Endpoints::

    POST /api/v1/wavef/scratch/configs
    GET  /api/v1/wavef/scratch/configs/{config_id}
    POST /api/v1/wavef/scratch/cards
    POST /api/v1/wavef/scratch/cards/{card_id}/reveal
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import redis.asyncio as aioredis

from app.deps import get_current_user
from app.redis_client import get_redis
from app.services import wavef_scratch as svc


router = APIRouter()


class CreateConfigRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    win_probability: float = Field(..., gt=0, le=1)
    symbol_pool: list[str] | None = Field(default=None, max_length=32)
    win_payload: dict[str, Any] = Field(default_factory=dict)


class ConfigOut(BaseModel):
    config_id: str
    brand_id: str
    win_probability: float
    symbol_pool: list[str]
    win_payload: dict[str, Any]


class IssueCardRequest(BaseModel):
    config_id: str = Field(..., min_length=1)


class IssueCardResponse(BaseModel):
    card_id: str
    config_id: str
    grid_masked: list[str]
    user_id: str


class RevealResponse(BaseModel):
    card_id: str
    grid: list[str]
    won: bool
    payload: dict[str, Any]


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
            body.win_probability,
            symbol_pool=body.symbol_pool,
            win_payload=body.win_payload,
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


@router.post("/cards", response_model=IssueCardResponse)
async def issue_card(
    body: IssueCardRequest,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> IssueCardResponse:
    try:
        res = await svc.issue_card(r, body.config_id, current_user["sub"])
    except ValueError as exc:
        msg = str(exc)
        code = 404 if msg == "config not found" else 400
        raise HTTPException(status_code=code, detail=msg) from exc
    return IssueCardResponse(**res)


@router.post("/cards/{card_id}/reveal", response_model=RevealResponse)
async def reveal(
    card_id: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> RevealResponse:
    try:
        res = await svc.reveal_card(r, card_id, current_user["sub"])
    except ValueError as exc:
        msg = str(exc)
        if msg == "card not found":
            raise HTTPException(status_code=404, detail=msg) from exc
        if msg == "not your card":
            raise HTTPException(status_code=403, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    return RevealResponse(**res)
