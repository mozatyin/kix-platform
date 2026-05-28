"""Cooperative multiplayer router — 4 group mechanics.

Modules
───────
  1. CooperativeQuest — N users contribute toward a shared goal; on
     completion each member receives reward_per_member.
  2. GroupRaid        — N users team up to drain a boss HP pool by
     submitting damage. Atomic DECR on hp; on hp<=0 reward all.
  3. SquadMultiplier  — Friends squad up; play() returns a score boosted
     by squad size (configurable multiplier_per_member).
  4. Territory        — Pokemon-gym style FCFS claim; highest attack_score
     wins / holds. Owner gets a passive bonus computed on next interaction.

All state in Redis, brand-isolated. Reward distribution reuses the same
key conventions as primitives/p2p:
  energy:balance:{brand_id}:{user_id}                                INT
  currency:{name}:{user_id}:{brand_id}                               INT
  user:{user_id}:inventory:{brand_id}                                HASH

Redis keys
──────────
  coop:{coop_id}                          HASH config + status
  coop:{coop_id}:contributors             ZSET score=contribution, member=user_id
  brand:{brand_id}:coops                  SET coop_ids

  raid:{party_id}                         HASH boss_hp/state
  raid:{party_id}:members                 SET user_ids
  raid:{party_id}:damage                  ZSET score=damage, member=user_id
  brand:{brand_id}:raids                  SET party_ids

  squad:{squad_id}                        HASH config / state
  squad:{squad_id}:members                SET user_ids
  brand:{brand_id}:squads                 SET squad_ids

  territory:{territory_id}                HASH owner/defense/claimed_at
  brand:{brand_id}:territories            SET territory_ids
  user:{uid}:territory_last_bonus_at:{tid}  INT (unix ts)
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.redis_client import get_redis

router = APIRouter()


# ═════════════════════════════════════════════════════════════════════════
# Shared helpers
# ═════════════════════════════════════════════════════════════════════════


def _share_url(prefix: str, oid: str) -> str:
    return f"https://play.kix.app/m/{prefix}/{oid}"


def _energy_key(brand_id: str, user_id: str) -> str:
    return f"energy:balance:{brand_id}:{user_id}"


def _currency_key(currency: str, user_id: str, brand_id: str) -> str:
    return f"currency:{currency}:{user_id}:{brand_id}"


def _inventory_key(user_id: str, brand_id: str) -> str:
    return f"user:{user_id}:inventory:{brand_id}"


class Reward(BaseModel):
    """A reward bundle (analog of QuestReward / TradeBundle)."""

    xp: int = 0
    energy: int = 0
    currency: dict[str, int] = Field(default_factory=dict)
    items: dict[str, int] = Field(default_factory=dict)


async def _grant_reward(
    r: aioredis.Redis,
    user_id: str,
    brand_id: str,
    reward: Reward,
) -> dict[str, Any]:
    """Apply a Reward bundle to a single user. Returns granted summary."""
    granted: dict[str, Any] = {}

    if reward.xp > 0:
        new_xp = int(await r.incrby(f"user:{user_id}:xp", reward.xp))
        await r.zincrby(
            f"brand:{brand_id}:xp_leaderboard", reward.xp, user_id
        )
        granted["xp"] = {"amount": reward.xp, "total": new_xp}

    if reward.energy > 0:
        new_bal = int(
            await r.incrby(_energy_key(brand_id, user_id), reward.energy)
        )
        granted["energy"] = {"amount": reward.energy, "balance": new_bal}

    if reward.currency:
        granted_cur: dict[str, int] = {}
        for cur, amt in reward.currency.items():
            if amt <= 0:
                continue
            bal = int(
                await r.incrby(_currency_key(cur, user_id, brand_id), amt)
            )
            granted_cur[cur] = bal
        if granted_cur:
            granted["currency"] = granted_cur

    if reward.items:
        granted_items: dict[str, int] = {}
        inv_key = _inventory_key(user_id, brand_id)
        for iid, qty in reward.items.items():
            if qty <= 0:
                continue
            current = int(await r.hget(inv_key, iid) or 0)
            new_qty = current + qty
            await r.hset(inv_key, iid, new_qty)
            granted_items[iid] = new_qty
        if granted_items:
            granted["items"] = granted_items

    return granted


# ═════════════════════════════════════════════════════════════════════════
# 1. COOPERATIVE QUEST
# ═════════════════════════════════════════════════════════════════════════


class CoopCreate(BaseModel):
    brand_id: str
    quest_id: str
    name: str
    goal_total: int = Field(gt=0)
    reward_per_member: Reward = Field(default_factory=Reward)
    max_members: int = Field(default=10, gt=0)
    window_hours: int = Field(default=24, gt=0)


class CoopJoin(BaseModel):
    user_id: str


class CoopContribute(BaseModel):
    user_id: str
    amount: int = Field(gt=0)


def _coop_key(coop_id: str) -> str:
    return f"coop:{coop_id}"


def _coop_contrib_key(coop_id: str) -> str:
    return f"coop:{coop_id}:contributors"


@router.post("/coop-quest/create")
async def coop_create(
    body: CoopCreate, r: aioredis.Redis = Depends(get_redis)
):
    coop_id = uuid.uuid4().hex
    now = int(time.time())
    expires_at = now + body.window_hours * 3600

    state = {
        "coop_id": coop_id,
        "brand_id": body.brand_id,
        "quest_id": body.quest_id,
        "name": body.name,
        "goal_total": body.goal_total,
        "progress": 0,
        "reward_per_member": body.reward_per_member.model_dump_json(),
        "max_members": body.max_members,
        "status": "open",
        "created_at": now,
        "expires_at": expires_at,
    }
    await r.hset(
        _coop_key(coop_id),
        mapping={k: str(v) for k, v in state.items()},
    )
    await r.expire(_coop_key(coop_id), body.window_hours * 3600 + 86400)
    await r.expire(_coop_contrib_key(coop_id), body.window_hours * 3600 + 86400)
    await r.sadd(f"brand:{body.brand_id}:coops", coop_id)

    return {
        "coop_id": coop_id,
        "share_url": _share_url("coop", coop_id),
        "expires_at": expires_at,
        "status": "open",
    }


@router.post("/coop-quest/{coop_id}/join")
async def coop_join(
    coop_id: str, body: CoopJoin, r: aioredis.Redis = Depends(get_redis)
):
    state = await r.hgetall(_coop_key(coop_id))
    if not state:
        raise HTTPException(404, detail="coop not found")
    if state.get("status") != "open":
        raise HTTPException(
            409, detail={"error": "coop_not_open", "status": state.get("status")}
        )
    now = int(time.time())
    if now > int(state.get("expires_at", 0)):
        await r.hset(_coop_key(coop_id), "status", "expired")
        raise HTTPException(410, detail="coop expired")

    contrib_key = _coop_contrib_key(coop_id)
    # ZADD with NX so re-join is idempotent (score 0 only set once)
    added = await r.zadd(contrib_key, {body.user_id: 0}, nx=True)
    member_count = await r.zcard(contrib_key)
    max_members = int(state.get("max_members", 0))

    if added and max_members and member_count > max_members:
        # Roll back the over-cap add
        await r.zrem(contrib_key, body.user_id)
        raise HTTPException(
            409, detail={"error": "coop_full", "max_members": max_members}
        )

    return {
        "ok": True,
        "coop_id": coop_id,
        "user_id": body.user_id,
        "members": int(await r.zcard(contrib_key)),
        "already_joined": added == 0,
    }


@router.post("/coop-quest/{coop_id}/contribute")
async def coop_contribute(
    coop_id: str,
    body: CoopContribute,
    r: aioredis.Redis = Depends(get_redis),
):
    state = await r.hgetall(_coop_key(coop_id))
    if not state:
        raise HTTPException(404, detail="coop not found")
    if state.get("status") != "open":
        raise HTTPException(
            409, detail={"error": "coop_not_open", "status": state.get("status")}
        )
    now = int(time.time())
    if now > int(state.get("expires_at", 0)):
        await r.hset(_coop_key(coop_id), "status", "expired")
        raise HTTPException(410, detail="coop expired")

    contrib_key = _coop_contrib_key(coop_id)
    # Member must have joined first.
    if await r.zscore(contrib_key, body.user_id) is None:
        raise HTTPException(403, detail="join coop before contributing")

    goal_total = int(state["goal_total"])
    your_contrib = int(
        await r.zincrby(contrib_key, body.amount, body.user_id)
    )
    # Atomic INCR on coop progress.
    new_progress = int(
        await r.hincrby(_coop_key(coop_id), "progress", body.amount)
    )

    completed_now = False
    distribution: list[dict[str, Any]] = []

    if new_progress >= goal_total:
        # Try to claim the "completing" responsibility atomically.
        # Whoever flips status=open→completed first runs the payout.
        async with r.pipeline(transaction=True) as pipe:
            while True:
                try:
                    await pipe.watch(_coop_key(coop_id))
                    cur_status = await pipe.hget(_coop_key(coop_id), "status")
                    if cur_status != "open":
                        await pipe.unwatch()
                        break
                    pipe.multi()
                    pipe.hset(
                        _coop_key(coop_id),
                        mapping={
                            "status": "completed",
                            "completed_at": int(time.time()),
                        },
                    )
                    await pipe.execute()
                    completed_now = True
                    break
                except aioredis.WatchError:
                    continue

        if completed_now:
            # Distribute reward to every contributor.
            try:
                reward = Reward(
                    **json.loads(state.get("reward_per_member", "{}"))
                )
            except Exception:
                reward = Reward()
            brand_id = state["brand_id"]
            members = await r.zrange(contrib_key, 0, -1)
            for uid in members:
                g = await _grant_reward(r, uid, brand_id, reward)
                distribution.append({"user_id": uid, "granted": g})

    # Build all_contributors snapshot.
    raw = await r.zrange(contrib_key, 0, -1, withscores=True)
    all_contributors = [
        {"user_id": uid, "contribution": int(score)} for uid, score in raw
    ]
    final_status = "completed" if new_progress >= goal_total else "open"

    return {
        "ok": True,
        "coop_id": coop_id,
        "progress": min(new_progress, goal_total),
        "target": goal_total,
        "status": final_status,
        "your_contribution": your_contrib,
        "all_contributors": all_contributors,
        "newly_completed": completed_now,
        "distribution": distribution,
    }


@router.get("/coop-quest/{coop_id}")
async def coop_get(
    coop_id: str, r: aioredis.Redis = Depends(get_redis)
):
    state = await r.hgetall(_coop_key(coop_id))
    if not state:
        raise HTTPException(404, detail="coop not found")
    raw = await r.zrange(_coop_contrib_key(coop_id), 0, -1, withscores=True)
    contribs = [
        {"user_id": uid, "contribution": int(s)} for uid, s in raw
    ]
    try:
        reward = json.loads(state.get("reward_per_member", "{}"))
    except Exception:
        reward = {}
    return {
        "coop_id": coop_id,
        "brand_id": state.get("brand_id"),
        "quest_id": state.get("quest_id"),
        "name": state.get("name"),
        "goal_total": int(state.get("goal_total", 0)),
        "progress": int(state.get("progress", 0)),
        "status": state.get("status"),
        "max_members": int(state.get("max_members", 0)),
        "expires_at": int(state.get("expires_at", 0)),
        "reward_per_member": reward,
        "contributors": contribs,
    }


# ═════════════════════════════════════════════════════════════════════════
# 2. GROUP RAID
# ═════════════════════════════════════════════════════════════════════════


class RaidCreate(BaseModel):
    brand_id: str
    raid_id: str
    boss_hp: int = Field(gt=0)
    max_party: int = Field(default=5, gt=0)
    time_limit_minutes: int = Field(default=30, gt=0)
    reward_per_member: Reward = Field(default_factory=Reward)


class RaidJoin(BaseModel):
    user_id: str


class RaidAttack(BaseModel):
    user_id: str
    damage_amount: int = Field(gt=0)


def _raid_key(party_id: str) -> str:
    return f"raid:{party_id}"


def _raid_members_key(party_id: str) -> str:
    return f"raid:{party_id}:members"


def _raid_damage_key(party_id: str) -> str:
    return f"raid:{party_id}:damage"


@router.post("/raid/create")
async def raid_create(
    body: RaidCreate, r: aioredis.Redis = Depends(get_redis)
):
    party_id = uuid.uuid4().hex
    now = int(time.time())
    ends_at = now + body.time_limit_minutes * 60

    state = {
        "party_id": party_id,
        "brand_id": body.brand_id,
        "raid_id": body.raid_id,
        "boss_hp_initial": body.boss_hp,
        "hp_remaining": body.boss_hp,
        "max_party": body.max_party,
        "reward_per_member": body.reward_per_member.model_dump_json(),
        "status": "open",
        "created_at": now,
        "ends_at": ends_at,
    }
    await r.hset(
        _raid_key(party_id),
        mapping={k: str(v) for k, v in state.items()},
    )
    await r.expire(_raid_key(party_id), body.time_limit_minutes * 60 + 86400)
    await r.expire(_raid_members_key(party_id), body.time_limit_minutes * 60 + 86400)
    await r.expire(_raid_damage_key(party_id), body.time_limit_minutes * 60 + 86400)
    await r.sadd(f"brand:{body.brand_id}:raids", party_id)

    return {
        "party_id": party_id,
        "share_url": _share_url("raid", party_id),
        "boss_hp": body.boss_hp,
        "ends_at": ends_at,
        "status": "open",
    }


@router.post("/raid/{party_id}/join")
async def raid_join(
    party_id: str, body: RaidJoin, r: aioredis.Redis = Depends(get_redis)
):
    state = await r.hgetall(_raid_key(party_id))
    if not state:
        raise HTTPException(404, detail="raid not found")
    if state.get("status") != "open":
        raise HTTPException(
            409, detail={"error": "raid_not_open", "status": state.get("status")}
        )
    now = int(time.time())
    if now > int(state.get("ends_at", 0)):
        await r.hset(_raid_key(party_id), "status", "expired")
        raise HTTPException(410, detail="raid expired")

    members_key = _raid_members_key(party_id)
    added = await r.sadd(members_key, body.user_id)
    size = await r.scard(members_key)
    max_party = int(state.get("max_party", 0))

    if added and max_party and size > max_party:
        await r.srem(members_key, body.user_id)
        raise HTTPException(
            409, detail={"error": "raid_full", "max_party": max_party}
        )

    return {
        "ok": True,
        "party_id": party_id,
        "user_id": body.user_id,
        "party_size": int(await r.scard(members_key)),
        "hp_remaining": int(state.get("hp_remaining", 0)),
        "already_joined": added == 0,
    }


@router.post("/raid/{party_id}/attack")
async def raid_attack(
    party_id: str,
    body: RaidAttack,
    r: aioredis.Redis = Depends(get_redis),
):
    """Atomic DECR on boss hp. Reward all members on kill."""
    state = await r.hgetall(_raid_key(party_id))
    if not state:
        raise HTTPException(404, detail="raid not found")
    if state.get("status") != "open":
        raise HTTPException(
            409,
            detail={"error": "raid_not_open", "status": state.get("status")},
        )
    now = int(time.time())
    if now > int(state.get("ends_at", 0)):
        await r.hset(_raid_key(party_id), "status", "expired")
        raise HTTPException(410, detail="raid expired")

    if not await r.sismember(_raid_members_key(party_id), body.user_id):
        raise HTTPException(403, detail="join raid before attacking")

    # Atomic decrement.
    new_hp = int(
        await r.hincrby(_raid_key(party_id), "hp_remaining", -body.damage_amount)
    )
    # Damage attributed to this user (over-damage clamped for display).
    initial_hp = int(state.get("boss_hp_initial", 0))
    prior_hp = int(state.get("hp_remaining", 0))
    actual_damage = min(body.damage_amount, max(prior_hp, 0))
    await r.zincrby(_raid_damage_key(party_id), actual_damage, body.user_id)

    raid_complete = False
    distribution: list[dict[str, Any]] = []

    if new_hp <= 0:
        # Try to win the kill race.
        async with r.pipeline(transaction=True) as pipe:
            while True:
                try:
                    await pipe.watch(_raid_key(party_id))
                    cur_status = await pipe.hget(_raid_key(party_id), "status")
                    if cur_status != "open":
                        await pipe.unwatch()
                        break
                    pipe.multi()
                    pipe.hset(
                        _raid_key(party_id),
                        mapping={
                            "status": "completed",
                            "completed_at": int(time.time()),
                            "killing_blow_by": body.user_id,
                        },
                    )
                    await pipe.execute()
                    raid_complete = True
                    break
                except aioredis.WatchError:
                    continue

        if raid_complete:
            try:
                reward = Reward(
                    **json.loads(state.get("reward_per_member", "{}"))
                )
            except Exception:
                reward = Reward()
            brand_id = state["brand_id"]
            members = await r.smembers(_raid_members_key(party_id))
            for uid in members:
                g = await _grant_reward(r, uid, brand_id, reward)
                distribution.append({"user_id": uid, "granted": g})

    return {
        "ok": True,
        "party_id": party_id,
        "user_id": body.user_id,
        "damage_dealt": actual_damage,
        "hp_remaining": max(new_hp, 0),
        "boss_hp_initial": initial_hp,
        "raid_complete": raid_complete or new_hp <= 0,
        "distribution": distribution,
    }


@router.get("/raid/{party_id}")
async def raid_get(
    party_id: str, r: aioredis.Redis = Depends(get_redis)
):
    state = await r.hgetall(_raid_key(party_id))
    if not state:
        raise HTTPException(404, detail="raid not found")
    members = list(await r.smembers(_raid_members_key(party_id)))
    raw = await r.zrange(
        _raid_damage_key(party_id), 0, -1, withscores=True, desc=True
    )
    damage_leaderboard = [
        {"user_id": uid, "damage": int(d)} for uid, d in raw
    ]
    try:
        reward = json.loads(state.get("reward_per_member", "{}"))
    except Exception:
        reward = {}
    return {
        "party_id": party_id,
        "brand_id": state.get("brand_id"),
        "raid_id": state.get("raid_id"),
        "boss_hp_initial": int(state.get("boss_hp_initial", 0)),
        "hp_remaining": max(int(state.get("hp_remaining", 0)), 0),
        "max_party": int(state.get("max_party", 0)),
        "members": members,
        "party_size": len(members),
        "status": state.get("status"),
        "ends_at": int(state.get("ends_at", 0)),
        "killing_blow_by": state.get("killing_blow_by"),
        "damage_leaderboard": damage_leaderboard,
        "reward_per_member": reward,
    }


# ═════════════════════════════════════════════════════════════════════════
# 3. SQUAD MULTIPLIER
# ═════════════════════════════════════════════════════════════════════════


class SquadCreate(BaseModel):
    brand_id: str
    leader_user_id: str
    max_size: int = Field(default=4, gt=0)
    multiplier_per_member: float = Field(default=0.25, ge=0)


class SquadJoin(BaseModel):
    user_id: str


class SquadPlay(BaseModel):
    user_id: str
    base_score: float = Field(ge=0)


class SquadDisband(BaseModel):
    leader_user_id: str


def _squad_key(squad_id: str) -> str:
    return f"squad:{squad_id}"


def _squad_members_key(squad_id: str) -> str:
    return f"squad:{squad_id}:members"


@router.post("/squad/create")
async def squad_create(
    body: SquadCreate, r: aioredis.Redis = Depends(get_redis)
):
    squad_id = uuid.uuid4().hex
    now = int(time.time())
    state = {
        "squad_id": squad_id,
        "brand_id": body.brand_id,
        "leader_user_id": body.leader_user_id,
        "max_size": body.max_size,
        "multiplier_per_member": body.multiplier_per_member,
        "status": "active",
        "created_at": now,
    }
    await r.hset(
        _squad_key(squad_id),
        mapping={k: str(v) for k, v in state.items()},
    )
    await r.sadd(_squad_members_key(squad_id), body.leader_user_id)
    await r.sadd(f"brand:{body.brand_id}:squads", squad_id)

    return {
        "squad_id": squad_id,
        "leader_user_id": body.leader_user_id,
        "max_size": body.max_size,
        "multiplier_per_member": body.multiplier_per_member,
        "status": "active",
    }


@router.post("/squad/{squad_id}/join")
async def squad_join(
    squad_id: str,
    body: SquadJoin,
    r: aioredis.Redis = Depends(get_redis),
):
    state = await r.hgetall(_squad_key(squad_id))
    if not state:
        raise HTTPException(404, detail="squad not found")
    if state.get("status") != "active":
        raise HTTPException(
            409,
            detail={"error": "squad_not_active", "status": state.get("status")},
        )

    members_key = _squad_members_key(squad_id)
    added = await r.sadd(members_key, body.user_id)
    size = int(await r.scard(members_key))
    max_size = int(state.get("max_size", 0))

    if added and max_size and size > max_size:
        await r.srem(members_key, body.user_id)
        raise HTTPException(
            409, detail={"error": "squad_full", "max_size": max_size}
        )

    return {
        "ok": True,
        "squad_id": squad_id,
        "user_id": body.user_id,
        "squad_size": int(await r.scard(members_key)),
        "already_joined": added == 0,
    }


@router.post("/squad/{squad_id}/play")
async def squad_play(
    squad_id: str,
    body: SquadPlay,
    r: aioredis.Redis = Depends(get_redis),
):
    """Score boosted by 1 + multiplier_per_member * active_size."""
    state = await r.hgetall(_squad_key(squad_id))
    if not state:
        raise HTTPException(404, detail="squad not found")
    if state.get("status") != "active":
        raise HTTPException(
            409,
            detail={"error": "squad_not_active", "status": state.get("status")},
        )
    members_key = _squad_members_key(squad_id)
    if not await r.sismember(members_key, body.user_id):
        raise HTTPException(403, detail="not a squad member")

    active_size = int(await r.scard(members_key))
    multiplier_per_member = float(state.get("multiplier_per_member", 0))
    multiplier = 1 + multiplier_per_member * active_size
    boosted = body.base_score * multiplier

    return {
        "ok": True,
        "squad_id": squad_id,
        "user_id": body.user_id,
        "base_score": body.base_score,
        "multiplier_per_member": multiplier_per_member,
        "squad_size_at_play": active_size,
        "multiplier": multiplier,
        "boosted_score": boosted,
    }


@router.post("/squad/{squad_id}/disband")
async def squad_disband(
    squad_id: str,
    body: SquadDisband,
    r: aioredis.Redis = Depends(get_redis),
):
    state = await r.hgetall(_squad_key(squad_id))
    if not state:
        raise HTTPException(404, detail="squad not found")
    if state.get("leader_user_id") != body.leader_user_id:
        raise HTTPException(403, detail="only leader can disband")
    if state.get("status") != "active":
        raise HTTPException(
            409,
            detail={"error": "squad_not_active", "status": state.get("status")},
        )
    now = int(time.time())
    await r.hset(
        _squad_key(squad_id),
        mapping={"status": "disbanded", "disbanded_at": now},
    )
    return {"ok": True, "squad_id": squad_id, "status": "disbanded"}


@router.get("/squad/{squad_id}")
async def squad_get(
    squad_id: str, r: aioredis.Redis = Depends(get_redis)
):
    state = await r.hgetall(_squad_key(squad_id))
    if not state:
        raise HTTPException(404, detail="squad not found")
    members = list(await r.smembers(_squad_members_key(squad_id)))
    return {
        "squad_id": squad_id,
        "brand_id": state.get("brand_id"),
        "leader_user_id": state.get("leader_user_id"),
        "max_size": int(state.get("max_size", 0)),
        "multiplier_per_member": float(state.get("multiplier_per_member", 0)),
        "status": state.get("status"),
        "members": members,
        "size": len(members),
    }


# ═════════════════════════════════════════════════════════════════════════
# 4. TERRITORY (Pokemon Gym FCFS claim)
# ═════════════════════════════════════════════════════════════════════════


class TerritoryCreate(BaseModel):
    brand_id: str
    territory_id: str
    name: str
    defense_score_required: int = Field(gt=0)
    # Passive bonus while owning, per hour held.
    passive_xp_per_hour: int = 0
    passive_currency: dict[str, int] = Field(default_factory=dict)  # name → per-hour


class TerritoryClaim(BaseModel):
    user_id: str
    attack_score: int = Field(gt=0)


def _territory_key(territory_id: str) -> str:
    return f"territory:{territory_id}"


def _territory_bonus_key(user_id: str, territory_id: str) -> str:
    return f"user:{user_id}:territory_last_bonus_at:{territory_id}"


async def _settle_territory_bonus(
    r: aioredis.Redis, territory_id: str, state: dict[str, str]
) -> dict[str, Any]:
    """Pay out passive bonus for the current owner since last settlement.

    Called on every interaction (claim / get). Computes hours held and
    credits XP + currency.
    """
    owner = state.get("owner")
    if not owner:
        return {}

    brand_id = state.get("brand_id", "")
    now = int(time.time())
    last_key = _territory_bonus_key(owner, territory_id)
    last_at = int(await r.get(last_key) or state.get("controlled_since", now))
    hours = (now - last_at) // 3600
    if hours <= 0:
        return {}

    passive_xp = int(state.get("passive_xp_per_hour", 0))
    try:
        passive_currency = json.loads(state.get("passive_currency", "{}"))
    except Exception:
        passive_currency = {}

    granted: dict[str, Any] = {}
    if passive_xp > 0:
        gained_xp = passive_xp * hours
        new_xp = int(await r.incrby(f"user:{owner}:xp", gained_xp))
        await r.zincrby(f"brand:{brand_id}:xp_leaderboard", gained_xp, owner)
        granted["xp"] = {"amount": gained_xp, "total": new_xp}

    if passive_currency:
        granted_cur: dict[str, int] = {}
        for cur, per_hour in passive_currency.items():
            amt = int(per_hour) * hours
            if amt <= 0:
                continue
            bal = int(
                await r.incrby(_currency_key(cur, owner, brand_id), amt)
            )
            granted_cur[cur] = bal
        if granted_cur:
            granted["currency"] = granted_cur

    # Update last-bonus timestamp to now (rounded to hour boundary so
    # fractional hours roll forward).
    new_last = last_at + hours * 3600
    await r.set(last_key, new_last)
    return granted


@router.post("/territory/create")
async def territory_create(
    body: TerritoryCreate, r: aioredis.Redis = Depends(get_redis)
):
    key = _territory_key(body.territory_id)
    if await r.exists(key):
        raise HTTPException(
            409,
            detail={
                "error": "territory_exists",
                "territory_id": body.territory_id,
            },
        )
    now = int(time.time())
    state = {
        "territory_id": body.territory_id,
        "brand_id": body.brand_id,
        "name": body.name,
        "defense_score_required": body.defense_score_required,
        "defense_score": 0,
        "owner": "",
        "controlled_since": 0,
        "passive_xp_per_hour": body.passive_xp_per_hour,
        "passive_currency": json.dumps(body.passive_currency),
        "created_at": now,
    }
    await r.hset(key, mapping={k: str(v) for k, v in state.items()})
    await r.sadd(f"brand:{body.brand_id}:territories", body.territory_id)
    return {
        "ok": True,
        "territory_id": body.territory_id,
        "defense_score_required": body.defense_score_required,
    }


@router.post("/territory/{territory_id}/claim")
async def territory_claim(
    territory_id: str,
    body: TerritoryClaim,
    r: aioredis.Redis = Depends(get_redis),
):
    """FCFS claim. attack_score must beat current defense AND meet required."""
    key = _territory_key(territory_id)

    # Atomic compare-and-swap via WATCH/MULTI.
    async with r.pipeline(transaction=True) as pipe:
        attempts = 0
        while attempts < 10:
            attempts += 1
            try:
                await pipe.watch(key)
                state = await pipe.hgetall(key)
                if not state:
                    await pipe.unwatch()
                    raise HTTPException(404, detail="territory not found")

                required = int(state.get("defense_score_required", 0))
                current_def = int(state.get("defense_score", 0))
                current_owner = state.get("owner", "")

                if body.attack_score < required:
                    await pipe.unwatch()
                    raise HTTPException(
                        422,
                        detail={
                            "error": "below_threshold",
                            "attack_score": body.attack_score,
                            "required": required,
                        },
                    )
                if body.attack_score <= current_def and current_owner:
                    await pipe.unwatch()
                    return {
                        "ok": False,
                        "territory_id": territory_id,
                        "claimed": False,
                        "reason": "attack_score_did_not_beat_defense",
                        "attack_score": body.attack_score,
                        "current_defense_score": current_def,
                        "current_owner": current_owner,
                    }

                # First settle any prior owner's passive bonus before flipping.
                if current_owner:
                    # Run settlement outside the transaction context (best-effort).
                    await pipe.unwatch()
                    await _settle_territory_bonus(r, territory_id, state)
                    # Re-acquire watch and recheck (could have changed).
                    await pipe.watch(key)
                    state = await pipe.hgetall(key)
                    if int(state.get("defense_score", 0)) != current_def or \
                       state.get("owner", "") != current_owner:
                        continue  # retry

                now = int(time.time())
                pipe.multi()
                pipe.hset(
                    key,
                    mapping={
                        "owner": body.user_id,
                        "defense_score": body.attack_score,
                        "controlled_since": now,
                    },
                )
                await pipe.execute()

                # Initialize new owner's last_bonus_at to now.
                await r.set(
                    _territory_bonus_key(body.user_id, territory_id), now
                )

                return {
                    "ok": True,
                    "territory_id": territory_id,
                    "claimed": True,
                    "owner": body.user_id,
                    "defense_score": body.attack_score,
                    "previous_owner": current_owner or None,
                    "controlled_since": now,
                }

            except aioredis.WatchError:
                continue

    raise HTTPException(503, detail="claim contention too high; please retry")


@router.get("/territory/{territory_id}")
async def territory_get(
    territory_id: str, r: aioredis.Redis = Depends(get_redis)
):
    key = _territory_key(territory_id)
    state = await r.hgetall(key)
    if not state:
        raise HTTPException(404, detail="territory not found")

    # Settle passive bonus for current owner on read.
    passive_granted = await _settle_territory_bonus(r, territory_id, state)
    if passive_granted:
        # Re-read to reflect any state we touched (none in current impl,
        # but keeps the contract clean if we extend later).
        state = await r.hgetall(key)

    try:
        passive_currency = json.loads(state.get("passive_currency", "{}"))
    except Exception:
        passive_currency = {}

    return {
        "territory_id": territory_id,
        "brand_id": state.get("brand_id"),
        "name": state.get("name"),
        "owner": state.get("owner") or None,
        "defense_score": int(state.get("defense_score", 0)),
        "defense_score_required": int(state.get("defense_score_required", 0)),
        "controlled_since": int(state.get("controlled_since", 0)) or None,
        "passive_xp_per_hour": int(state.get("passive_xp_per_hour", 0)),
        "passive_currency_per_hour": passive_currency,
        "passive_granted_now": passive_granted,
    }


@router.get("/territories")
async def list_territories(
    brand_id: str, r: aioredis.Redis = Depends(get_redis)
):
    ids = await r.smembers(f"brand:{brand_id}:territories")
    out: list[dict[str, Any]] = []
    for tid in ids:
        st = await r.hgetall(_territory_key(tid))
        if not st:
            continue
        out.append(
            {
                "territory_id": tid,
                "name": st.get("name"),
                "owner": st.get("owner") or None,
                "defense_score": int(st.get("defense_score", 0)),
                "defense_score_required": int(
                    st.get("defense_score_required", 0)
                ),
                "controlled_since": int(st.get("controlled_since", 0)) or None,
            }
        )
    return {"brand_id": brand_id, "territories": out}
