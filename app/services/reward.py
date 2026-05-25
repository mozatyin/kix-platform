"""Reward evaluation service for KiX Platform R5.

Implements the cold-path reward engine:
- P70 percentile qualification (dynamic after 100 games)
- Probability roll with pool-depletion dampening
- Voucher allocation via SELECT ... FOR UPDATE SKIP LOCKED
"""

from __future__ import annotations

import logging
import random
import uuid
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BrandConfig, VoucherPool
from app.schemas import RewardEvaluateRequest, RewardEvaluateResponse

logger = logging.getLogger(__name__)


def _no_reward(reason: str) -> RewardEvaluateResponse:
    return RewardEvaluateResponse(decision="no_reward", voucher=None, reason=reason)


async def evaluate_reward(
    db: AsyncSession,
    r: aioredis.Redis,
    request: RewardEvaluateRequest,
) -> RewardEvaluateResponse:
    """Evaluate whether a game session earns a voucher reward.

    Steps:
        1. Load brand config (Redis → DB fallback).
        2. Parse reward_rules for matching game_id.
        3. P70 qualification check.
        4. Probability roll.
        5. Daily cap enforcement.
        6. Pool depletion dampening.
        7. Voucher allocation with row-level locking.
    """

    # ── 1. Load brand config ────────────────────────────────────────────
    config_raw = await r.get(f"config:{request.brand_id}")
    if config_raw is None:
        # Fallback to DB
        result = await db.execute(
            select(BrandConfig).where(BrandConfig.brand_id == request.brand_id)
        )
        brand = result.scalar_one_or_none()
        if brand is None:
            return _no_reward("brand_not_found")
        config = brand.config_json
    else:
        import json
        config = json.loads(config_raw)

    # ── 2. Parse reward_rules ───────────────────────────────────────────
    reward_rules = config.get("reward_rules", [])
    game_rules = None
    if isinstance(reward_rules, list):
        for rule in reward_rules:
            if rule.get("game_id") == request.game_id:
                game_rules = rule
                break
    elif isinstance(reward_rules, dict):
        game_rules = reward_rules.get(request.game_id)
    if game_rules is None:
        return _no_reward("no_reward_rules_for_game")

    threshold_score: int = game_rules.get("threshold_score", 500)
    win_rate: float = game_rules.get("win_rate", 0.1)
    daily_cap: int = game_rules.get("daily_cap_per_user", 3)
    tier: str = game_rules.get("tier", "bronze")

    # ── 3. P70 qualification check ──────────────────────────────────────
    leaderboard_key = (
        f"leaderboard:{request.brand_id}:{request.game_id}:{request.season_id}"
    )
    total_count = await r.zcard(leaderboard_key)

    if total_count < 100:
        # Pre-100 games: use static threshold from config
        p70_score = threshold_score
    else:
        # Dynamic P70: top 30% = scores above index (total * 0.3)
        p70_offset = int(total_count * 0.3)
        # ZREVRANGE returns [highest..lowest]; the element at offset p70_offset
        # is the boundary score.
        p70_entries = await r.zrevrange(
            leaderboard_key, p70_offset, p70_offset, withscores=True
        )
        if p70_entries:
            p70_score = int(p70_entries[0][1])
        else:
            p70_score = threshold_score

    if request.score <= p70_score:
        return _no_reward("score_below_threshold")

    # ── 4. Probability roll ─────────────────────────────────────────────
    if random.random() >= win_rate:
        return _no_reward("probability_miss")

    # ── 5. Daily cap check ──────────────────────────────────────────────
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    user_uuid = uuid.UUID(request.user_id)
    daily_count_result = await db.execute(
        select(func.count(VoucherPool.id)).where(
            VoucherPool.assigned_to == user_uuid,
            VoucherPool.assigned_at >= today_start,
        )
    )
    daily_count = daily_count_result.scalar() or 0
    if daily_count >= daily_cap:
        return _no_reward("daily_cap_reached")

    # ── 6. Pool depletion check ─────────────────────────────────────────
    available_result = await db.execute(
        select(func.count(VoucherPool.id)).where(
            VoucherPool.brand_id == request.brand_id,
            VoucherPool.tier == tier,
            VoucherPool.status == "available",
        )
    )
    available_count = available_result.scalar() or 0

    if available_count == 0:
        return _no_reward("pool_empty")

    total_pool_result = await db.execute(
        select(func.count(VoucherPool.id)).where(
            VoucherPool.brand_id == request.brand_id,
            VoucherPool.tier == tier,
        )
    )
    total_pool = total_pool_result.scalar() or 1  # avoid division by zero

    if available_count / total_pool < 0.10:
        # Pool below 10%: halve the effective win rate and re-roll
        effective_win_rate = win_rate * 0.5
        if random.random() >= effective_win_rate:
            return _no_reward("probability_miss")

    # ── 7. Voucher allocation (row-level lock) ──────────────────────────
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=30)

    # Use raw SQL for FOR UPDATE SKIP LOCKED (not directly supported by ORM)
    alloc_result = await db.execute(
        text(
            """
            SELECT id, code, description, tier
            FROM voucher_pool
            WHERE brand_id = :brand_id
              AND tier = :tier
              AND status = 'available'
            ORDER BY id
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        ),
        {
            "brand_id": request.brand_id,
            "tier": tier,
        },
    )
    row = alloc_result.fetchone()

    if row is None:
        # Race condition: another worker grabbed the last voucher
        return _no_reward("pool_empty")

    voucher_id = row.id
    voucher_code = row.code
    voucher_description = row.description
    voucher_tier = row.tier

    # Update the voucher to assigned status
    await db.execute(
        update(VoucherPool)
        .where(VoucherPool.id == voucher_id)
        .values(
            status="assigned",
            assigned_to=user_uuid,
            assigned_at=now,
            expires_at=expires_at,
        )
    )

    logger.info(
        "Voucher %s assigned to user %s (brand=%s, tier=%s)",
        voucher_code,
        request.user_id,
        request.brand_id,
        voucher_tier,
    )

    return RewardEvaluateResponse(
        decision="reward",
        voucher={
            "id": voucher_id,
            "code": voucher_code,
            "description": voucher_description,
            "tier": voucher_tier,
            "expires_at": expires_at.isoformat(),
        },
        reason="qualified",
    )
