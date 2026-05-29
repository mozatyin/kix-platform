"""Game router — start/end game sessions."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
import httpx
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
    GameEndRequest,
    GameEndResponse,
    GameStartRequest,
    GameStartResponse,
    RewardInfo,
)
from app.services.energy import confirm_energy, reserve_energy
from app.services.session import get_brand_config, get_season_id, submit_score
from app.services.token import create_access_token

logger = logging.getLogger(__name__)

router = APIRouter()


# ── POST /start ──────────────────────────────────────────────────────────


@router.post(
    "/start",
    response_model=GameStartResponse,
    responses={
        402: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
)
async def start_game(
    body: GameStartRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    r: aioredis.Redis = Depends(get_redis),
):
    """Start a game session.

    Reserves energy (Rule 6: reservation pattern) and issues a new JWT
    with session_id embedded (Rule 3: session rotation).
    """
    user_id = user["sub"]
    brand_id = user["brand_id"]
    device_sig = user["device_sig"]

    # 1. Get brand config from Redis
    config = await get_brand_config(r, body.brand_id)

    # 2. Verify brand_id matches token
    if body.brand_id != brand_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Brand mismatch between token and request",
        )

    # 3. Generate session_id and season_id
    session_id = str(uuid.uuid4())
    season_id = get_season_id()

    # 4. Check is_day1 from user profile
    stmt = select(UserProfile).where(UserProfile.user_id == uuid.UUID(user_id))
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()
    is_day1 = False
    if profile is not None:
        is_day1 = datetime.now(timezone.utc) < profile.day1_expires_at

    # 5. Reserve energy (handles regen + reserve atomically via Lua)
    try:
        remaining, cost = await reserve_energy(
            r,
            brand_id=brand_id,
            user_id=user_id,
            game_id=body.game_id,
            season_id=season_id,
            session_id=session_id,
            config=config,
            is_day1=is_day1,
        )
    except Exception as exc:
        err = str(exc)
        if "INSUFFICIENT_ENERGY" in err:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="Insufficient energy",
            )
        if "SESSION_DUPLICATE" in err:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Active session already exists",
            )
        raise

    # 6. Issue new JWT with session_id (Rule 3: session rotation)
    access_token = create_access_token(
        user_id=user_id,
        brand_id=brand_id,
        device_sig=device_sig,
        session_id=session_id,
        is_day1=is_day1,
    )

    # Dashboard daily counters (best-effort, never block response).
    #    Tracks games_played + first-seen-per-day for new-user proxy.
    try:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        await r.incr(f"brand:{brand_id}:game_plays:{day}")
        await r.expire(f"brand:{brand_id}:game_plays:{day}", 60 * 60 * 24 * 35)
        # New users today (first time we see this user_id for this brand)
        if await r.sadd(f"brand:{brand_id}:users_ever", user_id):
            await r.sadd(f"brand:{brand_id}:users_acquired:{day}", user_id)
            await r.expire(
                f"brand:{brand_id}:users_acquired:{day}", 60 * 60 * 24 * 35
            )
    except Exception:  # pragma: no cover
        logger.warning("dashboard counters failed for brand=%s", brand_id)

    return GameStartResponse(
        session_id=session_id,
        access_token=access_token,
        energy_remaining=remaining,
        cost_charged=cost,
    )


# ── POST /end ────────────────────────────────────────────────────────────


@router.post(
    "/end",
    response_model=GameEndResponse,
    responses={400: {"model": ErrorResponse}},
)
async def end_game(
    body: GameEndRequest,
    user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
):
    """End a game session — submit score, confirm energy, evaluate reward.

    Rule 17: score_submit -> energy_confirm chaining.
    """
    user_id = user["sub"]
    brand_id = user["brand_id"]

    # 1. Load session data from Redis to get game_id
    session_data = await r.hgetall(f"session:{body.session_id}")
    if not session_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session not found",
        )

    game_id = session_data.get("game_id", "")
    season_id = session_data.get("season_id", get_season_id())

    # Verify session belongs to this user
    if session_data.get("user_id") != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Session does not belong to this user",
        )

    # 2. Submit score via Lua (handles idempotency)
    try:
        score, rank = await submit_score(
            r,
            session_id=body.session_id,
            brand_id=brand_id,
            game_id=game_id,
            season_id=season_id,
            score=body.score,
            user_id=user_id,
        )
    except Exception as exc:
        err = str(exc)
        if "GAME_TOO_SHORT" in err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Game duration too short",
            )
        if "INVALID_SCORE" in err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Score out of valid range (0-100000)",
            )
        if "INVALID_SESSION_STATE" in err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Session is not in a valid state for score submission",
            )
        raise

    # 3. Rule 17: confirm energy right after score_submit
    try:
        await confirm_energy(r, brand_id, user_id, body.session_id)
    except Exception:
        logger.warning(
            "Energy confirm failed for session=%s, score already submitted",
            body.session_id,
        )

    # 4. Call Reward Engine (degraded: reward=None if it fails)
    reward: RewardInfo | None = None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                "http://localhost:8000/internal/reward/",
                json={
                    "session_id": body.session_id,
                    "user_id": user_id,
                    "brand_id": brand_id,
                    "game_id": game_id,
                    "score": score,
                    "season_id": season_id,
                    "rank": rank,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("decision") == "reward" and data.get("voucher"):
                    v = data["voucher"]
                    reward = RewardInfo(
                        type=v.get("tier", "bronze"),
                        voucher_code=v.get("code"),
                        voucher_description=v.get("description"),
                        voucher_expires_at=v.get("expires_at"),
                    )
    except Exception:
        logger.warning(
            "Reward engine unavailable for session=%s, degraded response",
            body.session_id,
        )

    # Dashboard daily counters (best-effort).
    try:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        await r.incr(f"brand:{brand_id}:games_completed:{day}")
        await r.expire(
            f"brand:{brand_id}:games_completed:{day}", 60 * 60 * 24 * 35
        )
        # Session duration: started_at is set by reserve_energy in session hash.
        started_at = session_data.get("created_at")
        if started_at:
            try:
                started_f = float(started_at)
                dur = max(0, int(datetime.now(timezone.utc).timestamp() - started_f))
                if 0 < dur < 3600:  # filter implausible sessions
                    sd_key = f"brand:{brand_id}:session_dur:{day}"
                    await r.rpush(sd_key, dur)
                    await r.expire(sd_key, 60 * 60 * 24 * 35)
            except (TypeError, ValueError):
                pass
    except Exception:  # pragma: no cover
        logger.warning("dashboard counters failed for brand=%s", brand_id)

    return GameEndResponse(
        rank=rank,
        season_id=season_id,
        reward=reward,
    )
