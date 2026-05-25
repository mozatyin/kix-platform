"""QR router — internal QR token generation and rotation.

Protected by Nginx deny rules (no JWT auth). Provides endpoints
for generating and force-rotating QR tokens at brand locations.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.redis_client import get_redis
from app.models import BrandConfig, BrandLocation
from app.schemas import QRGenerateRequest, QRGenerateResponse
from app.services.qr import generate_qr_token

logger = logging.getLogger(__name__)

router = APIRouter()


class ForceRotateRequest(BaseModel):
    brand_id: str
    location_id: str


async def _generate_and_store(
    r: aioredis.Redis,
    db: AsyncSession,
    brand_id: str,
    location_id: str,
    duration_minutes: int,
) -> QRGenerateResponse:
    """Shared logic: generate a QR token, store in Redis, return response."""

    # Look up brand_slug for the QR URL
    result = await db.execute(
        select(BrandConfig.brand_slug).where(BrandConfig.brand_id == brand_id)
    )
    row = result.first()
    brand_slug = row.brand_slug if row else brand_id

    qr_token, qr_url, valid_until, next_rotation_at = generate_qr_token(
        brand_id=brand_id,
        location_id=location_id,
        duration_minutes=duration_minutes,
        brand_slug=brand_slug,
    )

    # Store in Redis for active lookup
    redis_key = f"current_qr:{brand_id}:{location_id}"
    ttl_seconds = int(
        (valid_until - datetime.now(timezone.utc)).total_seconds()
    ) + 60  # Extra 60s beyond validity for grace lookups

    await r.set(redis_key, qr_token, ex=max(ttl_seconds, 60))

    logger.info(
        "QR generated: brand=%s location=%s valid_until=%s",
        brand_id,
        location_id,
        valid_until.isoformat(),
    )

    return QRGenerateResponse(
        qr_token=qr_token,
        qr_url=qr_url,
        valid_until=valid_until.isoformat(),
        next_rotation_at=next_rotation_at.isoformat(),
    )


@router.post(
    "/generate",
    response_model=QRGenerateResponse,
    summary="Generate a QR token for a brand location",
    description=(
        "Internal endpoint to generate a time-limited, HMAC-signed QR token. "
        "The token is stored in Redis for the active period."
    ),
)
async def generate(
    request: QRGenerateRequest,
    db: AsyncSession = Depends(get_db),
    r: aioredis.Redis = Depends(get_redis),
) -> QRGenerateResponse:
    """Generate a new QR token for a brand location."""
    return await _generate_and_store(
        r=r,
        db=db,
        brand_id=request.brand_id,
        location_id=request.location_id,
        duration_minutes=request.duration_minutes,
    )


@router.post(
    "/force-rotate",
    response_model=QRGenerateResponse,
    summary="Force-rotate QR token",
    description=(
        "Immediately invalidate the current QR token for a location and "
        "generate a new one. Used for security purposes or manual rotation."
    ),
)
async def force_rotate(
    request: ForceRotateRequest,
    db: AsyncSession = Depends(get_db),
    r: aioredis.Redis = Depends(get_redis),
) -> QRGenerateResponse:
    """Force-rotate the QR token for a brand location.

    Deletes the existing token from Redis and generates a fresh one
    with the default 15-minute duration.
    """
    # Delete old token first
    redis_key = f"current_qr:{request.brand_id}:{request.location_id}"
    await r.delete(redis_key)

    logger.info(
        "QR force-rotated: brand=%s location=%s",
        request.brand_id,
        request.location_id,
    )

    return await _generate_and_store(
        r=r,
        db=db,
        brand_id=request.brand_id,
        location_id=request.location_id,
        duration_minutes=15,
    )
