"""Wave F collect-a-set router — McDonald's Monopoly-style mechanic."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import redis.asyncio as aioredis

from app.deps import get_current_user
from app.redis_client import get_redis
from app.services import wavef_sets as svc


router = APIRouter()


# ── Models ───────────────────────────────────────────────────────────────


class PieceConfig(BaseModel):
    id: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    rarity_weight: float = Field(..., gt=0)
    grand: bool = False


class CreateCampaign(BaseModel):
    brand_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    pieces: list[PieceConfig] = Field(..., min_length=2, max_length=64)
    target: int = Field(..., ge=2)


class CampaignOut(BaseModel):
    campaign_id: str
    brand_id: str
    name: str
    pieces: list[PieceConfig]
    target: int


class DrawOut(BaseModel):
    piece: PieceConfig
    distinct: int
    target: int


class InventoryOut(BaseModel):
    campaign_id: str
    uid: str
    counts: dict[str, int]
    distinct: int
    target: int
    complete: bool
    redeemed: bool


class RedeemOut(BaseModel):
    redeemed: bool
    claimed_at_ms: int


# ── Routes ───────────────────────────────────────────────────────────────


@router.post("/campaigns", response_model=CampaignOut)
async def create_campaign(
    body: CreateCampaign,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> CampaignOut:
    try:
        res = await svc.create_campaign(
            r,
            brand_id=body.brand_id,
            name=body.name,
            pieces=[p.model_dump() for p in body.pieces],
            target=body.target,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return CampaignOut(**res)


@router.get("/campaigns/{cid}", response_model=CampaignOut)
async def read_campaign(
    cid: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> CampaignOut:
    res = await svc.get_campaign(r, cid)
    if res is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return CampaignOut(**res)


@router.post("/campaigns/{cid}/draw", response_model=DrawOut)
async def draw(
    cid: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> DrawOut:
    try:
        res = await svc.draw(r, cid, current_user["sub"])
    except ValueError as exc:
        msg = str(exc)
        code = 404 if "not found" in msg else 400
        raise HTTPException(status_code=code, detail=msg) from exc
    return DrawOut(**res)


@router.get("/campaigns/{cid}/inventory/{uid}", response_model=InventoryOut)
async def inventory(
    cid: str,
    uid: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> InventoryOut:
    res = await svc.inventory(r, cid, uid)
    if res is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return InventoryOut(**res)


@router.post("/campaigns/{cid}/redeem", response_model=RedeemOut)
async def redeem(
    cid: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> RedeemOut:
    try:
        res = await svc.redeem(r, cid, current_user["sub"])
    except ValueError as exc:
        msg = str(exc)
        code = 404 if "not found" in msg else 400
        raise HTTPException(status_code=code, detail=msg) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedeemOut(**res)
