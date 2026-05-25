"""Portal Auth router — brand portal login/logout/register for operators.

Phase 0 approach: hardcoded dev operator with bcrypt-hashed password.
Registered operators stored in Redis (portal_operator:{email}).
JWT tokens with 1h expiry for portal access, 30d refresh tokens in Redis.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import BrandConfig
from app.redis_client import get_redis
from app.schemas import (
    PortalLoginRequest,
    PortalLoginResponse,
    PortalRegisterRequest,
    PortalRegisterResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Phase 0: hardcoded dev operator ──────────────────────────────────────
# In production, replace with a portal_operators table or external IdP.

_DEV_OPERATOR_EMAIL = "admin@kix.app"
_DEV_OPERATOR_PASSWORD_HASH = bcrypt.hashpw(
    b"kix-admin-dev", bcrypt.gensalt(rounds=12)
)
_DEV_OPERATOR_BRAND_ID = "all"

# ── Constants ────────────────────────────────────────────────────────────

_PORTAL_ACCESS_TOKEN_EXPIRE_MINUTES = 60  # 1 hour
_PORTAL_REFRESH_TOKEN_TTL_SECONDS = 2_592_000  # 30 days
_REDIS_PORTAL_REFRESH_PREFIX = "portal_refresh:"
_REDIS_PORTAL_OPERATOR_PREFIX = "portal_operator:"
_REDIS_CONFIG_PREFIX = "config:"
_REDIS_INVALIDATION_CHANNEL = "config_invalidation"

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

bearer_scheme = HTTPBearer()

# ── Default brand config template ────────────────────────────────────────

_DEFAULT_BRAND_CONFIG: dict = {
    "brand_name": "",
    "brand_color": "#1a73e8",
    "brand_accent": "#FFFFFF",
    "energy": {
        "qr_grant": 10,
        "day1_bonus": 50,
        "day1_window": "24h",
        "cooldown": "4h",
        "max_balance": 100,
        "regen_rate": "1/5min",
        "overcap": 130,
    },
    "games": [
        {"game_id": "latte-art", "name": "Latte Art", "cost": 10, "energy_cost": 10},
        {"game_id": "bean-match", "name": "Bean Match", "cost": 15, "energy_cost": 15},
    ],
    "leaderboard": {
        "season_duration": "weekly",
        "top_n": 50,
        "nearby_range": 5,
    },
    "reward_rules": [
        {"game_id": "latte-art", "threshold_score": 500, "win_rate": 0.1, "daily_cap_per_user": 3, "tier": "bronze"},
        {"game_id": "bean-match", "threshold_score": 500, "win_rate": 0.1, "daily_cap_per_user": 3, "tier": "bronze"},
    ],
}


# ── Helpers ──────────────────────────────────────────────────────────────


def _create_portal_access_token(email: str, brand_id: str = "all") -> str:
    """Create a 1-hour JWT for portal access."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": email,
        "role": "portal_admin",
        "brand_id": brand_id,
        "iat": now,
        "exp": now + timedelta(minutes=_PORTAL_ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _create_portal_refresh_token() -> str:
    """Generate an opaque random refresh token (URL-safe, 32 bytes)."""
    return secrets.token_urlsafe(32)


def _verify_dev_operator(email: str, password: str) -> bool:
    """Verify operator credentials against the Phase 0 dev operator."""
    if email != _DEV_OPERATOR_EMAIL:
        return False
    return bcrypt.checkpw(password.encode("utf-8"), _DEV_OPERATOR_PASSWORD_HASH)


def _generate_brand_id(brand_name: str) -> str:
    """Generate brand_id from brand_name: lowercase, hyphens, alphanumeric only.

    For non-Latin names (Chinese etc.), uses a hex hash of the name.
    """
    slug = brand_name.lower().replace(" ", "-")
    slug = re.sub(r"[^a-z0-9\-]", "", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        import hashlib
        h = hashlib.md5(brand_name.encode()).hexdigest()[:8]
        slug = f"brand-{h}"
    return slug


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("/register", response_model=PortalRegisterResponse, status_code=status.HTTP_201_CREATED)
async def portal_register(
    body: PortalRegisterRequest,
    r: aioredis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
) -> PortalRegisterResponse:
    """Register a new portal operator and create their brand."""
    # Validate email format
    if not _EMAIL_PATTERN.match(body.email):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid email format",
        )

    # Validate password length
    if len(body.password) < 6:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password must be at least 6 characters",
        )

    # Validate brand_name not empty
    if not body.brand_name.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Brand name cannot be empty",
        )

    # Check if email already registered in Redis
    operator_key = f"{_REDIS_PORTAL_OPERATOR_PREFIX}{body.email}"
    existing_operator = await r.exists(operator_key)
    if existing_operator:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    # Also reject if trying to register with the dev operator email
    if body.email == _DEV_OPERATOR_EMAIL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    # Generate brand_id from brand_name
    brand_id = _generate_brand_id(body.brand_name)

    # Check if brand_id already exists in DB
    existing_brand = await db.get(BrandConfig, brand_id)
    if existing_brand is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Brand ID '{brand_id}' already exists. Choose a different brand name.",
        )

    # Hash password
    password_hash = bcrypt.hashpw(body.password.encode("utf-8"), bcrypt.gensalt(rounds=12))

    # Store operator in Redis (persistent, no TTL)
    await r.hset(operator_key, mapping={
        "password_hash": password_hash.decode("utf-8"),
        "brand_id": brand_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    # Create brand in DB with default config + brand info from registration
    brand_config = dict(_DEFAULT_BRAND_CONFIG)
    brand_config["brand_name"] = body.brand_name.strip()
    if body.brand_color:
        brand_config["brand_color"] = body.brand_color

    brand = BrandConfig(
        brand_id=brand_id,
        brand_name=body.brand_name.strip(),
        brand_slug=brand_id,
        config_json=brand_config,
    )
    db.add(brand)
    await db.flush()

    # Propagate config to Redis
    redis_config_key = f"{_REDIS_CONFIG_PREFIX}{brand_id}"
    await r.set(redis_config_key, json.dumps(brand_config))
    await r.publish(_REDIS_INVALIDATION_CHANNEL, brand_id)

    # Create tokens
    access_token = _create_portal_access_token(body.email, brand_id=brand_id)
    refresh_token = _create_portal_refresh_token()

    # Store refresh token in Redis
    refresh_key = f"{_REDIS_PORTAL_REFRESH_PREFIX}{refresh_token}"
    await r.hset(refresh_key, mapping={
        "email": body.email,
        "role": "portal_admin",
        "brand_id": brand_id,
    })
    await r.expire(refresh_key, _PORTAL_REFRESH_TOKEN_TTL_SECONDS)

    logger.info("Portal registration successful for %s, brand_id=%s", body.email, brand_id)

    return PortalRegisterResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        brand_id=brand_id,
        brand_name=body.brand_name.strip(),
    )


@router.post("/login", response_model=PortalLoginResponse)
async def portal_login(
    body: PortalLoginRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> PortalLoginResponse:
    """Authenticate a portal operator and issue JWT + refresh token.

    Checks Redis-stored operators first, then falls back to the hardcoded dev operator.
    """
    brand_id = "all"
    authenticated = False

    # 1. Check Redis operators first
    operator_key = f"{_REDIS_PORTAL_OPERATOR_PREFIX}{body.email}"
    operator_data = await r.hgetall(operator_key)
    if operator_data:
        stored_hash = operator_data.get(b"password_hash") or operator_data.get("password_hash")
        if stored_hash:
            if isinstance(stored_hash, str):
                stored_hash = stored_hash.encode("utf-8")
            if bcrypt.checkpw(body.password.encode("utf-8"), stored_hash):
                brand_id_raw = operator_data.get(b"brand_id") or operator_data.get("brand_id")
                if isinstance(brand_id_raw, bytes):
                    brand_id = brand_id_raw.decode("utf-8")
                else:
                    brand_id = str(brand_id_raw)
                authenticated = True

    # 2. Fall back to hardcoded dev operator
    if not authenticated:
        if _verify_dev_operator(body.email, body.password):
            brand_id = _DEV_OPERATOR_BRAND_ID
            authenticated = True

    if not authenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    access_token = _create_portal_access_token(body.email, brand_id=brand_id)
    refresh_token = _create_portal_refresh_token()

    # Store refresh token in Redis with 30-day TTL
    redis_key = f"{_REDIS_PORTAL_REFRESH_PREFIX}{refresh_token}"
    await r.hset(redis_key, mapping={
        "email": body.email,
        "role": "portal_admin",
        "brand_id": brand_id,
    })
    await r.expire(redis_key, _PORTAL_REFRESH_TOKEN_TTL_SECONDS)

    logger.info("Portal login successful for %s", body.email)

    return PortalLoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
    )


@router.post("/logout", status_code=status.HTTP_200_OK)
async def portal_logout(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    """Logout portal operator by invalidating the refresh token.

    Expects the refresh token in the Authorization header for deletion.
    """
    token = credentials.credentials

    # Delete the refresh token from Redis
    redis_key = f"{_REDIS_PORTAL_REFRESH_PREFIX}{token}"
    deleted = await r.delete(redis_key)

    if deleted:
        logger.info("Portal refresh token invalidated")
    else:
        logger.info("Portal logout — token not found (already expired or invalid)")

    return {"message": "Logged out successfully"}
