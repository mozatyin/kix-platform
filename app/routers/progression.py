"""Progression — Badges, Levels/XP, Daily Check-in.

Implements P0 items from kix-gamification-audit.md.

Redis schema:
  user:{user_id}:xp                       int (lifetime XP)
  user:{user_id}:level                    int (derived: floor(sqrt(xp / 100)))
  user:{user_id}:badges                   SET of badge_ids
  user:{user_id}:checkin:last             ISO date string (SGT)
  user:{user_id}:checkin:streak           int
  brand:{brand_id}:badges                 HASH {badge_id: badge_json}
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.redis_client import get_redis

router = APIRouter()

# ── Schemas ─────────────────────────────────────────────────────────────


class Badge(BaseModel):
    id: str
    name: str
    description: str = ""
    icon: str = ""           # data URI or asset path
    rarity: str = "common"   # common/rare/epic/legendary
    xp_reward: int = 0


class AwardBadgeRequest(BaseModel):
    user_id: str
    brand_id: str
    badge_id: str


class AwardXPRequest(BaseModel):
    user_id: str
    brand_id: str
    amount: int
    reason: str = ""


class ProgressionResponse(BaseModel):
    user_id: str
    xp: int
    level: int
    xp_to_next_level: int
    badges: list[Badge]
    daily_streak: int
    checked_in_today: bool


class CheckInResponse(BaseModel):
    user_id: str
    new_streak: int
    xp_awarded: int
    bonus_xp: int = 0       # streak milestone bonus
    new_badges: list[str] = []
    already_checked_in: bool = False


# ── Level math (industry standard: quadratic curve like Duolingo) ───────


def xp_to_level(xp: int) -> int:
    """Level n requires n² × 100 XP cumulative. Level 1 at 100, Level 5 at 2500, Level 10 at 10000."""
    if xp < 100:
        return 0
    return int(math.sqrt(xp / 100))


def level_to_xp(level: int) -> int:
    return level * level * 100


def xp_to_next_level(xp: int) -> int:
    return level_to_xp(xp_to_level(xp) + 1) - xp


# ── Daily check-in (SGT, like streak.py) ─────────────────────────────────


def _sgt_today() -> str:
    sgt = timezone(timedelta(hours=8))
    return datetime.now(sgt).strftime("%Y-%m-%d")


def _sgt_yesterday() -> str:
    sgt = timezone(timedelta(hours=8))
    return (datetime.now(sgt) - timedelta(days=1)).strftime("%Y-%m-%d")


# ── Brand Badge Management ───────────────────────────────────────────────


@router.post("/brand/{brand_id}/badges")
async def create_badge(brand_id: str, badge: Badge, r: aioredis.Redis = Depends(get_redis)):
    """Create a badge for a brand."""
    await r.hset(f"brand:{brand_id}:badges", badge.id, badge.model_dump_json())
    return {"ok": True, "badge_id": badge.id}


@router.get("/brand/{brand_id}/badges", response_model=list[Badge])
async def list_brand_badges(brand_id: str, r: aioredis.Redis = Depends(get_redis)):
    """List all badges for a brand."""
    raw = await r.hgetall(f"brand:{brand_id}:badges")
    badges = []
    for badge_json in raw.values():
        try:
            badges.append(Badge(**json.loads(badge_json)))
        except Exception:
            continue
    return badges


# ── Award (called by reward engine / game events) ────────────────────────


@router.post("/award/xp")
async def award_xp(body: AwardXPRequest, r: aioredis.Redis = Depends(get_redis)):
    """Award XP to a user. Returns new level if changed."""
    if body.amount <= 0:
        raise HTTPException(400, detail="amount must be positive")

    key = f"user:{body.user_id}:xp"
    old_xp = int(await r.get(key) or 0)
    new_xp = old_xp + body.amount
    await r.set(key, new_xp)

    old_level = xp_to_level(old_xp)
    new_level = xp_to_level(new_xp)
    level_up = new_level > old_level

    return {
        "user_id": body.user_id,
        "xp_awarded": body.amount,
        "total_xp": new_xp,
        "level": new_level,
        "level_up": level_up,
        "reason": body.reason,
    }


@router.post("/award/badge")
async def award_badge(body: AwardBadgeRequest, r: aioredis.Redis = Depends(get_redis)):
    """Award a badge to a user. Idempotent. Auto-awards XP if badge has xp_reward."""
    badge_json = await r.hget(f"brand:{body.brand_id}:badges", body.badge_id)
    if not badge_json:
        raise HTTPException(404, detail=f"Badge {body.badge_id} not found")

    badge = Badge(**json.loads(badge_json))

    user_badges_key = f"user:{body.user_id}:badges"
    already = await r.sismember(user_badges_key, body.badge_id)
    if already:
        return {"ok": True, "already_awarded": True, "badge_id": body.badge_id}

    await r.sadd(user_badges_key, body.badge_id)

    xp_awarded = 0
    if badge.xp_reward > 0:
        xp_key = f"user:{body.user_id}:xp"
        old_xp = int(await r.get(xp_key) or 0)
        await r.set(xp_key, old_xp + badge.xp_reward)
        xp_awarded = badge.xp_reward

    return {
        "ok": True,
        "badge_id": body.badge_id,
        "badge_name": badge.name,
        "xp_awarded": xp_awarded,
        "rarity": badge.rarity,
    }


# ── User Progression Snapshot ────────────────────────────────────────────


@router.get("/user/{user_id}/progression", response_model=ProgressionResponse)
async def get_progression(
    user_id: str, brand_id: str, r: aioredis.Redis = Depends(get_redis)
):
    """Get user's full progression state: XP, level, badges, streak."""
    xp = int(await r.get(f"user:{user_id}:xp") or 0)
    level = xp_to_level(xp)
    to_next = xp_to_next_level(xp)

    badge_ids = await r.smembers(f"user:{user_id}:badges")
    badges = []
    if badge_ids:
        raw_badges = await r.hmget(f"brand:{brand_id}:badges", *badge_ids)
        for raw in raw_badges:
            if raw:
                try:
                    badges.append(Badge(**json.loads(raw)))
                except Exception:
                    continue

    streak = int(await r.get(f"user:{user_id}:checkin:streak") or 0)
    last_checkin = await r.get(f"user:{user_id}:checkin:last")
    checked_today = (last_checkin == _sgt_today())

    return ProgressionResponse(
        user_id=user_id,
        xp=xp,
        level=level,
        xp_to_next_level=to_next,
        badges=badges,
        daily_streak=streak,
        checked_in_today=checked_today,
    )


# ── Daily Check-in (platformized — separate from streak.py which is brand-streak) ──


@router.post("/checkin", response_model=CheckInResponse)
async def daily_checkin(
    user_id: str, brand_id: str = "", r: aioredis.Redis = Depends(get_redis)
):
    """Daily check-in. Awards XP. Tracks streak. Idempotent per day."""
    today = _sgt_today()
    yesterday = _sgt_yesterday()

    last_key = f"user:{user_id}:checkin:last"
    streak_key = f"user:{user_id}:checkin:streak"
    last = await r.get(last_key)

    if last == today:
        streak = int(await r.get(streak_key) or 0)
        return CheckInResponse(
            user_id=user_id,
            new_streak=streak,
            xp_awarded=0,
            already_checked_in=True,
        )

    # Compute new streak
    if last == yesterday:
        new_streak = int(await r.get(streak_key) or 0) + 1
    else:
        new_streak = 1

    # Award base XP + streak milestone bonus
    base_xp = 10
    bonus = 0
    new_badges: list[str] = []

    # Streak milestones: 7, 30, 100 days
    if new_streak == 7:
        bonus = 50
        new_badges.append("streak_7")
    elif new_streak == 30:
        bonus = 200
        new_badges.append("streak_30")
    elif new_streak == 100:
        bonus = 1000
        new_badges.append("streak_100")
    elif new_streak == 365:
        bonus = 5000
        new_badges.append("streak_365")

    total_xp = base_xp + bonus

    # Persist
    await r.set(last_key, today)
    await r.set(streak_key, new_streak)
    xp_key = f"user:{user_id}:xp"
    old_xp = int(await r.get(xp_key) or 0)
    await r.set(xp_key, old_xp + total_xp)

    # Award milestone badges (if brand defined them)
    if brand_id and new_badges:
        user_badges_key = f"user:{user_id}:badges"
        for bid in new_badges:
            exists = await r.hexists(f"brand:{brand_id}:badges", bid)
            if exists:
                await r.sadd(user_badges_key, bid)

    return CheckInResponse(
        user_id=user_id,
        new_streak=new_streak,
        xp_awarded=base_xp,
        bonus_xp=bonus,
        new_badges=new_badges,
    )


# ── Leaderboard helpers (XP-based, complements existing score leaderboard) ──


@router.get("/leaderboard/xp/{brand_id}")
async def xp_leaderboard(
    brand_id: str, limit: int = 50, r: aioredis.Redis = Depends(get_redis)
):
    """XP leaderboard for a brand. Distinct from score-based leaderboard."""
    # We use a sorted set keyed by brand for efficient ranking
    lb_key = f"brand:{brand_id}:xp_leaderboard"
    entries = await r.zrevrange(lb_key, 0, limit - 1, withscores=True)
    return {
        "brand_id": brand_id,
        "entries": [
            {"user_id": uid, "xp": int(xp), "level": xp_to_level(int(xp))}
            for uid, xp in entries
        ],
    }
