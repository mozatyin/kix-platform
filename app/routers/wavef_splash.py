"""Wave F pre-game brand-splash router — Flarie pattern."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

import redis.asyncio as aioredis

from app.deps import get_current_user
from app.redis_client import get_redis
from app.services import wavef_splash as svc


router = APIRouter()


class SplashConfig(BaseModel):
    campaign_id: str
    enabled: bool
    logo_url: str
    tagline: str
    duration_ms: int = Field(..., ge=500, le=10000)
    brand_primary: str
    show_max_per_day: int = Field(..., ge=1, le=50)


class SplashUpsert(BaseModel):
    logo_url: str = Field(..., min_length=1)
    tagline: str = ""
    duration_ms: int = Field(default=3000, ge=500, le=10000)
    brand_primary: str = "#1F6FEB"
    show_max_per_day: int = Field(default=1, ge=1, le=50)
    enabled: bool = True


@router.get("/{campaign_id}")
async def get_splash(
    campaign_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Return splash config; 204 if not enabled or not set."""
    cfg = await svc.get_config(r, campaign_id)
    if cfg is None or not cfg["enabled"]:
        return Response(status_code=204)
    return SplashConfig(**cfg)


@router.put("/{campaign_id}", response_model=SplashConfig)
async def upsert_splash(
    campaign_id: str,
    body: SplashUpsert,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> SplashConfig:
    try:
        cfg = await svc.set_config(
            r,
            campaign_id=campaign_id,
            logo_url=body.logo_url,
            tagline=body.tagline,
            duration_ms=body.duration_ms,
            brand_primary=body.brand_primary,
            show_max_per_day=body.show_max_per_day,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SplashConfig(**cfg)


@router.delete("/{campaign_id}", response_model=SplashConfig)
async def disable_splash(
    campaign_id: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> SplashConfig:
    cfg = await svc.disable(r, campaign_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="splash not configured")
    return SplashConfig(**cfg)
