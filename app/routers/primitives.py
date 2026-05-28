"""Universal Gamification Primitives — Layer 1 building blocks.

Composable primitives used by all 30+ KiX gamification modules:
  Currency, Item, Achievement, Quest, Tier, Event.

These are CRUD building blocks; higher-level modules (battles, lotteries,
leagues, drops, etc.) compose them. All state in Redis (no SQL).

Redis key scheme
────────────────
  Currency
    user:{user_id}:currency:{brand_id}:{currency}              int balance
    user:{user_id}:currencies:{brand_id}                       SET of currency names

  Item
    brand:{brand_id}:items                                     HASH item_id → JSON
    user:{user_id}:inventory:{brand_id}                        HASH item_id → qty

  Achievement
    brand:{brand_id}:achievements                              HASH ach_id → JSON
    user:{user_id}:achievement:{brand_id}:{ach_id}:progress    int
    user:{user_id}:achievement:{brand_id}:{ach_id}:completed   "1" if completed

  Quest
    brand:{brand_id}:quests                                    HASH quest_id → JSON
    user:{user_id}:quest:{brand_id}:{quest_id}                 HASH status, current_step,
                                                                    steps_done(JSON list)
    user:{user_id}:quests:{brand_id}                           SET of quest_ids touched

  Tier
    brand:{brand_id}:tiers                                     HASH tier_id → JSON
    user:{user_id}:tier:{brand_id}                             current tier_id

  Event
    brand:{brand_id}:events                                    HASH event_id → JSON
    event:{event_id}:optins                                    SET of user_ids
"""

from __future__ import annotations

import json
import time
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.redis_client import get_redis
from app.routers.progression import xp_to_level

router = APIRouter()


# ═════════════════════════════════════════════════════════════════════════
# 1. CURRENCY  (energy / stars / coins / gems / ...)
# ═════════════════════════════════════════════════════════════════════════


class CurrencyOp(BaseModel):
    user_id: str
    brand_id: str
    amount: int = Field(gt=0)
    reason: str = ""


def _currency_key(user_id: str, brand_id: str, currency: str) -> str:
    return f"user:{user_id}:currency:{brand_id}:{currency}"


def _currency_set_key(user_id: str, brand_id: str) -> str:
    return f"user:{user_id}:currencies:{brand_id}"


@router.post("/currency/{currency_name}/grant")
async def grant_currency(
    currency_name: str,
    body: CurrencyOp,
    r: aioredis.Redis = Depends(get_redis),
):
    """Grant currency to a user. Creates the currency on first grant."""
    key = _currency_key(body.user_id, body.brand_id, currency_name)
    new_balance = await r.incrby(key, body.amount)
    await r.sadd(_currency_set_key(body.user_id, body.brand_id), currency_name)
    return {
        "ok": True,
        "user_id": body.user_id,
        "brand_id": body.brand_id,
        "currency": currency_name,
        "amount": body.amount,
        "balance": int(new_balance),
        "reason": body.reason,
    }


@router.post("/currency/{currency_name}/spend")
async def spend_currency(
    currency_name: str,
    body: CurrencyOp,
    r: aioredis.Redis = Depends(get_redis),
):
    """Spend currency. Returns 402 if insufficient. Atomic via DECRBY+rollback."""
    key = _currency_key(body.user_id, body.brand_id, currency_name)
    # Optimistic: decrement then re-check; if negative, roll back.
    new_balance = await r.decrby(key, body.amount)
    if int(new_balance) < 0:
        await r.incrby(key, body.amount)  # rollback
        current = int(await r.get(key) or 0)
        raise HTTPException(
            status_code=402,
            detail={
                "error": "insufficient_balance",
                "currency": currency_name,
                "required": body.amount,
                "available": current,
            },
        )
    return {
        "ok": True,
        "user_id": body.user_id,
        "brand_id": body.brand_id,
        "currency": currency_name,
        "spent": body.amount,
        "balance": int(new_balance),
        "reason": body.reason,
    }


@router.get("/currency/{user_id}/balances")
async def get_balances(
    user_id: str,
    brand_id: str = "",
    r: aioredis.Redis = Depends(get_redis),
):
    """Return all currency balances for a user.

    If brand_id given → only that brand. Else scan all brand sets we know
    of (best-effort: only returns brands we've seen via grant/spend).
    """
    out: dict[str, dict[str, int]] = {}
    if brand_id:
        brands = [brand_id]
    else:
        # Scan for currency set keys for this user
        pattern = f"user:{user_id}:currencies:*"
        brands = []
        async for k in r.scan_iter(match=pattern, count=100):
            brands.append(k.split(":")[-1])

    for bid in brands:
        currencies = await r.smembers(_currency_set_key(user_id, bid))
        balances: dict[str, int] = {}
        for c in currencies:
            bal = int(await r.get(_currency_key(user_id, bid, c)) or 0)
            balances[c] = bal
        if balances:
            out[bid] = balances

    return {"user_id": user_id, "balances": out}


# ═════════════════════════════════════════════════════════════════════════
# 2. ITEM  (collectibles / inventory)
# ═════════════════════════════════════════════════════════════════════════


class Item(BaseModel):
    id: str
    name: str
    icon: str = ""
    rarity: str = "common"  # common/rare/epic/legendary
    stackable: bool = True
    max_stack: int = 999


class ItemGrant(BaseModel):
    user_id: str
    brand_id: str
    item_id: str
    qty: int = 1


@router.post("/brand/{brand_id}/items")
async def create_item(
    brand_id: str, item: Item, r: aioredis.Redis = Depends(get_redis)
):
    """Define an item template for a brand."""
    await r.hset(f"brand:{brand_id}:items", item.id, item.model_dump_json())
    return {"ok": True, "brand_id": brand_id, "item_id": item.id}


@router.get("/brand/{brand_id}/items", response_model=list[Item])
async def list_items(brand_id: str, r: aioredis.Redis = Depends(get_redis)):
    raw = await r.hgetall(f"brand:{brand_id}:items")
    out: list[Item] = []
    for v in raw.values():
        try:
            out.append(Item(**json.loads(v)))
        except Exception:
            continue
    return out


@router.post("/item/grant")
async def grant_item(body: ItemGrant, r: aioredis.Redis = Depends(get_redis)):
    """Grant an item to a user. Idempotent (re-grants stack up to max_stack)."""
    if body.qty <= 0:
        raise HTTPException(422, detail="qty must be positive")

    item_json = await r.hget(f"brand:{body.brand_id}:items", body.item_id)
    if not item_json:
        raise HTTPException(404, detail=f"Item {body.item_id} not found")
    item = Item(**json.loads(item_json))

    inv_key = f"user:{body.user_id}:inventory:{body.brand_id}"

    if not item.stackable:
        # Non-stackable: just set to 1 (idempotent)
        current = int(await r.hget(inv_key, body.item_id) or 0)
        if current >= 1:
            return {
                "ok": True,
                "already_owned": True,
                "item_id": body.item_id,
                "qty": 1,
            }
        await r.hset(inv_key, body.item_id, 1)
        return {"ok": True, "item_id": body.item_id, "qty": 1}

    # Stackable: incrby, capped at max_stack
    current = int(await r.hget(inv_key, body.item_id) or 0)
    new_qty = min(current + body.qty, item.max_stack)
    await r.hset(inv_key, body.item_id, new_qty)
    return {
        "ok": True,
        "item_id": body.item_id,
        "granted": new_qty - current,
        "qty": new_qty,
        "capped": (new_qty < current + body.qty),
    }


@router.post("/item/consume")
async def consume_item(body: ItemGrant, r: aioredis.Redis = Depends(get_redis)):
    """Consume qty of an item. Returns 404 if item not owned / insufficient."""
    if body.qty <= 0:
        raise HTTPException(422, detail="qty must be positive")

    inv_key = f"user:{body.user_id}:inventory:{body.brand_id}"
    current = int(await r.hget(inv_key, body.item_id) or 0)
    if current < body.qty:
        raise HTTPException(
            404,
            detail={
                "error": "insufficient_inventory",
                "item_id": body.item_id,
                "have": current,
                "need": body.qty,
            },
        )
    new_qty = current - body.qty
    if new_qty == 0:
        await r.hdel(inv_key, body.item_id)
    else:
        await r.hset(inv_key, body.item_id, new_qty)
    return {
        "ok": True,
        "item_id": body.item_id,
        "consumed": body.qty,
        "qty": new_qty,
    }


@router.get("/user/{user_id}/inventory")
async def get_inventory(
    user_id: str, brand_id: str, r: aioredis.Redis = Depends(get_redis)
):
    """List user's inventory for a brand: [(item_meta, qty), ...]."""
    inv_key = f"user:{user_id}:inventory:{brand_id}"
    items_key = f"brand:{brand_id}:items"

    raw_inv = await r.hgetall(inv_key)
    if not raw_inv:
        return {"user_id": user_id, "brand_id": brand_id, "items": []}

    item_ids = list(raw_inv.keys())
    metas = await r.hmget(items_key, *item_ids)

    out = []
    for iid, meta in zip(item_ids, metas):
        qty = int(raw_inv[iid])
        item_dict: dict[str, Any] | None = None
        if meta:
            try:
                item_dict = json.loads(meta)
            except Exception:
                item_dict = None
        out.append({"item_id": iid, "qty": qty, "item": item_dict})
    return {"user_id": user_id, "brand_id": brand_id, "items": out}


# ═════════════════════════════════════════════════════════════════════════
# 3. ACHIEVEMENT  (multi-step trackable goals)
# ═════════════════════════════════════════════════════════════════════════


class Achievement(BaseModel):
    id: str
    name: str
    description: str = ""
    target_metric: str  # e.g. "games_played", "score_total"
    target_value: int = Field(gt=0)
    xp_reward: int = 0
    badge_id: str = ""


class AchievementProgress(BaseModel):
    user_id: str
    increment: int = Field(gt=0)


@router.post("/brand/{brand_id}/achievements")
async def create_achievement(
    brand_id: str, ach: Achievement, r: aioredis.Redis = Depends(get_redis)
):
    await r.hset(
        f"brand:{brand_id}:achievements", ach.id, ach.model_dump_json()
    )
    return {"ok": True, "brand_id": brand_id, "achievement_id": ach.id}


@router.get("/brand/{brand_id}/achievements", response_model=list[Achievement])
async def list_achievements(
    brand_id: str, r: aioredis.Redis = Depends(get_redis)
):
    raw = await r.hgetall(f"brand:{brand_id}:achievements")
    out: list[Achievement] = []
    for v in raw.values():
        try:
            out.append(Achievement(**json.loads(v)))
        except Exception:
            continue
    return out


async def _find_achievement(
    r: aioredis.Redis, achievement_id: str
) -> tuple[str, Achievement] | tuple[None, None]:
    """Search brands for an achievement_id. Returns (brand_id, Achievement)."""
    async for k in r.scan_iter(match="brand:*:achievements", count=100):
        v = await r.hget(k, achievement_id)
        if v:
            parts = k.split(":")
            brand_id = parts[1]
            try:
                return brand_id, Achievement(**json.loads(v))
            except Exception:
                return None, None
    return None, None


@router.post("/achievement/{achievement_id}/progress")
async def progress_achievement(
    achievement_id: str,
    body: AchievementProgress,
    r: aioredis.Redis = Depends(get_redis),
):
    """Increment progress. Auto-completes & grants rewards when target reached."""
    brand_id, ach = await _find_achievement(r, achievement_id)
    if not ach or not brand_id:
        raise HTTPException(404, detail=f"Achievement {achievement_id} not found")

    completed_key = (
        f"user:{body.user_id}:achievement:{brand_id}:{achievement_id}:completed"
    )
    already = await r.get(completed_key)
    if already:
        current = int(
            await r.get(
                f"user:{body.user_id}:achievement:{brand_id}:{achievement_id}:progress"
            )
            or ach.target_value
        )
        return {
            "current": current,
            "target": ach.target_value,
            "completed": True,
            "newly_completed": False,
        }

    progress_key = (
        f"user:{body.user_id}:achievement:{brand_id}:{achievement_id}:progress"
    )
    new_progress = int(await r.incrby(progress_key, body.increment))
    newly_completed = False
    xp_awarded = 0
    badge_awarded = ""

    if new_progress >= ach.target_value:
        # Cap at target, mark completed, grant rewards
        await r.set(progress_key, ach.target_value)
        await r.set(completed_key, "1")
        newly_completed = True

        # XP reward → write to user xp + brand leaderboard
        if ach.xp_reward > 0:
            xp_key = f"user:{body.user_id}:xp"
            await r.incrby(xp_key, ach.xp_reward)
            await r.zincrby(
                f"brand:{brand_id}:xp_leaderboard",
                ach.xp_reward,
                body.user_id,
            )
            xp_awarded = ach.xp_reward

        # Badge reward
        if ach.badge_id:
            badge_exists = await r.hexists(
                f"brand:{brand_id}:badges", ach.badge_id
            )
            if badge_exists:
                await r.sadd(f"user:{body.user_id}:badges", ach.badge_id)
                badge_awarded = ach.badge_id

    return {
        "current": min(new_progress, ach.target_value),
        "target": ach.target_value,
        "completed": new_progress >= ach.target_value,
        "newly_completed": newly_completed,
        "xp_awarded": xp_awarded,
        "badge_awarded": badge_awarded,
    }


@router.get("/user/{user_id}/achievements")
async def list_user_achievements(
    user_id: str, brand_id: str, r: aioredis.Redis = Depends(get_redis)
):
    raw = await r.hgetall(f"brand:{brand_id}:achievements")
    out = []
    for v in raw.values():
        try:
            ach = Achievement(**json.loads(v))
        except Exception:
            continue
        progress = int(
            await r.get(
                f"user:{user_id}:achievement:{brand_id}:{ach.id}:progress"
            )
            or 0
        )
        completed = bool(
            await r.get(
                f"user:{user_id}:achievement:{brand_id}:{ach.id}:completed"
            )
        )
        out.append(
            {
                "achievement": ach.model_dump(),
                "current": min(progress, ach.target_value),
                "target": ach.target_value,
                "completed": completed,
            }
        )
    return {"user_id": user_id, "brand_id": brand_id, "achievements": out}


# ═════════════════════════════════════════════════════════════════════════
# 4. QUEST  (multi-step missions)
# ═════════════════════════════════════════════════════════════════════════


class QuestStep(BaseModel):
    action: str
    target: int = 1
    reward_xp: int = 0


class QuestReward(BaseModel):
    xp: int = 0
    currency: dict[str, int] = Field(default_factory=dict)  # {"coins": 50}
    item: dict[str, int] = Field(default_factory=dict)      # {"item_id": qty}
    badge: str = ""


class Quest(BaseModel):
    id: str
    name: str
    description: str = ""
    steps: list[QuestStep]
    total_reward: QuestReward = Field(default_factory=QuestReward)


class QuestStart(BaseModel):
    user_id: str
    quest_id: str


class QuestStepAdvance(BaseModel):
    user_id: str
    quest_id: str
    step_index: int


@router.post("/brand/{brand_id}/quests")
async def create_quest(
    brand_id: str, quest: Quest, r: aioredis.Redis = Depends(get_redis)
):
    if not quest.steps:
        raise HTTPException(422, detail="quest must have at least one step")
    await r.hset(f"brand:{brand_id}:quests", quest.id, quest.model_dump_json())
    return {"ok": True, "brand_id": brand_id, "quest_id": quest.id}


@router.get("/brand/{brand_id}/quests", response_model=list[Quest])
async def list_brand_quests(
    brand_id: str, r: aioredis.Redis = Depends(get_redis)
):
    raw = await r.hgetall(f"brand:{brand_id}:quests")
    out: list[Quest] = []
    for v in raw.values():
        try:
            out.append(Quest(**json.loads(v)))
        except Exception:
            continue
    return out


async def _find_quest(
    r: aioredis.Redis, quest_id: str
) -> tuple[str, Quest] | tuple[None, None]:
    async for k in r.scan_iter(match="brand:*:quests", count=100):
        v = await r.hget(k, quest_id)
        if v:
            brand_id = k.split(":")[1]
            try:
                return brand_id, Quest(**json.loads(v))
            except Exception:
                return None, None
    return None, None


@router.post("/quest/start")
async def start_quest(body: QuestStart, r: aioredis.Redis = Depends(get_redis)):
    brand_id, quest = await _find_quest(r, body.quest_id)
    if not quest or not brand_id:
        raise HTTPException(404, detail=f"Quest {body.quest_id} not found")

    state_key = f"user:{body.user_id}:quest:{brand_id}:{body.quest_id}"
    existing_status = await r.hget(state_key, "status")
    if existing_status in {"active", "completed"}:
        return {
            "ok": True,
            "quest_id": body.quest_id,
            "status": existing_status,
            "already_started": True,
        }

    await r.hset(
        state_key,
        mapping={
            "status": "active",
            "current_step": 0,
            "steps_done": json.dumps([]),
            "started_at": int(time.time()),
            "brand_id": brand_id,
        },
    )
    await r.sadd(f"user:{body.user_id}:quests:{brand_id}", body.quest_id)
    return {
        "ok": True,
        "quest_id": body.quest_id,
        "brand_id": brand_id,
        "status": "active",
        "total_steps": len(quest.steps),
    }


async def _grant_quest_reward(
    r: aioredis.Redis, user_id: str, brand_id: str, reward: QuestReward
) -> dict[str, Any]:
    """Apply quest completion rewards. Returns summary of what was granted."""
    granted: dict[str, Any] = {}

    if reward.xp > 0:
        await r.incrby(f"user:{user_id}:xp", reward.xp)
        await r.zincrby(
            f"brand:{brand_id}:xp_leaderboard", reward.xp, user_id
        )
        granted["xp"] = reward.xp

    if reward.currency:
        granted_curr: dict[str, int] = {}
        for cname, amt in reward.currency.items():
            if amt <= 0:
                continue
            new_bal = await r.incrby(
                _currency_key(user_id, brand_id, cname), amt
            )
            await r.sadd(_currency_set_key(user_id, brand_id), cname)
            granted_curr[cname] = int(new_bal)
        if granted_curr:
            granted["currency"] = granted_curr

    if reward.item:
        granted_items: dict[str, int] = {}
        inv_key = f"user:{user_id}:inventory:{brand_id}"
        for item_id, qty in reward.item.items():
            if qty <= 0:
                continue
            meta = await r.hget(f"brand:{brand_id}:items", item_id)
            if not meta:
                continue
            try:
                it = Item(**json.loads(meta))
            except Exception:
                continue
            current = int(await r.hget(inv_key, item_id) or 0)
            if it.stackable:
                new_qty = min(current + qty, it.max_stack)
            else:
                new_qty = 1 if current < 1 else current
            await r.hset(inv_key, item_id, new_qty)
            granted_items[item_id] = new_qty
        if granted_items:
            granted["items"] = granted_items

    if reward.badge:
        exists = await r.hexists(f"brand:{brand_id}:badges", reward.badge)
        if exists:
            await r.sadd(f"user:{user_id}:badges", reward.badge)
            granted["badge"] = reward.badge

    return granted


@router.post("/quest/step")
async def advance_quest_step(
    body: QuestStepAdvance, r: aioredis.Redis = Depends(get_redis)
):
    """Advance a quest by completing the given step_index.

    Steps must be completed in order (step_index == current_step). The last
    step auto-completes the quest and grants total_reward.
    """
    brand_id, quest = await _find_quest(r, body.quest_id)
    if not quest or not brand_id:
        raise HTTPException(404, detail=f"Quest {body.quest_id} not found")

    state_key = f"user:{body.user_id}:quest:{brand_id}:{body.quest_id}"
    state = await r.hgetall(state_key)
    if not state:
        raise HTTPException(
            404, detail="Quest not started for this user; call /quest/start first"
        )

    status = state.get("status", "active")
    current_step = int(state.get("current_step", 0))

    if status == "completed":
        return {
            "ok": True,
            "quest_id": body.quest_id,
            "status": "completed",
            "current_step": current_step,
            "total_steps": len(quest.steps),
            "already_completed": True,
        }

    if body.step_index != current_step:
        raise HTTPException(
            422,
            detail={
                "error": "step_out_of_order",
                "expected_step_index": current_step,
                "got": body.step_index,
            },
        )
    if body.step_index >= len(quest.steps):
        raise HTTPException(422, detail="step_index beyond quest length")

    step = quest.steps[body.step_index]
    steps_done = json.loads(state.get("steps_done", "[]"))
    steps_done.append(body.step_index)

    # Per-step XP
    step_xp = 0
    if step.reward_xp > 0:
        await r.incrby(f"user:{body.user_id}:xp", step.reward_xp)
        await r.zincrby(
            f"brand:{brand_id}:xp_leaderboard", step.reward_xp, body.user_id
        )
        step_xp = step.reward_xp

    new_step = current_step + 1
    completed = new_step >= len(quest.steps)
    total_granted: dict[str, Any] = {}

    if completed:
        await r.hset(
            state_key,
            mapping={
                "status": "completed",
                "current_step": new_step,
                "steps_done": json.dumps(steps_done),
                "completed_at": int(time.time()),
            },
        )
        total_granted = await _grant_quest_reward(
            r, body.user_id, brand_id, quest.total_reward
        )
    else:
        await r.hset(
            state_key,
            mapping={
                "current_step": new_step,
                "steps_done": json.dumps(steps_done),
            },
        )

    new_xp = int(await r.get(f"user:{body.user_id}:xp") or 0)
    return {
        "ok": True,
        "quest_id": body.quest_id,
        "brand_id": brand_id,
        "step_completed": body.step_index,
        "current_step": new_step,
        "total_steps": len(quest.steps),
        "status": "completed" if completed else "active",
        "step_xp_awarded": step_xp,
        "completion_reward": total_granted,
        "total_xp": new_xp,
        "level": xp_to_level(new_xp),
    }


@router.get("/user/{user_id}/quests")
async def list_user_quests(
    user_id: str,
    brand_id: str,
    status: str = "",  # "active" | "completed" | "" (all)
    r: aioredis.Redis = Depends(get_redis),
):
    quest_ids = await r.smembers(f"user:{user_id}:quests:{brand_id}")
    out = []
    for qid in quest_ids:
        state_key = f"user:{user_id}:quest:{brand_id}:{qid}"
        state = await r.hgetall(state_key)
        if not state:
            continue
        s = state.get("status", "active")
        if status and s != status:
            continue
        quest_json = await r.hget(f"brand:{brand_id}:quests", qid)
        quest_dict = None
        if quest_json:
            try:
                quest_dict = json.loads(quest_json)
            except Exception:
                quest_dict = None
        out.append(
            {
                "quest_id": qid,
                "status": s,
                "current_step": int(state.get("current_step", 0)),
                "steps_done": json.loads(state.get("steps_done", "[]")),
                "quest": quest_dict,
            }
        )
    return {"user_id": user_id, "brand_id": brand_id, "quests": out}


# ═════════════════════════════════════════════════════════════════════════
# 5. TIER  (loyalty levels: Bronze / Silver / Gold / Platinum)
# ═════════════════════════════════════════════════════════════════════════


class Tier(BaseModel):
    id: str
    name: str
    threshold_xp: int = Field(ge=0)
    perks: list[str] = Field(default_factory=list)


@router.post("/brand/{brand_id}/tiers")
async def create_tier(
    brand_id: str, tier: Tier, r: aioredis.Redis = Depends(get_redis)
):
    await r.hset(f"brand:{brand_id}:tiers", tier.id, tier.model_dump_json())
    return {"ok": True, "brand_id": brand_id, "tier_id": tier.id}


@router.get("/brand/{brand_id}/tiers", response_model=list[Tier])
async def list_tiers(brand_id: str, r: aioredis.Redis = Depends(get_redis)):
    raw = await r.hgetall(f"brand:{brand_id}:tiers")
    out: list[Tier] = []
    for v in raw.values():
        try:
            out.append(Tier(**json.loads(v)))
        except Exception:
            continue
    out.sort(key=lambda t: t.threshold_xp)
    return out


async def _compute_tier(
    r: aioredis.Redis, brand_id: str, xp: int
) -> tuple[Tier | None, Tier | None]:
    """Return (current_tier, next_tier) for an XP value."""
    raw = await r.hgetall(f"brand:{brand_id}:tiers")
    tiers: list[Tier] = []
    for v in raw.values():
        try:
            tiers.append(Tier(**json.loads(v)))
        except Exception:
            continue
    if not tiers:
        return None, None
    tiers.sort(key=lambda t: t.threshold_xp)

    current: Tier | None = None
    nxt: Tier | None = None
    for t in tiers:
        if xp >= t.threshold_xp:
            current = t
        else:
            nxt = t
            break
    return current, nxt


@router.get("/user/{user_id}/tier")
async def get_user_tier(
    user_id: str, brand_id: str, r: aioredis.Redis = Depends(get_redis)
):
    """Return current tier + next + xp_to_next. Auto-promotes if XP qualifies."""
    xp = int(await r.get(f"user:{user_id}:xp") or 0)
    current, nxt = await _compute_tier(r, brand_id, xp)

    # Persist current tier; detect promotion vs stored
    stored_key = f"user:{user_id}:tier:{brand_id}"
    stored = await r.get(stored_key)
    promoted = False
    if current and current.id != stored:
        await r.set(stored_key, current.id)
        promoted = stored is not None  # promotion only if user had a prior tier

    return {
        "user_id": user_id,
        "brand_id": brand_id,
        "xp": xp,
        "current_tier": current.model_dump() if current else None,
        "next_tier": nxt.model_dump() if nxt else None,
        "xp_to_next": (nxt.threshold_xp - xp) if nxt else 0,
        "promoted": promoted,
    }


# ═════════════════════════════════════════════════════════════════════════
# 6. EVENT  (time-windowed campaigns w/ multipliers + opt-in)
# ═════════════════════════════════════════════════════════════════════════


class Event(BaseModel):
    id: str
    name: str
    description: str = ""
    start_at: int  # unix seconds
    end_at: int    # unix seconds
    modules_enabled: list[str] = Field(default_factory=list)
    multipliers: dict[str, float] = Field(default_factory=dict)  # {"xp": 2.0}
    reward_pool: dict[str, Any] = Field(default_factory=dict)


class EventOptIn(BaseModel):
    user_id: str


@router.post("/brand/{brand_id}/events")
async def create_event(
    brand_id: str, event: Event, r: aioredis.Redis = Depends(get_redis)
):
    if event.end_at <= event.start_at:
        raise HTTPException(422, detail="end_at must be after start_at")
    payload = event.model_dump()
    payload["brand_id"] = brand_id
    await r.hset(f"brand:{brand_id}:events", event.id, json.dumps(payload))
    return {"ok": True, "brand_id": brand_id, "event_id": event.id}


@router.get("/brand/{brand_id}/events")
async def list_events(
    brand_id: str,
    active: bool = False,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await r.hgetall(f"brand:{brand_id}:events")
    now = int(time.time())
    out = []
    for v in raw.values():
        try:
            ev = json.loads(v)
        except Exception:
            continue
        if active:
            if not (ev.get("start_at", 0) <= now <= ev.get("end_at", 0)):
                continue
        out.append(ev)
    return {"brand_id": brand_id, "events": out}


@router.post("/event/{event_id}/optin")
async def event_optin(
    event_id: str,
    body: EventOptIn,
    r: aioredis.Redis = Depends(get_redis),
):
    """Opt a user into an event. Validates event exists & is not ended."""
    # Find event across brands
    found_brand: str | None = None
    ev_dict: dict[str, Any] | None = None
    async for k in r.scan_iter(match="brand:*:events", count=100):
        v = await r.hget(k, event_id)
        if v:
            found_brand = k.split(":")[1]
            try:
                ev_dict = json.loads(v)
            except Exception:
                ev_dict = None
            break
    if not ev_dict or not found_brand:
        raise HTTPException(404, detail=f"Event {event_id} not found")

    now = int(time.time())
    if now > ev_dict.get("end_at", 0):
        raise HTTPException(422, detail="event has ended")

    optin_key = f"event:{event_id}:optins"
    added = await r.sadd(optin_key, body.user_id)
    return {
        "ok": True,
        "event_id": event_id,
        "brand_id": found_brand,
        "user_id": body.user_id,
        "already_opted_in": added == 0,
        "starts_in": max(0, ev_dict.get("start_at", 0) - now),
        "ends_in": max(0, ev_dict.get("end_at", 0) - now),
    }
