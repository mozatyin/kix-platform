"""Brands router — brand configuration CRUD and location management.

R5 Config Propagation:
  PG write → Redis SET config:{brand_id} → Redis PUBLISH config_invalidation {brand_id}
  Read: Redis first, PG fallback (populate Redis on miss).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from app.database import get_db
from app.redis_client import get_redis
from app.models import BrandConfig, BrandLocation
from app.schemas import (
    BrandConfigCreate,
    BrandConfigUpdate,
    BrandConfigResponse,
    BrandLocationCreate,
    BrandLocationResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Helpers ──────────────────────────────────────────────────────────────

_REQUIRED_CONFIG_SECTIONS = {"energy", "games", "leaderboard"}
_REDIS_CONFIG_PREFIX = "config:"
_REDIS_INVALIDATION_CHANNEL = "config_invalidation"


def _validate_config_json(config_json: dict) -> None:
    """Ensure config_json contains all required top-level sections."""
    missing = _REQUIRED_CONFIG_SECTIONS - set(config_json.keys())
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"config_json missing required sections: {sorted(missing)}",
        )


def _brand_to_response(brand: BrandConfig) -> BrandConfigResponse:
    return BrandConfigResponse(
        brand_id=brand.brand_id,
        brand_name=brand.brand_name,
        brand_slug=brand.brand_slug,
        config_json=brand.config_json,
        status=brand.status,
        created_at=brand.created_at.isoformat(),
        updated_at=brand.updated_at.isoformat(),
    )


def _location_to_response(loc: BrandLocation) -> BrandLocationResponse:
    return BrandLocationResponse(
        location_id=loc.location_id,
        brand_id=loc.brand_id,
        location_name=loc.location_name,
        address=loc.address,
        latitude=float(loc.latitude) if loc.latitude is not None else None,
        longitude=float(loc.longitude) if loc.longitude is not None else None,
        status=loc.status,
    )


async def _propagate_config(
    r: aioredis.Redis, brand_id: str, config_json: dict
) -> None:
    """Write config to Redis and publish invalidation event."""
    redis_key = f"{_REDIS_CONFIG_PREFIX}{brand_id}"
    await r.set(redis_key, json.dumps(config_json))
    await r.publish(_REDIS_INVALIDATION_CHANNEL, brand_id)
    logger.info("Config propagated for brand_id=%s", brand_id)


# ── Brand Config Endpoints ───────────────────────────────────────────────


@router.post(
    "/",
    response_model=BrandConfigResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_brand_config(
    body: BrandConfigCreate,
    db: AsyncSession = Depends(get_db),
    r: aioredis.Redis = Depends(get_redis),
) -> BrandConfigResponse:
    """Create a new brand configuration."""
    _validate_config_json(body.config_json)

    # Check for duplicate brand_id
    existing = await db.get(BrandConfig, body.brand_id)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Brand {body.brand_id} already exists",
        )

    brand = BrandConfig(
        brand_id=body.brand_id,
        brand_name=body.brand_name,
        brand_slug=body.brand_slug,
        config_json=body.config_json,
    )
    db.add(brand)
    await db.flush()
    await db.refresh(brand)

    # Propagate: PG → Redis SET → Redis PUBLISH
    await _propagate_config(r, brand.brand_id, brand.config_json)

    return _brand_to_response(brand)


@router.get("/{brand_id}", response_model=BrandConfigResponse)
async def get_brand_config(
    brand_id: str,
    db: AsyncSession = Depends(get_db),
    r: aioredis.Redis = Depends(get_redis),
) -> BrandConfigResponse:
    """Get brand configuration — Redis first, PG fallback."""
    redis_key = f"{_REDIS_CONFIG_PREFIX}{brand_id}"

    # Try Redis first
    cached = await r.get(redis_key)
    if cached is not None:
        # Still need PG for non-config fields (brand_name, slug, timestamps)
        brand = await db.get(BrandConfig, brand_id)
        if brand is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Brand {brand_id} not found",
            )
        return _brand_to_response(brand)

    # Redis miss — read from PG and populate cache
    brand = await db.get(BrandConfig, brand_id)
    if brand is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Brand {brand_id} not found",
        )

    # Populate Redis for next read
    await r.set(redis_key, json.dumps(brand.config_json))
    logger.info("Cache miss for brand_id=%s, populated Redis", brand_id)

    return _brand_to_response(brand)


@router.put("/{brand_id}/config", response_model=BrandConfigResponse)
async def update_brand_config(
    brand_id: str,
    body: BrandConfigUpdate,
    db: AsyncSession = Depends(get_db),
    r: aioredis.Redis = Depends(get_redis),
) -> BrandConfigResponse:
    """Update brand configuration JSON."""
    _validate_config_json(body.config_json)

    brand = await db.get(BrandConfig, brand_id)
    if brand is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Brand {brand_id} not found",
        )

    now = datetime.now(timezone.utc)
    await db.execute(
        update(BrandConfig)
        .where(BrandConfig.brand_id == brand_id)
        .values(config_json=body.config_json, updated_at=now)
    )
    await db.flush()
    await db.refresh(brand)

    # Propagate: PG → Redis SET → Redis PUBLISH
    await _propagate_config(r, brand_id, body.config_json)

    return _brand_to_response(brand)


# ── Location Endpoints ───────────────────────────────────────────────────


@router.get("/{brand_id}/locations", response_model=list[BrandLocationResponse])
async def list_locations(
    brand_id: str,
    db: AsyncSession = Depends(get_db),
) -> list[BrandLocationResponse]:
    """List all locations for a brand."""
    result = await db.execute(
        select(BrandLocation).where(BrandLocation.brand_id == brand_id)
    )
    locations = result.scalars().all()
    return [_location_to_response(loc) for loc in locations]


@router.post(
    "/{brand_id}/locations",
    response_model=BrandLocationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_location(
    brand_id: str,
    body: BrandLocationCreate,
    db: AsyncSession = Depends(get_db),
) -> BrandLocationResponse:
    """Create a new location for a brand."""
    # Verify brand exists
    brand = await db.get(BrandConfig, brand_id)
    if brand is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Brand {brand_id} not found",
        )

    # Check for duplicate location_id
    existing = await db.get(BrandLocation, body.location_id)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Location {body.location_id} already exists",
        )

    location = BrandLocation(
        location_id=body.location_id,
        brand_id=brand_id,
        location_name=body.location_name,
        address=body.address,
        latitude=body.latitude,
        longitude=body.longitude,
    )
    db.add(location)
    await db.flush()
    await db.refresh(location)

    return _location_to_response(location)


@router.put(
    "/{brand_id}/locations/{location_id}",
    response_model=BrandLocationResponse,
)
async def update_location(
    brand_id: str,
    location_id: str,
    body: BrandLocationCreate,
    db: AsyncSession = Depends(get_db),
) -> BrandLocationResponse:
    """Update an existing location."""
    location = await db.get(BrandLocation, location_id)
    if location is None or location.brand_id != brand_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Location {location_id} not found for brand {brand_id}",
        )

    location.location_name = body.location_name
    location.address = body.address
    location.latitude = body.latitude
    location.longitude = body.longitude
    await db.flush()
    await db.refresh(location)

    return _location_to_response(location)


@router.delete(
    "/{brand_id}/locations/{location_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_location(
    brand_id: str,
    location_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Soft-delete a location by setting status to 'inactive'."""
    location = await db.get(BrandLocation, location_id)
    if location is None or location.brand_id != brand_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Location {location_id} not found for brand {brand_id}",
        )

    location.status = "inactive"
    await db.flush()

    return Response(status_code=status.HTTP_204_NO_CONTENT)
