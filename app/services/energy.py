"""Energy service — Lua-backed energy economy for KiX Platform R5."""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis

from app.redis_client import lua_scripts

# ── SGT (UTC+8) helper ──────────────────────────────────────────────────
_SGT = timezone(timedelta(hours=8))


def get_sgt_date() -> str:
    """Return current date in SGT (UTC+8) as 'YYYY-MM-DD'."""
    return datetime.now(_SGT).strftime("%Y-%m-%d")


# ── Core energy operations ──────────────────────────────────────────────


async def regen_energy(
    r: aioredis.Redis,
    brand_id: str,
    user_id: str,
    config: dict,
) -> tuple[int, int]:
    """Run lazy energy regeneration via Lua.

    Returns (balance, last_regen_at).
    """
    regen_interval = config.get("regen_interval", 1500)  # 25 min default
    regen_cap = config.get("regen_cap", 100)
    now = int(time.time())

    result = await lua_scripts["energy_regen"](
        keys=[
            f"energy:balance:{brand_id}:{user_id}",
            f"energy:regen_at:{brand_id}:{user_id}",
        ],
        args=[now, regen_interval, regen_cap],
    )
    return int(result[0]), int(result[1])


async def reserve_energy(
    r: aioredis.Redis,
    brand_id: str,
    user_id: str,
    game_id: str,
    season_id: str,
    session_id: str,
    config: dict,
    is_day1: bool,
) -> tuple[int, int]:
    """Reserve energy for a game session.

    Rule 19: cost = ceil(base * modifier).
    Returns (remaining_balance, cost_charged).
    """
    # Determine base cost from config per game_id
    game_costs = config.get("game_costs", {})
    base_cost = game_costs.get(game_id, config.get("default_energy_cost", 8))

    # Day-1 modifier
    if is_day1:
        day1_modifier = config.get("day1_modifier", 0.5)
        cost = math.ceil(base_cost * day1_modifier)
    else:
        cost = base_cost

    now = int(time.time())
    session_ttl = config.get("session_ttl", 120)
    regen_interval = config.get("regen_interval", 1500)
    regen_cap = config.get("regen_cap", 100)

    result = await lua_scripts["energy_reserve"](
        keys=[
            f"energy:balance:{brand_id}:{user_id}",
            f"energy:regen_at:{brand_id}:{user_id}",
            f"energy:reservation:{brand_id}:{user_id}:{session_id}",
            f"active_session:{brand_id}:{user_id}",
            f"session:{session_id}",
        ],
        args=[
            now,
            cost,
            session_id,
            user_id,
            brand_id,
            game_id,
            season_id,
            session_ttl,
            regen_interval,
            regen_cap,
        ],
    )
    return int(result[0]), int(result[1])


async def confirm_energy(
    r: aioredis.Redis,
    brand_id: str,
    user_id: str,
    session_id: str,
) -> None:
    """Confirm energy reservation after score submission (Rule 17)."""
    await lua_scripts["energy_confirm"](
        keys=[
            f"energy:reservation:{brand_id}:{user_id}:{session_id}",
            f"session:{session_id}",
            f"active_session:{brand_id}:{user_id}",
        ],
        args=[session_id],
    )


async def refund_energy(
    r: aioredis.Redis,
    brand_id: str,
    user_id: str,
    session_id: str,
) -> tuple[int, int]:
    """Refund energy for an expired/abandoned session.

    Returns (new_balance, refunded_amount).
    """
    result = await lua_scripts["energy_refund"](
        keys=[
            f"energy:balance:{brand_id}:{user_id}",
            f"energy:reservation:{brand_id}:{user_id}:{session_id}",
            f"session:{session_id}",
            f"active_session:{brand_id}:{user_id}",
        ],
        args=[session_id],
    )
    return int(result[0]), int(result[1])


async def grant_energy(
    r: aioredis.Redis,
    brand_id: str,
    user_id: str,
    nonce: str,
    config: dict,
) -> tuple[int, int]:
    """Grant energy from QR scan via Lua.

    Returns (new_balance, actual_granted).
    """
    now = int(time.time())
    grant_amount = config.get("qr_grant_amount", 25)
    overcap = config.get("qr_overcap", 130)
    cooldown_ttl = config.get("qr_cooldown_ttl", 14400)  # 4 hours
    idempotency_ttl = config.get("idempotency_ttl", 1800)  # 30 min
    regen_interval = config.get("regen_interval", 1500)
    regen_cap = config.get("regen_cap", 100)

    result = await lua_scripts["energy_grant"](
        keys=[
            f"energy:balance:{brand_id}:{user_id}",
            f"energy:regen_at:{brand_id}:{user_id}",
            f"cooldown:{brand_id}:{user_id}",
            f"grant_idempotency:{user_id}:{nonce}",
        ],
        args=[
            now,
            grant_amount,
            overcap,
            cooldown_ttl,
            idempotency_ttl,
            regen_interval,
            regen_cap,
        ],
    )
    return int(result[0]), int(result[1])


async def init_energy(
    r: aioredis.Redis,
    brand_id: str,
    user_id: str,
    starting: int = 100,
) -> None:
    """Initialize energy for a new user (Rule 18).

    SET balance=starting and regen_at=now, only if balance key doesn't exist.
    """
    balance_key = f"energy:balance:{brand_id}:{user_id}"
    regen_key = f"energy:regen_at:{brand_id}:{user_id}"
    now = int(time.time())

    # Only initialize if user has no balance key yet (SETNX)
    was_set = await r.setnx(balance_key, starting)
    if was_set:
        await r.set(regen_key, now)


async def check_welcome_back(
    r: aioredis.Redis,
    brand_id: str,
    user_id: str,
    last_seen: datetime,
    config: dict,
) -> int:
    """Check and grant welcome-back bonus if eligible (Rule 15).

    Returns the actual bonus granted (0 if ineligible or capped).
    """
    welcome_threshold = config.get("welcome_threshold", 28800)  # 8 hours
    welcome_bonus = config.get("welcome_bonus", 15)
    welcome_cap = config.get("welcome_cap", 100)
    regen_interval = config.get("regen_interval", 1500)
    regen_cap = config.get("regen_cap", 100)

    now_utc = datetime.now(timezone.utc)
    # Ensure last_seen is timezone-aware
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)

    elapsed = (now_utc - last_seen).total_seconds()
    if elapsed < welcome_threshold:
        return 0

    sgt_date = get_sgt_date()
    wb_key = f"welcome_back:{user_id}:{sgt_date}"

    # SETNX: only grant once per SGT day, TTL 48h
    was_set = await r.setnx(wb_key, "1")
    if not was_set:
        return 0
    await r.expire(wb_key, 172800)  # 48h

    # Run regen first to get current balance
    balance, _ = await regen_energy(r, brand_id, user_id, {
        "regen_interval": regen_interval,
        "regen_cap": regen_cap,
    })

    # Cap at welcome_cap, don't over-cap
    actual_bonus = min(welcome_bonus, welcome_cap - balance)
    if actual_bonus <= 0:
        return 0

    balance_key = f"energy:balance:{brand_id}:{user_id}"
    await r.incrby(balance_key, actual_bonus)
    return actual_bonus
