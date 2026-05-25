"""Streak router — daily streak tracking."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends

from app.deps import get_current_user
from app.redis_client import get_redis, lua_scripts
from app.schemas import StreakCheckRequest, StreakCheckResponse

import redis.asyncio as aioredis

router = APIRouter()

# SGT = UTC+8
_SGT = timezone(timedelta(hours=8))


def _get_sgt_today() -> str:
    """Return current SGT date as 'YYYY-MM-DD'."""
    return datetime.now(_SGT).strftime("%Y-%m-%d")


def _get_sgt_yesterday() -> str:
    """Return yesterday's SGT date as 'YYYY-MM-DD'."""
    return (datetime.now(_SGT) - timedelta(days=1)).strftime("%Y-%m-%d")


async def _get_brand_streak_milestones(
    r: aioredis.Redis, brand_id: str
) -> list[dict]:
    """Load streak milestones from brand config in Redis.

    Brand config is stored as JSON at key config:{brand_id}.
    Expected structure: { "streak": { "milestones": [...] } }
    """
    raw = await r.get(f"config:{brand_id}")
    if not raw:
        return []

    try:
        config = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return []

    streak_config = config.get("streak", {})
    milestones = streak_config.get("milestones", [])
    # Sort by days ascending for predictable lookup
    return sorted(milestones, key=lambda m: m.get("days", 0))


@router.post("/check", response_model=StreakCheckResponse)
async def streak_check(
    body: StreakCheckRequest,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> StreakCheckResponse:
    """Check and update the user's daily streak."""
    user_id = current_user["sub"]
    brand_id = body.brand_id
    today_sgt = _get_sgt_today()

    redis_key = f"streak:{brand_id}:{user_id}"

    # Execute the Lua script atomically
    result = await lua_scripts["streak_check"](
        keys=[redis_key],
        args=[today_sgt, brand_id, user_id],
    )

    # Lua returns [current_streak, longest_streak, today_completed (1/0)]
    current_streak = int(result[0])
    longest_streak = int(result[1])
    today_completed = int(result[2]) == 1

    # Load milestones from brand config
    milestones = await _get_brand_streak_milestones(r, brand_id)

    # Determine next milestone and current milestone reward
    next_milestone: int | None = None
    milestone_reward: dict | None = None

    for ms in milestones:
        days = ms.get("days", 0)
        if days > current_streak:
            # First milestone beyond current streak = next target
            next_milestone = days
            break
        if days == current_streak and today_completed:
            # User just hit this milestone today
            milestone_reward = {
                "reward_type": ms.get("reward_type"),
                "reward_amount": ms.get("reward_amount"),
            }

    return StreakCheckResponse(
        current_streak=current_streak,
        longest_streak=longest_streak,
        today_completed=today_completed,
        next_milestone=next_milestone,
        milestone_reward=milestone_reward,
    )
