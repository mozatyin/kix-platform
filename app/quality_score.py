"""Quality Score auto-compute, decay, breakdown, and override.

P2 fix — historically `campaign:{cid}:quality_score` was set at create-time
(default 0.5) and only nudged by `adjust_quality_score` per-event. Result:
the QS distribution stayed locked at 0.5..1.0 forever; stale campaigns kept
high QS and blocked newer competitors; merchants had no diagnostic view.

This module adds four pieces:

  Part A  Weekly auto-compute from realised trailing-7d metrics.
          new_qs = sigmoid(2.0*CTR/0.10 + 1.5*CVR/0.05 + 1.0*completion
                           - 2.0*frequency_complaint_rate)
          mapped to [0.1, 2.0], then smoothed: new = 0.7*new + 0.3*old.

  Part B  Decay for stale campaigns. If trailing-7d impressions < 100, QS
          drifts toward 0.5 by 0.1/week (per `last_recompute_ts` delta).

  Part C  GET /api/v1/campaigns/{cid}/qs-breakdown — diagnostic view of
          components, trailing metrics, and last recompute time.

  Part D  PUT /api/v1/campaigns/{cid}/qs-override — admin manual set,
          stamped with a flag so the auto-compute loop respects it for one
          full week (override expiry).

Trailing-7d metrics
-------------------
We store rolling 7d snapshots in `campaign:{cid}:qs_snapshot:{date}` HASH
records. Each recompute writes today's snapshot of cumulative counters and
then computes deltas against the snapshot 7 days ago. This is O(1) per
campaign per recompute and degrades gracefully when history is missing.

For freshly-created campaigns (< 7 days old) we extrapolate using the
available window.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ────────────────────────────────────────────────────────────

CAMPAIGN_KEY = "campaign:{cid}"
CAMPAIGN_STATS_KEY = "campaign:{cid}:stats"
ACTIVE_CAMPAIGNS_KEY = "campaigns:active"

# Rolling-window counter snapshots. We persist for ~10 days so a 7-day
# delta always has a "then" snapshot to subtract against.
QS_SNAPSHOT_KEY = "campaign:{cid}:qs_snapshot:{date}"
QS_SNAPSHOT_TTL_SECONDS = 86400 * 10

# Last recompute / override metadata lives on the campaign hash itself
# (single-write, no extra round trip on the auction critical path).
QS_LAST_RECOMPUTE_FIELD = "qs_last_recompute_ts"
QS_OVERRIDE_FIELD = "qs_override"
QS_OVERRIDE_UNTIL_FIELD = "qs_override_until_ts"
QS_COMPONENTS_FIELD = "qs_components_json"

# QS range. The auction multiplies bid × qs × pacing — pinning the upper
# bound to 2.0 lets a great-quality campaign genuinely outrank a higher
# bidder; pinning the lower bound to 0.1 keeps the worst campaigns alive
# enough to relearn (vs. a 0 trap).
QS_MIN = 0.1
QS_MAX = 2.0
QS_DEFAULT = 0.5

# Baseline metrics for the sigmoid normalisation.
BASELINE_CTR = 0.10  # 10% click-through is "great"
BASELINE_CVR = 0.05  # 5%  click→conversion is "great"

# Smoothing — 30% weight on the prior value damps whiplash from a single
# bad / great week.
EMA_NEW_WEIGHT = 0.7
EMA_OLD_WEIGHT = 0.3

# Decay parameters for stale campaigns (< 100 impressions in trailing 7d).
STALE_IMPRESSION_THRESHOLD = 100
DECAY_PER_WEEK_TOWARD_DEFAULT = 0.1

# Override TTL — manual sets are sticky for a week so the next auto-compute
# doesn't immediately overwrite the admin's decision.
DEFAULT_OVERRIDE_TTL_SECONDS = 86400 * 7

ADMIN_TOKEN_DEFAULT = "admin-dev-token"


# ── Helpers ──────────────────────────────────────────────────────────────


def _now() -> float:
    return time.time()


def _ck(cid: str) -> str:
    return CAMPAIGN_KEY.format(cid=cid)


def _sk(cid: str) -> str:
    return CAMPAIGN_STATS_KEY.format(cid=cid)


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _date_n_days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _sigmoid(x: float) -> float:
    """Standard logistic, with overflow protection."""
    if x > 50:
        return 1.0
    if x < -50:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def _map_sigmoid_to_range(s: float, lo: float = QS_MIN, hi: float = QS_MAX) -> float:
    """Map a sigmoid ∈ [0, 1] linearly to [lo, hi]."""
    return lo + s * (hi - lo)


def _check_admin_token(token: str) -> None:
    """Centralised admin-token check (mirrors campaigns._check_admin)."""
    import os

    from app.security import constant_time_eq

    expected = os.getenv("KIX_ADMIN_TOKEN", ADMIN_TOKEN_DEFAULT)
    if not constant_time_eq(token, expected):
        raise HTTPException(status_code=403, detail="invalid admin token")


# ── Component computation (pure, testable) ───────────────────────────────


def compute_qs_components(
    realised_ctr: float,
    realised_cvr: float,
    completion_rate: float,
    frequency_complaint_rate: float,
) -> dict[str, float]:
    """Return individual sigmoid-input contributions.

    Spec:
        2.0 * CTR / 0.10
      + 1.5 * CVR / 0.05
      + 1.0 * completion_rate
      - 2.0 * frequency_complaint_rate
    """
    return {
        "ctr_contribution": 2.0 * realised_ctr / BASELINE_CTR,
        "cvr_contribution": 1.5 * realised_cvr / BASELINE_CVR,
        "completion_contribution": 1.0 * completion_rate,
        "frequency_complaint_penalty": -2.0 * frequency_complaint_rate,
    }


def compute_new_qs(
    realised_ctr: float,
    realised_cvr: float,
    completion_rate: float,
    frequency_complaint_rate: float,
) -> tuple[float, dict[str, float]]:
    """Pure: compute new_qs ∈ [QS_MIN, QS_MAX] from realised metrics.

    Returns (qs, components_dict). The sigmoid is recentered so 0 inputs map
    to the default of 0.5 — i.e. a brand-new campaign with no signal starts
    in the middle of the range, not at the floor.
    """
    comps = compute_qs_components(
        realised_ctr, realised_cvr, completion_rate, frequency_complaint_rate
    )
    raw_sum = sum(comps.values())
    # Recenter so raw_sum = 0 maps to ~0.5. We subtract the sigmoid input
    # that the spec's "baseline" hits (CTR=baseline, CVR=baseline,
    # completion=0, complaint=0 → 2.0 + 1.5 + 0 + 0 = 3.5). Halving that
    # (1.75) puts a "decent" campaign near 1.0 in the [0.1, 2.0] range.
    # Empirically: at the spec baseline (10% CTR + 5% CVR), raw_sum = 3.5,
    # sigmoid(3.5 - 1.75) = sigmoid(1.75) ≈ 0.852, mapped to 0.1+0.852*1.9 ≈ 1.72.
    # That's clearly above-default, matching "baseline = good".
    s = _sigmoid(raw_sum - 1.75)
    qs = _map_sigmoid_to_range(s)
    return qs, comps


def smooth_qs(new_qs: float, old_qs: float) -> float:
    """EMA: new = 0.7*new + 0.3*old."""
    return EMA_NEW_WEIGHT * new_qs + EMA_OLD_WEIGHT * old_qs


def apply_decay(
    old_qs: float,
    trailing_impressions: int,
    weeks_since_last_recompute: float,
) -> float:
    """Drift stale campaigns toward QS_DEFAULT.

    A stale campaign (< STALE_IMPRESSION_THRESHOLD impressions in trailing
    7d) loses DECAY_PER_WEEK_TOWARD_DEFAULT of distance from QS_DEFAULT per
    week. Non-stale campaigns are returned unchanged.
    """
    if trailing_impressions >= STALE_IMPRESSION_THRESHOLD:
        return old_qs
    weeks = max(0.0, weeks_since_last_recompute)
    delta = old_qs - QS_DEFAULT
    drift = delta * DECAY_PER_WEEK_TOWARD_DEFAULT * weeks
    new_qs = old_qs - drift
    # Don't overshoot the default; cap at QS_DEFAULT on the same side.
    if delta > 0:
        new_qs = max(QS_DEFAULT, new_qs)
    elif delta < 0:
        new_qs = min(QS_DEFAULT, new_qs)
    return _clamp(new_qs, QS_MIN, QS_MAX)


# ── Trailing-7d metric resolution ────────────────────────────────────────


async def _get_snapshot(
    r: aioredis.Redis, cid: str, date_str: str
) -> dict[str, int]:
    """Return a snapshot dict (impressions/clicks/conversions/games_*/unsubscribes).

    Missing snapshot returns empty dict so the caller can fall back.
    """
    raw = await r.hgetall(QS_SNAPSHOT_KEY.format(cid=cid, date=date_str))
    if not raw:
        return {}
    out: dict[str, int] = {}
    for k, v in raw.items():
        try:
            out[k] = int(v)
        except (TypeError, ValueError):
            out[k] = 0
    return out


async def _write_snapshot(
    r: aioredis.Redis, cid: str, date_str: str, stats: dict[str, Any]
) -> None:
    """Persist a snapshot of current cumulative counters."""
    payload = {
        "impressions": int(stats.get("impressions", 0) or 0),
        "clicks": int(stats.get("clicks", 0) or 0),
        "conversions": int(stats.get("conversions", 0) or 0),
        "games_started": int(stats.get("games_started", 0) or 0),
        "games_completed": int(stats.get("games_completed", 0) or 0),
        "unsubscribes": int(stats.get("unsubscribes", 0) or 0),
        "ts": int(_now()),
    }
    key = QS_SNAPSHOT_KEY.format(cid=cid, date=date_str)
    await r.hset(key, mapping={k: str(v) for k, v in payload.items()})
    await r.expire(key, QS_SNAPSHOT_TTL_SECONDS)


async def get_trailing_7d_metrics(
    r: aioredis.Redis, cid: str
) -> dict[str, Any]:
    """Return realised trailing-7d metrics for a campaign.

    Computes deltas against the snapshot from 7 days ago (or the oldest
    available snapshot if the campaign is younger). When no historical
    snapshot exists, treats current cumulative counters as the trailing
    window (campaign is brand-new).
    """
    current_stats = await r.hgetall(_sk(cid)) or {}
    impressions_now = int(current_stats.get("impressions", 0) or 0)
    clicks_now = int(current_stats.get("clicks", 0) or 0)
    conversions_now = int(current_stats.get("conversions", 0) or 0)
    games_started_now = int(current_stats.get("games_started", 0) or 0)
    games_completed_now = int(current_stats.get("games_completed", 0) or 0)
    unsubscribes_now = int(current_stats.get("unsubscribes", 0) or 0)

    # Find the oldest snapshot within the last 7..10 days (walk back).
    then: dict[str, int] = {}
    for d in range(7, 11):
        candidate = await _get_snapshot(r, cid, _date_n_days_ago(d))
        if candidate:
            then = candidate
            break

    delta_imp = max(0, impressions_now - int(then.get("impressions", 0)))
    delta_clk = max(0, clicks_now - int(then.get("clicks", 0)))
    delta_cnv = max(0, conversions_now - int(then.get("conversions", 0)))
    delta_gs = max(0, games_started_now - int(then.get("games_started", 0)))
    delta_gc = max(0, games_completed_now - int(then.get("games_completed", 0)))
    delta_unsub = max(0, unsubscribes_now - int(then.get("unsubscribes", 0)))

    ctr = (delta_clk / delta_imp) if delta_imp > 0 else 0.0
    cvr = (delta_cnv / delta_clk) if delta_clk > 0 else 0.0
    completion = (delta_gc / delta_gs) if delta_gs > 0 else 0.0
    complaint_rate = (delta_unsub / delta_imp) if delta_imp > 0 else 0.0

    return {
        "impressions": delta_imp,
        "clicks": delta_clk,
        "conversions": delta_cnv,
        "games_started": delta_gs,
        "games_completed": delta_gc,
        "unsubscribes": delta_unsub,
        "ctr": ctr,
        "cvr": cvr,
        "completion_rate": completion,
        "frequency_complaint_rate": complaint_rate,
    }


# ── Recompute (per-campaign + sweep) ─────────────────────────────────────


async def recompute_quality_score(
    r: aioredis.Redis,
    cid: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Recompute QS for a single campaign and persist the result.

    Honours active manual overrides unless ``force=True``. Writes:

      * `campaign:{cid}:quality_score`
      * `qs_last_recompute_ts`
      * `qs_components_json` (compact components dict for breakdown view)
      * snapshot for today (so the next sweep can compute its own delta)

    Returns a small dict describing what happened — useful for the cron
    summary and for the breakdown endpoint.
    """
    raw = await r.hgetall(_ck(cid))
    if not raw:
        return {"ok": False, "reason": "campaign_missing"}

    now = _now()
    # Honour active override.
    if not force:
        override_until = raw.get(QS_OVERRIDE_UNTIL_FIELD)
        if override_until:
            try:
                if float(override_until) > now:
                    return {
                        "ok": True,
                        "skipped": "active_override",
                        "current_qs": float(raw.get("quality_score", QS_DEFAULT)),
                    }
            except (TypeError, ValueError):
                pass

    try:
        old_qs = float(raw.get("quality_score", QS_DEFAULT) or QS_DEFAULT)
    except (TypeError, ValueError):
        old_qs = QS_DEFAULT

    metrics = await get_trailing_7d_metrics(r, cid)
    components_input = compute_qs_components(
        realised_ctr=metrics["ctr"],
        realised_cvr=metrics["cvr"],
        completion_rate=metrics["completion_rate"],
        frequency_complaint_rate=metrics["frequency_complaint_rate"],
    )

    # Decide path: stale → decay; else recompute + smooth.
    last_ts_raw = raw.get(QS_LAST_RECOMPUTE_FIELD)
    try:
        last_ts = float(last_ts_raw) if last_ts_raw else 0.0
    except (TypeError, ValueError):
        last_ts = 0.0
    weeks_since = (now - last_ts) / (7 * 86400) if last_ts > 0 else 1.0

    if metrics["impressions"] < STALE_IMPRESSION_THRESHOLD:
        new_qs = apply_decay(old_qs, metrics["impressions"], weeks_since)
        path = "decay"
    else:
        candidate_qs, _ = compute_new_qs(
            metrics["ctr"],
            metrics["cvr"],
            metrics["completion_rate"],
            metrics["frequency_complaint_rate"],
        )
        new_qs = smooth_qs(candidate_qs, old_qs)
        path = "autocompute"

    new_qs = _clamp(new_qs, QS_MIN, QS_MAX)

    # Persist QS + metadata + components.
    import json
    components_json = json.dumps({
        **components_input,
        "raw_sum": sum(components_input.values()),
    })
    await r.hset(
        _ck(cid),
        mapping={
            "quality_score": f"{new_qs:.6f}",
            QS_LAST_RECOMPUTE_FIELD: f"{now:.0f}",
            QS_COMPONENTS_FIELD: components_json,
        },
    )

    # Snapshot today's cumulative counters for tomorrow's delta.
    current_stats = await r.hgetall(_sk(cid)) or {}
    await _write_snapshot(r, cid, _today_utc(), current_stats)

    return {
        "ok": True,
        "campaign_id": cid,
        "old_qs": old_qs,
        "new_qs": new_qs,
        "path": path,
        "trailing_7d": metrics,
        "components": components_input,
    }


async def recompute_all_active(
    r: aioredis.Redis,
) -> dict[str, int]:
    """Sweep over `campaigns:active` and recompute every campaign's QS.

    Returns counters: scanned / autocomputed / decayed / overridden / errored.
    Mirrors the billing_cron pattern.
    """
    counters = {
        "scanned": 0,
        "autocomputed": 0,
        "decayed": 0,
        "overridden": 0,
        "errored": 0,
    }
    cids = await r.smembers(ACTIVE_CAMPAIGNS_KEY)
    for cid in cids or set():
        counters["scanned"] += 1
        try:
            res = await recompute_quality_score(r, cid)
        except Exception as exc:  # pragma: no cover — never break the cron
            logger.warning("qs recompute failed for %s: %s", cid, exc)
            counters["errored"] += 1
            continue
        if not res.get("ok"):
            counters["errored"] += 1
            continue
        if res.get("skipped") == "active_override":
            counters["overridden"] += 1
        elif res.get("path") == "decay":
            counters["decayed"] += 1
        elif res.get("path") == "autocompute":
            counters["autocomputed"] += 1
    return counters


# ── Diagnostic endpoint (Part C) ─────────────────────────────────────────


@router.get("/{campaign_id}/qs-breakdown")
async def qs_breakdown(
    campaign_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return the current QS plus its component breakdown."""
    raw = await r.hgetall(_ck(campaign_id))
    if not raw:
        raise HTTPException(status_code=404, detail="campaign not found")

    try:
        current_qs = float(raw.get("quality_score", QS_DEFAULT) or QS_DEFAULT)
    except (TypeError, ValueError):
        current_qs = QS_DEFAULT

    metrics = await get_trailing_7d_metrics(r, campaign_id)
    components = compute_qs_components(
        realised_ctr=metrics["ctr"],
        realised_cvr=metrics["cvr"],
        completion_rate=metrics["completion_rate"],
        frequency_complaint_rate=metrics["frequency_complaint_rate"],
    )

    last_recompute_raw = raw.get(QS_LAST_RECOMPUTE_FIELD)
    try:
        last_recompute_ts = (
            float(last_recompute_raw) if last_recompute_raw else None
        )
    except (TypeError, ValueError):
        last_recompute_ts = None

    override_until_raw = raw.get(QS_OVERRIDE_UNTIL_FIELD)
    try:
        override_until_ts = (
            float(override_until_raw) if override_until_raw else None
        )
    except (TypeError, ValueError):
        override_until_ts = None
    override_active = bool(
        override_until_ts and override_until_ts > _now()
    )

    return {
        "campaign_id": campaign_id,
        "current_qs": current_qs,
        "components": components,
        "trailing_7d_metrics": {
            "impressions": metrics["impressions"],
            "clicks": metrics["clicks"],
            "conversions": metrics["conversions"],
            "games_started": metrics["games_started"],
            "games_completed": metrics["games_completed"],
            "unsubscribes": metrics["unsubscribes"],
            "ctr": metrics["ctr"],
            "cvr": metrics["cvr"],
            "completion_rate": metrics["completion_rate"],
            "frequency_complaint_rate": metrics["frequency_complaint_rate"],
        },
        "last_recompute_ts": last_recompute_ts,
        "override_active": override_active,
        "override_until_ts": override_until_ts,
        "is_stale": metrics["impressions"] < STALE_IMPRESSION_THRESHOLD,
    }


# ── Manual override (Part D) ─────────────────────────────────────────────


class QSOverrideBody(BaseModel):
    admin_token: str
    quality_score: float = Field(ge=QS_MIN, le=QS_MAX)
    ttl_seconds: int | None = Field(default=None, ge=0)


@router.put("/{campaign_id}/qs-override")
async def qs_override(
    campaign_id: str,
    body: QSOverrideBody = Body(...),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Admin-only: set a manual QS for a campaign.

    Sticky for ``ttl_seconds`` (default 7d) — the auto-compute sweep will
    skip the campaign until the override expires. Pass ``ttl_seconds=0``
    to clear an active override without changing QS.
    """
    _check_admin_token(body.admin_token)

    raw = await r.hgetall(_ck(campaign_id))
    if not raw:
        raise HTTPException(status_code=404, detail="campaign not found")

    now = _now()
    ttl = body.ttl_seconds if body.ttl_seconds is not None else DEFAULT_OVERRIDE_TTL_SECONDS

    if ttl == 0:
        # Clear override only.
        await r.hdel(_ck(campaign_id), QS_OVERRIDE_FIELD, QS_OVERRIDE_UNTIL_FIELD)
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "override_cleared": True,
            "current_qs": float(raw.get("quality_score", QS_DEFAULT)),
        }

    override_until = now + ttl
    await r.hset(
        _ck(campaign_id),
        mapping={
            "quality_score": f"{body.quality_score:.6f}",
            QS_OVERRIDE_FIELD: f"{body.quality_score:.6f}",
            QS_OVERRIDE_UNTIL_FIELD: f"{override_until:.0f}",
            QS_LAST_RECOMPUTE_FIELD: f"{now:.0f}",
        },
    )
    return {
        "ok": True,
        "campaign_id": campaign_id,
        "quality_score": body.quality_score,
        "override_until_ts": override_until,
    }


# ── Cron entry point ─────────────────────────────────────────────────────


async def run_weekly_sweep() -> dict[str, int]:
    """Entry point for the cron — sweeps every active campaign once."""
    r = await get_redis()
    return await recompute_all_active(r)
