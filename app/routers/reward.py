"""Reward router — internal reward evaluation endpoint.

Protected by Nginx deny rules (no JWT auth). Called by the game-end
handler to determine if a session earns a voucher reward.
"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.redis_client import get_redis
from app.schemas import RewardEvaluateRequest, RewardEvaluateResponse
from app.services.reward import evaluate_reward

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/",
    response_model=RewardEvaluateResponse,
    summary="Evaluate reward eligibility",
    description=(
        "Internal endpoint called after game-end to determine if the player "
        "qualifies for a voucher reward. Applies P70 percentile qualification, "
        "probability roll, daily cap, and pool depletion rules."
    ),
)
async def evaluate(
    request: RewardEvaluateRequest,
    db: AsyncSession = Depends(get_db),
    r: aioredis.Redis = Depends(get_redis),
) -> RewardEvaluateResponse:
    """Evaluate whether a completed game session earns a voucher."""
    logger.info(
        "Reward evaluation: user=%s brand=%s game=%s score=%d",
        request.user_id,
        request.brand_id,
        request.game_id,
        request.score,
    )
    return await evaluate_reward(db, r, request)
