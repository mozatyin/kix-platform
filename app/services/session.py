"""Session FSM and brand-config service for KiX Platform R5."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
import redis.asyncio as aioredis

from app.redis_client import lua_scripts

# ── SGT (UTC+8) ─────────────────────────────────────────────────────────
_SGT = timezone(timedelta(hours=8))

# Rule 5: composite score tie-breaking constant
_MAX_TS = 1_099_511_627_776  # 2^40


def get_season_id() -> str:
    """Return the daily season identifier in SGT: 'daily:YYYY-MM-DD'."""
    return f"daily:{datetime.now(_SGT).strftime('%Y-%m-%d')}"


async def get_brand_config(r: aioredis.Redis, brand_id: str) -> dict:
    """Fetch brand configuration from Redis.

    Raises HTTP 404 if the config key does not exist.
    """
    raw = await r.get(f"config:{brand_id}")
    if raw is None:
        raise HTTPException(status_code=404, detail="Brand config not found")
    return json.loads(raw)


async def submit_score(
    r: aioredis.Redis,
    session_id: str,
    brand_id: str,
    game_id: str,
    season_id: str,
    score: int,
    user_id: str,
) -> tuple[int, int]:
    """Submit a score for a game session via Lua.

    The Lua script handles idempotency, anti-cheat (min game duration),
    and composite-score tie-breaking (Rule 5).

    Returns (score, rank).
    """
    now = int(time.time())
    result = await lua_scripts["score_submit"](
        keys=[
            f"session:{session_id}",
            f"leaderboard:{brand_id}:{game_id}:{season_id}",
        ],
        args=[score, now, user_id, _MAX_TS],
    )
    return int(result[0]), int(result[1])
