"""Wave F flash-promo router — Gamify/BRAME "happy hour" pattern.

Endpoints:
    POST /api/v1/wavef/flash/windows
    GET  /api/v1/wavef/flash/active            query: brand_id
    GET  /api/v1/wavef/flash/{window_id}
    POST /api/v1/wavef/flash/{window_id}/claim

NEW file — no existing module touched.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

import redis.asyncio as aioredis

from app.deps import get_current_user
from app.redis_client import get_redis
from app.services import wavef_flash_promo as svc


router = APIRouter()


class CreateWindowRequest(BaseModel):
    brand_id: str
    campaign_id: str
    starts_at: int = Field(..., gt=0, description="unix seconds")
    duration_s: int = Field(..., gt=0, le=7 * 86_400)
    bonus_payload: Optional[dict] = None


class WindowResponse(BaseModel):
    window_id: str
    brand_id: str
    campaign_id: str
    starts_at: int
    ends_at: int
    bonus_payload: dict


class ActiveResponse(BaseModel):
    brand_id: str
    items: list[WindowResponse]
    count: int


class ClaimResponse(BaseModel):
    window_id: str
    user_id: str
    bonus_payload: dict
    claimed_at: int


@router.post("/windows", response_model=WindowResponse)
async def create_window_endpoint(
    body: CreateWindowRequest,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> WindowResponse:
    try:
        res = await svc.create_window(
            r,
            brand_id=body.brand_id,
            campaign_id=body.campaign_id,
            starts_at=body.starts_at,
            duration_s=body.duration_s,
            bonus_payload=body.bonus_payload,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return WindowResponse(**res)


@router.get("/active", response_model=ActiveResponse)
async def active_endpoint(
    brand_id: str = Query(...),
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> ActiveResponse:
    items = await svc.active_windows(r, brand_id)
    return ActiveResponse(
        brand_id=brand_id,
        items=[WindowResponse(**w) for w in items],
        count=len(items),
    )


@router.get("/{window_id}", response_model=WindowResponse)
async def get_window_endpoint(
    window_id: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> WindowResponse:
    w = await svc.get_window(r, window_id)
    if not w:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="window not found"
        )
    return WindowResponse(**w)


@router.post("/{window_id}/claim", response_model=ClaimResponse)
async def claim_endpoint(
    window_id: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> ClaimResponse:
    user_id = current_user["sub"]
    try:
        res = await svc.claim(r, window_id, user_id)
    except svc.WindowNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="window not found"
        )
    except svc.OutOfWindow:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "OUT_OF_WINDOW"},
        )
    except svc.AlreadyClaimed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "ALREADY_CLAIMED"},
        )
    return ClaimResponse(**res)
