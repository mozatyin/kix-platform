"""Game Catalog router — browse ELTM game library, place orders, add to brand."""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt as jose_jwt
from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from app.config import settings
from app.database import get_db
from app.models import BrandConfig
from app.redis_client import get_redis
from app.schemas import (
    AddGameToBrandRequest,
    AddGameToBrandResponse,
    GameCatalogEntry,
    GameCatalogResponse,
    GameOrderListResponse,
    GameOrderRequest,
    GameOrderResponse,
)

# ── ELTM import ─────────────────────────────────────────────────────────
sys.path.insert(0, "/Users/mozat/eltm")
from eltm.kix_channel import rank_games_for_business, build_for_business, research_business

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Portal auth dependency ──────────────────────────────────────────────

_bearer_scheme = HTTPBearer()


async def _get_portal_operator(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> dict:
    """Decode portal JWT and return operator claims (email, brand_id)."""
    try:
        payload = jose_jwt.decode(
            credentials.credentials,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired portal token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if payload.get("role") != "portal_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a portal operator token",
        )
    return {
        "email": payload.get("sub"),
        "brand_id": payload.get("brand_id"),
    }


# ── Request bodies for new endpoints ────────────────────────────────────

class RecommendRequest(BaseModel):
    business_description: str
    top_n: int = 10


class BuildForBusinessRequest(BaseModel):
    business_description: str
    game_slug: str

# ── Load catalog at module level ─────────────────────────────────────────
_CATALOG_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "game_catalog.json"

with open(_CATALOG_PATH, encoding="utf-8") as f:
    _CATALOG: list[dict] = json.load(f)

# Pre-compute unique sorted categories
_CATEGORIES: list[str] = sorted({g["category"] for g in _CATALOG})

# Build a lookup by slug for O(1) access
_CATALOG_BY_SLUG: dict[str, dict] = {g["slug"]: g for g in _CATALOG}


# ── Helpers ──────────────────────────────────────────────────────────────

def _matches_query(game: dict, q: str) -> bool:
    """Case-insensitive partial match against slug, name, and aliases."""
    q_lower = q.lower()
    if q_lower in game["slug"].lower():
        return True
    if q_lower in game["name"].lower():
        return True
    for alias in game.get("aliases", []):
        if q_lower in alias.lower():
            return True
    return False


def _game_to_entry(game: dict) -> GameCatalogEntry:
    return GameCatalogEntry(
        slug=game["slug"],
        name=game["name"],
        category=game["category"],
        description=game["description"],
        player_count=game["player_count"],
        primary_color=game["primary_color"],
        accent_color=game["accent_color"],
    )


# ── 1. Game Catalog Search ───────────────────────────────────────────────

@router.get("", response_model=GameCatalogResponse)
async def search_game_catalog(
    q: str | None = Query(None, description="Search text (matches slug, name, aliases)"),
    category: str | None = Query(None, description="Exact category filter"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> GameCatalogResponse:
    """Browse and search the ELTM game catalog (612 games)."""
    results = _CATALOG

    # Filter by category (exact match)
    if category is not None:
        results = [g for g in results if g["category"] == category]

    # Filter by search query
    if q is not None and q.strip():
        results = [g for g in results if _matches_query(g, q.strip())]

    total = len(results)

    # Paginate
    page = results[offset : offset + limit]

    return GameCatalogResponse(
        games=[_game_to_entry(g) for g in page],
        total=total,
        categories=_CATEGORIES,
    )


# ── 2. Game Order — Create ───────────────────────────────────────────────

_REDIS_ORDER_PREFIX = "game_order:"
_REDIS_BRAND_ORDERS_PREFIX = "game_orders_brand:"


@router.post(
    "/order",
    response_model=GameOrderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_game_order(
    body: GameOrderRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> GameOrderResponse:
    """Create a custom game order (stored in Redis for Phase 0)."""
    # Validate game_slug if provided
    if body.game_slug is not None and body.game_slug not in _CATALOG_BY_SLUG:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Game slug '{body.game_slug}' not found in catalog",
        )

    order_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    order_data = {
        "order_id": order_id,
        "brand_id": body.brand_id,
        "game_slug": body.game_slug or "",
        "description": body.description,
        "theme": body.theme or "",
        "requirements": body.requirements or "",
        "status": "pending",
        "created_at": now,
    }

    # Store order in Redis hash
    order_key = f"{_REDIS_ORDER_PREFIX}{order_id}"
    await r.hset(order_key, mapping=order_data)

    # Add order_id to brand's order set for listing
    brand_key = f"{_REDIS_BRAND_ORDERS_PREFIX}{body.brand_id}"
    await r.sadd(brand_key, order_id)

    logger.info("Game order created: order_id=%s brand_id=%s", order_id, body.brand_id)

    return GameOrderResponse(
        order_id=order_id,
        status="pending",
        game_slug=body.game_slug,
        description=body.description,
        created_at=now,
    )


# ── 3. Game Order — List by Brand ────────────────────────────────────────

@router.get("/orders/{brand_id}", response_model=GameOrderListResponse)
async def list_game_orders(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> GameOrderListResponse:
    """List all game orders for a brand."""
    brand_key = f"{_REDIS_BRAND_ORDERS_PREFIX}{brand_id}"
    order_ids = await r.smembers(brand_key)

    orders: list[GameOrderResponse] = []
    for oid in order_ids:
        order_key = f"{_REDIS_ORDER_PREFIX}{oid}"
        data = await r.hgetall(order_key)
        if not data:
            continue
        orders.append(GameOrderResponse(
            order_id=data.get("order_id", oid),
            status=data.get("status", "unknown"),
            game_slug=data.get("game_slug") or None,
            description=data.get("description", ""),
            created_at=data.get("created_at", ""),
            game_file=data.get("game_file") or None,
            game_name=data.get("game_name") or None,
            order_type=data.get("order_type") or None,
            error=data.get("error") or None,
        ))

    orders.sort(key=lambda o: o.created_at, reverse=True)
    return GameOrderListResponse(orders=orders)


@router.get("/orders/{brand_id}/{order_id}")
async def get_order_status(
    brand_id: str,
    order_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Poll status of a single game order (for Portal async polling)."""
    order_key = f"{_REDIS_ORDER_PREFIX}{order_id}"
    data = await r.hgetall(order_key)
    if not data:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")

    return {
        "order_id": order_id,
        "status": data.get("status", "unknown"),
        "game_slug": data.get("game_slug") or None,
        "game_name": data.get("game_name") or None,
        "game_file": data.get("game_file") or None,
        "order_type": data.get("order_type") or None,
        "error": data.get("error") or None,
    }


# ── 4. Add Catalog Game to Brand ─────────────────────────────────────────

_REDIS_CONFIG_PREFIX = "config:"
_REDIS_INVALIDATION_CHANNEL = "config_invalidation"


@router.post(
    "/add-to-brand",
    response_model=AddGameToBrandResponse,
    status_code=status.HTTP_200_OK,
)
async def add_game_to_brand(
    body: AddGameToBrandRequest,
    db: AsyncSession = Depends(get_db),
    r: aioredis.Redis = Depends(get_redis),
) -> AddGameToBrandResponse:
    """Add a catalog game to a brand's config_json.games array."""
    # Validate game_slug exists in catalog
    catalog_game = _CATALOG_BY_SLUG.get(body.game_slug)
    if catalog_game is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Game slug '{body.game_slug}' not found in catalog",
        )

    # Load brand from DB
    brand = await db.get(BrandConfig, body.brand_id)
    if brand is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Brand '{body.brand_id}' not found",
        )

    # Read config_json and append game
    config = dict(brand.config_json)
    games_list: list[dict] = list(config.get("games", []))

    # Check if game already exists
    existing_slugs = {g.get("game_id") or g.get("slug") for g in games_list}
    if body.game_slug in existing_slugs:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Game '{body.game_slug}' already exists in brand config",
        )

    default_energy_cost = 10
    games_list.append({
        "game_id": body.game_slug,
        "name": catalog_game["name"],
        "energy_cost": default_energy_cost,
    })
    config["games"] = games_list

    # Save back to DB
    now = datetime.now(timezone.utc)
    await db.execute(
        update(BrandConfig)
        .where(BrandConfig.brand_id == body.brand_id)
        .values(config_json=config, updated_at=now)
    )
    await db.flush()

    # Propagate to Redis
    redis_key = f"{_REDIS_CONFIG_PREFIX}{body.brand_id}"
    await r.set(redis_key, json.dumps(config))
    await r.publish(_REDIS_INVALIDATION_CHANNEL, body.brand_id)

    logger.info(
        "Game '%s' added to brand '%s' with energy_cost=%d",
        body.game_slug,
        body.brand_id,
        default_energy_cost,
    )

    return AddGameToBrandResponse(
        brand_id=body.brand_id,
        game_slug=body.game_slug,
        game_name=catalog_game["name"],
        energy_cost=default_energy_cost,
        message=f"Game '{catalog_game['name']}' added to brand successfully",
    )


# ── 5. Recommend Games for Business ────────────────────────────────────

@router.post("/recommend")
async def recommend_games(
    body: RecommendRequest,
    operator: dict = Depends(_get_portal_operator),
):
    """Use ELTM to rank games best suited for a merchant's business."""
    try:
        ranked = rank_games_for_business(
            body.business_description,
            top_n=body.top_n,
        )
    except Exception as exc:
        logger.exception("rank_games_for_business failed for brand_id=%s", operator["brand_id"])
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Game recommendation failed: {exc}",
        )

    return ranked


# ── 6. Build Branded Game for Business (Async via Redis queue) ─────────────

@router.post("/build-for-business")
async def build_game_for_business(
    body: BuildForBusinessRequest,
    operator: dict = Depends(_get_portal_operator),
    r: aioredis.Redis = Depends(get_redis),
):
    """Enqueue a branded game generation order. Worker processes asynchronously.

    Returns order_id immediately. Client polls GET /game-catalog/orders/{brand_id}
    or GET /game-catalog/orders/{brand_id}/{order_id} for completion.
    """
    brand_id = operator["brand_id"]

    # Validate game_slug exists in catalog
    if body.game_slug not in _CATALOG_BY_SLUG:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Game slug '{body.game_slug}' not found in catalog",
        )

    order_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    order_data = {
        "order_id": order_id,
        "brand_id": brand_id,
        "game_slug": body.game_slug,
        "business_description": body.business_description,
        "order_type": "build_for_business",
        "status": "pending",
        "created_at": now,
    }

    order_key = f"{_REDIS_ORDER_PREFIX}{order_id}"
    await r.hset(order_key, mapping=order_data)

    brand_key = f"{_REDIS_BRAND_ORDERS_PREFIX}{brand_id}"
    await r.sadd(brand_key, order_id)

    logger.info(
        "Build-for-business order enqueued: order_id=%s brand_id=%s game=%s",
        order_id, brand_id, body.game_slug,
    )

    return {
        "order_id": order_id,
        "status": "pending",
        "game_slug": body.game_slug,
        "message": f"Order enqueued. Worker will process it.",
    }
