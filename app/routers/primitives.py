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
import logging
import math
import statistics
import time
import uuid
from typing import Any, Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.redis_client import get_redis
from app.routers.progression import xp_to_level

logger = logging.getLogger(__name__)

router = APIRouter()


async def _fire_attribute_changed(
    r: aioredis.Redis,
    *,
    user_id: str,
    brand_id: str | None,
    key: str,
    old_value: Any,
    new_value: Any,
    meta: dict[str, Any] | None = None,
) -> None:
    """Best-effort dispatch into rule_engine's attribute-watch rules.

    Failures are swallowed so a misbehaving rule cannot break the
    underlying attribute write. ``brand_id`` is normalised to "" for
    the global scope (matches rule_engine's brand key contract).
    """
    try:
        from app.routers.rule_engine import on_attribute_changed
    except ImportError:
        return
    try:
        await on_attribute_changed(
            r,
            user_id=user_id,
            brand_id=brand_id or "",
            key=key,
            old_value=old_value,
            new_value=new_value,
            meta=meta or {},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "on_attribute_changed failed for uid=%s key=%s: %s",
            user_id,
            key,
            e,
        )


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


# Legacy tier API — kept for back-compat. Prefer /tier/configure +
# /user/{uid}/tier?brand_id=... which read brand-scoped XP and return the
# canonical {name, xp_min} contract.
_LEGACY_TIER_DEPRECATION = (
    "Use POST /api/v1/primitives/tier/configure and "
    "GET /api/v1/primitives/user/{uid}/tier?brand_id=..."
)


@router.post("/brand/{brand_id}/tiers")
async def create_tier(
    brand_id: str, tier: Tier, r: aioredis.Redis = Depends(get_redis)
):
    """[DEPRECATED] Append one tier to the legacy ``brand:{bid}:tiers`` HASH.

    Prefer ``POST /tier/configure`` which sets the entire ladder in a
    single, brand-scoped XP-aware contract.
    """
    await r.hset(f"brand:{brand_id}:tiers", tier.id, tier.model_dump_json())
    return {
        "ok": True,
        "brand_id": brand_id,
        "tier_id": tier.id,
        "deprecated": _LEGACY_TIER_DEPRECATION,
    }


@router.get("/brand/{brand_id}/tiers")
async def list_tiers(brand_id: str, r: aioredis.Redis = Depends(get_redis)):
    """[DEPRECATED] List configured tiers for a brand.

    Reads from the legacy ``brand:{bid}:tiers`` HASH first, then falls
    back to the canonical ``tier_config:{bid}`` HASH so callers see the
    full ladder regardless of which API wrote it. The response includes
    a ``deprecated`` field pointing at the modern endpoints.
    """
    out: list[dict[str, Any]] = []
    raw = await r.hgetall(f"brand:{brand_id}:tiers")
    for v in raw.values():
        try:
            t = Tier(**json.loads(v))
            out.append(t.model_dump())
        except Exception:
            continue
    if not out:
        # Reconcile: surface canonical tier_config so callers don't see
        # a phantom-empty ladder just because they're querying the old key.
        canonical = await _read_tier_config(r, brand_id)
        for t in canonical:
            out.append(
                {
                    "id": t["name"],
                    "name": t["name"],
                    "threshold_xp": int(t.get("xp_min", 0)),
                    "perks": t.get("perks", []),
                }
            )
    out.sort(key=lambda d: int(d.get("threshold_xp", 0)))
    return {
        "brand_id": brand_id,
        "tiers": out,
        "deprecated": _LEGACY_TIER_DEPRECATION,
    }


async def _compute_tier(
    r: aioredis.Redis, brand_id: str, xp: int
) -> tuple[Tier | None, Tier | None]:
    """Return (current_tier, next_tier) for an XP value.

    NOTE: callers should pass an XP value resolved via ``_read_user_xp``
    (brand-scoped → global fallback). The function itself is XP-source-
    agnostic; it only consumes the int.
    """
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


async def _read_user_xp(
    r: aioredis.Redis, user_id: str, brand_id: str | None
) -> tuple[int, str | None]:
    """Read user XP. Source-of-truth = brand-scoped currency XP.

    Resolution order:
      1. brand_id given → ``user:{uid}:currency:{brand_id}:xp``
      2. No brand → aggregate across all brands the user has currency XP in.
      3. Fall back to legacy global ``user:{uid}:xp`` if no brand-scoped XP
         exists (back-compat with achievement/quest endpoints that still
         increment the legacy key).

    Returns (xp, resolved_brand_id). When aggregating, resolved_brand_id is
    None.
    """
    if brand_id:
        # Prefer brand-scoped currency XP; fall back to legacy global XP if
        # the brand has none yet (so freshly-onboarded users with quest XP
        # still tier correctly).
        scoped = int(await r.get(_currency_key(user_id, brand_id, "xp")) or 0)
        if scoped > 0:
            return scoped, brand_id
        legacy = int(await r.get(f"user:{user_id}:xp") or 0)
        return legacy, brand_id

    # Aggregate across brands by scanning currency keys.
    total = 0
    cursor = 0
    pattern = f"user:{user_id}:currency:*:xp"
    while True:
        cursor, batch = await r.scan(cursor=cursor, match=pattern, count=100)
        for k in batch:
            try:
                total += int(await r.get(k) or 0)
            except (TypeError, ValueError):
                continue
        if cursor == 0:
            break
    if total == 0:
        # Final fall-back to legacy global XP.
        total = int(await r.get(f"user:{user_id}:xp") or 0)
    return total, None


async def _read_tier_config(
    r: aioredis.Redis, brand_id: str
) -> list[dict[str, Any]]:
    """Read configured tier thresholds.

    Two stores are consulted, in order:
      1. ``tier_config:{brand_id}`` HASH (canonical, set via
         /primitives/tier/configure)
      2. legacy ``brand:{brand_id}:tiers`` HASH (Tier objects)

    Returns a list of dicts ``{name, xp_min, perks}`` sorted ascending.
    """
    out: list[dict[str, Any]] = []
    raw_cfg = await r.hgetall(f"tier_config:{brand_id}")
    if raw_cfg:
        for v in raw_cfg.values():
            try:
                d = json.loads(v)
                out.append(
                    {
                        "name": d.get("name", ""),
                        "xp_min": int(d.get("xp_min", 0)),
                        "perks": d.get("perks", []),
                    }
                )
            except Exception:
                continue
    else:
        raw_legacy = await r.hgetall(f"brand:{brand_id}:tiers")
        for v in raw_legacy.values():
            try:
                t = json.loads(v)
                out.append(
                    {
                        "name": t.get("name") or t.get("id", ""),
                        "xp_min": int(t.get("threshold_xp", 0)),
                        "perks": t.get("perks", []),
                    }
                )
            except Exception:
                continue
    out.sort(key=lambda d: d["xp_min"])
    return out


def _resolve_tier_from_config(
    tiers: list[dict[str, Any]], xp: int
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Returns (current, next) tier dicts for the given xp."""
    if not tiers:
        return None, None
    current: dict[str, Any] | None = None
    nxt: dict[str, Any] | None = None
    for t in tiers:
        if xp >= t["xp_min"]:
            current = t
        else:
            nxt = t
            break
    return current, nxt


# Global default tier ladder used when neither tier_config:{brand_id} nor
# brand:{brand_id}:tiers is configured. Keeps the endpoint useful for
# brand-agnostic / cross-brand aggregate lookups.
_DEFAULT_TIER_LADDER = [
    {"name": "guest", "xp_min": 0, "perks": []},
    {"name": "silver", "xp_min": 100, "perks": []},
    {"name": "gold", "xp_min": 1000, "perks": []},
    {"name": "vip", "xp_min": 10000, "perks": []},
]


@router.get("/user/{user_id}/tier")
async def get_user_tier(
    user_id: str,
    brand_id: str | None = None,
    region_id: str | None = None,
    master_id: str | None = None,
    scope: Literal["brand", "region", "master", "global"] | None = None,
    r: aioredis.Redis = Depends(get_redis),
):
    """Return current tier + next + progress.

    XP source-of-truth is brand-scoped: ``user:{uid}:currency:{brand_id}:xp``
    (written by /currency/xp/grant). If ``brand_id`` is omitted, XP is
    aggregated across every brand the user has XP in. Tier thresholds come
    from ``tier_config:{brand_id}`` (or legacy ``brand:{bid}:tiers``); a
    sensible global default is used when neither is configured.

    Round 5: an explicit ``scope`` parameter routes resolution through the
    master_accounts helper so callers can ask for region/master/global
    portability without juggling two endpoints. When ``scope`` is omitted
    the legacy brand-only path is used (back-compat).

    Auto-promotes the stored ``user:{uid}:tier:{brand_id}`` pointer if the
    current XP qualifies for a higher tier than the last known one.
    """
    # Scoped path — delegate to master_accounts.resolve_tier_for_scope.
    if scope:
        try:
            from app.routers.master_accounts import resolve_tier_for_scope
        except ImportError:
            resolve_tier_for_scope = None  # type: ignore
        if resolve_tier_for_scope is not None:
            payload = await resolve_tier_for_scope(
                r,
                user_id,
                scope=scope,
                brand_id=brand_id,
                region_id=region_id,
                master_id=master_id,
            )
            xp = int(payload.get("xp") or 0)
            current_name = payload.get("tier")
            next_threshold = payload.get("next_threshold")
            return {
                "user_id": user_id,
                "scope": scope,
                "brand_id": brand_id,
                "region_id": region_id,
                "master_id": payload.get("master_id") or master_id,
                "xp": xp,
                "tier": current_name,
                "next_tier_threshold": next_threshold,
                "scoped_payload": payload,
            }

    xp, resolved_brand = await _read_user_xp(r, user_id, brand_id)

    tiers: list[dict[str, Any]] = []
    if resolved_brand:
        tiers = await _read_tier_config(r, resolved_brand)
    if not tiers:
        tiers = _DEFAULT_TIER_LADDER

    current, nxt = _resolve_tier_from_config(tiers, xp)

    # Persist current tier pointer + detect promotion (only when scoped).
    promoted = False
    if resolved_brand and current:
        stored_key = f"user:{user_id}:tier:{resolved_brand}"
        stored = await r.get(stored_key)
        if current["name"] != stored:
            await r.set(stored_key, current["name"])
            promoted = stored is not None

    # Progress toward next tier.
    if nxt and current:
        span = max(nxt["xp_min"] - current["xp_min"], 1)
        progress_pct = min(100.0, max(0.0, ((xp - current["xp_min"]) / span) * 100.0))
    elif nxt and not current:
        progress_pct = min(100.0, max(0.0, (xp / max(nxt["xp_min"], 1)) * 100.0))
    else:
        progress_pct = 100.0

    next_threshold = nxt["xp_min"] if nxt else None

    return {
        "user_id": user_id,
        "brand_id": resolved_brand,
        "xp": xp,
        "tier": current["name"] if current else None,
        "current_tier": current,
        "next_tier": nxt,
        "next_tier_threshold": next_threshold,
        "xp_to_next": (nxt["xp_min"] - xp) if nxt else 0,
        "progress_pct": round(progress_pct, 2),
        "promoted": promoted,
    }


# ── Tier configuration (canonical) ───────────────────────────────────────


class TierThreshold(BaseModel):
    name: str
    xp_min: int = Field(ge=0)
    perks: list[str] = Field(default_factory=list)


class TierConfigure(BaseModel):
    brand_id: str
    tiers: list[TierThreshold]


@router.post("/tier/configure")
async def configure_tiers(
    body: TierConfigure, r: aioredis.Redis = Depends(get_redis)
):
    """Set the tier ladder for a brand.

    Stored at ``tier_config:{brand_id}`` HASH keyed by tier name. Overwrites
    any previous configuration for the brand. Use this instead of
    /brand/{brand_id}/tiers when you want the lightweight {name, xp_min}
    contract that /user/{uid}/tier consumes.
    """
    if not body.tiers:
        raise HTTPException(422, detail="at least one tier required")
    seen_names: set[str] = set()
    seen_thresholds: set[int] = set()
    for t in body.tiers:
        if t.name in seen_names:
            raise HTTPException(422, detail=f"duplicate tier name: {t.name}")
        if t.xp_min in seen_thresholds:
            raise HTTPException(422, detail=f"duplicate xp_min: {t.xp_min}")
        seen_names.add(t.name)
        seen_thresholds.add(t.xp_min)

    key = f"tier_config:{body.brand_id}"
    # Replace, don't merge.
    await r.delete(key)
    mapping = {t.name: json.dumps(t.model_dump()) for t in body.tiers}
    await r.hset(key, mapping=mapping)
    return {
        "ok": True,
        "brand_id": body.brand_id,
        "tier_count": len(body.tiers),
        "tiers": [t.model_dump() for t in body.tiers],
    }


# ═════════════════════════════════════════════════════════════════════════
# 5b. USER ATTRIBUTES  (free-form key/value + lifecycle stage)
# ═════════════════════════════════════════════════════════════════════════
#
# Used for life-stage segmentation ("new_mom_0_3mo"), declared preferences,
# inferred traits, etc. All values are stored as strings — clients serialize
# objects/lists themselves (Redis HASH constraint). Two scopes:
#   * Global:        user:{uid}:attributes
#   * Brand-scoped:  user:{uid}:attributes:{brand_id}
#
# Per-key TTLs (used by /attributes/{key} POST) are tracked in a sidecar
# string key user:{uid}:attribute:{scope}:{key} that mirrors the value and
# expires; the HASH copy is best-effort and the sidecar wins on read when
# present.


class AttributesSet(BaseModel):
    brand_id: str | None = None
    attrs: dict[str, Any]


class AttributeSet(BaseModel):
    value: Any
    ttl_seconds: int | None = Field(default=None, ge=1)


class LifecycleStageSet(BaseModel):
    stage: str
    source: str = "self_declared"  # self_declared | inferred | verified
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


_VALID_LIFECYCLE_SOURCES = {"self_declared", "inferred", "verified"}


def _attr_hash_key(user_id: str, brand_id: str | None) -> str:
    if brand_id:
        return f"user:{user_id}:attributes:{brand_id}"
    return f"user:{user_id}:attributes"


def _attr_sidecar_key(user_id: str, brand_id: str | None, key: str) -> str:
    if brand_id:
        return f"user:{user_id}:attribute:{brand_id}:{key}"
    return f"user:{user_id}:attribute:global:{key}"


def _serialize_attr_value(value: Any) -> str:
    """Coerce a JSON-ish value to its stored string form."""
    if isinstance(value, str):
        return value
    return json.dumps(value)


@router.post("/user/{user_id}/attributes")
async def set_user_attributes(
    user_id: str,
    body: AttributesSet,
    r: aioredis.Redis = Depends(get_redis),
):
    """Bulk-set arbitrary attributes. Values are always stored as strings."""
    if not body.attrs:
        raise HTTPException(422, detail="attrs must be non-empty")
    key = _attr_hash_key(user_id, body.brand_id)
    mapping = {k: _serialize_attr_value(v) for k, v in body.attrs.items()}

    # Snapshot prior values so we can fire attribute-watch rules with
    # both old + new. Order matches mapping iteration.
    prior_raw = await r.hmget(key, *mapping.keys()) if mapping else []
    prior = {
        field: prior_raw[i] for i, field in enumerate(mapping.keys())
    }

    await r.hset(key, mapping=mapping)

    for field, new_val in mapping.items():
        await _fire_attribute_changed(
            r,
            user_id=user_id,
            brand_id=body.brand_id,
            key=field,
            old_value=prior.get(field),
            new_value=new_val,
            meta=None,
        )

    return {
        "ok": True,
        "user_id": user_id,
        "brand_id": body.brand_id,
        "scope": "brand" if body.brand_id else "global",
        "attrs_set": list(mapping.keys()),
        "count": len(mapping),
    }


@router.get("/user/{user_id}/attributes")
async def get_user_attributes(
    user_id: str,
    brand_id: str | None = None,
    key: str | None = None,
    r: aioredis.Redis = Depends(get_redis),
):
    """Get all attrs (or a single key) for a user under a given scope."""
    hkey = _attr_hash_key(user_id, brand_id)
    if key:
        # Prefer sidecar (carries TTL) when present.
        sidecar = await r.get(_attr_sidecar_key(user_id, brand_id, key))
        if sidecar is not None:
            return {
                "user_id": user_id,
                "brand_id": brand_id,
                "key": key,
                "value": sidecar,
            }
        v = await r.hget(hkey, key)
        return {
            "user_id": user_id,
            "brand_id": brand_id,
            "key": key,
            "value": v,
        }
    raw = await r.hgetall(hkey) or {}
    return {
        "user_id": user_id,
        "brand_id": brand_id,
        "scope": "brand" if brand_id else "global",
        "attrs": raw,
        "count": len(raw),
    }


@router.post("/user/{user_id}/attributes/{key}")
async def set_user_attribute(
    user_id: str,
    key: str,
    body: AttributeSet,
    brand_id: str | None = None,
    r: aioredis.Redis = Depends(get_redis),
):
    """Set a single attribute, optionally with a TTL."""
    value = _serialize_attr_value(body.value)
    hkey = _attr_hash_key(user_id, brand_id)
    old_value = await r.hget(hkey, key)
    pipe = r.pipeline()
    pipe.hset(hkey, key, value)
    if body.ttl_seconds:
        side = _attr_sidecar_key(user_id, brand_id, key)
        pipe.set(side, value, ex=body.ttl_seconds)
    await pipe.execute()

    await _fire_attribute_changed(
        r,
        user_id=user_id,
        brand_id=brand_id,
        key=key,
        old_value=old_value,
        new_value=value,
        meta=None,
    )

    return {
        "ok": True,
        "user_id": user_id,
        "brand_id": brand_id,
        "key": key,
        "value": value,
        "ttl_seconds": body.ttl_seconds,
    }


@router.delete("/user/{user_id}/attributes/{key}")
async def delete_user_attribute(
    user_id: str,
    key: str,
    brand_id: str | None = None,
    r: aioredis.Redis = Depends(get_redis),
):
    """Remove a single attribute (HASH field + any sidecar)."""
    hkey = _attr_hash_key(user_id, brand_id)
    pipe = r.pipeline()
    pipe.hdel(hkey, key)
    pipe.delete(_attr_sidecar_key(user_id, brand_id, key))
    res = await pipe.execute()
    return {
        "ok": True,
        "user_id": user_id,
        "brand_id": brand_id,
        "key": key,
        "removed": bool(res[0]) if res else False,
    }


@router.post("/user/{user_id}/attributes/lifecycle-stage")
async def set_lifecycle_stage(
    user_id: str,
    body: LifecycleStageSet,
    r: aioredis.Redis = Depends(get_redis),
):
    """Typed shortcut for life-stage segmentation.

    Common stages used by maternity / family brands:
      new_mom_0_3mo, new_mom_3_6mo, new_mom_6_12mo,
      toddler_1_2yr, kid_2_5yr, kid_5_10yr, teen, adult

    The value is free-form so brands can introduce their own taxonomy.
    """
    if body.source not in _VALID_LIFECYCLE_SOURCES:
        raise HTTPException(
            422,
            detail=f"source must be one of {sorted(_VALID_LIFECYCLE_SOURCES)}",
        )
    payload = {
        "stage": body.stage,
        "source": body.source,
        "confidence": (
            f"{body.confidence:.4f}" if body.confidence is not None else ""
        ),
        "set_at": str(int(time.time())),
    }
    await r.hset(f"user:{user_id}:lifecycle_stage", mapping=payload)
    return {
        "ok": True,
        "user_id": user_id,
        **payload,
        "confidence": body.confidence,
    }


@router.get("/user/{user_id}/attributes/lifecycle-stage")
async def get_lifecycle_stage(
    user_id: str, r: aioredis.Redis = Depends(get_redis)
):
    raw = await r.hgetall(f"user:{user_id}:lifecycle_stage")
    if not raw:
        return {"user_id": user_id, "stage": None}
    confidence = raw.get("confidence")
    try:
        confidence_val = float(confidence) if confidence else None
    except (TypeError, ValueError):
        confidence_val = None
    return {
        "user_id": user_id,
        "stage": raw.get("stage"),
        "source": raw.get("source"),
        "confidence": confidence_val,
        "set_at": int(raw.get("set_at", 0) or 0),
    }


# ═════════════════════════════════════════════════════════════════════════
# 5c. ATTRIBUTE TIME-SERIES LOG
# ═════════════════════════════════════════════════════════════════════════
#
# Append-only history per (user, attribute) for "weight over time", "PR over
# time", "mood/sleep streams". Coexists with the existing "current value"
# HASH (5b). Each log entry is a JSON blob in a Redis LIST (newest first).
#
# Keys
#   user:{uid}:attr_log:{key}                       LIST (global scope)
#   user:{uid}:attr_log:{brand_id}:{key}            LIST (brand scope)
#   user:{uid}:attr_log:{key}:stats                 HASH cache (TTL 1h)
#   user:{uid}:attr_log:{brand_id}:{key}:stats      HASH cache (TTL 1h)

_ATTR_LOG_MAX_ENTRIES = 5000
_ATTR_LOG_STATS_TTL = 3600


def _attr_log_key(user_id: str, brand_id: str | None, key: str) -> str:
    if brand_id:
        return f"user:{user_id}:attr_log:{brand_id}:{key}"
    return f"user:{user_id}:attr_log:{key}"


def _attr_log_stats_key(user_id: str, brand_id: str | None, key: str) -> str:
    return _attr_log_key(user_id, brand_id, key) + ":stats"


def _try_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return None


class AttrLogEntry(BaseModel):
    value: Any
    brand_id: str | None = None
    ts: int | None = None
    source: Literal["self_declared", "measured", "inferred"] = "self_declared"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    meta: dict[str, Any] | None = None


@router.post("/user/{user_id}/attributes/{key}/log")
async def log_user_attribute(
    user_id: str,
    key: str,
    body: AttrLogEntry,
    r: aioredis.Redis = Depends(get_redis),
):
    """Append a value to the time-series log for `key`.

    Also updates the "current value" HASH (5b) for backwards compat: the
    most recent log entry IS the current value.
    """
    user_id = await resolve_canonical(r, user_id)
    ts = int(body.ts) if body.ts is not None else int(time.time())
    entry_id = uuid.uuid4().hex[:16]
    serialized_value = _serialize_attr_value(body.value)

    entry = {
        "entry_id": entry_id,
        "ts": ts,
        "value": serialized_value,
        "source": body.source,
    }
    if body.confidence is not None:
        entry["confidence"] = round(float(body.confidence), 4)
    if body.meta:
        entry["meta"] = body.meta

    log_key = _attr_log_key(user_id, body.brand_id, key)
    hash_key = _attr_hash_key(user_id, body.brand_id)
    stats_key = _attr_log_stats_key(user_id, body.brand_id, key)

    # Snapshot previous current value for rule_engine dispatch.
    old_value = await r.hget(hash_key, key)

    pipe = r.pipeline()
    pipe.lpush(log_key, json.dumps(entry))
    pipe.ltrim(log_key, 0, _ATTR_LOG_MAX_ENTRIES - 1)
    pipe.hset(hash_key, key, serialized_value)
    pipe.delete(stats_key)  # invalidate cached stats
    await pipe.execute()

    await _fire_attribute_changed(
        r,
        user_id=user_id,
        brand_id=body.brand_id,
        key=key,
        old_value=old_value,
        new_value=serialized_value,
        meta=body.meta or {},
    )

    return {
        "ok": True,
        "user_id": user_id,
        "brand_id": body.brand_id,
        "key": key,
        "entry_id": entry_id,
        "ts": ts,
        "value": serialized_value,
    }


def _decode_log_entries(raw: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in raw:
        try:
            out.append(json.loads(item))
        except Exception:
            continue
    return out


def _numeric_stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    s = {
        "min": min(values),
        "max": max(values),
        "avg": sum(values) / len(values),
        "median": statistics.median(values),
    }
    if len(values) >= 2:
        s["std"] = statistics.pstdev(values)
    else:
        s["std"] = 0.0
    return {k: round(v, 6) for k, v in s.items()}


@router.get("/user/{user_id}/attributes/{key}/history")
async def get_user_attribute_history(
    user_id: str,
    key: str,
    brand_id: str | None = None,
    from_ts: int | None = None,
    to_ts: int | None = None,
    limit: int = 100,
    order: Literal["asc", "desc"] = "desc",
    r: aioredis.Redis = Depends(get_redis),
):
    """Return chronological history + summary stats for `key`."""
    user_id = await resolve_canonical(r, user_id)
    if limit < 1:
        limit = 1
    if limit > _ATTR_LOG_MAX_ENTRIES:
        limit = _ATTR_LOG_MAX_ENTRIES

    log_key = _attr_log_key(user_id, brand_id, key)
    raw = await r.lrange(log_key, 0, _ATTR_LOG_MAX_ENTRIES - 1) or []
    entries = _decode_log_entries(raw)  # newest first

    # Filter by timestamp range.
    if from_ts is not None:
        entries = [e for e in entries if int(e.get("ts", 0)) >= from_ts]
    if to_ts is not None:
        entries = [e for e in entries if int(e.get("ts", 0)) <= to_ts]

    count = len(entries)
    latest = entries[0] if entries else None
    earliest = entries[-1] if entries else None

    # Numeric stats (if every value is parseable as float).
    numeric_values: list[float] = []
    all_numeric = bool(entries)
    for e in entries:
        v = _try_float(e.get("value"))
        if v is None:
            all_numeric = False
            break
        numeric_values.append(v)

    stats: dict[str, float] = {}
    delta: dict[str, float | None] = {}
    if all_numeric and numeric_values:
        stats = _numeric_stats(numeric_values)
        first_val = numeric_values[-1]  # earliest
        last_val = numeric_values[0]    # latest
        delta["from_first"] = round(last_val - first_val, 6)
        delta["from_last"] = 0.0
        now = int(time.time())
        cutoff_30d = now - 30 * 86400
        prior = [
            _try_float(e.get("value"))
            for e in entries
            if int(e.get("ts", 0)) <= cutoff_30d
        ]
        prior_vals = [p for p in prior if p is not None]
        if prior_vals:
            delta["vs_30d_ago"] = round(last_val - prior_vals[0], 6)
        else:
            delta["vs_30d_ago"] = None

    # Order + limit for output.
    out_entries = entries if order == "desc" else list(reversed(entries))
    out_entries = out_entries[:limit]

    return {
        "user_id": user_id,
        "brand_id": brand_id,
        "key": key,
        "count": count,
        "latest": (
            {"value": latest.get("value"), "ts": int(latest.get("ts", 0))}
            if latest
            else None
        ),
        "earliest": (
            {"value": earliest.get("value"), "ts": int(earliest.get("ts", 0))}
            if earliest
            else None
        ),
        "numeric": all_numeric and bool(numeric_values),
        "stats": stats,
        "delta": delta,
        "entries": out_entries,
    }


@router.get("/user/{user_id}/attributes/{key}/trend")
async def get_user_attribute_trend(
    user_id: str,
    key: str,
    window_days: int = 30,
    brand_id: str | None = None,
    r: aioredis.Redis = Depends(get_redis),
):
    """Linear-regression slope over the last `window_days`.

    Returns direction (improving / declining / stable), slope per day, and
    milestones crossed (round-number boundaries between earliest and latest
    value inside the window).
    """
    user_id = await resolve_canonical(r, user_id)
    if window_days < 1:
        window_days = 1
    log_key = _attr_log_key(user_id, brand_id, key)
    raw = await r.lrange(log_key, 0, _ATTR_LOG_MAX_ENTRIES - 1) or []
    entries = _decode_log_entries(raw)
    now = int(time.time())
    cutoff = now - window_days * 86400
    window = [e for e in entries if int(e.get("ts", 0)) >= cutoff]

    # Need at least 2 numeric points for a slope.
    points: list[tuple[float, float]] = []
    for e in window:
        v = _try_float(e.get("value"))
        if v is None:
            continue
        points.append((float(int(e.get("ts", 0))), v))

    if len(points) < 2:
        return {
            "user_id": user_id,
            "brand_id": brand_id,
            "key": key,
            "window_days": window_days,
            "direction": "stable",
            "slope": 0.0,
            "slope_per_day": 0.0,
            "point_count": len(points),
            "milestones_crossed": [],
        }

    # Ordinary least squares.
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    n = len(points)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    slope_per_sec = num / den if den else 0.0
    slope_per_day = slope_per_sec * 86400

    # Direction relative to noise (std/window).
    spread = max(ys) - min(ys)
    if spread == 0 or abs(slope_per_day) < (spread / max(window_days, 1)) * 0.05:
        direction = "stable"
    elif slope_per_day > 0:
        direction = "improving"
    else:
        direction = "declining"

    # Milestones: integer boundaries crossed between earliest and latest.
    # points sorted by ts ascending so we walk chronologically.
    points_sorted = sorted(points, key=lambda p: p[0])
    milestones: list[dict[str, Any]] = []
    if len(points_sorted) >= 2:
        for i in range(1, len(points_sorted)):
            prev_v = points_sorted[i - 1][1]
            cur_v = points_sorted[i][1]
            lo, hi = (prev_v, cur_v) if prev_v < cur_v else (cur_v, prev_v)
            # Choose milestone granularity from value magnitude.
            mag = max(abs(prev_v), abs(cur_v), 1.0)
            step = 10 ** max(0, int(math.log10(mag)) - 1) if mag >= 10 else 1
            start = math.ceil(lo / step) * step
            m = start
            while m <= hi:
                if lo < m <= hi:
                    milestones.append(
                        {"value": m, "ts": int(points_sorted[i][0])}
                    )
                m += step
                if len(milestones) > 50:
                    break
            if len(milestones) > 50:
                break

    return {
        "user_id": user_id,
        "brand_id": brand_id,
        "key": key,
        "window_days": window_days,
        "direction": direction,
        "slope": round(slope_per_sec, 9),
        "slope_per_day": round(slope_per_day, 6),
        "point_count": n,
        "milestones_crossed": milestones,
    }


# ═════════════════════════════════════════════════════════════════════════
# 5d. USER RELATIONSHIPS  (parent_of / spouse / employee / buddy / …)
# ═════════════════════════════════════════════════════════════════════════
#
# First-class graph edges between users — replaces the K12 pattern of
# stuffing parent/child links into attribute JSON blobs.
#
# Keys
#   user:{uid}:relationships                          HASH related_uid → JSON
#   user:{uid}:relationship_by_type:{type}            SET of related_uids

_RELATIONSHIP_REVERSE_MAP: dict[str, str] = {
    # ── Existing core ────────────────────────────────────────────────
    "parent_of": "child_of",
    "child_of": "parent_of",
    "spouse": "spouse",
    "sibling": "sibling",
    "household_member": "household_member",
    "guardian": "ward",
    "ward": "guardian",
    "employee": "manager",
    "manager": "employee",
    "buddy": "buddy",
    "emergency_contact": "primary_user",
    "primary_user": "emergency_contact",
    # ── Service relationships (agent/doctor/teacher/trainer/stylist) ──
    "agent_of": "client_of",
    "client_of": "agent_of",
    "doctor_of": "patient_of",
    "patient_of": "doctor_of",
    "teacher_of": "student_of",
    "student_of": "teacher_of",
    "trainer_of": "trainee_of",
    "trainee_of": "trainer_of",
    "stylist_of": "client_of",  # polymorphic: stylist→client (also doctor→client)
    # ── Logistics ────────────────────────────────────────────────────
    "sender_of": "recipient_of",
    "recipient_of": "sender_of",
    "courier_of": "delivery_for",
    "delivery_for": "courier_of",
    # ── Ownership (human→entity, also generic) ───────────────────────
    "owns": "owned_by",
    "owned_by": "owns",
    # ── B2B ──────────────────────────────────────────────────────────
    "supplier_of": "buyer_of",
    "buyer_of": "supplier_of",
    "partner_with": "partner_with",  # symmetric
    # ── Pet / Entity (alt naming) ────────────────────────────────────
    "guardian_of": "ward_of",
    "ward_of": "guardian_of",
}

_VALID_RELATIONSHIPS = set(_RELATIONSHIP_REVERSE_MAP.keys())


def _rel_hash_key(user_id: str) -> str:
    return f"user:{user_id}:relationships"


def _rel_index_key(user_id: str, rel_type: str) -> str:
    return f"user:{user_id}:relationship_by_type:{rel_type}"


class RelationshipCreate(BaseModel):
    related_user_id: str
    relationship: str
    bidirectional: bool = True
    meta: dict[str, Any] | None = None


class RelationshipLookup(BaseModel):
    relationship: str


async def _write_relationship_edge(
    r: aioredis.Redis,
    src: str,
    dst: str,
    rel_type: str,
    meta: dict[str, Any] | None,
) -> str:
    rel_id = uuid.uuid4().hex[:16]
    payload = {
        "relationship_id": rel_id,
        "relationship": rel_type,
        "meta": meta or {},
        "created_at": int(time.time()),
    }
    pipe = r.pipeline()
    pipe.hset(_rel_hash_key(src), dst, json.dumps(payload))
    pipe.sadd(_rel_index_key(src, rel_type), dst)
    await pipe.execute()
    return rel_id


@router.post("/users/{user_id}/relationships")
async def create_user_relationship(
    user_id: str,
    body: RelationshipCreate,
    r: aioredis.Redis = Depends(get_redis),
):
    """Create a relationship edge user_id --rel--> related_user_id.

    If bidirectional, the reverse edge (per REVERSE_MAP) is also created.
    """
    if body.relationship not in _VALID_RELATIONSHIPS:
        raise HTTPException(
            422,
            detail=(
                f"relationship must be one of "
                f"{sorted(_VALID_RELATIONSHIPS)}"
            ),
        )
    user_id = await resolve_canonical(r, user_id)
    related_id = await resolve_canonical(r, body.related_user_id)
    if user_id == related_id:
        raise HTTPException(422, detail="cannot relate user to itself")

    rel_id = await _write_relationship_edge(
        r, user_id, related_id, body.relationship, body.meta
    )
    reverse_rel_id: str | None = None
    if body.bidirectional:
        rev_type = _RELATIONSHIP_REVERSE_MAP[body.relationship]
        reverse_rel_id = await _write_relationship_edge(
            r, related_id, user_id, rev_type, body.meta
        )

    return {
        "ok": True,
        "user_id": user_id,
        "related_user_id": related_id,
        "relationship_id": rel_id,
        "relationship": body.relationship,
        "bidirectional": body.bidirectional,
        "reverse_relationship_id": reverse_rel_id,
    }


@router.get("/users/{user_id}/relationships")
async def list_user_relationships(
    user_id: str,
    relationship: str | None = None,
    depth: int = 1,
    r: aioredis.Redis = Depends(get_redis),
):
    """List all (or a specific type of) relationships for `user_id`.

    `depth` is currently 1 (direct edges only); reserved for future
    transitive expansion.
    """
    user_id = await resolve_canonical(r, user_id)
    raw = await r.hgetall(_rel_hash_key(user_id)) or {}
    out: list[dict[str, Any]] = []
    for related_uid, payload_json in raw.items():
        try:
            payload = json.loads(payload_json)
        except Exception:
            continue
        if relationship and payload.get("relationship") != relationship:
            continue
        out.append(
            {
                "related_user_id": related_uid,
                "relationship": payload.get("relationship"),
                "meta": payload.get("meta") or {},
                "created_at": int(payload.get("created_at", 0) or 0),
                "relationship_id": payload.get("relationship_id"),
            }
        )

    return {
        "user_id": user_id,
        "depth": depth,
        "filter": relationship,
        "count": len(out),
        "relationships": out,
    }


@router.delete("/users/{user_id}/relationships/{related_user_id}")
async def delete_user_relationship(
    user_id: str,
    related_user_id: str,
    bidirectional: bool = True,
    r: aioredis.Redis = Depends(get_redis),
):
    """Delete an edge. If bidirectional, both directions are removed."""
    user_id = await resolve_canonical(r, user_id)
    related_user_id = await resolve_canonical(r, related_user_id)

    fwd_raw = await r.hget(_rel_hash_key(user_id), related_user_id)
    rev_raw = await r.hget(_rel_hash_key(related_user_id), user_id)

    removed_forward = False
    removed_reverse = False

    if fwd_raw:
        try:
            fwd_payload = json.loads(fwd_raw)
            fwd_type = fwd_payload.get("relationship")
        except Exception:
            fwd_type = None
        pipe = r.pipeline()
        pipe.hdel(_rel_hash_key(user_id), related_user_id)
        if fwd_type:
            pipe.srem(_rel_index_key(user_id, fwd_type), related_user_id)
        res = await pipe.execute()
        removed_forward = bool(res[0])

    if bidirectional and rev_raw:
        try:
            rev_payload = json.loads(rev_raw)
            rev_type = rev_payload.get("relationship")
        except Exception:
            rev_type = None
        pipe = r.pipeline()
        pipe.hdel(_rel_hash_key(related_user_id), user_id)
        if rev_type:
            pipe.srem(_rel_index_key(related_user_id, rev_type), user_id)
        res = await pipe.execute()
        removed_reverse = bool(res[0])

    return {
        "ok": True,
        "user_id": user_id,
        "related_user_id": related_user_id,
        "removed_forward": removed_forward,
        "removed_reverse": removed_reverse,
        "bidirectional": bidirectional,
    }


@router.post("/users/{user_id}/relationships/lookup")
async def lookup_user_relationships(
    user_id: str,
    body: RelationshipLookup,
    r: aioredis.Redis = Depends(get_redis),
):
    """Fast set-lookup of one specific relationship type."""
    if body.relationship not in _VALID_RELATIONSHIPS:
        raise HTTPException(
            422,
            detail=(
                f"relationship must be one of "
                f"{sorted(_VALID_RELATIONSHIPS)}"
            ),
        )
    user_id = await resolve_canonical(r, user_id)
    members = await r.smembers(_rel_index_key(user_id, body.relationship))
    return {
        "user_id": user_id,
        "relationship": body.relationship,
        "related_user_ids": sorted(members or []),
        "count": len(members or []),
    }


# ═════════════════════════════════════════════════════════════════════════
# 5d-bis. RESOURCES  (stylist / doctor / property / agent / room / vehicle)
# ═════════════════════════════════════════════════════════════════════════
#
# A *resource* is a bookable, brand-owned slot of capacity — a stylist's
# chair, a doctor's calendar, a hotel room, a car. Decoupled from the
# reservations module: reservations *reference* resources by id, but
# resources are usable for any availability/scheduling primitive (waitlists,
# capacity caps, etc.).
#
# Keys
#   brand:{bid}:resources                       SET of resource_ids
#   brand:{bid}:resources:by_type:{type}        SET of resource_ids
#   resource:{rid}                              HASH (full state)
#   resource:{rid}:stats                        HASH counters (bookings_30d, ...)

_VALID_RESOURCE_TYPES = {
    "stylist", "doctor", "property", "agent",
    "trainer", "room", "vehicle", "slot", "other",
}


def _resource_key(rid: str) -> str:
    return f"resource:{rid}"


def _resource_stats_key(rid: str) -> str:
    return f"resource:{rid}:stats"


def _brand_resources_key(bid: str) -> str:
    return f"brand:{bid}:resources"


def _brand_resources_by_type_key(bid: str, rtype: str) -> str:
    return f"brand:{bid}:resources:by_type:{rtype}"


class ResourceCreate(BaseModel):
    resource_id: str = Field(..., min_length=1, max_length=128)
    type: str = Field(..., min_length=1, max_length=64)
    name: str = Field("", max_length=256)
    attributes: dict[str, Any] = Field(default_factory=dict)
    schedule: dict[str, Any] = Field(default_factory=dict)
    active: bool = True


class ResourceStatsUpdate(BaseModel):
    bookings_30d: int | None = Field(None, ge=0)
    utilization_pct: float | None = Field(None, ge=0.0, le=100.0)
    avg_rating: float | None = Field(None, ge=0.0, le=5.0)
    no_show_rate: float | None = Field(None, ge=0.0, le=1.0)


def _resource_hash_to_dict(raw: dict[str, str]) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        attrs = json.loads(raw.get("attributes") or "{}")
    except Exception:
        attrs = {}
    try:
        schedule = json.loads(raw.get("schedule") or "{}")
    except Exception:
        schedule = {}
    return {
        "resource_id": raw.get("resource_id", ""),
        "brand_id": raw.get("brand_id", ""),
        "type": raw.get("type", ""),
        "name": raw.get("name", ""),
        "attributes": attrs,
        "schedule": schedule,
        "active": raw.get("active", "1") == "1",
        "created_at": int(raw.get("created_at") or 0),
        "updated_at": int(raw.get("updated_at") or 0),
    }


@router.post("/brand/{brand_id}/resources")
async def create_resource(
    brand_id: str,
    body: ResourceCreate,
    r: aioredis.Redis = Depends(get_redis),
):
    """Register a bookable resource under a brand.

    The ``type`` is free-form but should normally be one of
    ``stylist|doctor|property|agent|trainer|room|vehicle|slot|other``.
    """
    # Soft validation: unknown types accepted so brands can introduce
    # vocabulary, but normal types should match _VALID_RESOURCE_TYPES.
    if await r.hexists(_resource_key(body.resource_id), "resource_id"):
        raise HTTPException(
            409,
            detail=f"resource {body.resource_id} already exists",
        )

    now = int(time.time())
    state = {
        "resource_id": body.resource_id,
        "brand_id": brand_id,
        "type": body.type,
        "name": body.name,
        "attributes": json.dumps(body.attributes),
        "schedule": json.dumps(body.schedule),
        "active": "1" if body.active else "0",
        "created_at": str(now),
        "updated_at": str(now),
    }
    pipe = r.pipeline()
    pipe.hset(_resource_key(body.resource_id), mapping=state)
    pipe.sadd(_brand_resources_key(brand_id), body.resource_id)
    pipe.sadd(_brand_resources_by_type_key(brand_id, body.type), body.resource_id)
    await pipe.execute()
    return {
        "ok": True,
        "brand_id": brand_id,
        "resource_id": body.resource_id,
        "type": body.type,
        "active": body.active,
    }


@router.get("/brand/{brand_id}/resources")
async def list_resources(
    brand_id: str,
    type: str | None = None,
    active: bool | None = None,
    r: aioredis.Redis = Depends(get_redis),
):
    """List a brand's resources, optionally filtered by type and active flag."""
    if type:
        members = await r.smembers(_brand_resources_by_type_key(brand_id, type))
    else:
        members = await r.smembers(_brand_resources_key(brand_id))
    out: list[dict[str, Any]] = []
    for rid in members or []:
        raw = await r.hgetall(_resource_key(rid))
        if not raw:
            continue
        item = _resource_hash_to_dict(raw)
        if active is not None and item.get("active") != active:
            continue
        out.append(item)
    out.sort(key=lambda d: d.get("resource_id", ""))
    return {
        "brand_id": brand_id,
        "type": type,
        "count": len(out),
        "resources": out,
    }


@router.get("/brand/{brand_id}/resources/{resource_id}")
async def get_resource(
    brand_id: str,
    resource_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await r.hgetall(_resource_key(resource_id))
    if not raw:
        raise HTTPException(404, detail=f"resource {resource_id} not found")
    out = _resource_hash_to_dict(raw)
    if out.get("brand_id") and out["brand_id"] != brand_id:
        raise HTTPException(
            404,
            detail=f"resource {resource_id} not owned by brand {brand_id}",
        )
    return out


@router.delete("/brand/{brand_id}/resources/{resource_id}")
async def delete_resource(
    brand_id: str,
    resource_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await r.hgetall(_resource_key(resource_id))
    if not raw:
        return {"ok": True, "deleted": False, "reason": "not_found"}
    if raw.get("brand_id") and raw["brand_id"] != brand_id:
        raise HTTPException(
            404,
            detail=f"resource {resource_id} not owned by brand {brand_id}",
        )
    rtype = raw.get("type", "")
    pipe = r.pipeline()
    pipe.delete(_resource_key(resource_id))
    pipe.delete(_resource_stats_key(resource_id))
    pipe.srem(_brand_resources_key(brand_id), resource_id)
    if rtype:
        pipe.srem(_brand_resources_by_type_key(brand_id, rtype), resource_id)
    res = await pipe.execute()
    return {
        "ok": True,
        "deleted": bool(res[0]),
        "resource_id": resource_id,
    }


@router.post("/brand/{brand_id}/resources/{resource_id}/stats")
async def update_resource_stats(
    brand_id: str,
    resource_id: str,
    body: ResourceStatsUpdate,
    r: aioredis.Redis = Depends(get_redis),
):
    """Upsert operational stats for a resource.

    Stats live on a side-car hash so callers can refresh them on a schedule
    without touching the resource definition.
    """
    raw = await r.hgetall(_resource_key(resource_id))
    if not raw:
        raise HTTPException(404, detail=f"resource {resource_id} not found")
    if raw.get("brand_id") and raw["brand_id"] != brand_id:
        raise HTTPException(
            404,
            detail=f"resource {resource_id} not owned by brand {brand_id}",
        )
    mapping: dict[str, str] = {"updated_at": str(int(time.time()))}
    if body.bookings_30d is not None:
        mapping["bookings_30d"] = str(body.bookings_30d)
    if body.utilization_pct is not None:
        mapping["utilization_pct"] = f"{body.utilization_pct:.4f}"
    if body.avg_rating is not None:
        mapping["avg_rating"] = f"{body.avg_rating:.4f}"
    if body.no_show_rate is not None:
        mapping["no_show_rate"] = f"{body.no_show_rate:.4f}"
    if len(mapping) == 1:
        raise HTTPException(422, detail="no stat fields supplied")
    await r.hset(_resource_stats_key(resource_id), mapping=mapping)
    stats_raw = await r.hgetall(_resource_stats_key(resource_id))
    return {
        "ok": True,
        "brand_id": brand_id,
        "resource_id": resource_id,
        "stats": {
            "bookings_30d": int(stats_raw.get("bookings_30d") or 0),
            "utilization_pct": float(stats_raw.get("utilization_pct") or 0.0),
            "avg_rating": float(stats_raw.get("avg_rating") or 0.0),
            "no_show_rate": float(stats_raw.get("no_show_rate") or 0.0),
            "updated_at": int(stats_raw.get("updated_at") or 0),
        },
    }


# ═════════════════════════════════════════════════════════════════════════
# 5d-ter. ENTITIES  (non-human subjects: pets, vehicles, properties, ...)
# ═════════════════════════════════════════════════════════════════════════
#
# A KiX ID (``kid_xxx``) addresses a human (and burns one phone-number slot).
# Pets, vehicles, devices, packages, and so on need identity + attributes
# but should NOT consume the human namespace. Entities mint their own
# ``eid_xxx`` and are always rooted at an owning user (the human responsible).
#
# Keys
#   entity:{eid}                                 HASH (full state)
#   entity:{eid}:attributes                      HASH (current values)
#   entity:{eid}:attr_log:{key}                  LIST (time-series, lpush)
#   entity:{eid}:reservations                    ZSET score=scheduled_at
#   user:{uid}:entities                          SET of eids
#   user:{uid}:entities:by_type:{type}           SET of eids
#
# Ownership is also recorded as a relationship edge:
#     user --owns--> entity         (forward)
#     entity --owned_by--> user     (reverse, lives on entity side only)

_VALID_ENTITY_TYPES = {
    "pet", "vehicle", "property", "device", "package", "other",
}


def _entity_key(eid: str) -> str:
    return f"entity:{eid}"


def _entity_attrs_key(eid: str) -> str:
    return f"entity:{eid}:attributes"


def _entity_attr_log_key(eid: str, key: str) -> str:
    return f"entity:{eid}:attr_log:{key}"


def _entity_reservations_key(eid: str) -> str:
    return f"entity:{eid}:reservations"


def _user_entities_key(uid: str) -> str:
    return f"user:{uid}:entities"


def _user_entities_by_type_key(uid: str, etype: str) -> str:
    return f"user:{uid}:entities:by_type:{etype}"


def _new_eid() -> str:
    return f"eid_{uuid.uuid4().hex[:12]}"


class EntityRegister(BaseModel):
    entity_type: str = Field(..., min_length=1, max_length=64)
    owner_user_id: str = Field(..., min_length=1, max_length=128)
    name: str | None = Field(None, max_length=256)
    species: str | None = Field(None, max_length=128)
    breed: str | None = Field(None, max_length=128)
    attributes: dict[str, Any] = Field(default_factory=dict)


class EntityUpdate(BaseModel):
    name: str | None = Field(None, max_length=256)
    species: str | None = Field(None, max_length=128)
    breed: str | None = Field(None, max_length=128)
    attributes: dict[str, Any] | None = None


class EntityTransfer(BaseModel):
    new_owner_user_id: str = Field(..., min_length=1, max_length=128)
    evidence: dict[str, Any] | None = None


class EntityAttrSet(BaseModel):
    value: Any
    log: bool = Field(
        False,
        description="If true, also append a time-series log entry for this key.",
    )
    history: bool = False  # alias for log
    trend: bool = False    # alias for log (semantic hint)
    source: Literal["self_declared", "measured", "inferred"] = "self_declared"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    meta: dict[str, Any] | None = None
    ttl_seconds: int | None = Field(default=None, ge=1)


def _entity_hash_to_dict(raw: dict[str, str]) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        attrs = json.loads(raw.get("attributes") or "{}")
    except Exception:
        attrs = {}
    return {
        "entity_id": raw.get("entity_id", ""),
        "entity_type": raw.get("entity_type", ""),
        "owner_user_id": raw.get("owner_user_id", ""),
        "name": raw.get("name") or None,
        "species": raw.get("species") or None,
        "breed": raw.get("breed") or None,
        "attributes": attrs,
        "created_at": int(raw.get("created_at") or 0),
        "updated_at": int(raw.get("updated_at") or 0),
    }


@router.post("/entities/register")
async def register_entity(
    body: EntityRegister,
    r: aioredis.Redis = Depends(get_redis),
):
    """Mint an ``eid_xxx`` for a non-human subject and root it under an owner.

    Automatically writes an ``owns`` relationship edge from the owner to the
    new entity (and an ``owned_by`` reverse edge on the entity side so the
    same relationship API can navigate human↔entity).
    """
    owner_uid = await resolve_canonical(r, body.owner_user_id)
    eid = _new_eid()
    now = int(time.time())
    state: dict[str, str] = {
        "entity_id": eid,
        "entity_type": body.entity_type,
        "owner_user_id": owner_uid,
        "attributes": json.dumps(body.attributes),
        "created_at": str(now),
        "updated_at": str(now),
    }
    if body.name is not None:
        state["name"] = body.name
    if body.species is not None:
        state["species"] = body.species
    if body.breed is not None:
        state["breed"] = body.breed

    pipe = r.pipeline()
    pipe.hset(_entity_key(eid), mapping=state)
    pipe.sadd(_user_entities_key(owner_uid), eid)
    pipe.sadd(_user_entities_by_type_key(owner_uid, body.entity_type), eid)
    # owner → entity "owns" edge (reuse relationship index so callers can
    # lookup via /users/{uid}/relationships?relationship=owns).
    rel_payload_owns = {
        "relationship_id": uuid.uuid4().hex[:16],
        "relationship": "owns",
        "meta": {"entity_type": body.entity_type},
        "created_at": now,
    }
    pipe.hset(_rel_hash_key(owner_uid), eid, json.dumps(rel_payload_owns))
    pipe.sadd(_rel_index_key(owner_uid, "owns"), eid)
    # entity → owner "owned_by" reverse (lives on entity's own rel hash so the
    # entity can answer "who owns me?" via the same machinery).
    rel_payload_owned = {
        "relationship_id": uuid.uuid4().hex[:16],
        "relationship": "owned_by",
        "meta": {"entity_type": body.entity_type},
        "created_at": now,
    }
    pipe.hset(_rel_hash_key(eid), owner_uid, json.dumps(rel_payload_owned))
    pipe.sadd(_rel_index_key(eid, "owned_by"), owner_uid)
    await pipe.execute()

    return {
        "ok": True,
        "entity_id": eid,
        "entity_type": body.entity_type,
        "owner_user_id": owner_uid,
        "owns_edge_created": True,
    }


@router.get("/entities/{eid}")
async def get_entity(eid: str, r: aioredis.Redis = Depends(get_redis)):
    raw = await r.hgetall(_entity_key(eid))
    if not raw:
        raise HTTPException(404, detail=f"entity {eid} not found")
    return _entity_hash_to_dict(raw)


@router.post("/entities/{eid}/update")
async def update_entity(
    eid: str,
    body: EntityUpdate,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await r.hgetall(_entity_key(eid))
    if not raw:
        raise HTTPException(404, detail=f"entity {eid} not found")
    mapping: dict[str, str] = {"updated_at": str(int(time.time()))}
    if body.name is not None:
        mapping["name"] = body.name
    if body.species is not None:
        mapping["species"] = body.species
    if body.breed is not None:
        mapping["breed"] = body.breed
    if body.attributes is not None:
        # Merge instead of overwrite: callers expect partial updates here.
        try:
            existing = json.loads(raw.get("attributes") or "{}")
        except Exception:
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        existing.update(body.attributes)
        mapping["attributes"] = json.dumps(existing)
    if len(mapping) == 1:
        raise HTTPException(422, detail="no fields supplied")
    await r.hset(_entity_key(eid), mapping=mapping)
    new_raw = await r.hgetall(_entity_key(eid))
    return {"ok": True, "entity_id": eid, **_entity_hash_to_dict(new_raw)}


@router.post("/entities/{eid}/transfer-ownership")
async def transfer_entity_ownership(
    eid: str,
    body: EntityTransfer,
    r: aioredis.Redis = Depends(get_redis),
):
    """Move ownership from current owner to new owner.

    Rewrites:
      * ``entity:{eid}`` owner field
      * ``user:{old}:entities`` / ``user:{new}:entities`` (and by-type sets)
      * ``user:{old} --owns--> eid`` edge → ``user:{new} --owns--> eid``
      * ``entity:{eid} --owned_by--> user`` reverse edge
    Idempotent if old and new are the same.
    """
    raw = await r.hgetall(_entity_key(eid))
    if not raw:
        raise HTTPException(404, detail=f"entity {eid} not found")
    old_owner = raw.get("owner_user_id", "")
    new_owner = await resolve_canonical(r, body.new_owner_user_id)
    if old_owner == new_owner:
        return {
            "ok": True,
            "entity_id": eid,
            "owner_user_id": new_owner,
            "noop": True,
        }
    etype = raw.get("entity_type", "")
    now = int(time.time())

    pipe = r.pipeline()
    pipe.hset(
        _entity_key(eid),
        mapping={"owner_user_id": new_owner, "updated_at": str(now)},
    )
    if old_owner:
        pipe.srem(_user_entities_key(old_owner), eid)
        if etype:
            pipe.srem(_user_entities_by_type_key(old_owner, etype), eid)
        pipe.hdel(_rel_hash_key(old_owner), eid)
        pipe.srem(_rel_index_key(old_owner, "owns"), eid)
    pipe.sadd(_user_entities_key(new_owner), eid)
    if etype:
        pipe.sadd(_user_entities_by_type_key(new_owner, etype), eid)
    rel_payload_owns = {
        "relationship_id": uuid.uuid4().hex[:16],
        "relationship": "owns",
        "meta": {
            "entity_type": etype,
            "transferred_from": old_owner,
            "evidence": body.evidence or {},
        },
        "created_at": now,
    }
    pipe.hset(_rel_hash_key(new_owner), eid, json.dumps(rel_payload_owns))
    pipe.sadd(_rel_index_key(new_owner, "owns"), eid)
    # Rewrite owned_by reverse on entity side.
    rel_payload_owned = {
        "relationship_id": uuid.uuid4().hex[:16],
        "relationship": "owned_by",
        "meta": {"entity_type": etype, "transferred_from": old_owner},
        "created_at": now,
    }
    pipe.delete(_rel_hash_key(eid))  # wipe — owner is the only edge here
    pipe.hset(_rel_hash_key(eid), new_owner, json.dumps(rel_payload_owned))
    if old_owner:
        pipe.srem(_rel_index_key(eid, "owned_by"), old_owner)
    pipe.sadd(_rel_index_key(eid, "owned_by"), new_owner)
    await pipe.execute()

    return {
        "ok": True,
        "entity_id": eid,
        "old_owner_user_id": old_owner,
        "owner_user_id": new_owner,
        "transferred_at": now,
    }


@router.get("/users/{uid}/entities")
async def list_user_entities(
    uid: str,
    entity_type: str | None = None,
    r: aioredis.Redis = Depends(get_redis),
):
    """List entities owned by a user, optionally filtered by ``entity_type``."""
    uid = await resolve_canonical(r, uid)
    if entity_type:
        members = await r.smembers(_user_entities_by_type_key(uid, entity_type))
    else:
        members = await r.smembers(_user_entities_key(uid))
    out: list[dict[str, Any]] = []
    for eid in members or []:
        raw = await r.hgetall(_entity_key(eid))
        if not raw:
            continue
        out.append(_entity_hash_to_dict(raw))
    out.sort(key=lambda d: d.get("entity_id", ""))
    return {
        "user_id": uid,
        "entity_type": entity_type,
        "count": len(out),
        "entities": out,
    }


@router.post("/entities/{eid}/attributes")
async def set_entity_attribute(
    eid: str,
    body: dict[str, Any],
    key: str | None = None,
    r: aioredis.Redis = Depends(get_redis),
):
    """Set one or many attributes on an entity.

    Single mode (canonical) — body has ``key`` + ``value`` (plus optional
    log/history/trend/source/confidence/meta/ttl_seconds).

    Bulk mode (merchant-intuitive alias) — body has ``attrs: {k1: v1, k2: v2,
    ...}``. Each pair is stored as its own attribute. Other top-level fields
    (``log``, ``source``, etc.) apply to every pair so the caller doesn't
    need to repeat them. Returns ``{ok, entity_id, attrs: {k: value_str},
    logged, ts}``.
    """
    if not await r.exists(_entity_key(eid)):
        raise HTTPException(404, detail=f"entity {eid} not found")

    # ── Bulk mode ───────────────────────────────────────────────────────
    if isinstance(body.get("attrs"), dict):
        bulk_attrs = body["attrs"]
        if not bulk_attrs:
            raise HTTPException(422, detail="'attrs' must be non-empty")
        meta_kwargs = {
            k: v for k, v in body.items()
            if k not in ("attrs", "key", "value")
        }
        now = int(time.time())
        try:
            parsed_meta = EntityAttrSet(value=None, **meta_kwargs)
        except Exception as exc:
            raise HTTPException(422, detail=f"invalid body: {exc}") from exc
        do_log = parsed_meta.log or parsed_meta.history or parsed_meta.trend

        pipe = r.pipeline()
        results: dict[str, str] = {}
        for ak, av in bulk_attrs.items():
            if not isinstance(ak, str) or not ak:
                raise HTTPException(422, detail=f"invalid attr key {ak!r}")
            value_str = _serialize_attr_value(av)
            results[ak] = value_str
            pipe.hset(_entity_attrs_key(eid), ak, value_str)
            if parsed_meta.ttl_seconds:
                pipe.set(
                    f"entity:{eid}:attribute:{ak}",
                    value_str,
                    ex=parsed_meta.ttl_seconds,
                )
            if do_log:
                entry = {
                    "entry_id": uuid.uuid4().hex[:16],
                    "ts": now,
                    "value": value_str,
                    "source": parsed_meta.source,
                }
                if parsed_meta.confidence is not None:
                    entry["confidence"] = round(float(parsed_meta.confidence), 4)
                if parsed_meta.meta:
                    entry["meta"] = parsed_meta.meta
                log_key = _entity_attr_log_key(eid, ak)
                pipe.lpush(log_key, json.dumps(entry))
                pipe.ltrim(log_key, 0, _ATTR_LOG_MAX_ENTRIES - 1)
        pipe.hset(_entity_key(eid), mapping={"updated_at": str(now)})
        await pipe.execute()
        return {
            "ok": True,
            "entity_id": eid,
            "attrs": results,
            "logged": do_log,
            "ts": now,
        }

    # ── Single mode (canonical) ────────────────────────────────────────
    attr_key = key or body.get("key")
    if not attr_key or not isinstance(attr_key, str):
        raise HTTPException(422, detail="missing 'key'")
    if "value" not in body:
        raise HTTPException(422, detail="missing 'value'")
    try:
        parsed = EntityAttrSet(**{k: v for k, v in body.items() if k != "key"})
    except Exception as exc:
        raise HTTPException(422, detail=f"invalid body: {exc}") from exc

    value_str = _serialize_attr_value(parsed.value)
    now = int(time.time())
    do_log = parsed.log or parsed.history or parsed.trend

    pipe = r.pipeline()
    pipe.hset(_entity_attrs_key(eid), attr_key, value_str)
    pipe.hset(
        _entity_key(eid), mapping={"updated_at": str(now)},
    )
    if parsed.ttl_seconds:
        # Sidecar TTL (mirrors user-attr pattern at the entity scope).
        pipe.set(
            f"entity:{eid}:attribute:{attr_key}",
            value_str,
            ex=parsed.ttl_seconds,
        )
    if do_log:
        entry = {
            "entry_id": uuid.uuid4().hex[:16],
            "ts": now,
            "value": value_str,
            "source": parsed.source,
        }
        if parsed.confidence is not None:
            entry["confidence"] = round(float(parsed.confidence), 4)
        if parsed.meta:
            entry["meta"] = parsed.meta
        log_key = _entity_attr_log_key(eid, attr_key)
        pipe.lpush(log_key, json.dumps(entry))
        pipe.ltrim(log_key, 0, _ATTR_LOG_MAX_ENTRIES - 1)
    await pipe.execute()

    return {
        "ok": True,
        "entity_id": eid,
        "key": attr_key,
        "value": value_str,
        "logged": do_log,
        "ts": now,
    }


# ═════════════════════════════════════════════════════════════════════════
# 5e. IDENTITY MERGE  (secondary → primary, optional alias)
# ═════════════════════════════════════════════════════════════════════════
#
# Merges all per-user state from secondary into primary, then optionally
# creates an alias mapping (`user:alias:{secondary}` = primary) so future
# writes addressed to the secondary route to the primary.
#
# Other modules should call `resolve_canonical(r, user_id)` before touching
# user-scoped keys.


# Keys that are HASH-style: keep primary on conflict, copy from secondary
# when primary missing.
_MERGE_HASH_PATTERNS: tuple[str, ...] = (
    "user:{uid}:profile",
    "user:{uid}:attributes",
    "user:{uid}:lifecycle_stage",
    "user:{uid}:relationships",
)

# Keys that are LIST/SET-style: union into primary.
_MERGE_SET_PATTERNS: tuple[str, ...] = (
    "user:{uid}:audiences",
    "user:{uid}:friends",
    "user:{uid}:followers",
    "user:{uid}:following",
    "user:{uid}:brand_follows",
    "user:{uid}:badges",
    "user:{uid}:segments",
    "user:{uid}:groups",
    "user:{uid}:quests",
)

# Numeric counters: sum.
_MERGE_COUNTER_PATTERNS: tuple[str, ...] = (
    "user:{uid}:xp",
    "user:{uid}:points",
    "user:{uid}:stars",
    "user:{uid}:lifetime_spend_cents",
    "user:{uid}:purchases_count",
    "user:{uid}:games_played",
)

# ZSETs (timeline-style): merge by score, dedupe by member.
_MERGE_ZSET_PATTERNS: tuple[str, ...] = (
    "user:{uid}:visits",
    "user:{uid}:journey_events",
    "user:{uid}:attribution_events",
    "user:{uid}:pixel_events",
)


async def resolve_canonical(r: aioredis.Redis, user_id: str) -> str:
    """Resolve a user_id through the alias table.

    Exported helper for attribution / pixel / triggers / etc. so that
    writes addressed to a merged-away secondary land on the primary.
    Falls back to the input user_id when no alias is recorded.
    """
    try:
        target = await r.get(f"user:alias:{user_id}")
    except Exception:
        return user_id
    if target:
        # Single-hop chase (alias table should not chain, but be safe).
        try:
            next_hop = await r.get(f"user:alias:{target}")
        except Exception:
            next_hop = None
        return next_hop or target
    return user_id


class MergeRequest(BaseModel):
    primary_user_id: str
    secondary_user_id: str
    strategy: Literal[
        "merge_all", "merge_attributes_only", "merge_journeys_only"
    ] = "merge_all"
    keep_secondary_as_alias: bool = True
    admin_token: str | None = None


async def _user_exists(r: aioredis.Redis, uid: str) -> bool:
    # Cheap probe: any key starting with user:{uid}: ?
    async for _ in r.scan_iter(match=f"user:{uid}:*", count=10):
        return True
    return False


async def _merge_hash(
    r: aioredis.Redis,
    pattern: str,
    primary: str,
    secondary: str,
) -> dict[str, Any]:
    p_key = pattern.format(uid=primary)
    s_key = pattern.format(uid=secondary)
    s_raw = await r.hgetall(s_key) or {}
    if not s_raw:
        return {"copied": 0, "conflicts": []}
    p_raw = await r.hgetall(p_key) or {}
    copied = 0
    conflicts: list[dict[str, Any]] = []
    to_set: dict[str, str] = {}
    for field, sval in s_raw.items():
        pval = p_raw.get(field)
        if pval is None:
            to_set[field] = sval
            copied += 1
        elif pval != sval:
            conflicts.append(
                {
                    "field": field,
                    "primary_value": pval,
                    "secondary_value": sval,
                }
            )
    if to_set:
        await r.hset(p_key, mapping=to_set)
    return {"copied": copied, "conflicts": conflicts}


async def _merge_set_or_list(
    r: aioredis.Redis,
    pattern: str,
    primary: str,
    secondary: str,
) -> int:
    p_key = pattern.format(uid=primary)
    s_key = pattern.format(uid=secondary)
    try:
        ktype = await r.type(s_key)
    except Exception:
        return 0
    if ktype == "set":
        members = await r.smembers(s_key)
        if not members:
            return 0
        await r.sadd(p_key, *members)
        return len(members)
    if ktype == "list":
        items = await r.lrange(s_key, 0, -1) or []
        if not items:
            return 0
        # Append at tail to preserve relative order.
        await r.rpush(p_key, *items)
        return len(items)
    return 0


async def _merge_counter(
    r: aioredis.Redis, pattern: str, primary: str, secondary: str
) -> tuple[int, int, int]:
    p_key = pattern.format(uid=primary)
    s_key = pattern.format(uid=secondary)
    p_before_raw = await r.get(p_key)
    s_raw = await r.get(s_key)
    try:
        p_before = int(p_before_raw or 0)
    except (TypeError, ValueError):
        p_before = 0
    try:
        s_val = int(s_raw or 0)
    except (TypeError, ValueError):
        s_val = 0
    if s_val:
        await r.incrby(p_key, s_val)
    return p_before, p_before + s_val, s_val


async def _merge_zset(
    r: aioredis.Redis, pattern: str, primary: str, secondary: str
) -> int:
    p_key = pattern.format(uid=primary)
    s_key = pattern.format(uid=secondary)
    try:
        ktype = await r.type(s_key)
    except Exception:
        return 0
    if ktype != "zset":
        return 0
    pairs = await r.zrange(s_key, 0, -1, withscores=True) or []
    if not pairs:
        return 0
    mapping = {member: score for member, score in pairs}
    if mapping:
        await r.zadd(p_key, mapping)
    return len(mapping)


async def _delete_user_keys(r: aioredis.Redis, uid: str) -> int:
    deleted = 0
    batch: list[str] = []
    async for k in r.scan_iter(match=f"user:{uid}:*", count=200):
        batch.append(k)
        if len(batch) >= 200:
            deleted += await r.delete(*batch)
            batch = []
    if batch:
        deleted += await r.delete(*batch)
    return deleted


@router.post("/users/merge")
async def merge_users(
    body: MergeRequest, r: aioredis.Redis = Depends(get_redis)
):
    """Merge secondary user into primary.

    Strategy:
      merge_all              — attributes + journeys + counters + sets + zsets
      merge_attributes_only  — HASH-style only (profile/attributes/...)
      merge_journeys_only    — ZSET-style only (visits/journey/...)
    """
    if body.primary_user_id == body.secondary_user_id:
        raise HTTPException(422, detail="primary and secondary must differ")

    # Resolve any pre-existing aliases first (idempotent re-merge protection).
    primary = await resolve_canonical(r, body.primary_user_id)
    secondary = await resolve_canonical(r, body.secondary_user_id)
    if primary == secondary:
        return {
            "ok": True,
            "primary_user_id": primary,
            "secondary_user_id": body.secondary_user_id,
            "noop": True,
            "reason": "already aliased to primary",
        }

    primary_exists = await _user_exists(r, primary)
    secondary_exists = await _user_exists(r, secondary)
    if not primary_exists and not secondary_exists:
        raise HTTPException(404, detail="neither user has any state")

    report: dict[str, Any] = {
        "attributes": {"count_secondary": 0, "count_conflicts": 0, "conflicts": []},
        "journey_events": {"count_secondary": 0, "total_after_merge": 0},
        "vouchers": {"count_transferred": 0},
        "tier_xp": {
            "primary_xp_before": 0,
            "primary_xp_after": 0,
            "secondary_xp": 0,
        },
        "relationships": {"count_transferred": 0},
        "audiences": {"count_transferred": 0},
        "pixel_events": {"count": 0},
    }

    do_hash = body.strategy in ("merge_all", "merge_attributes_only")
    do_journey = body.strategy in ("merge_all", "merge_journeys_only")
    do_full = body.strategy == "merge_all"

    # ── HASH merges (attributes, profile, lifecycle, relationships hash) ──
    if do_hash:
        attr_conflicts: list[dict[str, Any]] = []
        attr_count = 0
        for pat in _MERGE_HASH_PATTERNS:
            res = await _merge_hash(r, pat, primary, secondary)
            attr_count += int(res.get("copied", 0))
            conflicts = res.get("conflicts", [])
            for c in conflicts:
                attr_conflicts.append({"key_pattern": pat, **c})

        # Brand-scoped attribute hashes (user:{uid}:attributes:{brand_id})
        async for s_key in r.scan_iter(
            match=f"user:{secondary}:attributes:*", count=100
        ):
            suffix = s_key[len(f"user:{secondary}:") :]
            pat = "user:{uid}:" + suffix
            res = await _merge_hash(r, pat, primary, secondary)
            attr_count += int(res.get("copied", 0))
            for c in res.get("conflicts", []):
                attr_conflicts.append({"key_pattern": pat, **c})

        report["attributes"]["count_secondary"] = attr_count
        report["attributes"]["count_conflicts"] = len(attr_conflicts)
        report["attributes"]["conflicts"] = attr_conflicts[:100]

        # Relationship reverse-index SETs need an explicit union too.
        if do_full:
            async for s_idx in r.scan_iter(
                match=f"user:{secondary}:relationship_by_type:*", count=100
            ):
                suffix = s_idx[len(f"user:{secondary}:") :]
                p_idx = f"user:{primary}:{suffix}"
                members = await r.smembers(s_idx)
                if members:
                    await r.sadd(p_idx, *members)
                    report["relationships"]["count_transferred"] += len(members)

    # ── ZSET merges (visits / journey / attribution / pixel) ──
    if do_journey:
        journey_total = 0
        for pat in _MERGE_ZSET_PATTERNS:
            n = await _merge_zset(r, pat, primary, secondary)
            journey_total += n
            if "pixel" in pat:
                report["pixel_events"]["count"] += n
        # Total entries in journey after merge.
        try:
            after = await r.zcard(f"user:{primary}:journey_events")
        except Exception:
            after = 0
        report["journey_events"]["count_secondary"] = journey_total
        report["journey_events"]["total_after_merge"] = int(after or 0)

    # ── Full merge: counters + sets + vouchers + audiences ──
    if do_full:
        # Counters
        xp_p_before, xp_p_after, xp_s = await _merge_counter(
            r, "user:{uid}:xp", primary, secondary
        )
        report["tier_xp"]["primary_xp_before"] = xp_p_before
        report["tier_xp"]["primary_xp_after"] = xp_p_after
        report["tier_xp"]["secondary_xp"] = xp_s
        for pat in _MERGE_COUNTER_PATTERNS:
            if pat == "user:{uid}:xp":
                continue
            await _merge_counter(r, pat, primary, secondary)

        # SET / LIST unions
        audience_transferred = 0
        for pat in _MERGE_SET_PATTERNS:
            n = await _merge_set_or_list(r, pat, primary, secondary)
            if pat == "user:{uid}:audiences":
                audience_transferred = n
        report["audiences"]["count_transferred"] = audience_transferred

        # Vouchers: re-assign holder_user_id from secondary → primary.
        # Source-of-truth list: user:{uid}:vouchers (SET or LIST of voucher_ids).
        voucher_count = 0
        s_voucher_key = f"user:{secondary}:vouchers"
        try:
            vtype = await r.type(s_voucher_key)
        except Exception:
            vtype = "none"
        voucher_ids: list[str] = []
        if vtype == "set":
            voucher_ids = list(await r.smembers(s_voucher_key) or [])
        elif vtype == "list":
            voucher_ids = list(await r.lrange(s_voucher_key, 0, -1) or [])
        for vid in voucher_ids:
            # Update voucher record's holder if it lives in a HASH keyed by id.
            vkey = f"voucher:{vid}"
            try:
                exists = await r.exists(vkey)
            except Exception:
                exists = 0
            if exists:
                try:
                    await r.hset(vkey, "holder_user_id", primary)
                except Exception:
                    pass
            voucher_count += 1
        if voucher_ids:
            # Mirror into primary's voucher set/list.
            primary_voucher_key = f"user:{primary}:vouchers"
            if vtype == "set":
                await r.sadd(primary_voucher_key, *voucher_ids)
            elif vtype == "list":
                await r.rpush(primary_voucher_key, *voucher_ids)
        report["vouchers"]["count_transferred"] = voucher_count

        # Pixel events: rewrite user_id field on individual event hashes.
        # Convention: pixel:event:{event_id} HASH with user_id field, and
        # user:{uid}:pixel_event_ids SET enumerating them.
        s_pixel_ids_key = f"user:{secondary}:pixel_event_ids"
        try:
            ptype = await r.type(s_pixel_ids_key)
        except Exception:
            ptype = "none"
        pixel_ids: list[str] = []
        if ptype == "set":
            pixel_ids = list(await r.smembers(s_pixel_ids_key) or [])
        elif ptype == "list":
            pixel_ids = list(await r.lrange(s_pixel_ids_key, 0, -1) or [])
        for eid in pixel_ids:
            try:
                await r.hset(f"pixel:event:{eid}", "user_id", primary)
            except Exception:
                pass
        if pixel_ids:
            primary_pixel_key = f"user:{primary}:pixel_event_ids"
            if ptype == "set":
                await r.sadd(primary_pixel_key, *pixel_ids)
            elif ptype == "list":
                await r.rpush(primary_pixel_key, *pixel_ids)
            report["pixel_events"]["count"] += len(pixel_ids)

    # ── Alias or delete secondary ──
    alias_created = False
    if body.keep_secondary_as_alias:
        await r.set(f"user:alias:{secondary}", primary)
        alias_created = True
    else:
        await _delete_user_keys(r, secondary)

    return {
        "ok": True,
        "primary_user_id": primary,
        "secondary_user_id": secondary,
        "strategy": body.strategy,
        "merged": report,
        "alias_created": alias_created,
    }


@router.get("/users/{user_id}/canonical")
async def get_canonical_user(
    user_id: str, r: aioredis.Redis = Depends(get_redis)
):
    """Resolve a user_id through the alias table."""
    canonical = await resolve_canonical(r, user_id)
    return {
        "user_id": user_id,
        "canonical_user_id": canonical,
        "was_alias": canonical != user_id,
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
