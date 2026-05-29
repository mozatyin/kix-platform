"""PI (Proportional-Integral) pacing controller for the auction engine.

Replaces the legacy hourly-bucket pacing in ``app.routers.auction`` whose
recompute cadence (1h) let bursty traffic over- or under-shoot the daily
budget by enormous margins — sim runs showed an expected-vs-actual drift
of >96 percentage points on small campaigns.

Design
------

* **Setpoint** = ``daily_budget_cents / (24 * 60)`` cents per minute.
* **Process variable** = cents actually charged in the last 60 seconds.
* **Error**           = setpoint − actual (positive = under-spending).
* **P term**          = ``Kp * error``           (Kp = 1.0)
* **I term**          = ``Ki * cumulative_error`` (Ki = 0.05, capped to
  prevent integral wind-up).
* **Output**          = ``clamp(1.0 + P + I, 0.1, 2.0)``
* **Cadence**         = lazy-recompute every 60 s on read; no background
  task required (avoids extra moving parts).

Sliding-window spend is tracked via a Redis ZSET (``score = unix_ts``,
``member = "<ts>:<uuid>:<cents>"``). This makes the controller robust
against multiple rapid charges within the same recompute window — every
charge is counted exactly once because it carries a unique member ID.

Skip semantics (``should_skip_for_pacing``)
------------------------------------------

The auction maps the PI factor to a Bernoulli skip-rate so the
controller can both *brake* (over-spend) and *encourage* (under-spend):

* factor ≥ 1.0  → never skip (run all auctions, chase under-spend)
* factor  < 1.0 → skip with probability ``(1 - factor) / 0.9``
                 (i.e. factor=0.1 → 100% skip, factor=0.55 → 50%)

Backwards compatibility
-----------------------

The legacy schedule-window check is preserved by ``in_schedule_window`` —
campaigns outside ``schedule.hours_local`` still return a *hard* skip
("pacing_factor=0") so the auction loop can keep the same fast-path
``if pacing <= 0: continue`` it has today.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import redis.asyncio as aioredis


# ── Tuning constants ─────────────────────────────────────────────────────

KP: float = 1.0
KI: float = 0.05

# Hard caps for the PI output factor. 0.1 = "brake to 10% of normal",
# 2.0 = "double-throttle to chase under-spend".
FACTOR_MIN: float = 0.1
FACTOR_MAX: float = 2.0

# Integral wind-up cap (cents): bound on |cumulative_error| so a long
# pause early in the day cannot overshoot for the rest of the day.
INTEGRAL_CAP_CENTS: float = 10_000.0

# Recompute cadence: PI is recomputed at most once per ``RECOMPUTE_PERIOD``
# seconds. Reads in between return the cached factor.
RECOMPUTE_PERIOD: float = 60.0

# Sliding window for the "actual spend" process variable, in seconds.
WINDOW_SECONDS: float = 60.0

MINUTES_PER_DAY: int = 24 * 60


# ── Redis keys ───────────────────────────────────────────────────────────

K_SETPOINT = "pacing:{cid}:setpoint_cents_per_min"
K_ACTUAL_WINDOW = "pacing:{cid}:actual_cents_last_60s"  # ZSET (sliding window)
K_CUM_ERROR = "pacing:{cid}:cumulative_error_cents"
K_FACTOR = "pacing:{cid}:current_factor"
K_LAST_RECOMPUTE = "pacing:{cid}:last_recompute_ts"


def _now() -> float:
    return time.time()


# ── Sliding-window spend bookkeeping ─────────────────────────────────────


async def record_spend(
    r: aioredis.Redis, campaign_id: str, cents: int, *, now: float | None = None
) -> None:
    """Record a charge into the campaign's sliding-window ZSET.

    Each charge gets a unique member so the same call sequence cannot
    self-collide (ZSET would otherwise dedupe identical members).
    """
    if cents <= 0 or not campaign_id:
        return
    ts = now if now is not None else _now()
    key = K_ACTUAL_WINDOW.format(cid=campaign_id)
    member = f"{ts}:{uuid.uuid4().hex}:{cents}"
    pipe = r.pipeline()
    pipe.zadd(key, {member: ts})
    # ZSET TTL: 2× the window so stale entries die even if the campaign
    # goes idle. Recompute also trims, but TTL guards against forever-fill.
    pipe.expire(key, int(WINDOW_SECONDS * 2))
    await pipe.execute()


async def _actual_cents_in_window(
    r: aioredis.Redis, campaign_id: str, *, now: float | None = None
) -> int:
    """Sum cents charged in the last WINDOW_SECONDS via the ZSET members.

    Trims expired entries (score < now - WINDOW) as a side effect so the
    set cannot grow unbounded for long-running campaigns.
    """
    ts = now if now is not None else _now()
    key = K_ACTUAL_WINDOW.format(cid=campaign_id)
    cutoff = ts - WINDOW_SECONDS
    # Trim expired entries first.
    await r.zremrangebyscore(key, "-inf", cutoff)
    members = await r.zrangebyscore(key, cutoff, "+inf")
    total = 0
    for m in members:
        # Member format: "<ts>:<uuid>:<cents>"
        try:
            cents = int(m.rsplit(":", 1)[-1])
        except (ValueError, AttributeError):
            continue
        total += cents
    return total


# ── PI computation ───────────────────────────────────────────────────────


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _setpoint_cents_per_min(daily_budget_cents: int) -> float:
    if daily_budget_cents <= 0:
        return 0.0
    return daily_budget_cents / MINUTES_PER_DAY


async def recompute_factor(
    r: aioredis.Redis,
    campaign_id: str,
    daily_budget_cents: int,
    *,
    now: float | None = None,
    force: bool = False,
) -> float:
    """Recompute (or return cached) PI factor for a campaign.

    The factor is recomputed at most once per ``RECOMPUTE_PERIOD`` unless
    ``force=True``. ``daily_budget_cents <= 0`` short-circuits to 1.0
    (no budget cap → no pacing pressure, but auction can still run).
    """
    ts = now if now is not None else _now()

    if daily_budget_cents <= 0:
        # No budget → no pacing pressure. Return neutral 1.0 and don't
        # touch the state so the diagnostic endpoint stays meaningful if
        # the merchant later sets a budget.
        return 1.0

    last_raw = await r.get(K_LAST_RECOMPUTE.format(cid=campaign_id))
    try:
        last_recompute = float(last_raw) if last_raw else 0.0
    except (TypeError, ValueError):
        last_recompute = 0.0

    if not force and (ts - last_recompute) < RECOMPUTE_PERIOD:
        cached_raw = await r.get(K_FACTOR.format(cid=campaign_id))
        if cached_raw is not None:
            try:
                return float(cached_raw)
            except (TypeError, ValueError):
                pass  # fall through to recompute

    # Per-minute setpoint. Window is 60 s ⇒ setpoint_per_window == setpoint_per_min.
    setpoint = _setpoint_cents_per_min(daily_budget_cents)
    actual = await _actual_cents_in_window(r, campaign_id, now=ts)
    error = setpoint - actual  # positive = under-spending

    # Cumulative error, capped to prevent wind-up.
    cum_raw = await r.get(K_CUM_ERROR.format(cid=campaign_id))
    try:
        cum_error = float(cum_raw) if cum_raw is not None else 0.0
    except (TypeError, ValueError):
        cum_error = 0.0
    cum_error = _clamp(cum_error + error, -INTEGRAL_CAP_CENTS, INTEGRAL_CAP_CENTS)

    # Normalise terms by setpoint so the factor scale is unit-free.
    norm = setpoint if setpoint > 0 else 1.0
    p_term = KP * (error / norm)
    i_term = KI * (cum_error / norm)
    factor = _clamp(1.0 + p_term + i_term, FACTOR_MIN, FACTOR_MAX)

    pipe = r.pipeline()
    pipe.set(K_SETPOINT.format(cid=campaign_id), str(setpoint))
    pipe.set(K_CUM_ERROR.format(cid=campaign_id), str(cum_error))
    pipe.set(K_FACTOR.format(cid=campaign_id), str(factor))
    pipe.set(K_LAST_RECOMPUTE.format(cid=campaign_id), str(ts))
    await pipe.execute()

    return factor


async def get_state(
    r: aioredis.Redis,
    campaign_id: str,
    daily_budget_cents: int | None = None,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Read the full PI state for diagnostics — no recompute, no side effects."""
    setpoint_raw = await r.get(K_SETPOINT.format(cid=campaign_id))
    cum_raw = await r.get(K_CUM_ERROR.format(cid=campaign_id))
    factor_raw = await r.get(K_FACTOR.format(cid=campaign_id))
    last_raw = await r.get(K_LAST_RECOMPUTE.format(cid=campaign_id))
    actual = await _actual_cents_in_window(r, campaign_id, now=now)

    def _f(s: str | None, default: float = 0.0) -> float:
        try:
            return float(s) if s is not None else default
        except (TypeError, ValueError):
            return default

    setpoint = _f(setpoint_raw)
    if setpoint == 0.0 and daily_budget_cents and daily_budget_cents > 0:
        # Lazy default — surfaces the *would-be* setpoint when the
        # controller has never run for this campaign yet.
        setpoint = _setpoint_cents_per_min(daily_budget_cents)

    return {
        "setpoint_cents_per_min": setpoint,
        "actual_cents_last_60s": actual,
        "cumulative_error_cents": _f(cum_raw),
        "current_factor": _f(factor_raw, default=1.0),
        "last_recompute_ts": _f(last_raw),
        "kp": KP,
        "ki": KI,
        "integral_cap_cents": INTEGRAL_CAP_CENTS,
        "window_seconds": WINDOW_SECONDS,
        "recompute_period_seconds": RECOMPUTE_PERIOD,
    }


# ── Schedule-window helper (preserves legacy semantics) ──────────────────


def in_schedule_window(schedule_json: str | None, current_hour: int) -> bool:
    """True if ``current_hour`` (0..23 local) is inside the campaign window.

    Returns True when no window is configured (default 0..24). Supports
    wrap-around windows (e.g. 22→6).
    """
    if not schedule_json:
        return True
    try:
        sched = json.loads(schedule_json)
    except (json.JSONDecodeError, TypeError):
        return True
    hours = sched.get("hours_local") or [0, 24]
    try:
        h_start, h_end = int(hours[0]), int(hours[1])
    except (TypeError, ValueError, IndexError):
        return True
    if h_start <= h_end:
        return h_start <= current_hour < h_end
    return current_hour >= h_start or current_hour < h_end


# ── Auction-side hooks ───────────────────────────────────────────────────


async def should_skip_for_pacing(
    r: aioredis.Redis,
    campaign_id: str,
    daily_budget_cents: int,
    *,
    rand: float | None = None,
    now: float | None = None,
) -> tuple[bool, float]:
    """Probabilistic gate keyed off the current PI factor.

    Returns ``(skip, factor)``. ``factor`` is the PI output (1.0 when no
    budget). Skip semantics:

    * factor ≥ 1.0 → never skip
    * factor  < 1.0 → skip with probability ``(1.0 - factor) / 0.9``
    """
    factor = await recompute_factor(
        r, campaign_id, daily_budget_cents, now=now
    )
    if factor >= 1.0:
        return False, factor
    # Map factor∈[0.1, 1.0) → skip-prob∈[0.0, 1.0]
    skip_prob = (1.0 - factor) / 0.9
    if rand is None:
        import random as _random  # lazy — keep module import cheap
        rand = _random.random()
    return rand < skip_prob, factor


async def pacing_factor_for_auction(
    r: aioredis.Redis,
    campaign: dict[str, str],
    current_hour: int,
    *,
    now: float | None = None,
) -> float:
    """Drop-in replacement for the legacy ``_pacing_factor`` in auction.py.

    Returns 0.0 when the campaign is outside its schedule window (so the
    auction's fast-path ``if pacing <= 0: continue`` keeps working).
    Otherwise returns the PI factor clamped to [0.1, 2.0].
    """
    if not in_schedule_window(campaign.get("schedule"), current_hour):
        return 0.0
    cid = campaign.get("campaign_id", "")
    if not cid:
        return 1.0
    try:
        daily_budget = int(campaign.get("daily_budget_cents", 0))
    except (TypeError, ValueError):
        daily_budget = 0
    return await recompute_factor(r, cid, daily_budget, now=now)
