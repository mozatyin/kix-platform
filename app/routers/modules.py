"""KiX Composable Gamification Modules.

Ten production-grade engagement mechanics inspired by the world's best
gamification platforms (Duolingo, Starbucks, Fortnite, Smartico, Centrical,
BARQ, Pokemon, Captain Up). Each module composes the platform primitives
(XP/Level/Badge/Streak/Currency/Items/Quests/Tiers) into a complete feature
and is brand-agnostic.

Redis key scheme:
  brand:{brand_id}:module:{module_name}:config                  JSON config
  brand:{brand_id}:module:{module_name}:{sub_id}                shared/global state
  user:{user_id}:module:{module_name}:{brand_id}[:{sub_id}]     per-user state
  module:{module_name}:{global_id}                              cross-user state

Energy currency reuses the existing wallet key:
  energy:balance:{brand_id}:{user_id}

Modules included (each in its own section below):
  1. RewardRoulette  (BARQ)
  2. League          (Duolingo)
  3. Tier            (Starbucks)
  4. Pass            (Fortnite Battle Pass)
  5. SmartQuests     (Smartico adaptive)
  6. StoryQuest      (Centrical narrative)
  7. LifeSystem      (Duolingo hearts)
  8. Tourney         (Smartico tournament)
  9. Collection      (Pokemon-style)
 10. BadgeWall       (Captain Up showcase)
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.redis_client import get_redis
from app.routers.conditions import (
    _check_and_reserve_module,
    commit_reservation_internal,
    refund_reservation_internal,
)

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _energy_key(brand_id: str, user_id: str) -> str:
    return f"energy:balance:{brand_id}:{user_id}"


async def _spend_energy(
    r: aioredis.Redis, brand_id: str, user_id: str, cost: int
) -> int:
    """Atomically deduct energy. Raises 402 if insufficient. Returns new balance."""
    if cost <= 0:
        return int(await r.get(_energy_key(brand_id, user_id)) or 0)
    key = _energy_key(brand_id, user_id)
    # Use Lua-ish atomic via WATCH/MULTI
    async with r.pipeline(transaction=True) as pipe:
        while True:
            try:
                await pipe.watch(key)
                current = int(await pipe.get(key) or 0)
                if current < cost:
                    await pipe.unwatch()
                    raise HTTPException(402, detail=f"Insufficient energy: have {current}, need {cost}")
                pipe.multi()
                pipe.decrby(key, cost)
                result = await pipe.execute()
                return int(result[0])
            except aioredis.WatchError:
                continue


async def _add_energy(r: aioredis.Redis, brand_id: str, user_id: str, amount: int) -> int:
    if amount <= 0:
        return int(await r.get(_energy_key(brand_id, user_id)) or 0)
    return int(await r.incrby(_energy_key(brand_id, user_id), amount))


async def _get_lifetime_xp(r: aioredis.Redis, user_id: str) -> int:
    return int(await r.get(f"user:{user_id}:xp") or 0)


async def _award_xp(r: aioredis.Redis, user_id: str, amount: int) -> int:
    if amount <= 0:
        return await _get_lifetime_xp(r, user_id)
    return int(await r.incrby(f"user:{user_id}:xp", amount))


def _xp_to_level(xp: int) -> int:
    if xp < 100:
        return 0
    return int(math.sqrt(xp / 100))


def _weighted_choice(items: list[dict[str, Any]], weight_key: str, seed: int | None = None) -> dict[str, Any]:
    """Weighted random pick. Items: list of dicts with a `weight_key` numeric field."""
    if not items:
        raise ValueError("empty items")
    weights = [max(0.0, float(it.get(weight_key, 0))) for it in items]
    total = sum(weights)
    if total <= 0:
        return random.choice(items)
    rng = random.Random(seed) if seed is not None else random
    pick = rng.random() * total
    acc = 0.0
    for it, w in zip(items, weights):
        acc += w
        if pick <= acc:
            return it
    return items[-1]


# =============================================================================
#  MODULE 1 — RewardRoulette  (BARQ-style spin-the-wheel)
# =============================================================================


class WheelSlot(BaseModel):
    label: str
    weight: float = Field(gt=0)
    reward_type: Literal["energy", "xp", "discount", "cashback", "voucher", "nothing"] = "energy"
    reward_value: float = 0


class RouletteConfigBody(BaseModel):
    brand_id: str
    wheel: list[WheelSlot] = Field(min_length=2)


class RouletteSpinBody(BaseModel):
    user_id: str
    brand_id: str
    cost_energy: int = 10


@router.post("/roulette/configure")
async def roulette_configure(body: RouletteConfigBody, r: aioredis.Redis = Depends(get_redis)):
    """Configure the wheel for a brand."""
    key = f"brand:{body.brand_id}:module:roulette:config"
    payload = {"wheel": [s.model_dump() for s in body.wheel]}
    await r.set(key, json.dumps(payload))
    return {"ok": True, "brand_id": body.brand_id, "slots": len(body.wheel)}


@router.get("/roulette/config/{brand_id}")
async def roulette_get_config(brand_id: str, r: aioredis.Redis = Depends(get_redis)):
    raw = await r.get(f"brand:{brand_id}:module:roulette:config")
    if not raw:
        raise HTTPException(404, detail="Roulette not configured")
    return json.loads(raw)


@router.post("/roulette/spin")
async def roulette_spin(body: RouletteSpinBody, r: aioredis.Redis = Depends(get_redis)):
    raw = await r.get(f"brand:{body.brand_id}:module:roulette:config")
    if not raw:
        raise HTTPException(404, detail="Roulette not configured")
    cfg = json.loads(raw)
    wheel = cfg.get("wheel", [])
    if not wheel:
        raise HTTPException(400, detail="Empty wheel")

    # ── Conditions gate ─────────────────────────────────────────────────
    ok, rid_or_blockers, hints = await _check_and_reserve_module(
        r, body.brand_id, body.user_id, "reward_roulette", value_cents=0,
    )
    if not ok:
        raise HTTPException(
            status_code=403,
            detail={"blocked_by": rid_or_blockers, "fix_hints": hints},
        )
    reservation_id = rid_or_blockers

    try:
        # Pay the cost
        await _spend_energy(r, body.brand_id, body.user_id, body.cost_energy)

        # Spin
        seed = int.from_bytes(uuid.uuid4().bytes[:8], "big")
        prize = _weighted_choice(wheel, "weight", seed=seed)

        # Auto-grant common reward types
        granted: dict[str, Any] = {}
        rtype = prize.get("reward_type")
        rval = prize.get("reward_value", 0)
        if rtype == "energy" and rval:
            granted["new_energy_balance"] = await _add_energy(r, body.brand_id, body.user_id, int(rval))
        elif rtype == "xp" and rval:
            granted["new_xp"] = await _award_xp(r, body.user_id, int(rval))

        # Log spin (last 50)
        log_key = f"user:{body.user_id}:module:roulette:{body.brand_id}:log"
        await r.lpush(log_key, json.dumps({"ts": _iso(_utcnow()), "prize": prize, "seed": seed}))
        await r.ltrim(log_key, 0, 49)

        await commit_reservation_internal(r, reservation_id)
        return {
            "prize": {"label": prize["label"], "reward_type": rtype, "reward_value": rval},
            "animation_seed": seed,
            "granted": granted,
        }
    except Exception as exc:
        await refund_reservation_internal(r, reservation_id, reason=str(exc))
        raise


@router.get("/roulette/{user_id}/history")
async def roulette_history(user_id: str, brand_id: str, r: aioredis.Redis = Depends(get_redis)):
    entries = await r.lrange(f"user:{user_id}:module:roulette:{brand_id}:log", 0, -1)
    return {"history": [json.loads(e) for e in entries]}


# =============================================================================
#  MODULE 2 — League  (Duolingo weekly cohort leaderboard with promote/demote)
# =============================================================================

LEAGUE_TIERS = ["bronze", "silver", "gold", "diamond", "obsidian"]
LEAGUE_COHORT_SIZE = 30
LEAGUE_PROMOTE_N = 5
LEAGUE_DEMOTE_N = 5


def _current_week_id() -> str:
    now = _utcnow()
    iso = now.isocalendar()
    return f"{iso[0]}W{iso[1]:02d}"


class LeagueJoinBody(BaseModel):
    user_id: str
    brand_id: str


class LeagueSubmitBody(BaseModel):
    user_id: str
    brand_id: str
    xp_earned: int = Field(gt=0)


class LeagueCycleBody(BaseModel):
    brand_id: str


async def _user_league_tier(r: aioredis.Redis, brand_id: str, user_id: str) -> str:
    tier = await r.get(f"user:{user_id}:module:league:{brand_id}:tier")
    return tier or "bronze"


async def _assign_cohort(r: aioredis.Redis, brand_id: str, user_id: str, tier: str, week: str) -> str:
    """Find an open cohort for {brand,tier,week} or create a new one."""
    open_key = f"brand:{brand_id}:module:league:{week}:{tier}:open"
    cohort_id = await r.get(open_key)
    if cohort_id:
        size = await r.zcard(f"module:league:{cohort_id}:scores")
        if size < LEAGUE_COHORT_SIZE:
            await r.sadd(f"module:league:{cohort_id}:members", user_id)
            await r.zadd(f"module:league:{cohort_id}:scores", {user_id: 0}, nx=True)
            return cohort_id
    # New cohort
    cohort_id = f"{brand_id}:{week}:{tier}:{uuid.uuid4().hex[:8]}"
    await r.set(open_key, cohort_id)
    await r.sadd(f"module:league:{cohort_id}:members", user_id)
    await r.zadd(f"module:league:{cohort_id}:scores", {user_id: 0}, nx=True)
    await r.hset(
        f"module:league:{cohort_id}:meta",
        mapping={"brand_id": brand_id, "week": week, "tier": tier, "created_at": _iso(_utcnow())},
    )
    # Index cohort under brand+week for cycle pass
    await r.sadd(f"brand:{brand_id}:module:league:{week}:cohorts", cohort_id)
    return cohort_id


@router.post("/league/join")
async def league_join(body: LeagueJoinBody, r: aioredis.Redis = Depends(get_redis)):
    week = _current_week_id()
    user_cohort_key = f"user:{body.user_id}:module:league:{body.brand_id}:cohort:{week}"
    existing = await r.get(user_cohort_key)
    if existing:
        return {"cohort_id": existing, "week_id": week, "joined": False}
    tier = await _user_league_tier(r, body.brand_id, body.user_id)
    cohort_id = await _assign_cohort(r, body.brand_id, body.user_id, tier, week)
    await r.set(user_cohort_key, cohort_id, ex=60 * 60 * 24 * 14)
    return {"cohort_id": cohort_id, "week_id": week, "tier": tier, "joined": True}


@router.post("/league/submit")
async def league_submit(body: LeagueSubmitBody, r: aioredis.Redis = Depends(get_redis)):
    week = _current_week_id()
    user_cohort_key = f"user:{body.user_id}:module:league:{body.brand_id}:cohort:{week}"
    cohort_id = await r.get(user_cohort_key)
    if not cohort_id:
        # Auto-join
        tier = await _user_league_tier(r, body.brand_id, body.user_id)
        cohort_id = await _assign_cohort(r, body.brand_id, body.user_id, tier, week)
        await r.set(user_cohort_key, cohort_id, ex=60 * 60 * 24 * 14)
    new_score = await r.zincrby(f"module:league:{cohort_id}:scores", body.xp_earned, body.user_id)
    return {"cohort_id": cohort_id, "week_id": week, "new_cohort_xp": int(new_score)}


@router.get("/league/{user_id}")
async def league_status(user_id: str, brand_id: str, r: aioredis.Redis = Depends(get_redis)):
    week = _current_week_id()
    cohort_id = await r.get(f"user:{user_id}:module:league:{brand_id}:cohort:{week}")
    tier = await _user_league_tier(r, brand_id, user_id)
    if not cohort_id:
        return {
            "cohort_id": None,
            "week_id": week,
            "my_rank": None,
            "top_10": [],
            "league_tier": tier,
            "promote_threshold": LEAGUE_PROMOTE_N,
            "demote_threshold": LEAGUE_DEMOTE_N,
            "joined": False,
        }
    scores_key = f"module:league:{cohort_id}:scores"
    rank = await r.zrevrank(scores_key, user_id)
    top = await r.zrevrange(scores_key, 0, 9, withscores=True)
    total = await r.zcard(scores_key)
    return {
        "cohort_id": cohort_id,
        "week_id": week,
        "my_rank": (rank + 1) if rank is not None else None,
        "cohort_size": total,
        "top_10": [{"user_id": u, "xp": int(s)} for u, s in top],
        "league_tier": tier,
        "promote_threshold": LEAGUE_PROMOTE_N,
        "demote_threshold": LEAGUE_DEMOTE_N,
        "joined": True,
    }


@router.post("/league/cycle")
async def league_cycle(body: LeagueCycleBody, r: aioredis.Redis = Depends(get_redis)):
    """Process current week's cohorts: top N promote, bottom N demote. Idempotent per week."""
    week = _current_week_id()
    cycle_flag = f"brand:{body.brand_id}:module:league:{week}:cycled"
    if await r.get(cycle_flag):
        return {"ok": True, "already_cycled": True, "week_id": week}

    cohort_ids = await r.smembers(f"brand:{body.brand_id}:module:league:{week}:cohorts")
    promoted = 0
    demoted = 0
    for cohort_id in cohort_ids:
        meta = await r.hgetall(f"module:league:{cohort_id}:meta")
        tier = meta.get("tier", "bronze")
        idx = LEAGUE_TIERS.index(tier) if tier in LEAGUE_TIERS else 0
        top = await r.zrevrange(f"module:league:{cohort_id}:scores", 0, LEAGUE_PROMOTE_N - 1)
        bottom = await r.zrange(f"module:league:{cohort_id}:scores", 0, LEAGUE_DEMOTE_N - 1)

        if idx < len(LEAGUE_TIERS) - 1:
            for uid in top:
                await r.set(f"user:{uid}:module:league:{body.brand_id}:tier", LEAGUE_TIERS[idx + 1])
                promoted += 1
        if idx > 0:
            for uid in bottom:
                await r.set(f"user:{uid}:module:league:{body.brand_id}:tier", LEAGUE_TIERS[idx - 1])
                demoted += 1

    await r.set(cycle_flag, "1", ex=60 * 60 * 24 * 14)
    return {"ok": True, "week_id": week, "cohorts": len(cohort_ids), "promoted": promoted, "demoted": demoted}


# =============================================================================
#  MODULE 3 — Tier  (Starbucks-style lifetime loyalty tiers based on XP)
# =============================================================================


class TierDef(BaseModel):
    name: str
    min_xp: int = Field(ge=0)
    perks: list[str] = []
    color: str = "#cccccc"


class TierConfigBody(BaseModel):
    brand_id: str
    tiers: list[TierDef] = Field(min_length=1)


@router.post("/tier/configure")
async def tier_configure(body: TierConfigBody, r: aioredis.Redis = Depends(get_redis)):
    tiers = sorted([t.model_dump() for t in body.tiers], key=lambda x: x["min_xp"])
    await r.set(f"brand:{body.brand_id}:module:tier:config", json.dumps({"tiers": tiers}))
    return {"ok": True, "brand_id": body.brand_id, "tier_count": len(tiers)}


@router.get("/tier/{user_id}")
async def tier_status(user_id: str, brand_id: str, r: aioredis.Redis = Depends(get_redis)):
    raw = await r.get(f"brand:{brand_id}:module:tier:config")
    if not raw:
        raise HTTPException(404, detail="Tier program not configured")
    tiers = json.loads(raw)["tiers"]
    xp = await _get_lifetime_xp(r, user_id)

    current = tiers[0]
    next_tier = None
    for i, t in enumerate(tiers):
        if xp >= t["min_xp"]:
            current = t
            next_tier = tiers[i + 1] if i + 1 < len(tiers) else None

    xp_to_next = (next_tier["min_xp"] - xp) if next_tier else 0

    # Percentile in brand — rough estimate via xp leaderboard sorted set
    lb_key = f"brand:{brand_id}:xp_leaderboard"
    total = await r.zcard(lb_key)
    rank = await r.zrevrank(lb_key, user_id)
    percentile = None
    if total and rank is not None:
        percentile = round(100.0 * (1.0 - (rank / max(total, 1))), 1)

    return {
        "user_id": user_id,
        "xp": xp,
        "current_tier": current,
        "next_tier": next_tier,
        "xp_to_next": xp_to_next,
        "perks_unlocked": current.get("perks", []),
        "percentile_in_brand": percentile,
    }


# =============================================================================
#  MODULE 4 — Pass  (Fortnite-style Battle Pass: 100 levels, free + paid tracks)
# =============================================================================


class PassReward(BaseModel):
    level: int = Field(ge=1)
    type: str  # energy, xp, badge, item, voucher, cosmetic
    value: Any = None
    label: str = ""


class PassConfigBody(BaseModel):
    brand_id: str
    season_id: str
    duration_days: int = 90
    levels: int = 100
    xp_per_level: int = 1000
    rewards_free: list[PassReward] = []
    rewards_paid: list[PassReward] = []
    price_cents: int = 999


class PassBuyBody(BaseModel):
    user_id: str
    brand_id: str
    season_id: str


class PassClaimBody(BaseModel):
    user_id: str
    brand_id: str
    season_id: str
    level: int = Field(ge=1)
    track: Literal["free", "paid"] = "free"


def _pass_cfg_key(brand_id: str, season_id: str) -> str:
    return f"brand:{brand_id}:module:pass:{season_id}:config"


def _pass_user_key(user_id: str, brand_id: str, season_id: str) -> str:
    return f"user:{user_id}:module:pass:{brand_id}:{season_id}"


@router.post("/pass/configure")
async def pass_configure(body: PassConfigBody, r: aioredis.Redis = Depends(get_redis)):
    cfg = body.model_dump()
    cfg["start_at"] = _iso(_utcnow())
    cfg["end_at"] = _iso(_utcnow() + timedelta(days=body.duration_days))
    await r.set(_pass_cfg_key(body.brand_id, body.season_id), json.dumps(cfg))
    return {"ok": True, "season_id": body.season_id, "ends_at": cfg["end_at"]}


@router.post("/pass/buy")
async def pass_buy(body: PassBuyBody, r: aioredis.Redis = Depends(get_redis)):
    cfg_raw = await r.get(_pass_cfg_key(body.brand_id, body.season_id))
    if not cfg_raw:
        raise HTTPException(404, detail="Season not configured")
    cfg = json.loads(cfg_raw)
    price_cents = int(cfg.get("price_cents", 0) or 0)

    # ── Conditions gate (budget = pass price) ───────────────────────────
    ok, rid_or_blockers, hints = await _check_and_reserve_module(
        r, body.brand_id, body.user_id, "battle_pass", value_cents=price_cents,
    )
    if not ok:
        raise HTTPException(
            status_code=403,
            detail={"blocked_by": rid_or_blockers, "fix_hints": hints},
        )
    reservation_id = rid_or_blockers

    try:
        key = _pass_user_key(body.user_id, body.brand_id, body.season_id)
        is_premium = await r.hget(key, "is_premium")
        if is_premium == "1":
            # No-op success — still consume the reservation so frequency
            # counters reflect the call attempt? No: refund, idempotent reentry.
            await refund_reservation_internal(r, reservation_id, reason="already_premium")
            return {"ok": True, "already_premium": True}
        await r.hset(key, mapping={"is_premium": "1", "purchased_at": _iso(_utcnow())})
        await commit_reservation_internal(r, reservation_id)
        return {"ok": True, "is_premium": True}
    except HTTPException:
        await refund_reservation_internal(r, reservation_id, reason="http_error")
        raise
    except Exception as exc:
        await refund_reservation_internal(r, reservation_id, reason=str(exc))
        raise


@router.post("/pass/award-xp")
async def pass_award_xp(body: PassBuyBody, r: aioredis.Redis = Depends(get_redis), amount: int = 100):
    """Add XP toward a pass season (does not affect lifetime XP — separate season track)."""
    cfg_raw = await r.get(_pass_cfg_key(body.brand_id, body.season_id))
    if not cfg_raw:
        raise HTTPException(404, detail="Season not configured")
    key = _pass_user_key(body.user_id, body.brand_id, body.season_id)
    new_xp = await r.hincrby(key, "season_xp", amount)
    return {"season_xp": int(new_xp)}


@router.post("/pass/claim")
async def pass_claim(body: PassClaimBody, r: aioredis.Redis = Depends(get_redis)):
    cfg_raw = await r.get(_pass_cfg_key(body.brand_id, body.season_id))
    if not cfg_raw:
        raise HTTPException(404, detail="Season not configured")
    cfg = json.loads(cfg_raw)
    user_key = _pass_user_key(body.user_id, body.brand_id, body.season_id)
    state = await r.hgetall(user_key)
    season_xp = int(state.get("season_xp", 0))
    current_level = season_xp // max(cfg.get("xp_per_level", 1000), 1)
    if body.level > current_level:
        raise HTTPException(400, detail=f"Level {body.level} not yet reached (current {current_level})")
    if body.track == "paid" and state.get("is_premium") != "1":
        raise HTTPException(403, detail="Premium pass required for paid track")

    claim_field = f"claimed:{body.track}:{body.level}"
    if state.get(claim_field) == "1":
        return {"ok": True, "already_claimed": True}

    rewards = cfg.get("rewards_paid" if body.track == "paid" else "rewards_free", [])
    reward = next((r_ for r_ in rewards if int(r_.get("level", 0)) == body.level), None)
    if not reward:
        raise HTTPException(404, detail="No reward at that level for this track")

    # Apply reward
    granted: dict[str, Any] = {"reward": reward}
    rtype = reward.get("type")
    rval = reward.get("value")
    if rtype == "energy" and rval:
        granted["new_energy_balance"] = await _add_energy(r, body.brand_id, body.user_id, int(rval))
    elif rtype == "xp" and rval:
        granted["new_xp"] = await _award_xp(r, body.user_id, int(rval))
    elif rtype == "badge" and rval:
        await r.sadd(f"user:{body.user_id}:badges", str(rval))
        granted["badge_granted"] = str(rval)

    await r.hset(user_key, claim_field, "1")
    return {"ok": True, **granted}


@router.get("/pass/{user_id}")
async def pass_status(
    user_id: str, brand_id: str, season_id: str, r: aioredis.Redis = Depends(get_redis)
):
    cfg_raw = await r.get(_pass_cfg_key(brand_id, season_id))
    if not cfg_raw:
        raise HTTPException(404, detail="Season not configured")
    cfg = json.loads(cfg_raw)
    state = await r.hgetall(_pass_user_key(user_id, brand_id, season_id))
    season_xp = int(state.get("season_xp", 0))
    xp_per = max(cfg.get("xp_per_level", 1000), 1)
    current_level = min(season_xp // xp_per, cfg.get("levels", 100))
    xp_to_next = xp_per - (season_xp % xp_per) if current_level < cfg.get("levels", 100) else 0
    is_premium = state.get("is_premium") == "1"

    def _unclaimed(track: str, rewards: list[dict]):
        out = []
        for rw in rewards:
            lvl = int(rw.get("level", 0))
            if lvl <= current_level and state.get(f"claimed:{track}:{lvl}") != "1":
                out.append(rw)
        return out

    return {
        "season_id": season_id,
        "current_level": current_level,
        "season_xp": season_xp,
        "xp_to_next": xp_to_next,
        "is_premium": is_premium,
        "ends_at": cfg.get("end_at"),
        "free_unclaimed": _unclaimed("free", cfg.get("rewards_free", [])),
        "paid_unclaimed": _unclaimed("paid", cfg.get("rewards_paid", [])) if is_premium else [],
    }


# =============================================================================
#  MODULE 5 — SmartQuests  (Smartico-style adaptive-difficulty quests)
# =============================================================================


class SmartQuestGenerateBody(BaseModel):
    user_id: str
    brand_id: str


def _difficulty_for_xp(xp: int) -> str:
    if xp < 500:
        return "beginner"
    if xp < 5_000:
        return "easy"
    if xp < 25_000:
        return "medium"
    if xp < 100_000:
        return "hard"
    return "expert"


_QUEST_TEMPLATES = {
    "beginner": [
        {"id": "play_1_game", "title": "Play 1 game", "target": 1, "metric": "games_played", "reward_xp": 50, "reward_energy": 10},
        {"id": "earn_50_xp", "title": "Earn 50 XP", "target": 50, "metric": "xp_earned", "reward_xp": 25, "reward_energy": 5},
        {"id": "checkin_today", "title": "Check in today", "target": 1, "metric": "checkin", "reward_xp": 30, "reward_energy": 5},
    ],
    "easy": [
        {"id": "play_3_games", "title": "Play 3 games", "target": 3, "metric": "games_played", "reward_xp": 100, "reward_energy": 15},
        {"id": "earn_200_xp", "title": "Earn 200 XP", "target": 200, "metric": "xp_earned", "reward_xp": 75, "reward_energy": 10},
        {"id": "win_1_game", "title": "Win 1 game", "target": 1, "metric": "games_won", "reward_xp": 150, "reward_energy": 20},
    ],
    "medium": [
        {"id": "play_5_games", "title": "Play 5 games", "target": 5, "metric": "games_played", "reward_xp": 200, "reward_energy": 25},
        {"id": "earn_500_xp", "title": "Earn 500 XP", "target": 500, "metric": "xp_earned", "reward_xp": 150, "reward_energy": 20},
        {"id": "win_3_games", "title": "Win 3 games", "target": 3, "metric": "games_won", "reward_xp": 300, "reward_energy": 40},
    ],
    "hard": [
        {"id": "play_10_games", "title": "Play 10 games", "target": 10, "metric": "games_played", "reward_xp": 500, "reward_energy": 50},
        {"id": "earn_1500_xp", "title": "Earn 1500 XP", "target": 1500, "metric": "xp_earned", "reward_xp": 400, "reward_energy": 40},
        {"id": "win_7_games", "title": "Win 7 games", "target": 7, "metric": "games_won", "reward_xp": 800, "reward_energy": 80},
    ],
    "expert": [
        {"id": "play_20_games", "title": "Play 20 games", "target": 20, "metric": "games_played", "reward_xp": 1200, "reward_energy": 100},
        {"id": "earn_5000_xp", "title": "Earn 5000 XP", "target": 5000, "metric": "xp_earned", "reward_xp": 1000, "reward_energy": 80},
        {"id": "win_15_games", "title": "Win 15 games", "target": 15, "metric": "games_won", "reward_xp": 2000, "reward_energy": 150},
    ],
}


def _smartquests_key(user_id: str, brand_id: str) -> str:
    return f"user:{user_id}:module:smartquests:{brand_id}"


@router.post("/smartquests/generate")
async def smartquests_generate(body: SmartQuestGenerateBody, r: aioredis.Redis = Depends(get_redis)):
    xp = await _get_lifetime_xp(r, body.user_id)
    difficulty = _difficulty_for_xp(xp)
    quests = [dict(q, progress=0, claimed=False) for q in _QUEST_TEMPLATES[difficulty]]
    payload = {
        "difficulty": difficulty,
        "generated_at": _iso(_utcnow()),
        "expires_at": _iso(_utcnow() + timedelta(days=1)),
        "quests": quests,
    }
    await r.set(_smartquests_key(body.user_id, body.brand_id), json.dumps(payload), ex=60 * 60 * 24 * 2)
    return payload


@router.post("/smartquests/progress")
async def smartquests_progress(
    user_id: str, brand_id: str, metric: str, amount: int = 1,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await r.get(_smartquests_key(user_id, brand_id))
    if not raw:
        raise HTTPException(404, detail="No active quests — call /smartquests/generate first")
    payload = json.loads(raw)
    updated = []
    for q in payload["quests"]:
        if q["metric"] == metric and not q.get("claimed"):
            q["progress"] = min(q["target"], q.get("progress", 0) + amount)
            if q["progress"] >= q["target"] and not q.get("completed"):
                q["completed"] = True
                # Grant rewards once
                await _award_xp(r, user_id, int(q.get("reward_xp", 0)))
                await _add_energy(r, brand_id, user_id, int(q.get("reward_energy", 0)))
                q["claimed"] = True
            updated.append(q)
    await r.set(_smartquests_key(user_id, brand_id), json.dumps(payload), ex=60 * 60 * 24 * 2)
    return {"updated": updated, "all": payload["quests"]}


@router.get("/smartquests/{user_id}")
async def smartquests_status(user_id: str, brand_id: str, r: aioredis.Redis = Depends(get_redis)):
    raw = await r.get(_smartquests_key(user_id, brand_id))
    if not raw:
        # Auto-generate
        return await smartquests_generate(SmartQuestGenerateBody(user_id=user_id, brand_id=brand_id), r)
    return json.loads(raw)


# =============================================================================
#  MODULE 6 — StoryQuest  (Centrical narrative-driven progression)
# =============================================================================


class StoryChapter(BaseModel):
    title: str
    narrative: str
    completion_criteria: dict[str, Any]  # e.g. {"metric": "games_played", "target": 3}
    next_action: str = ""
    rewards: dict[str, Any] = {}  # e.g. {"xp": 200, "energy": 30, "badge": "chapter1_done"}


class StoryConfigBody(BaseModel):
    brand_id: str
    story_id: str = "main"
    title: str = "Your Journey"
    chapters: list[StoryChapter] = Field(min_length=1)


class StoryAdvanceBody(BaseModel):
    user_id: str
    brand_id: str
    story_id: str = "main"
    action_completed: str  # corresponds to completion_criteria.metric event
    amount: int = 1


def _story_cfg_key(brand_id: str, story_id: str) -> str:
    return f"brand:{brand_id}:module:storyquest:{story_id}:config"


def _story_user_key(user_id: str, brand_id: str, story_id: str) -> str:
    return f"user:{user_id}:module:storyquest:{brand_id}:{story_id}"


@router.post("/storyquest/configure")
async def storyquest_configure(body: StoryConfigBody, r: aioredis.Redis = Depends(get_redis)):
    cfg = {"title": body.title, "chapters": [c.model_dump() for c in body.chapters]}
    await r.set(_story_cfg_key(body.brand_id, body.story_id), json.dumps(cfg))
    return {"ok": True, "story_id": body.story_id, "chapter_count": len(body.chapters)}


@router.post("/storyquest/advance")
async def storyquest_advance(body: StoryAdvanceBody, r: aioredis.Redis = Depends(get_redis)):
    cfg_raw = await r.get(_story_cfg_key(body.brand_id, body.story_id))
    if not cfg_raw:
        raise HTTPException(404, detail="Story not configured")
    cfg = json.loads(cfg_raw)
    user_key = _story_user_key(body.user_id, body.brand_id, body.story_id)
    state = await r.hgetall(user_key)
    chapter_idx = int(state.get("chapter_idx", 0))
    progress = int(state.get("progress", 0))

    if chapter_idx >= len(cfg["chapters"]):
        return {"completed": True, "chapter": chapter_idx, "total": len(cfg["chapters"])}

    chapter = cfg["chapters"][chapter_idx]
    crit = chapter.get("completion_criteria", {})
    if crit.get("metric") != body.action_completed:
        return {
            "advanced": False,
            "reason": "action doesn't match current chapter criteria",
            "current_chapter": chapter_idx,
            "expected_metric": crit.get("metric"),
        }
    progress += body.amount
    target = int(crit.get("target", 1))
    chapter_completed = False
    granted: dict[str, Any] = {}
    if progress >= target:
        # Complete chapter
        rewards = chapter.get("rewards", {})
        if rewards.get("xp"):
            granted["xp"] = await _award_xp(r, body.user_id, int(rewards["xp"]))
        if rewards.get("energy"):
            granted["energy"] = await _add_energy(r, body.brand_id, body.user_id, int(rewards["energy"]))
        if rewards.get("badge"):
            await r.sadd(f"user:{body.user_id}:badges", str(rewards["badge"]))
            granted["badge"] = str(rewards["badge"])
        chapter_idx += 1
        progress = 0
        chapter_completed = True
    await r.hset(user_key, mapping={"chapter_idx": chapter_idx, "progress": progress})
    return {
        "advanced": True,
        "chapter_completed": chapter_completed,
        "current_chapter": chapter_idx,
        "progress": progress,
        "granted": granted,
    }


@router.get("/storyquest/{user_id}")
async def storyquest_status(
    user_id: str, brand_id: str, story_id: str = "main",
    r: aioredis.Redis = Depends(get_redis),
):
    cfg_raw = await r.get(_story_cfg_key(brand_id, story_id))
    if not cfg_raw:
        raise HTTPException(404, detail="Story not configured")
    cfg = json.loads(cfg_raw)
    state = await r.hgetall(_story_user_key(user_id, brand_id, story_id))
    chapter_idx = int(state.get("chapter_idx", 0))
    progress = int(state.get("progress", 0))
    total = len(cfg["chapters"])
    if chapter_idx >= total:
        return {
            "story_id": story_id,
            "title": cfg.get("title"),
            "current_chapter": total,
            "total_chapters": total,
            "completed": True,
            "progress_pct": 100.0,
        }
    chapter = cfg["chapters"][chapter_idx]
    target = max(int(chapter.get("completion_criteria", {}).get("target", 1)), 1)
    chapter_pct = 100.0 * progress / target
    overall_pct = round((chapter_idx + progress / target) / total * 100, 1)
    return {
        "story_id": story_id,
        "title": cfg.get("title"),
        "current_chapter": chapter_idx + 1,
        "total_chapters": total,
        "chapter_title": chapter.get("title"),
        "narrative_text": chapter.get("narrative"),
        "next_action": chapter.get("next_action"),
        "chapter_progress_pct": round(chapter_pct, 1),
        "progress_pct": overall_pct,
        "completed": False,
    }


# =============================================================================
#  MODULE 7 — LifeSystem  (Duolingo hearts: limited attempts with regen)
# =============================================================================


class LivesConfigBody(BaseModel):
    brand_id: str
    max_lives: int = 5
    regen_minutes: int = 30
    refill_cost_currency: Literal["energy"] = "energy"
    refill_cost: int = 20


class LivesLoseBody(BaseModel):
    user_id: str
    brand_id: str
    count: int = 1


class LivesRefillBody(BaseModel):
    user_id: str
    brand_id: str
    source: Literal["purchase", "ad_view", "invite"] = "purchase"


def _lives_cfg_key(brand_id: str) -> str:
    return f"brand:{brand_id}:module:lives:config"


def _lives_user_key(user_id: str, brand_id: str) -> str:
    return f"user:{user_id}:module:lives:{brand_id}"


async def _load_lives_config(r: aioredis.Redis, brand_id: str) -> dict:
    raw = await r.get(_lives_cfg_key(brand_id))
    if not raw:
        # Sensible defaults
        return {"max_lives": 5, "regen_minutes": 30, "refill_cost_currency": "energy", "refill_cost": 20}
    return json.loads(raw)


async def _compute_lives(r: aioredis.Redis, user_id: str, brand_id: str) -> tuple[int, datetime | None, dict]:
    cfg = await _load_lives_config(r, brand_id)
    max_lives = int(cfg["max_lives"])
    regen_sec = int(cfg["regen_minutes"]) * 60
    state = await r.hgetall(_lives_user_key(user_id, brand_id))
    now = _utcnow()
    if not state:
        return max_lives, None, cfg
    lives = int(state.get("lives", max_lives))
    last_regen = state.get("last_regen_at")
    if not last_regen:
        return lives, None, cfg
    last_dt = datetime.fromisoformat(last_regen)
    elapsed = (now - last_dt).total_seconds()
    regen_count = int(elapsed // regen_sec)
    if regen_count > 0 and lives < max_lives:
        lives = min(max_lives, lives + regen_count)
        last_dt = last_dt + timedelta(seconds=regen_count * regen_sec)
        await r.hset(
            _lives_user_key(user_id, brand_id),
            mapping={"lives": lives, "last_regen_at": _iso(last_dt)},
        )
    next_regen = (last_dt + timedelta(seconds=regen_sec)) if lives < max_lives else None
    return lives, next_regen, cfg


@router.post("/lives/configure")
async def lives_configure(body: LivesConfigBody, r: aioredis.Redis = Depends(get_redis)):
    await r.set(_lives_cfg_key(body.brand_id), json.dumps(body.model_dump()))
    return {"ok": True, "brand_id": body.brand_id}


@router.get("/lives/{user_id}")
async def lives_status(user_id: str, brand_id: str, r: aioredis.Redis = Depends(get_redis)):
    lives, next_regen, cfg = await _compute_lives(r, user_id, brand_id)
    return {
        "user_id": user_id,
        "current_lives": lives,
        "max_lives": cfg["max_lives"],
        "next_regen_at": _iso(next_regen) if next_regen else None,
        "regen_minutes": cfg["regen_minutes"],
    }


@router.post("/lives/lose")
async def lives_lose(body: LivesLoseBody, r: aioredis.Redis = Depends(get_redis)):
    lives, _, cfg = await _compute_lives(r, body.user_id, body.brand_id)
    new_lives = max(0, lives - body.count)
    # If we dropped from full -> partial, start the regen clock now.
    key = _lives_user_key(body.user_id, body.brand_id)
    state = await r.hgetall(key)
    last_regen = state.get("last_regen_at") if lives < cfg["max_lives"] else _iso(_utcnow())
    if not last_regen:
        last_regen = _iso(_utcnow())
    await r.hset(key, mapping={"lives": new_lives, "last_regen_at": last_regen})
    return {"current_lives": new_lives, "max_lives": cfg["max_lives"]}


@router.post("/lives/refill")
async def lives_refill(body: LivesRefillBody, r: aioredis.Redis = Depends(get_redis)):
    cfg = await _load_lives_config(r, body.brand_id)
    max_lives = int(cfg["max_lives"])
    cost = int(cfg.get("refill_cost", 20))
    if body.source == "purchase":
        await _spend_energy(r, body.brand_id, body.user_id, cost)
    elif body.source == "ad_view":
        # Daily cap of 3 ad refills
        cap_key = f"user:{body.user_id}:module:lives:{body.brand_id}:adcap:{_utcnow().strftime('%Y-%m-%d')}"
        used = int(await r.get(cap_key) or 0)
        if used >= 3:
            raise HTTPException(429, detail="Daily ad refill cap reached")
        await r.incr(cap_key)
        await r.expire(cap_key, 60 * 60 * 30)
    elif body.source == "invite":
        # One-shot per friend invite — caller should ensure the invite was real.
        pass
    await r.hset(
        _lives_user_key(body.user_id, body.brand_id),
        mapping={"lives": max_lives, "last_regen_at": _iso(_utcnow())},
    )
    return {"current_lives": max_lives, "max_lives": max_lives, "source": body.source}


# =============================================================================
#  MODULE 8 — Tourney  (Smartico-style time-bound tournament with prize pool)
# =============================================================================


class TourneyPrize(BaseModel):
    rank: int  # 1, 2, 3, ...
    type: str  # energy/xp/voucher/badge
    value: Any


class TourneyCreateBody(BaseModel):
    brand_id: str
    name: str
    start: datetime
    end: datetime
    entry_cost_energy: int = 0
    prize_pool: list[TourneyPrize] = Field(min_length=1)


class TourneyJoinBody(BaseModel):
    user_id: str


class TourneySubmitBody(BaseModel):
    user_id: str
    score: int


def _tourney_key(tid: str) -> str:
    return f"module:tourney:{tid}"


@router.post("/tourney/create")
async def tourney_create(body: TourneyCreateBody, r: aioredis.Redis = Depends(get_redis)):
    tid = uuid.uuid4().hex[:12]
    cfg = body.model_dump()
    cfg["id"] = tid
    cfg["start"] = _iso(body.start)
    cfg["end"] = _iso(body.end)
    cfg["status"] = "active"
    cfg["created_at"] = _iso(_utcnow())
    await r.set(f"{_tourney_key(tid)}:config", json.dumps(cfg))
    await r.sadd(f"brand:{body.brand_id}:module:tourney:list", tid)
    return {"tourney_id": tid, **cfg}


@router.get("/tourney/{tourney_id}")
async def tourney_status(tourney_id: str, r: aioredis.Redis = Depends(get_redis)):
    raw = await r.get(f"{_tourney_key(tourney_id)}:config")
    if not raw:
        raise HTTPException(404, detail="Tournament not found")
    cfg = json.loads(raw)
    participants = await r.scard(f"{_tourney_key(tourney_id)}:participants")
    lb = await r.zrevrange(f"{_tourney_key(tourney_id)}:scores", 0, 49, withscores=True)
    end_dt = datetime.fromisoformat(cfg["end"])
    seconds_left = max(0, int((end_dt - _utcnow()).total_seconds()))
    return {
        "tourney_id": tourney_id,
        "name": cfg.get("name"),
        "brand_id": cfg.get("brand_id"),
        "status": cfg.get("status"),
        "starts_at": cfg.get("start"),
        "ends_at": cfg.get("end"),
        "participants_count": participants,
        "leaderboard": [{"user_id": u, "score": int(s)} for u, s in lb],
        "time_left_seconds": seconds_left,
        "prizes": cfg.get("prize_pool", []),
        "entry_cost_energy": cfg.get("entry_cost_energy", 0),
    }


@router.post("/tourney/{tourney_id}/join")
async def tourney_join(tourney_id: str, body: TourneyJoinBody, r: aioredis.Redis = Depends(get_redis)):
    raw = await r.get(f"{_tourney_key(tourney_id)}:config")
    if not raw:
        raise HTTPException(404, detail="Tournament not found")
    cfg = json.loads(raw)
    if cfg.get("status") != "active":
        raise HTTPException(400, detail=f"Tournament is {cfg.get('status')}")
    if datetime.fromisoformat(cfg["end"]) < _utcnow():
        raise HTTPException(400, detail="Tournament has ended")
    members_key = f"{_tourney_key(tourney_id)}:participants"
    if await r.sismember(members_key, body.user_id):
        return {"ok": True, "already_joined": True}

    # ── Conditions gate ─────────────────────────────────────────────────
    ok, rid_or_blockers, hints = await _check_and_reserve_module(
        r, cfg["brand_id"], body.user_id, "tourney", value_cents=0,
    )
    if not ok:
        raise HTTPException(
            status_code=403,
            detail={"blocked_by": rid_or_blockers, "fix_hints": hints},
        )
    reservation_id = rid_or_blockers

    try:
        cost = int(cfg.get("entry_cost_energy", 0))
        if cost > 0:
            await _spend_energy(r, cfg["brand_id"], body.user_id, cost)
        await r.sadd(members_key, body.user_id)
        await r.zadd(f"{_tourney_key(tourney_id)}:scores", {body.user_id: 0}, nx=True)
        await commit_reservation_internal(r, reservation_id)
        return {"ok": True, "tourney_id": tourney_id, "entry_cost_paid": cost}
    except Exception as exc:
        await refund_reservation_internal(r, reservation_id, reason=str(exc))
        raise


@router.post("/tourney/{tourney_id}/submit")
async def tourney_submit(tourney_id: str, body: TourneySubmitBody, r: aioredis.Redis = Depends(get_redis)):
    raw = await r.get(f"{_tourney_key(tourney_id)}:config")
    if not raw:
        raise HTTPException(404, detail="Tournament not found")
    cfg = json.loads(raw)
    if datetime.fromisoformat(cfg["end"]) < _utcnow():
        raise HTTPException(400, detail="Tournament has ended")
    members_key = f"{_tourney_key(tourney_id)}:participants"
    if not await r.sismember(members_key, body.user_id):
        raise HTTPException(403, detail="Not joined this tournament")
    scores_key = f"{_tourney_key(tourney_id)}:scores"
    # Use ZADD GT semantics — keep best score
    current = await r.zscore(scores_key, body.user_id)
    if current is None or body.score > current:
        await r.zadd(scores_key, {body.user_id: body.score})
        new_best = body.score
    else:
        new_best = int(current)
    rank = await r.zrevrank(scores_key, body.user_id)
    return {
        "ok": True,
        "best_score": new_best,
        "current_rank": (rank + 1) if rank is not None else None,
    }


@router.post("/tourney/{tourney_id}/settle")
async def tourney_settle(tourney_id: str, r: aioredis.Redis = Depends(get_redis)):
    raw = await r.get(f"{_tourney_key(tourney_id)}:config")
    if not raw:
        raise HTTPException(404, detail="Tournament not found")
    cfg = json.loads(raw)
    settled_flag = f"{_tourney_key(tourney_id)}:settled"
    if await r.get(settled_flag):
        return {"ok": True, "already_settled": True}
    prizes = sorted(cfg.get("prize_pool", []), key=lambda p: p["rank"])
    top = await r.zrevrange(f"{_tourney_key(tourney_id)}:scores", 0, len(prizes) - 1, withscores=True)
    awards = []
    for prize, (uid, score) in zip(prizes, top):
        rtype = prize.get("type")
        rval = prize.get("value")
        if rtype == "energy" and rval:
            await _add_energy(r, cfg["brand_id"], uid, int(rval))
        elif rtype == "xp" and rval:
            await _award_xp(r, uid, int(rval))
        elif rtype == "badge" and rval:
            await r.sadd(f"user:{uid}:badges", str(rval))
        awards.append({"rank": prize["rank"], "user_id": uid, "score": int(score), "prize": prize})
    cfg["status"] = "settled"
    cfg["settled_at"] = _iso(_utcnow())
    await r.set(f"{_tourney_key(tourney_id)}:config", json.dumps(cfg))
    await r.set(settled_flag, "1")
    return {"ok": True, "tourney_id": tourney_id, "awards": awards}


# =============================================================================
#  MODULE 9 — Collection  (Pokemon-style gacha + completion bonus)
# =============================================================================


class CollectionItem(BaseModel):
    id: str
    name: str
    image: str = ""
    rarity: Literal["common", "uncommon", "rare", "epic", "legendary"] = "common"
    drop_weight: float = Field(gt=0)


class CollectionConfigBody(BaseModel):
    brand_id: str
    collection_id: str
    name: str = ""
    items: list[CollectionItem] = Field(min_length=1)
    completion_reward: dict[str, Any] = {}  # e.g. {"xp": 5000, "badge": "collector", "energy": 200}


class CollectionDrawBody(BaseModel):
    user_id: str
    brand_id: str
    collection_id: str
    cost_energy: int = 25


def _col_cfg_key(brand_id: str, collection_id: str) -> str:
    return f"brand:{brand_id}:module:collection:{collection_id}:config"


def _col_user_key(user_id: str, brand_id: str, collection_id: str) -> str:
    return f"user:{user_id}:module:collection:{brand_id}:{collection_id}"


@router.post("/collection/configure")
async def collection_configure(body: CollectionConfigBody, r: aioredis.Redis = Depends(get_redis)):
    cfg = body.model_dump()
    await r.set(_col_cfg_key(body.brand_id, body.collection_id), json.dumps(cfg))
    return {"ok": True, "collection_id": body.collection_id, "item_count": len(body.items)}


@router.post("/collection/draw")
async def collection_draw(body: CollectionDrawBody, r: aioredis.Redis = Depends(get_redis)):
    raw = await r.get(_col_cfg_key(body.brand_id, body.collection_id))
    if not raw:
        raise HTTPException(404, detail="Collection not configured")
    cfg = json.loads(raw)
    items = cfg["items"]

    # ── Conditions gate ─────────────────────────────────────────────────
    ok, rid_or_blockers, hints = await _check_and_reserve_module(
        r, body.brand_id, body.user_id, "collection", value_cents=0,
    )
    if not ok:
        raise HTTPException(
            status_code=403,
            detail={"blocked_by": rid_or_blockers, "fix_hints": hints},
        )
    reservation_id = rid_or_blockers

    try:
        await _spend_energy(r, body.brand_id, body.user_id, body.cost_energy)
        seed = int.from_bytes(uuid.uuid4().bytes[:8], "big")
        picked = _weighted_choice(items, "drop_weight", seed=seed)

        owned_key = f"{_col_user_key(body.user_id, body.brand_id, body.collection_id)}:owned"
        is_new = await r.sadd(owned_key, picked["id"]) == 1
        # Track duplicate counts
        await r.hincrby(f"{_col_user_key(body.user_id, body.brand_id, body.collection_id)}:counts", picked["id"], 1)

        completion_granted: dict[str, Any] = {}
        if is_new:
            owned_count = await r.scard(owned_key)
            if owned_count >= len(items):
                # 100% completion — auto-grant the big reward, idempotent
                done_flag = f"{_col_user_key(body.user_id, body.brand_id, body.collection_id)}:completed"
                if not await r.get(done_flag):
                    reward = cfg.get("completion_reward", {})
                    if reward.get("xp"):
                        completion_granted["xp"] = await _award_xp(r, body.user_id, int(reward["xp"]))
                    if reward.get("energy"):
                        completion_granted["energy"] = await _add_energy(
                            r, body.brand_id, body.user_id, int(reward["energy"])
                        )
                    if reward.get("badge"):
                        await r.sadd(f"user:{body.user_id}:badges", str(reward["badge"]))
                        completion_granted["badge"] = str(reward["badge"])
                    await r.set(done_flag, "1")
                    completion_granted["completion_unlocked"] = True

        await commit_reservation_internal(r, reservation_id)
        return {
            "item": picked,
            "is_new": is_new,
            "animation_seed": seed,
            "completion_granted": completion_granted,
        }
    except Exception as exc:
        await refund_reservation_internal(r, reservation_id, reason=str(exc))
        raise


@router.get("/collection/{user_id}")
async def collection_status(
    user_id: str, brand_id: str, collection_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await r.get(_col_cfg_key(brand_id, collection_id))
    if not raw:
        raise HTTPException(404, detail="Collection not configured")
    cfg = json.loads(raw)
    items = cfg["items"]
    owned_set = await r.smembers(f"{_col_user_key(user_id, brand_id, collection_id)}:owned")
    counts = await r.hgetall(f"{_col_user_key(user_id, brand_id, collection_id)}:counts")
    owned = [dict(it, count=int(counts.get(it["id"], 0))) for it in items if it["id"] in owned_set]
    not_owned = [it for it in items if it["id"] not in owned_set]
    pct = round(100.0 * len(owned) / max(len(items), 1), 1)
    return {
        "collection_id": collection_id,
        "name": cfg.get("name"),
        "owned": owned,
        "not_owned": not_owned,
        "total_items": len(items),
        "owned_count": len(owned),
        "completion_pct": pct,
        "completed": pct >= 100.0,
    }


# =============================================================================
#  MODULE 10 — BadgeWall  (Captain Up visual showcase + rarity stats)
# =============================================================================

RARITY_ORDER = {"common": 0, "uncommon": 1, "rare": 2, "epic": 3, "legendary": 4}


class BadgeWallConfigBody(BaseModel):
    brand_id: str
    layout: Literal["grid", "timeline", "trophy_case"] = "grid"


class BadgeWallShowcaseBody(BaseModel):
    badge_ids: list[str] = Field(min_length=0, max_length=3)


def _bw_cfg_key(brand_id: str) -> str:
    return f"brand:{brand_id}:module:badgewall:config"


def _bw_showcase_key(user_id: str, brand_id: str) -> str:
    return f"user:{user_id}:module:badgewall:{brand_id}:showcase"


@router.post("/badgewall/configure")
async def badgewall_configure(body: BadgeWallConfigBody, r: aioredis.Redis = Depends(get_redis)):
    await r.set(_bw_cfg_key(body.brand_id), json.dumps({"layout": body.layout}))
    return {"ok": True, "layout": body.layout}


@router.get("/badgewall/{user_id}")
async def badgewall_status(user_id: str, brand_id: str, r: aioredis.Redis = Depends(get_redis)):
    cfg_raw = await r.get(_bw_cfg_key(brand_id))
    layout = json.loads(cfg_raw)["layout"] if cfg_raw else "grid"

    badge_ids = await r.smembers(f"user:{user_id}:badges")
    badges: list[dict] = []
    if badge_ids:
        raw_badges = await r.hmget(f"brand:{brand_id}:badges", *badge_ids)
        for raw in raw_badges:
            if not raw:
                continue
            try:
                badges.append(json.loads(raw))
            except Exception:
                continue

    rarity_counts: dict[str, int] = {k: 0 for k in RARITY_ORDER}
    for b in badges:
        rar = b.get("rarity", "common")
        rarity_counts[rar] = rarity_counts.get(rar, 0) + 1

    showcase_ids = await r.lrange(_bw_showcase_key(user_id, brand_id), 0, -1)
    showcase = [b for b in badges if b.get("id") in showcase_ids]
    if not showcase:
        # Default — top 3 by rarity
        showcase = sorted(
            badges,
            key=lambda b: RARITY_ORDER.get(b.get("rarity", "common"), 0),
            reverse=True,
        )[:3]

    # Total badges defined for the brand (used for completion %)
    total_brand_badges = await r.hlen(f"brand:{brand_id}:badges")

    return {
        "user_id": user_id,
        "layout": layout,
        "badges_earned": badges,
        "earned_count": len(badges),
        "total_brand_badges": total_brand_badges,
        "completion_pct": round(100.0 * len(badges) / max(total_brand_badges, 1), 1),
        "rarity_breakdown": rarity_counts,
        "showcase_top_3": showcase,
    }


@router.post("/badgewall/{user_id}/showcase")
async def badgewall_set_showcase(
    user_id: str, brand_id: str, body: BadgeWallShowcaseBody,
    r: aioredis.Redis = Depends(get_redis),
):
    # Verify user actually owns these badges
    owned = await r.smembers(f"user:{user_id}:badges")
    invalid = [bid for bid in body.badge_ids if bid not in owned]
    if invalid:
        raise HTTPException(400, detail=f"User does not own these badges: {invalid}")
    key = _bw_showcase_key(user_id, brand_id)
    await r.delete(key)
    if body.badge_ids:
        await r.rpush(key, *body.badge_ids)
    return {"ok": True, "showcase": body.badge_ids}
