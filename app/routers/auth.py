"""Auth router — token issuance, refresh, and Centrifugo tokens."""

from __future__ import annotations

import logging
import random
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models import UserProfile
from app.redis_client import get_redis
from app.schemas import (
    ErrorResponse,
    RefreshRequest,
    TokenRequest,
    TokenResponse,
)
from app.services.energy import check_welcome_back, init_energy, regen_energy
from app.services.session import get_brand_config
from app.services.token import (
    create_access_token,
    create_refresh_token,
    store_refresh_token,
    validate_refresh_token,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── POST /token ──────────────────────────────────────────────────────────


@router.post(
    "/token",
    response_model=TokenResponse,
    responses={404: {"model": ErrorResponse}},
)
async def issue_token(
    body: TokenRequest,
    db: AsyncSession = Depends(get_db),
    r: aioredis.Redis = Depends(get_redis),
):
    """Issue a JWT access token + refresh token.

    Creates a new user if the (brand_id, device_sig) pair is unknown.
    Initializes energy for new users (Rule 18) or regens + welcome-back
    for returning users.
    """
    # 1. Load brand config (validates brand_id exists)
    config = await get_brand_config(r, body.brand_id)

    # 2. Look up user by (brand_id, device_sig)
    stmt = select(UserProfile).where(
        UserProfile.brand_id == body.brand_id,
        UserProfile.device_sig == body.device_sig,
    )
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    is_new = user is None

    if is_new:
        # 3. Create new user
        user_id = uuid.uuid4()
        display_name = f"Player_{random.randint(1000, 9999)}"
        day1_expires = now + timedelta(hours=24)

        user = UserProfile(
            user_id=user_id,
            brand_id=body.brand_id,
            device_sig=body.device_sig,
            display_name=display_name,
            day1_expires_at=day1_expires,
        )
        db.add(user)
        await db.flush()  # get user_id assigned

        # 4. Rule 18: init energy (balance=100, regen_at=now)
        await init_energy(r, body.brand_id, str(user_id))
        energy_balance = 100
    else:
        user_id = user.user_id

        # 5. Regen energy for returning user
        energy_balance, _ = await regen_energy(
            r, body.brand_id, str(user_id), config
        )

        # Check welcome-back bonus
        wb_bonus = await check_welcome_back(
            r, body.brand_id, str(user_id), user.last_seen_at, config
        )
        if wb_bonus > 0:
            energy_balance += wb_bonus
            logger.info(
                "Welcome-back bonus: user=%s bonus=%d new_balance=%d",
                user_id, wb_bonus, energy_balance,
            )

    # 6. Determine is_day1
    is_day1 = now < user.day1_expires_at

    # 7. Create JWT + refresh token
    access_token = create_access_token(
        user_id=str(user_id),
        brand_id=body.brand_id,
        device_sig=body.device_sig,
        is_day1=is_day1,
    )
    refresh_token = create_refresh_token()

    # 8. Store refresh token in Redis
    await store_refresh_token(
        r, refresh_token, str(user_id), body.brand_id, body.device_sig
    )

    # 9. Update last_seen_at
    user.last_seen_at = now

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user_id=str(user_id),
        energy=energy_balance,
        is_day1=is_day1,
        day1_expires_at=user.day1_expires_at.isoformat() if is_day1 else None,
    )


# ── POST /token/refresh ─────────────────────────────────────────────────


@router.post(
    "/token/refresh",
    response_model=TokenResponse,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
    },
)
async def refresh_token(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
    r: aioredis.Redis = Depends(get_redis),
):
    """Refresh an access token.

    Validates the old refresh token (rotation: old is deleted),
    verifies device_sig matches, and issues a new pair.
    """
    # 1. Validate refresh token (rotation: old token deleted)
    data = await validate_refresh_token(r, body.refresh_token)
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    # 2. Check device_sig matches
    if data["device_sig"] != body.device_sig:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="DEVICE_MISMATCH",
        )

    user_id = data["user_id"]
    brand_id = data["brand_id"]

    # 3. Load user for is_day1 check
    stmt = select(UserProfile).where(UserProfile.user_id == uuid.UUID(user_id))
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    now = datetime.now(timezone.utc)
    is_day1 = now < user.day1_expires_at

    # 4. Issue new JWT + new refresh token
    access_token = create_access_token(
        user_id=user_id,
        brand_id=brand_id,
        device_sig=body.device_sig,
        is_day1=is_day1,
    )
    new_refresh = create_refresh_token()
    await store_refresh_token(r, new_refresh, user_id, brand_id, body.device_sig)

    # 5. Get current energy balance
    balance_key = f"energy:balance:{brand_id}:{user_id}"
    raw_balance = await r.get(balance_key)
    energy_balance = int(raw_balance) if raw_balance is not None else 0

    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh,
        user_id=user_id,
        energy=energy_balance,
        is_day1=is_day1,
        day1_expires_at=user.day1_expires_at.isoformat() if is_day1 else None,
    )


# ── POST /centrifugo-token ──────────────────────────────────────────────


@router.post("/centrifugo-token")
async def centrifugo_token(
    user: dict = Depends(get_current_user),
):
    """Issue a short-lived Centrifugo JWT (5 min) for real-time channels."""
    from jose import jwt as jose_jwt

    now = datetime.now(timezone.utc)
    user_id = user["sub"]
    brand_id = user["brand_id"]

    # Centrifugo expects: sub, exp, channels (optional)
    channels = [
        f"user:{user_id}",
        f"leaderboard:{brand_id}",
    ]
    payload = {
        "sub": user_id,
        "exp": now + timedelta(minutes=5),
        "channels": channels,
    }
    token = jose_jwt.encode(
        payload, settings.jwt_secret, algorithm=settings.jwt_algorithm
    )
    return {"token": token, "channels": channels}
