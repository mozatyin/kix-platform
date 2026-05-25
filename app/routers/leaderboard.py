"""Leaderboard router — rankings and nearby queries."""

from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Query

from app.deps import get_current_user
from app.redis_client import get_redis
from app.schemas import LeaderboardEntry, LeaderboardResponse, NearbyResponse

import redis.asyncio as aioredis

router = APIRouter()

# SGT = UTC+8
_SGT = timezone(timedelta(hours=8))

# Rule 5: MAX_TS = 2^40
_MAX_TS = 1099511627776


def get_season_id() -> str:
    """Return current daily season ID in SGT: 'daily:YYYY-MM-DD'."""
    now_sgt = datetime.now(_SGT)
    return f"daily:{now_sgt.strftime('%Y-%m-%d')}"


def decode_composite_score(composite: float) -> int:
    """Extract actual integer score from composite.

    composite_score = score + (1 - ts / MAX_TS)
    The fractional part is always < 1, so floor gives the original score.
    """
    return math.floor(composite)


async def _build_entries(
    r: aioredis.Redis,
    brand_id: str,
    members_with_scores: list,
    offset: int,
    current_user_id: str,
) -> list[LeaderboardEntry]:
    """Build LeaderboardEntry list from ZREVRANGE result."""
    entries: list[LeaderboardEntry] = []
    for idx, (member, composite) in enumerate(members_with_scores):
        user_id = str(member)
        score = decode_composite_score(composite)
        rank = offset + idx + 1

        # Look up display name from user profile hash
        display_name = await r.hget(f"user_profile:{brand_id}:{user_id}", "display_name")
        if display_name is None:
            display_name = f"Player-{user_id[:8]}"

        entries.append(
            LeaderboardEntry(
                rank=rank,
                user_id=user_id,
                display_name=display_name,
                score=score,
                is_self=(user_id == current_user_id),
            )
        )
    return entries


@router.get("/", response_model=LeaderboardResponse)
async def get_leaderboard(
    brand_id: str = Query(..., description="Brand identifier"),
    game_id: str = Query(..., description="Game identifier"),
    season_id: str | None = Query(None, description="Season ID (default: current daily)"),
    limit: int = Query(50, ge=1, le=100, description="Number of entries to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> LeaderboardResponse:
    """Get leaderboard rankings with pagination."""
    if season_id is None:
        season_id = get_season_id()

    redis_key = f"leaderboard:{brand_id}:{game_id}:{season_id}"

    # ZREVRANGE returns list of (member, score) tuples when withscores=True
    members_with_scores = await r.zrevrange(
        redis_key, offset, offset + limit - 1, withscores=True
    )

    entries = await _build_entries(
        r, brand_id, members_with_scores, offset, current_user["sub"]
    )

    total_players = await r.zcard(redis_key)

    return LeaderboardResponse(
        entries=entries,
        season_id=season_id,
        total_players=total_players,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


@router.get("/nearby", response_model=NearbyResponse)
async def get_nearby(
    brand_id: str = Query(..., description="Brand identifier"),
    game_id: str = Query(..., description="Game identifier"),
    season_id: str | None = Query(None, description="Season ID (default: current daily)"),
    range: int = Query(5, ge=1, le=50, alias="range", description="Number of entries above/below"),
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> NearbyResponse:
    """Get rankings near the current user's position."""
    if season_id is None:
        season_id = get_season_id()

    redis_key = f"leaderboard:{brand_id}:{game_id}:{season_id}"
    user_id = current_user["sub"]

    # Find the user's rank (0-based, highest score = rank 0)
    user_rank = await r.zrevrank(redis_key, user_id)

    if user_rank is None:
        return NearbyResponse(entries=[], self_rank=None, season_id=season_id)

    # Calculate window around user
    start = max(0, user_rank - range)
    stop = user_rank + range

    members_with_scores = await r.zrevrange(
        redis_key, start, stop, withscores=True
    )

    entries = await _build_entries(
        r, brand_id, members_with_scores, start, user_id
    )

    return NearbyResponse(
        entries=entries,
        self_rank=user_rank + 1,  # 1-based
        season_id=season_id,
    )
