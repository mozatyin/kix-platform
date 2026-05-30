"""Wave F geofenced voucher router — Gamify-style redeem-near-store.

Endpoints (all authenticated):
    POST /api/v1/wavef/geo-voucher/{voucher_id}/fence    set radius + anchor
    GET  /api/v1/wavef/geo-voucher/{voucher_id}/fence    fetch fence
    POST /api/v1/wavef/geo-voucher/{voucher_id}/check    validate caller location
    GET  /api/v1/wavef/geo-voucher/{voucher_id}/audit    recent fraud trail

The /check endpoint is the authoritative gate the existing redeem flow
can additively call; it returns 403 GEO_DENIED when the device is
outside the fence.

NEW file — no existing voucher router touched.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

import redis.asyncio as aioredis

from app.deps import get_current_user
from app.redis_client import get_redis
from app.services import wavef_geofenced_voucher as svc


router = APIRouter()


class FenceRequest(BaseModel):
    anchor_lat: float = Field(..., ge=-90.0, le=90.0)
    anchor_lng: float = Field(..., ge=-180.0, le=180.0)
    radius_m: int = Field(..., gt=0, le=50_000)


class FenceResponse(BaseModel):
    voucher_id: str
    anchor_lat: float
    anchor_lng: float
    radius_m: int
    owner_brand_id: Optional[str] = None


class CheckRequest(BaseModel):
    lat: Optional[float] = Field(None, ge=-90.0, le=90.0)
    lng: Optional[float] = Field(None, ge=-180.0, le=180.0)


class CheckResponse(BaseModel):
    voucher_id: str
    allowed: bool
    reason: Optional[str] = None
    distance_m: Optional[float] = None
    radius_m: Optional[int] = None


class AuditItem(BaseModel):
    uid: Optional[str] = None
    lat_t: Optional[float] = None
    lng_t: Optional[float] = None
    dist_m: Optional[float] = None
    allowed: bool
    ts_ms: int


class AuditResponse(BaseModel):
    voucher_id: str
    entries: list[AuditItem]


@router.post("/{voucher_id}/fence", response_model=FenceResponse)
async def set_fence(
    voucher_id: str,
    body: FenceRequest,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> FenceResponse:
    brand_id = current_user.get("brand_id")
    try:
        res = await svc.set_geofence(
            r,
            voucher_id,
            anchor_lat=body.anchor_lat,
            anchor_lng=body.anchor_lng,
            radius_m=body.radius_m,
            brand_id=brand_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return FenceResponse(owner_brand_id=brand_id, **res)


@router.get("/{voucher_id}/fence", response_model=FenceResponse)
async def get_fence(
    voucher_id: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> FenceResponse:
    fence = await svc.get_geofence(r, voucher_id)
    if fence is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no geofence set"
        )
    return FenceResponse(
        voucher_id=fence["voucher_id"],
        anchor_lat=fence["anchor_lat"],
        anchor_lng=fence["anchor_lng"],
        radius_m=fence["radius_m"],
        owner_brand_id=fence.get("owner_brand_id"),
    )


@router.post("/{voucher_id}/check", response_model=CheckResponse)
async def check_location(
    voucher_id: str,
    body: CheckRequest = Body(default=CheckRequest()),
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> CheckResponse:
    user_id = current_user.get("sub")
    res = await svc.check_geo(
        r, voucher_id, body.lat, body.lng, user_id=user_id
    )
    if not res["allowed"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": res["reason"] or "GEO_DENIED",
                "distance_m": res["distance_m"],
                "radius_m": res["radius_m"],
            },
        )
    return CheckResponse(voucher_id=voucher_id, **res)


@router.get("/{voucher_id}/audit", response_model=AuditResponse)
async def audit_endpoint(
    voucher_id: str,
    limit: int = Query(100, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> AuditResponse:
    entries = await svc.audit_log(r, voucher_id, limit=limit)
    return AuditResponse(
        voucher_id=voucher_id,
        entries=[AuditItem(**e) for e in entries],
    )
