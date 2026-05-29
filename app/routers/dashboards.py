"""Merchant Dashboards — the daily dopamine driver.

Per MERCHANT_FLOW_TRUTH.md: merchants need a TODAY dashboard showing
"X scans today, Y conversions" — this is the dopamine that keeps them
logged in. Endpoints aggregate from existing per-brand Redis indices
maintained by energy.py, game.py, vouchers.py, kix_id.py, auction.py.

Key indices consumed
--------------------
  brand:{bid}:qr_scans:{date}            SET  member="{user}:{nonce}"
  brand:{bid}:scanning_users:{date}      SET  member=user_id (uniques)
  brand:{bid}:game_plays:{date}          STR  INCR counter
  brand:{bid}:games_completed:{date}     STR  INCR counter
  brand:{bid}:issued_vouchers            ZSET score=issued_at, member=vid
  brand:{bid}:redeemed_vouchers          ZSET score=redeemed_at, member=vid
  brand:{bid}:users_acquired:{date}      SET  member=user_id
  brand:{bid}:phone_verified:{date}      SET  member=user_id
  brand:{bid}:session_dur:{date}         LIST integer seconds
  brand:{bid}:active_days                SET  member=YYYY-MM-DD (for streak)
  brand:{bid}:auction_skipped:existing_customer:{date}  STR
  brand:{bid}:voucher_stats              HASH (totals)
  brand:{bid}:stats                      HASH (spend/conversions)
  master:{mid}:brands                    SET  brand_ids (for leaderboard)

Cache strategy
--------------
  today      → 60s   (changes often, want freshness)
  cumulative → 300s  (rarely needed in real time)
  insights   → 300s
  leaderboard→ 60s
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── helpers ──────────────────────────────────────────────────────────────


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _yesterday() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def _day_offset(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d"
    )


async def _safe_scard(r: aioredis.Redis, key: str) -> int:
    try:
        v = await r.scard(key)
        return int(v or 0)
    except Exception:
        return 0


async def _safe_int(r: aioredis.Redis, key: str) -> int:
    try:
        v = await r.get(key)
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0
    except Exception:
        return 0


async def _zset_count_for_day(
    r: aioredis.Redis, key: str, day: str
) -> int:
    """Count ZSET members where score (epoch seconds) falls within `day`."""
    try:
        start = datetime.strptime(day, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
        end = start + timedelta(days=1)
        return int(
            await r.zcount(key, start.timestamp(), end.timestamp() - 0.001)
        )
    except Exception:
        return 0


async def _session_dur_avg(r: aioredis.Redis, key: str) -> int:
    try:
        durs = await r.lrange(key, 0, -1)
    except Exception:
        return 0
    if not durs:
        return 0
    nums: list[int] = []
    for d in durs:
        try:
            nums.append(int(d))
        except (TypeError, ValueError):
            continue
    return int(sum(nums) / len(nums)) if nums else 0


async def _cac_saved_cents(r: aioredis.Redis, brand_id: str, day: str) -> int:
    """Reuse the auction admin/savings logic to compute saved CAC cents."""
    key = f"brand:{brand_id}:auction_skipped:existing_customer:{day}"
    skipped = await _safe_int(r, key)
    avg_cpa_cents = 50
    try:
        stats = await r.hgetall(f"brand:{brand_id}:stats")
        spend = int(stats.get("spend_cents", 0) or 0)
        convs = int(stats.get("conversions", 0) or 0)
        if convs > 0 and spend > 0:
            avg_cpa_cents = max(1, spend // convs)
    except Exception:
        pass
    return skipped * avg_cpa_cents


async def _conversion_value_cents(
    r: aioredis.Redis, brand_id: str, day: str
) -> int:
    """Sum face_value_cents of vouchers redeemed today."""
    try:
        start = datetime.strptime(day, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
        end = start + timedelta(days=1)
        # Bounded: cap at 10k redemptions per day for dashboard aggregation.
        vids = await r.zrangebyscore(
            f"brand:{brand_id}:redeemed_vouchers",
            start.timestamp(),
            end.timestamp() - 0.001,
            start=0,
            num=10_000,
        )
    except Exception:
        return 0
    total = 0
    for vid in vids or []:
        try:
            v = await r.hgetall(f"voucher:{vid}")
        except Exception:
            continue
        try:
            total += int((v or {}).get("face_value_cents", 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


async def _compute_today_metrics(
    r: aioredis.Redis, brand_id: str, day: str
) -> dict[str, int]:
    """All scalars feeding the today dashboard for a single day."""
    return {
        "qr_scans_count": await _safe_scard(
            r, f"brand:{brand_id}:qr_scans:{day}"
        ),
        "unique_scanning_users": await _safe_scard(
            r, f"brand:{brand_id}:scanning_users:{day}"
        ),
        "games_played": await _safe_int(
            r, f"brand:{brand_id}:game_plays:{day}"
        ),
        "games_completed": await _safe_int(
            r, f"brand:{brand_id}:games_completed:{day}"
        ),
        "vouchers_issued": await _zset_count_for_day(
            r, f"brand:{brand_id}:issued_vouchers", day
        ),
        "vouchers_redeemed": await _zset_count_for_day(
            r, f"brand:{brand_id}:redeemed_vouchers", day
        ),
        "new_users": await _safe_scard(
            r, f"brand:{brand_id}:users_acquired:{day}"
        ),
        "phone_linked": await _safe_scard(
            r, f"brand:{brand_id}:phone_verified:{day}"
        ),
        "total_conversion_value_cents": await _conversion_value_cents(
            r, brand_id, day
        ),
        "cac_saved_cents": await _cac_saved_cents(r, brand_id, day),
        "avg_session_seconds": await _session_dur_avg(
            r, f"brand:{brand_id}:session_dur:{day}"
        ),
    }


async def _streak(r: aioredis.Redis, brand_id: str) -> int:
    """Consecutive days (ending today or yesterday) with >=1 scan."""
    try:
        members = await r.smembers(f"brand:{brand_id}:active_days")
    except Exception:
        return 0
    days_set = set(members or [])
    if not days_set:
        return 0
    # Walk backward from today; allow today to be missing if it's early UTC.
    streak = 0
    cursor = datetime.now(timezone.utc)
    if _today() not in days_set:
        cursor = cursor - timedelta(days=1)
    while cursor.strftime("%Y-%m-%d") in days_set:
        streak += 1
        cursor = cursor - timedelta(days=1)
    return streak


async def _cached_json(
    r: aioredis.Redis, key: str, ttl: int
) -> dict[str, Any] | None:
    try:
        raw = await r.get(key)
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


async def _store_cached_json(
    r: aioredis.Redis, key: str, value: dict[str, Any], ttl: int
) -> None:
    try:
        await r.set(key, json.dumps(value), ex=ttl)
    except Exception:
        pass


# ── GET /today ───────────────────────────────────────────────────────────


@router.get("/{brand_id}/today")
async def get_today_dashboard(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Today's key metrics + deltas vs yesterday + active streak.

    Cached for 60s under ``dashboard:{brand_id}:today:{YYYY-MM-DD}``.
    """
    day = _today()
    cache_key = f"dashboard:{brand_id}:today:{day}"
    cached = await _cached_json(r, cache_key, 60)
    if cached:
        return cached

    metrics = await _compute_today_metrics(r, brand_id, day)
    ytd_metrics = await _compute_today_metrics(r, brand_id, _yesterday())
    deltas = {k: metrics[k] - ytd_metrics.get(k, 0) for k in metrics}
    streak = await _streak(r, brand_id)

    # Pre-depletion + auto-recharge-failure surfaces (set by wallet flow).
    wallet_low = (await r.get(f"notification:brand:{brand_id}:wallet_low")) == "1"
    autorecharge_failed_at = await r.get(
        f"notification:brand:{brand_id}:autorecharge_failed"
    )

    result = {
        "brand_id": brand_id,
        "date": day,
        "metrics": metrics,
        "deltas": deltas,
        "streak": streak,
        "alerts": {
            "wallet_low": wallet_low,
            "autorecharge_failed_at": (
                float(autorecharge_failed_at) if autorecharge_failed_at else None
            ),
        },
    }
    await _store_cached_json(r, cache_key, result, 60)
    return result


# ── GET /cumulative ──────────────────────────────────────────────────────


@router.get("/{brand_id}/cumulative")
async def get_cumulative_dashboard(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """All-time totals + 30-day rolling averages + MoM growth.

    Cached for 5 minutes.
    """
    cache_key = f"dashboard:{brand_id}:cumulative"
    cached = await _cached_json(r, cache_key, 300)
    if cached:
        return cached

    # Totals from ZSET cardinality.
    try:
        total_issued = int(
            await r.zcard(f"brand:{brand_id}:issued_vouchers") or 0
        )
    except Exception:
        total_issued = 0
    try:
        total_redeemed = int(
            await r.zcard(f"brand:{brand_id}:redeemed_vouchers") or 0
        )
    except Exception:
        total_redeemed = 0

    total_users = 0
    try:
        total_users = int(await r.scard(f"brand:{brand_id}:users_ever") or 0)
    except Exception:
        pass

    # Walk active_days to sum scans + savings over 30 days.
    try:
        active_days = await r.smembers(f"brand:{brand_id}:active_days")
    except Exception:
        active_days = set()
    active_days = set(active_days or [])

    total_scans = 0
    daily_scan_counts: list[int] = []
    today = datetime.now(timezone.utc)
    last30 = {
        (today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(30)
    }
    total_cac_saved = 0
    total_revenue = 0
    for d in active_days:
        c = await _safe_scard(r, f"brand:{brand_id}:qr_scans:{d}")
        total_scans += c
        if d in last30:
            daily_scan_counts.append(c)
            total_cac_saved += await _cac_saved_cents(r, brand_id, d)
            total_revenue += await _conversion_value_cents(r, brand_id, d)

    avg_daily_scans_30d = (
        round(sum(daily_scan_counts) / len(daily_scan_counts), 2)
        if daily_scan_counts
        else 0.0
    )

    # MoM growth = (last 30 days scans / prior 30 days scans) - 1
    prior_window = 0
    for i in range(30, 60):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        prior_window += await _safe_scard(r, f"brand:{brand_id}:qr_scans:{d}")
    recent_window = sum(daily_scan_counts)
    growth_rate_30d = (
        round((recent_window / prior_window) - 1.0, 3)
        if prior_window > 0
        else 0.0
    )

    result = {
        "brand_id": brand_id,
        "total_scans": total_scans,
        "total_users": total_users,
        "total_vouchers_issued": total_issued,
        "total_vouchers_redeemed": total_redeemed,
        "total_cac_saved_cents": total_cac_saved,
        "total_revenue_attributed_cents": total_revenue,
        "avg_daily_scans_30d": avg_daily_scans_30d,
        "growth_rate_30d": growth_rate_30d,
    }
    await _store_cached_json(r, cache_key, result, 300)
    return result


# ── GET /leaderboard ─────────────────────────────────────────────────────


_VALID_LB_METRICS = {"scans", "conversions", "revenue"}
_VALID_LB_PERIODS = {"today", "week", "month"}


async def _metric_for_day(
    r: aioredis.Redis, brand_id: str, day: str, metric: str
) -> int:
    if metric == "scans":
        return await _safe_scard(r, f"brand:{brand_id}:qr_scans:{day}")
    if metric == "conversions":
        return await _zset_count_for_day(
            r, f"brand:{brand_id}:redeemed_vouchers", day
        )
    if metric == "revenue":
        return await _conversion_value_cents(r, brand_id, day)
    return 0


async def _period_days(period: str) -> list[str]:
    if period == "today":
        return [_today()]
    n = 7 if period == "week" else 30
    return [_day_offset(i) for i in range(n)]


@router.get("/{brand_id}/leaderboard")
async def get_leaderboard(
    brand_id: str,
    metric: str = Query("scans"),
    period: str = Query("today"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Cross-store leaderboard within a master account.

    If the brand is not under a master, returns only itself. Used for
    friendly inter-store competition inside a chain.
    """
    if metric not in _VALID_LB_METRICS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"metric must be one of {sorted(_VALID_LB_METRICS)}",
        )
    if period not in _VALID_LB_PERIODS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"period must be one of {sorted(_VALID_LB_PERIODS)}",
        )

    cache_key = f"dashboard:{brand_id}:leaderboard:{metric}:{period}"
    cached = await _cached_json(r, cache_key, 60)
    if cached:
        return cached

    # Resolve cohort: this brand's master, or just this brand.
    try:
        master_id = await r.get(f"brand:{brand_id}:master")
    except Exception:
        master_id = None
    brand_ids: list[str] = []
    if master_id:
        try:
            sib = await r.smembers(f"master:{master_id}:brands")
            brand_ids = list(sib or [])
        except Exception:
            brand_ids = []
    if not brand_ids:
        brand_ids = [brand_id]

    days = await _period_days(period)
    rows: list[dict[str, Any]] = []
    for bid in brand_ids:
        total = 0
        for d in days:
            total += await _metric_for_day(r, bid, d, metric)
        # Friendly name from BrandConfig HSET if cached, else brand_id.
        name = bid
        try:
            cfg = await r.hgetall(f"brand_config:{bid}")
            if cfg and cfg.get("brand_name"):
                name = cfg["brand_name"]
        except Exception:
            pass
        rows.append({"brand_id": bid, "brand_name": name, "value": total})
    rows.sort(key=lambda x: x["value"], reverse=True)
    for i, row in enumerate(rows, start=1):
        row["rank"] = i

    result = {
        "brand_id": brand_id,
        "master_id": master_id,
        "metric": metric,
        "period": period,
        "entries": rows,
    }
    await _store_cached_json(r, cache_key, result, 60)
    return result


# ── GET /insights ────────────────────────────────────────────────────────


async def _trend_insight(
    r: aioredis.Redis, brand_id: str
) -> dict[str, str] | None:
    """Last 7 days vs prior 7 days scan ratio."""
    today = datetime.now(timezone.utc)
    last_week = 0
    prior_week = 0
    for i in range(7):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        last_week += await _safe_scard(r, f"brand:{brand_id}:qr_scans:{d}")
    for i in range(7, 14):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        prior_week += await _safe_scard(r, f"brand:{brand_id}:qr_scans:{d}")
    if prior_week <= 0 or last_week <= 0:
        return None
    delta_pct = round(((last_week / prior_week) - 1.0) * 100)
    if delta_pct >= 10:
        return {
            "type": "trend_up",
            "text": f"扫码量比上周增长 {delta_pct}%",
            "icon": "📈",
        }
    if delta_pct <= -10:
        return {
            "type": "trend_down",
            "text": f"扫码量比上周下降 {abs(delta_pct)}%",
            "icon": "📉",
        }
    return None


async def _conversion_insight(
    r: aioredis.Redis, brand_id: str
) -> dict[str, str] | None:
    """If conversion (redeemed/issued) below 25% today, flag it."""
    day = _today()
    issued = await _zset_count_for_day(
        r, f"brand:{brand_id}:issued_vouchers", day
    )
    redeemed = await _zset_count_for_day(
        r, f"brand:{brand_id}:redeemed_vouchers", day
    )
    if issued < 5:  # not enough data
        return None
    rate = redeemed / issued if issued > 0 else 0.0
    if rate < 0.25:
        return {
            "type": "underutilized",
            "text": (
                f"今日转化率仅 {int(rate * 100)}%，可能需要更吸引人的奖励"
            ),
            "icon": "⚠️",
        }
    if rate >= 0.6:
        return {
            "type": "strong_conversion",
            "text": f"今日转化率 {int(rate * 100)}%，表现优异",
            "icon": "🔥",
        }
    return None


async def _peak_hour_insight(
    r: aioredis.Redis, brand_id: str
) -> dict[str, str] | None:
    """Quick: scan the last 7 days of redeemed_vouchers and find the modal hour."""
    today = datetime.now(timezone.utc)
    start = today - timedelta(days=7)
    try:
        # Bounded: 7-day window peak-hour insight capped at 10k samples.
        members = await r.zrangebyscore(
            f"brand:{brand_id}:redeemed_vouchers",
            start.timestamp(),
            today.timestamp(),
            start=0,
            num=10_000,
            withscores=True,
        )
    except Exception:
        return None
    if not members or len(members) < 10:
        return None
    counts: dict[int, int] = {}
    for _vid, ts in members:
        try:
            hour = datetime.fromtimestamp(float(ts), tz=timezone.utc).hour
        except Exception:
            continue
        counts[hour] = counts.get(hour, 0) + 1
    if not counts:
        return None
    best_hour = max(counts.items(), key=lambda kv: kv[1])[0]
    return {
        "type": "optimization",
        "text": (
            f"过去 7 天高峰在 {best_hour:02d}:00，"
            f"可以在那时段推送提高转化"
        ),
        "icon": "💡",
    }


@router.get("/{brand_id}/insights")
async def get_insights(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Smart insights derived from rolling windows. Cached 5 min."""
    cache_key = f"dashboard:{brand_id}:insights"
    cached = await _cached_json(r, cache_key, 300)
    if cached:
        return cached

    insights: list[dict[str, str]] = []
    for builder in (
        _trend_insight,
        _conversion_insight,
        _peak_hour_insight,
    ):
        try:
            ins = await builder(r, brand_id)
        except Exception:
            ins = None
        if ins:
            insights.append(ins)

    if not insights:
        insights.append(
            {
                "type": "getting_started",
                "text": "继续累积数据，1-2 天后将出现智能洞见",
                "icon": "🌱",
            }
        )

    result = {"brand_id": brand_id, "insights": insights}
    await _store_cached_json(r, cache_key, result, 300)
    return result
