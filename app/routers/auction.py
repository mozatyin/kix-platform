"""Auction Engine — quality-adjusted Vickrey (GSP) for KiX campaigns.

For every user impression slot, KiX runs an auction over all currently
``active`` campaigns. The flow:

  1. Fetch all members of ``campaigns:active``.
  2. Filter by targeting (geo / demo / interests / exclude / lookalike).
  3. Filter by schedule (start_at / end_at / hours_local / DOW).
  4. Filter by budget (daily / total).
  5. Rank by  rank = max_bid_cents × quality_score  (highest wins).
  6. Charge the winner the *second-price* equivalent: the minimum bid
     it would have needed to outrank #2, with a +1¢ tiebreak. If only
     one bidder is present, it pays its own bid (capped by max_bid).
  7. Create an ``impression:{token}`` record (TTL 7d) so downstream
     event reports (click / conversion) can settle the charge.
  8. Charge immediately for CPM; defer for CPC/CPA/CPS/CPV.

Spend is recorded via ``campaigns.record_spend`` which atomically
updates daily + total counters and re-derives status (auto-pausing on
budget exhaustion).

Wallet integration is best-effort — if the wallet router is present
we call its internal helper; otherwise we record spend locally so the
auction still works in dev / standalone deployments.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.pacing_controller import (
    pacing_factor_for_auction as _pi_pacing_factor,
    in_schedule_window as _pi_in_schedule_window,
    record_spend as _pi_record_spend,
    get_state as _pi_get_state,
)
from app.redis_client import get_redis
from app.routers.audiences import campaign_audience_matches
from datetime import datetime, timezone
from app.routers.campaigns import (
    ACTIVE_BY_COUNTRY_KEY,
    ACTIVE_BY_GEOHASH_KEY,
    ACTIVE_BY_OBJECTIVE_KEY,
    ACTIVE_CAMPAIGNS_KEY,
    ACTIVE_UNTARGETED_KEY,
    AUCTION_ELIGIBLE_STATUSES,
    CAMPAIGN_STATS_KEY,
    _ck,
    _geohash_encode,
    _read_daily_spend,
    _read_total_spend,
    _safe_json_loads,
    _sk,
    _user_matches_targeting,
    adjust_quality_score,
    record_spend,
)

# Best-effort import of frequency_cap.check_internal — sibling module is
# built in parallel. If unavailable, all checks pass.
try:
    from app.routers.frequency_cap import check_internal as _freq_check_internal  # type: ignore
except ImportError:  # pragma: no cover
    async def _freq_check_internal(  # type: ignore[misc]
        user_id: str | None,
        brand_id: str,
        slot: str,
        r: aioredis.Redis,
        *,
        device_fingerprint: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        return True, {}

logger = logging.getLogger(__name__)

router = APIRouter()

# Wave C: TriSoul auction boost (additive ±10% rank multiplier, behind
# per-user flag). Identity no-op when the integration module is missing
# or the flag is off.
try:  # pragma: no cover — best-effort
    from app.routers.trisoul_integration import (  # type: ignore
        maybe_boost_auction as _trisoul_boost_auction,
    )
except ImportError:  # pragma: no cover
    async def _trisoul_boost_auction(  # type: ignore[misc]
        uid: str | None, bid: str, base_rank: float, r: aioredis.Redis,
    ) -> tuple[float, dict[str, Any]]:
        return base_rank, {"trisoul": "unavailable"}


# ── Constants ────────────────────────────────────────────────────────────

IMPRESSION_KEY = "impression:{token}"
IMPRESSION_TTL = 86400 * 7  # 7 days

CLICK_KEY = "impression:{token}:click"
CONVERSION_KEY = "impression:{token}:conversion"
# CPE: one settlement per impression × engagement_type. Key shape mirrors
# CLICK_KEY so TTLs and inspection stay symmetric.
ENGAGEMENT_KEY = "impression:{token}:engagement:{etype}"

# Charge timing per bid_strategy.
CHARGE_ON_IMPRESSION = {"cpm"}
CHARGE_ON_CLICK = {"cpc", "cpv"}
CHARGE_ON_CONVERSION = {"cpa", "cps"}
# cpe = cost-per-engagement: charge fires on /report-engagement (game_play,
# video_complete, voucher_claim, level_up, streak_milestone, ...).
CHARGE_ON_ENGAGEMENT = {"cpe"}

# Engagement event types accepted by /report-engagement. Mirrors
# campaigns.ENGAGEMENT_TYPES — duplicated here so this module doesn't
# import a runtime set across the boundary.
ENGAGEMENT_TYPES = {
    "game_play_30s", "video_complete", "voucher_claim",
    "level_up", "streak_milestone",
}

# Default attribution window for impressions whose campaign did not set
# attribution_window_days. 7 days matches the long-standing implicit default.
DEFAULT_ATTRIBUTION_WINDOW_SECONDS = 7 * 86400

# Anti-fraud reject threshold.
FRAUD_REJECT_THRESHOLD = 70

# Reserve price (slot floor) — admin-tunable, in cents.
RESERVE_KEY = "auction:reserve:{slot}"
VALID_RESERVE_SLOTS = ("main", "banner", "interstitial", "push", "geofence")

# Cold-start QS learning phase — new campaigns get a decaying boost on rank
# for their first LEARNING_PHASE_HOURS hours so a default qs=0.5 can compete
# against incumbents with high earned QS.
LEARNING_PHASE_HOURS = 24
LEARNING_BOOST_MAX = 0.5  # 1 + 0.5 = 1.5× at t=0, decays linearly to 1.0

# Diversity floor — fraction of trailing auctions each brand is guaranteed
# to enter the top-3 for. Combats winner-take-all dynamics (cold-start
# starvation) when one high-bid brand dominates ranking.
DIVERSITY_WINDOW = 1000
DIVERSITY_FLOOR_PCT = float(os.environ.get("AUCTION_DIVERSITY_FLOOR_PCT", "3"))
DIVERSITY_TOP_K = 3
DIVERSITY_ENTERED_KEY = "auction:diversity:entered:{brand_id}"
DIVERSITY_WON_KEY = "auction:diversity:won:{brand_id}"
DIVERSITY_TOTAL_KEY = "auction:diversity:total"
DIVERSITY_TTL = 86400 * 7


# ── Pydantic ─────────────────────────────────────────────────────────────


class GeoContext(BaseModel):
    lat: float | None = None
    lng: float | None = None
    country: str | None = None
    city: str | None = None


class AuctionContext(BaseModel):
    current_brand: str | None = None
    time_of_day: int | None = None  # hour 0..23 local
    day_of_week: int | None = None  # 0..6
    device: Literal["mobile", "desktop", "tablet"] | None = None
    language: str | None = None
    gameplay_event: str | None = None
    screen: Literal["home", "game_over", "reward", "leaderboard"] | None = None


class AuctionRequest(BaseModel):
    user_id: str | None = None
    device_fingerprint: str
    geo: GeoContext | None = None
    context: AuctionContext = Field(default_factory=AuctionContext)
    objective_filter: Literal[
        "acquire", "sales", "awareness", "geo_visit",
        "engagement", "retention", "activation", "win_back",
    ] | None = None
    slot: Literal["main", "banner", "interstitial", "push", "geofence"] = "main"


class AuctionResponse(BaseModel):
    winner_campaign_id: str | None = None
    winner_brand_id: str | None = None
    winning_bid_cents: int = 0
    actual_charge_cents: int = 0
    creative: dict[str, Any] = Field(default_factory=dict)
    impression_token: str | None = None
    no_eligible_campaigns: bool = False
    eligible_count: int = 0
    # Optional diagnostics — populated when no_eligible_campaigns or when a
    # winner had to be skipped (reserve / freq-cap). Always omitted from the
    # happy path so existing clients are unaffected.
    reason: str | None = None
    reserve_threshold_cents: int | None = None


class ImpressionReport(BaseModel):
    impression_token: str


class ClickReport(BaseModel):
    impression_token: str
    user_id: str | None = None
    device_fingerprint: str | None = None


class ConversionReport(BaseModel):
    impression_token: str
    user_id: str
    conversion_value_cents: int = Field(ge=0)


class EngagementReport(BaseModel):
    """Cost-per-engagement event report.

    Fires for the configured ENGAGEMENT_TYPES. The campaign's actual_charge
    (settled from the auction's second-price) is applied once per impression
    token — duplicate calls (same token + same engagement_type) are
    idempotent.
    """
    impression_token: str
    engagement_type: Literal[
        "game_play_30s", "video_complete", "voucher_claim",
        "level_up", "streak_milestone",
    ]
    user_id: str | None = None
    value_seconds: int | None = Field(default=None, ge=0)
    device_fingerprint: str | None = None


# ── Helpers ──────────────────────────────────────────────────────────────


def _now() -> float:
    return time.time()


def _imp_key(token: str) -> str:
    return IMPRESSION_KEY.format(token=token)


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    )
    return 2 * R * math.asin(math.sqrt(a))


def _geo_match(targeting: dict[str, Any], geo_ctx: dict[str, Any] | None) -> bool:
    """Targeting geo includes a radius check that user-profile match cannot do."""
    geo = (targeting or {}).get("geo") or {}
    if not geo:
        return True
    ctx = geo_ctx or {}
    if geo.get("country") and ctx.get("country") and ctx["country"] != geo["country"]:
        return False
    if geo.get("city") and ctx.get("city") and ctx["city"] != geo["city"]:
        return False
    if geo.get("lat") is not None and ctx.get("lat") is not None:
        try:
            dist = _haversine_km(
                float(geo["lat"]),
                float(geo["lng"]),
                float(ctx["lat"]),
                float(ctx["lng"]),
            )
            if dist > float(geo.get("radius_km", 5)):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _schedule_match(schedule: dict[str, Any]) -> bool:
    if not schedule:
        return True
    now = _now()
    if schedule.get("start_at") and now < float(schedule["start_at"]):
        return False
    if schedule.get("end_at") and now > float(schedule["end_at"]):
        return False
    now_dt = time.localtime(now)
    hours = schedule.get("hours_local") or []
    if len(hours) == 2:
        h_start, h_end = int(hours[0]), int(hours[1])
        h_now = now_dt.tm_hour
        if h_start <= h_end:
            if not (h_start <= h_now < h_end):
                return False
        else:  # wraps midnight
            if not (h_now >= h_start or h_now < h_end):
                return False
    dow = schedule.get("days_of_week")
    if dow:
        # Python weekday: Monday=0..Sunday=6. We accept either convention; both
        # are 0..6 — the merchant picks. We treat 0 as the platform's own Mon.
        if now_dt.tm_wday not in [int(d) for d in dow]:
            return False
    return True


async def _has_budget(r: aioredis.Redis, c: dict[str, str]) -> bool:
    cid = c.get("campaign_id", "")
    brand_id = c.get("brand_id", "")
    daily_budget = int(c.get("daily_budget_cents", 0))
    total_budget = int(c.get("total_budget_cents", 0))
    max_bid = int(c.get("max_bid_cents", 0))
    if max_bid <= 0:
        return False

    # ── Wallet-side backpressure check (sim fix: log-flood on cap) ──────
    # If the wallet recently raised daily_budget_exceeded for this
    # campaign or brand, the wallet router set a Redis flag with a TTL
    # ending at the next UTC midnight. Short-circuit here so the auction
    # never re-awards an impression that's guaranteed to 402 on charge.
    if cid:
        if await r.exists(f"auction:campaign:{cid}:budget_blocked"):
            return False
    if brand_id:
        if await r.exists(f"auction:brand:{brand_id}:budget_blocked"):
            return False

    daily_spent = await _read_daily_spend(r, cid)
    total_spent = await _read_total_spend(r, cid)
    if daily_budget > 0 and daily_spent + max_bid > daily_budget:
        # Allow up to budget; only block if even a single charge would overshoot.
        if daily_spent >= daily_budget:
            return False
    if total_budget > 0 and total_spent >= total_budget:
        return False
    return True


async def _has_compliance(c: dict[str, str], region: str | None) -> bool:
    """Return True iff campaign ad content is allowed in the user's region.

    Additive regional gate on top of the CN ad-creative scanner in
    ``app.routers.compliance``. Reads the campaign's
    ``content_category`` field (set by the creative_gen pipeline) and
    checks it against the region's banned list. Missing region or
    missing category means we allow — this is fail-open by design so
    legacy CN campaigns keep flowing.
    """
    if not region:
        return True
    category = c.get("content_category", "")
    if not category:
        return True
    try:
        from app.compliance_regional import check_content_allowed
        allowed, _ = check_content_allowed(region, category)
    except KeyError:
        return True
    return allowed


async def _load_user_profile(
    r: aioredis.Redis, user_id: str | None
) -> dict[str, Any]:
    if not user_id:
        return {}
    prof = await r.hgetall(f"user:{user_id}")
    if prof:
        prof.setdefault("user_id", user_id)
    return prof


# ── Wallet integration (best-effort) ─────────────────────────────────────


_WALLET_REASON_MAP = {
    # auction reason → wallet ChargeRequest.reason literal
    "cpm_impression": "cpm_impression",
    "cpc_click": "cpv_visit",  # closest analogue
    "cpv_click": "cpv_visit",
    "cpa_conversion": "cpa_conversion",
    "cps_conversion": "cps_commission",
    # Engagement charges fold into the impression bucket from the wallet's
    # point of view — the engagement itself is the "render value".
    "cpe_engagement": "cpm_impression",
}


async def _wallet_charge(
    r: aioredis.Redis,
    brand_id: str,
    cents: int,
    reason: str,
    reference_id: str,
    campaign_id: str | None = None,
) -> bool:
    """Call the wallet router via its in-process handler; degrade gracefully."""
    try:
        from app.routers.wallet import charge as wallet_charge_handler
        from app.routers.wallet import ChargeRequest
    except (ImportError, AttributeError):
        logger.debug(
            "wallet router absent — recording local spend only (brand=%s, %d¢)",
            brand_id, cents,
        )
        return False

    wallet_reason = _WALLET_REASON_MAP.get(reason, "cpm_impression")
    try:
        body = ChargeRequest(
            amount_cents=cents,
            reason=wallet_reason,
            reference_id=reference_id[:128],
            campaign_id=campaign_id,
        )
        await wallet_charge_handler(brand_id=brand_id, body=body, r=r)
        return True
    except HTTPException as exc:
        logger.warning(
            "wallet charge declined brand=%s amount=%d reason=%s detail=%s",
            brand_id, cents, reason, exc.detail,
        )
        return False
    except Exception as exc:  # pragma: no cover — never break the auction
        logger.warning("wallet charge raised: %s", exc)
        return False


# ── Anti-fraud (best-effort) ─────────────────────────────────────────────


async def _fraud_score(
    r: aioredis.Redis,
    *,
    user_id: str | None,
    device_fingerprint: str | None,
    source_brand: str | None,
    target_brand: str | None,
) -> int:
    """Return fraud_score in [0, 100]. 100 = certain fraud."""
    # Self-attribution: source brand == target brand is a hard 100.
    if source_brand and target_brand and source_brand == target_brand:
        return 100

    # Velocity check — too many events from one fingerprint in 1 min.
    if device_fingerprint:
        velocity_key = f"fraud:velocity:{device_fingerprint}"
        n = await r.incr(velocity_key)
        if n == 1:
            await r.expire(velocity_key, 60)
        if n > 20:
            return 90
        if n > 10:
            return 60

    # Attempt to call attribution router's fraud checker if available.
    try:
        from app.routers.attribution import _run_fraud_checks  # type: ignore
        score, _reasons = await _run_fraud_checks(
            r,
            user_id=user_id,
            device_fingerprint=device_fingerprint,
            brand_id=target_brand or "",
            action_type="auction_event",
            source_brand=source_brand,
            target_brand=target_brand,
        )
        return int(score)
    except (ImportError, AttributeError):
        return 0
    except Exception as exc:  # pragma: no cover
        logger.warning("fraud check failed: %s", exc)
        return 0


# ── Reserve price (admin floor per slot) ─────────────────────────────────


async def _get_reserve_cents(r: aioredis.Redis, slot: str) -> int:
    raw = await r.get(RESERVE_KEY.format(slot=slot))
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


# ── Cold-start learning boost ────────────────────────────────────────────


def _learning_boost(campaign: dict[str, str], now: float | None = None) -> float:
    """Multiplier on rank for campaigns in their learning phase.

    Returns 1.0 once the campaign is past LEARNING_PHASE_HOURS, otherwise
    linearly decays from (1 + LEARNING_BOOST_MAX) at t=0 to 1.0 at the
    boundary. Lets a fresh campaign with default qs=0.5 outrank an
    incumbent for its first day so QS can actually learn.
    """
    try:
        created_at = float(campaign.get("created_at", 0) or 0)
    except (TypeError, ValueError):
        return 1.0
    if created_at <= 0:
        return 1.0
    now = now if now is not None else _now()
    hours_old = max(0.0, (now - created_at) / 3600.0)
    if hours_old >= LEARNING_PHASE_HOURS:
        return 1.0
    remaining = 1.0 - (hours_old / LEARNING_PHASE_HOURS)
    return 1.0 + remaining * LEARNING_BOOST_MAX


# ── Diversity floor (sliding window) ─────────────────────────────────────


async def _record_brand_auction_entered(
    r: aioredis.Redis, brand_ids: set[str]
) -> None:
    """Bump the trailing-window entered counters for every brand that bid."""
    if not brand_ids:
        return
    ts = _now()
    pipe = r.pipeline()
    for bid in brand_ids:
        key = DIVERSITY_ENTERED_KEY.format(brand_id=bid)
        pipe.zadd(key, {f"{ts}:{uuid4().hex[:8]}": ts})
        pipe.zremrangebyrank(key, 0, -DIVERSITY_WINDOW - 1)
        pipe.expire(key, DIVERSITY_TTL)
    pipe.incr(DIVERSITY_TOTAL_KEY)
    pipe.expire(DIVERSITY_TOTAL_KEY, DIVERSITY_TTL)
    await pipe.execute()


async def _record_brand_auction_won(r: aioredis.Redis, brand_id: str) -> None:
    if not brand_id:
        return
    ts = _now()
    key = DIVERSITY_WON_KEY.format(brand_id=brand_id)
    pipe = r.pipeline()
    pipe.zadd(key, {f"{ts}:{uuid4().hex[:8]}": ts})
    pipe.zremrangebyrank(key, 0, -DIVERSITY_WINDOW - 1)
    pipe.expire(key, DIVERSITY_TTL)
    await pipe.execute()


async def _brand_entered_count(r: aioredis.Redis, brand_id: str) -> int:
    if not brand_id:
        return 0
    try:
        return int(await r.zcard(DIVERSITY_ENTERED_KEY.format(brand_id=brand_id)))
    except Exception:  # pragma: no cover
        return 0


async def _brand_won_count(r: aioredis.Redis, brand_id: str) -> int:
    if not brand_id:
        return 0
    try:
        return int(await r.zcard(DIVERSITY_WON_KEY.format(brand_id=brand_id)))
    except Exception:  # pragma: no cover
        return 0


async def _trailing_total_auctions(r: aioredis.Redis) -> int:
    try:
        raw = await r.get(DIVERSITY_TOTAL_KEY)
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def _apply_diversity_floor(
    ranked: list[tuple[float, int, float, float, dict[str, str]]],
    starved_brand_ids: set[str],
) -> list[tuple[float, int, float, float, dict[str, str]]]:
    """Promote one starved brand's candidate to the winner slot.

    Diversity-floor semantics: when at least one brand in
    ``starved_brand_ids`` has a candidate in this auction, move its
    highest-ranked candidate to position 0. Top-3 inclusion alone does
    not move the "won" counter — only winner-slot promotion converts
    starvation back into actual share. The displaced items keep their
    relative order below.

    Idempotent: if a starved candidate is already at position 0, no
    change. Stable: total item count and brand set are preserved.
    """
    if not starved_brand_ids or not ranked:
        return ranked

    promote_idx: int | None = None
    for i, row in enumerate(ranked):
        if row[4].get("brand_id", "") in starved_brand_ids:
            promote_idx = i
            break
    if promote_idx is None or promote_idx == 0:
        return ranked

    starved_row = ranked[promote_idx]
    rest = ranked[:promote_idx] + ranked[promote_idx + 1:]
    return [starved_row] + rest


# ── Existing-customer exclusion (TikTok/Google/Facebook parity) ─────────
#
# Acquisition campaigns by default skip users already known to the
# advertiser, so merchants don't pay to "buy back" their own customers.
# A user counts as an existing customer if any of:
#   1. They're a member of ``brand:{bid}:users`` (registered)
#   2. They have a prior ``conversion`` event in ``brand:{bid}:attr_incoming``
#   3. They belong to an audience flagged ``is_existing_customer_list=1``
#
# The probe is cached for 60s per (brand, user) to keep the hot auction
# path fast — small brands (< 1000 users) skip cache since the SET probe
# alone is sub-millisecond.

EXISTING_CHECK_CACHE_KEY = "existing_check:{brand_id}:{user_id}"
EXISTING_CHECK_TTL = 60
EXISTING_CHECK_CACHE_MIN_BRAND_SIZE = 1000

AUCTION_SKIPPED_EXISTING_KEY = "brand:{brand_id}:auction_skipped:existing_customer:{date}"
AUCTION_SKIPPED_EXISTING_TTL = 86400 * 35  # ~5 weeks of daily counters


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _is_existing_customer(
    r: aioredis.Redis, user_id: str | None, brand_id: str
) -> bool:
    """Return True iff ``user_id`` is already a customer of ``brand_id``.

    Sources (any one is sufficient):
      1. Member of ``brand:{bid}:users`` SET
      2. Has a prior ``conversion`` event in ``brand:{bid}:attr_incoming`` ZSET
      3. Member of any audience tagged ``is_existing_customer_list``

    Result is cached for 60s per (brand, user) for brands above the
    minimum size threshold; tiny brands skip cache (SISMEMBER is faster
    than the round-trip).
    """
    if not user_id or not brand_id:
        return False

    # Estimate brand size cheaply to decide on caching.
    try:
        brand_size = await r.scard(f"brand:{brand_id}:users")
    except Exception:  # pragma: no cover — never break the auction
        brand_size = 0

    cache_key = EXISTING_CHECK_CACHE_KEY.format(brand_id=brand_id, user_id=user_id)
    use_cache = brand_size >= EXISTING_CHECK_CACHE_MIN_BRAND_SIZE

    if use_cache:
        cached = await r.get(cache_key)
        if cached is not None:
            return cached == "1"

    result = False

    # Source 1: registered users SET
    try:
        if await r.sismember(f"brand:{brand_id}:users", user_id):
            result = True
    except Exception as exc:  # pragma: no cover
        logger.warning("existing_customer SET probe failed: %s", exc)

    # Source 2: prior conversion event in attribution log
    if not result:
        try:
            events = await r.zrevrange(f"brand:{brand_id}:attr_incoming", 0, 50)
            for eid in events or []:
                e = await r.hgetall(f"attr:{eid}")
                if not e:
                    continue
                if e.get("user_id") == user_id and e.get("stage") == "conversion":
                    result = True
                    break
        except Exception as exc:  # pragma: no cover
            logger.warning("existing_customer attr probe failed: %s", exc)

    # Source 3: audience flag
    if not result:
        try:
            from app.routers.audiences import get_user_audience_memberships
            memberships = await get_user_audience_memberships(user_id, r)
            for aid in memberships:
                audience = await r.hgetall(f"audience:{aid}")
                if not audience:
                    continue
                if (
                    audience.get("brand_id") == brand_id
                    and audience.get("is_existing_customer_list") == "1"
                ):
                    result = True
                    break
        except ImportError:  # pragma: no cover
            pass
        except Exception as exc:  # pragma: no cover
            logger.warning("existing_customer audience probe failed: %s", exc)

    if use_cache:
        try:
            await r.set(cache_key, "1" if result else "0", ex=EXISTING_CHECK_TTL)
        except Exception:  # pragma: no cover
            pass

    return result


async def _record_existing_customer_skip(
    r: aioredis.Redis, brand_id: str
) -> None:
    """Bump the per-brand daily counter of skipped impressions.

    Lets merchants see "we saved you ¥X by not buying back your own
    customers" on their dashboard.
    """
    if not brand_id:
        return
    key = AUCTION_SKIPPED_EXISTING_KEY.format(
        brand_id=brand_id, date=_today_utc()
    )
    try:
        n = await r.incr(key)
        if n == 1:
            await r.expire(key, AUCTION_SKIPPED_EXISTING_TTL)
    except Exception:  # pragma: no cover
        pass


# ── Pacing (PI controller — see app.pacing_controller) ──────────────────
#
# The legacy hourly-bucket pacing was replaced by a Proportional-Integral
# controller in ``app/pacing_controller.py``. The controller recomputes a
# rank multiplier every 60s against a per-minute setpoint, so bursty
# traffic can no longer over- or under-shoot the daily budget by the >96
# percentage-point margins observed in sim. The PI factor is clamped to
# [0.1, 2.0]; outside-schedule-window still returns 0.0 so the auction's
# fast-path ``if pacing <= 0: continue`` keeps working unchanged.


def _pacing_recommendation(factor: float) -> str:
    """Human-readable recommendation derived from the PI factor."""
    if factor <= 0:
        return "outside_schedule_window"
    if factor < 0.5:
        return "overpacing — PI braking hard, reduce bids or pause"
    if factor < 0.9:
        return "overpacing — PI lightly braking"
    if factor > 1.5:
        return "underpacing — PI throttling up, broaden targeting"
    if factor > 1.1:
        return "underpacing — PI lightly accelerating"
    return "on track"


# ── Smart bidding (target CPA / target ROAS / maximize_conversions) ──────


def _cvr_estimate(stats: dict[str, str]) -> float:
    """clicks / impressions, clamped to [0.001, 1.0]. Conservative when sparse."""
    try:
        clicks = int(stats.get("clicks", 0))
        imps = int(stats.get("impressions", 0))
    except (TypeError, ValueError):
        return 0.01
    if imps < 50:
        # Not enough data — assume a low default (1%) so we don't over-bid.
        return 0.01
    return max(0.001, min(1.0, clicks / max(imps, 1)))


def _conv_rate_estimate(stats: dict[str, str]) -> float:
    """conversions / clicks, clamped to [0.001, 1.0]. Sparse → 1%."""
    try:
        clicks = int(stats.get("clicks", 0))
        convs = int(stats.get("conversions", 0))
    except (TypeError, ValueError):
        return 0.01
    if clicks < 20:
        return 0.01
    return max(0.001, min(1.0, convs / max(clicks, 1)))


def _avg_conversion_value(stats: dict[str, str]) -> float:
    try:
        rev = int(stats.get("revenue_cents", 0))
        convs = int(stats.get("conversions", 0))
    except (TypeError, ValueError):
        return 0.0
    if convs <= 0:
        return 0.0
    return rev / convs


def _compute_auto_bid(
    campaign: dict[str, str],
    stats: dict[str, str],
) -> int:
    """Resolve an effective bid (cents) for ranking given the bid strategy.

    Recognized auto-strategies (via ``bid_optimization`` / ``optimization``
    field on the campaign hash; falls back to manual ``max_bid_cents``):

      * ``target_cpa``    — bid = target_cpa × CVR(click→impression). Cap by max.
      * ``target_roas``   — bid = (avg_conv_value × conv_rate) / target_roas.
      * ``maximize_conversions`` — bid = max_bid_cents (use full headroom).

    For every other / missing optimization, returns ``max_bid_cents``.
    """
    try:
        max_bid = int(campaign.get("max_bid_cents", 0))
    except (TypeError, ValueError):
        max_bid = 0

    optimization = (
        campaign.get("bid_optimization")
        or campaign.get("optimization")
        or "manual"
    ).lower()

    if optimization == "target_cpa":
        try:
            target_cpa = int(campaign.get("target_cpa_cents", 0))
        except (TypeError, ValueError):
            target_cpa = 0
        cvr = _cvr_estimate(stats)
        if target_cpa <= 0 or cvr <= 0:
            return max_bid
        optimal = int(target_cpa * cvr)
        return min(optimal, max_bid) if max_bid > 0 else optimal

    if optimization == "target_roas":
        try:
            target_roas = float(campaign.get("target_roas", 0))
        except (TypeError, ValueError):
            target_roas = 0.0
        avg_value = _avg_conversion_value(stats)
        conv_rate = _conv_rate_estimate(stats)
        if target_roas <= 0 or avg_value <= 0 or conv_rate <= 0:
            return max_bid
        # bid so revenue/spend ≈ target_roas
        predicted_value_per_click = avg_value * conv_rate
        optimal = int(predicted_value_per_click / target_roas)
        return min(optimal, max_bid) if max_bid > 0 else optimal

    if optimization == "maximize_conversions":
        # Spend the headroom — auctions are second-price so over-bidding is
        # safe up to max_bid.
        return max_bid

    return max_bid


# ── Candidate selection (partitioned active set) ─────────────────────────


async def find_candidates_for_user(
    user_context: dict[str, Any],
    r: aioredis.Redis,
    *,
    objective_filter: str | None = None,
) -> set[str]:
    """Return the small slice of active campaigns possibly relevant to a user.

    Strategy — use the per-geo partition SETs (country, geohash5) UNIONed
    with the untargeted SET so we don't pay O(N) to enumerate the full
    ``campaigns:active`` aggregate when only a tiny fraction of campaigns
    target this user's geography. At 10K active campaigns this drops the
    candidate set from N → ~N / geo_fanout, cutting auction latency
    from ~500ms → ~20-30ms.

    Important — only **narrowing** dimensions are intersected:

      * ``country`` and ``geohash5`` are geo *narrowing* dims: a campaign
        targeting US lives only in the US partition; a user in US looks
        up only the US partition. We UNION across the two geo dims (a
        campaign targeting just country OR just a geohash should both
        match a user inside that geography).
      * ``objective_filter`` is a user-supplied filter the caller wants
        enforced before scoring. Applied as an INTERSECT against the
        geo-union result.
      * audience_id / brand_id are NOT used here because a user may
        belong to multiple audiences while a campaign may target a
        single audience — intersecting would over-narrow. Those filters
        are applied per-campaign by ``campaign_audience_matches`` /
        ``_is_existing_customer`` inside ``run_auction``.

    The untargeted SET is UNIONed onto the result so universal-reach
    campaigns still bid.

    Falls back to ``campaigns:active`` SMEMBERS when no narrowing signal
    is available (small platforms / admin tools).
    """
    narrowing_sets: list[str] = []

    country = (user_context.get("country") or "").strip()
    if country:
        narrowing_sets.append(
            ACTIVE_BY_COUNTRY_KEY.format(country=country.upper())
        )

    lat = user_context.get("lat")
    lng = user_context.get("lng")
    if lat is not None and lng is not None:
        try:
            gh = _geohash_encode(float(lat), float(lng), precision=5)
            narrowing_sets.append(ACTIVE_BY_GEOHASH_KEY.format(gh=gh))
        except (TypeError, ValueError):
            pass

    # No narrowing signal at all → fall back to the legacy aggregate.
    # Tiny platforms run sub-millisecond here; large platforms with no
    # geo signal degrade to the original O(N) — same as before
    # partitioning, so we're never worse than baseline.
    if not narrowing_sets and not objective_filter:
        return set(await r.smembers(ACTIVE_CAMPAIGNS_KEY))

    temp_key = f"_auction_temp:{uuid4().hex[:12]}"
    try:
        if narrowing_sets:
            # Candidate pool = (union of geo narrowings) ∪ untargeted.
            await r.sunionstore(
                temp_key, *narrowing_sets, ACTIVE_UNTARGETED_KEY
            )
            if objective_filter:
                obj_key = ACTIVE_BY_OBJECTIVE_KEY.format(
                    objective=objective_filter
                )
                await r.sinterstore(temp_key, temp_key, obj_key)
        else:
            # Objective filter only, no geo signal — intersect with the
            # full active aggregate.
            obj_key = ACTIVE_BY_OBJECTIVE_KEY.format(
                objective=objective_filter or ""
            )
            await r.sinterstore(temp_key, ACTIVE_CAMPAIGNS_KEY, obj_key)
        result = await r.smembers(temp_key)
    finally:
        await r.delete(temp_key)
    return set(result)


async def _augment_with_untargeted(r: aioredis.Redis) -> set[str]:
    """Always-bidding campaigns (no geo / no audience targeting).

    Campaigns whose targeting omits country, geohash AND audience must
    still bid for every user. They live in ``ACTIVE_UNTARGETED_KEY``,
    UNIONed onto whichever partition slice we computed.
    """
    members = await r.smembers(ACTIVE_UNTARGETED_KEY)
    return set(members) if members else set()


# ── Auction Core ─────────────────────────────────────────────────────────


@router.post("/run", response_model=AuctionResponse)
async def run_auction(
    body: AuctionRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> AuctionResponse:
    """Run a single quality-adjusted Vickrey auction."""
    # Build a thin user-context dict from the request for partitioned
    # candidate selection. We intentionally do NOT push objective_filter
    # into the partition step when it's None — the post-filter loop
    # already enforces the objective match.
    user_ctx: dict[str, Any] = {
        "country": body.geo.country if body.geo else None,
        "lat": body.geo.lat if body.geo else None,
        "lng": body.geo.lng if body.geo else None,
    }
    active_ids = await find_candidates_for_user(
        user_ctx, r, objective_filter=body.objective_filter
    )
    if not active_ids:
        return AuctionResponse(no_eligible_campaigns=True)

    user_profile = await _load_user_profile(r, body.user_id)
    geo_ctx: dict[str, Any] = body.geo.model_dump() if body.geo else {}

    eligible: list[dict[str, str]] = []
    for cid in active_ids:
        c = await r.hgetall(_ck(cid))
        if not c:
            await r.srem(ACTIVE_CAMPAIGNS_KEY, cid)
            continue
        if c.get("status") not in AUCTION_ELIGIBLE_STATUSES:
            continue
        if body.objective_filter and c.get("objective") != body.objective_filter:
            continue

        targeting = _safe_json_loads(c.get("targeting"), {})
        sched = _safe_json_loads(c.get("schedule"), {})

        if not _schedule_match(sched):
            continue
        if not _geo_match(targeting, geo_ctx):
            continue
        # User-side filters (demo/interests/exclude). Only if we have a profile.
        if user_profile and not _user_matches_targeting(user_profile, targeting):
            continue
        # Exclude users without a profile must still respect explicit excludes.
        if not user_profile and body.user_id:
            if body.user_id in (targeting.get("exclude_users") or []):
                continue

        # Audience gate: enforce campaign include/exclude audience linkage.
        if body.user_id and not await campaign_audience_matches(
            cid, body.user_id, r
        ):
            continue

        # Existing-customer exclusion (TikTok/Google/Facebook parity).
        # Default is "new_users_only" — acquisition campaigns skip users
        # who are already customers of the advertising brand. Merchants
        # opt into ``retargeting_only`` or ``all`` to override.
        target_audience = (
            c.get("target_audience") or "new_users_only"
        ).lower()
        cand_brand_id = c.get("brand_id", "")
        if body.user_id and cand_brand_id:
            if target_audience in ("new_users_only", "", None):
                if await _is_existing_customer(
                    r, body.user_id, cand_brand_id
                ):
                    await _record_existing_customer_skip(r, cand_brand_id)
                    continue
            elif target_audience == "retargeting_only":
                if not await _is_existing_customer(
                    r, body.user_id, cand_brand_id
                ):
                    continue
            # target_audience == "all" → no existing-customer filter.

        if not await _has_budget(r, c):
            continue

        eligible.append(c)

    if not eligible:
        return AuctionResponse(no_eligible_campaigns=True, eligible_count=0)

    # ── Rank (with smart-bid + pacing) ──────────────────────────────────
    current_hour = (
        body.context.time_of_day
        if body.context.time_of_day is not None
        else time.localtime(_now()).tm_hour
    )

    # rank = effective_bid × quality_score × pacing_factor
    # Optional ML path (feature flag KIX_ML_ENABLED): predict QS + bid
    # via LightGBM, falling back to the heuristic on any failure. The
    # import is lazy so the API stays bootable without the ml extras.
    try:
        from app.ml import is_enabled as _ml_enabled
        from app.ml.inference import (
            predict_quality_score as _ml_qs,
            predict_smart_bid as _ml_bid,
        )
        _use_ml = _ml_enabled()
    except Exception:  # noqa: BLE001 — ML module missing is non-fatal.
        _use_ml = False

    user_ctx = body.context.model_dump() if hasattr(body.context, "model_dump") else {}

    ranked: list[tuple[float, int, float, float, dict[str, str]]] = []
    for c in eligible:
        cid = c.get("campaign_id", "")
        stats = await r.hgetall(_sk(cid)) if cid else {}

        if _use_ml:
            try:
                qs = await _ml_qs(c, user=None, context=user_ctx, r=r)
            except Exception:  # noqa: BLE001
                qs = float(c.get("quality_score", 0.5) or 0.5)
        else:
            try:
                qs = float(c.get("quality_score", 0.5))
            except (ValueError, TypeError):
                continue
        if qs <= 0:
            continue

        # Smart bid (or manual max_bid_cents).
        if _use_ml:
            try:
                bid = await _ml_bid(c, user=None, context=user_ctx, stats=stats, r=r)
            except Exception:  # noqa: BLE001
                bid = _compute_auto_bid(c, stats)
        else:
            bid = _compute_auto_bid(c, stats)
        if bid <= 0:
            continue

        # PI pacing factor — outside-window or schedule-mismatch returns 0.
        # Replaces legacy hourly-bucket pacing; recomputes at most every 60s
        # per campaign via app.pacing_controller.
        pacing = await _pi_pacing_factor(r, c, current_hour)
        if pacing <= 0:
            continue

        boost = _learning_boost(c)
        rank = bid * qs * pacing * boost

        # Wave C: TriSoul affinity multiplier (additive ±10%, flag-gated).
        # Cannot invert ranking by itself (max swing 0.9× ↔ 1.1×) but adds
        # personalisation when the per-user flag is set.
        cand_brand_id = c.get("brand_id", "")
        if cand_brand_id:
            rank, _trisoul_meta = await _trisoul_boost_auction(
                body.user_id, cand_brand_id, rank, r,
            )

        ranked.append((rank, bid, qs, pacing, c))

    if not ranked:
        return AuctionResponse(no_eligible_campaigns=True, eligible_count=0)

    ranked.sort(key=lambda x: -x[0])

    # ── Diversity bookkeeping + floor injection ─────────────────────────
    entered_brands = {row[4].get("brand_id", "") for row in ranked if row[4].get("brand_id")}
    await _record_brand_auction_entered(r, entered_brands)

    total_auctions = await _trailing_total_auctions(r)
    if total_auctions >= DIVERSITY_WINDOW and DIVERSITY_FLOOR_PCT > 0:
        floor_count = max(1, int(DIVERSITY_WINDOW * DIVERSITY_FLOOR_PCT / 100.0))
        starved: set[str] = set()
        for bid_brand in entered_brands:
            won = await _brand_won_count(r, bid_brand)
            if won < floor_count:
                starved.add(bid_brand)
        if starved:
            ranked = _apply_diversity_floor(ranked, starved)

    # ── Reserve price gate (per-slot floor) ─────────────────────────────
    reserve_cents = await _get_reserve_cents(r, body.slot)

    # ── Pick a winner, respecting freq-cap (skip-and-fallback) ──────────
    winner: dict[str, str] | None = None
    winner_bid = 0
    winner_qs = 0.0
    winner_pacing = 0.0
    actual_charge = 0
    skipped_for_freq_cap = 0

    for idx, (rank, bid, qs, pacing, cand) in enumerate(ranked):
        # Compute GSP second-price for this candidate at this rank position.
        if idx + 1 < len(ranked):
            runner_rank = ranked[idx + 1][0]
            cand_charge = min(int(runner_rank / qs) + 1, bid)
        else:
            cand_charge = max(1, bid // 2)
        cand_charge = max(1, cand_charge)

        # Reserve price gate.
        if reserve_cents > 0 and cand_charge < reserve_cents:
            # All remaining candidates have lower rank → also below reserve in
            # practice. Bail out fast.
            return AuctionResponse(
                no_eligible_campaigns=True,
                eligible_count=len(ranked),
                reason="below_reserve",
                reserve_threshold_cents=reserve_cents,
            )

        # Frequency cap (best-effort, only when we have a freq_cap module).
        cand_brand = cand.get("brand_id", "")
        if cand_brand:
            try:
                allow, _details = await _freq_check_internal(
                    body.user_id,
                    cand_brand,
                    body.slot,
                    r,
                    device_fingerprint=body.device_fingerprint,
                )
            except Exception as exc:  # pragma: no cover — never fail auction
                logger.warning("freq_cap check raised: %s", exc)
                allow = True
            if not allow:
                skipped_for_freq_cap += 1
                continue

        winner = cand
        winner_bid = bid
        winner_qs = qs
        winner_pacing = pacing
        actual_charge = cand_charge
        break

    if winner is None:
        return AuctionResponse(
            no_eligible_campaigns=True,
            eligible_count=len(ranked),
            reason="all_capped",
        )

    winner_cid = winner.get("campaign_id", "")
    winner_bid_strategy = winner.get("bid_strategy", "cpm")
    _ = winner_pacing  # consumed by /admin/explain via separate path

    # ── Resolve attribution window (campaign override → default) ────────
    # Stored on the impression token so report_conversion enforces the
    # campaign-specific window, not a global default.
    try:
        attr_days = int(winner.get("attribution_window_days", 0))
    except (TypeError, ValueError):
        attr_days = 0
    attribution_window_seconds = (
        attr_days * 86400 if attr_days > 0
        else DEFAULT_ATTRIBUTION_WINDOW_SECONDS
    )

    # CPS percent-of-order bps stored on token so conversion-time charge
    # is independent of whether the campaign hash is mutated later.
    try:
        bid_percent_bps = int(winner.get("bid_percent_bps", 0))
    except (TypeError, ValueError):
        bid_percent_bps = 0

    # ── Impression token ────────────────────────────────────────────────
    # Persist dimensional context with the token so /report-* endpoints
    # can fan out multi-dim counters without re-asking the caller.
    impression_token = uuid4().hex
    winner_brand_id = winner.get("brand_id", "")
    geo_country = (geo_ctx or {}).get("country") or ""
    geo_city = (geo_ctx or {}).get("city") or ""
    ctx_device = body.context.device or ""
    ctx_language = body.context.language or ""
    ctx_dow = body.context.day_of_week
    if ctx_dow is None:
        ctx_dow = time.localtime(_now()).tm_wday
    ctx_hour = current_hour
    creative_dict = _safe_json_loads(winner.get("creative"), {})
    ad_id = str(creative_dict.get("ad_id") or creative_dict.get("id") or "")
    winner_targeting = _safe_json_loads(winner.get("targeting"), {})
    aud_list = (
        winner_targeting.get("audience_ids")
        if isinstance(winner_targeting, dict)
        else None
    )
    audience_id = ""
    if isinstance(aud_list, list) and aud_list:
        audience_id = str(aud_list[0])

    await r.hset(
        _imp_key(impression_token),
        mapping={
            "campaign_id": winner_cid,
            "brand_id": winner_brand_id,
            "actual_charge": str(actual_charge),
            "winning_bid": str(winner_bid),
            "bid_strategy": winner_bid_strategy,
            "user_id": body.user_id or "",
            "device_fingerprint": body.device_fingerprint,
            "slot": body.slot,
            "source_brand": body.context.current_brand or "",
            "attribution_window_seconds": str(attribution_window_seconds),
            "bid_percent_bps": str(bid_percent_bps),
            "max_bid_cents": str(winner.get("max_bid_cents", "0")),
            "created_at": str(_now()),
            "settled": "0",
            # Reporting dimensions.
            "dim_country": geo_country,
            "dim_city": geo_city,
            "dim_device": ctx_device,
            "dim_language": ctx_language,
            "dim_hour": str(ctx_hour),
            "dim_dow": str(ctx_dow),
            "dim_ad_id": ad_id,
            "dim_audience_id": audience_id,
            "dim_quality_score": str(winner_qs),
        },
    )
    await r.expire(_imp_key(impression_token), IMPRESSION_TTL)

    # ── Bookkeeping: impression count + QS update ───────────────────────
    await r.hincrby(_sk(winner_cid), "impressions", 1)
    await adjust_quality_score(r, winner_cid, impression=True)
    await _record_brand_auction_won(r, winner_brand_id)

    # ── Touchpoint log for cross-brand multi-touch attribution ──────────
    # P2 fix: same conversion needs to credit every brand the user touched
    # in the lookback window. Best-effort -- never blocks the auction.
    if body.user_id:
        try:
            from app.routers.attribution import log_touchpoint as _log_tp
            await _log_tp(
                r,
                user_id=body.user_id,
                touchpoint_id=impression_token,
                timestamp=_now(),
                campaign_id=winner_cid,
                brand_id=winner_brand_id,
            )
        except Exception as _tp_exc:  # pragma: no cover
            logger.debug("touchpoint log failed: %s", _tp_exc)

    # Death-spiral tracking (P1 fix): record every eligible candidate's
    # participation so the low-perf sweep can compute win_rate accurately.
    try:
        from app.routers.campaigns import record_auction_participation
        for _rk, _b, _q, _p, _cand in ranked:
            cand_cid = _cand.get("campaign_id", "")
            if not cand_cid:
                continue
            await record_auction_participation(
                r, cand_cid, won=(cand_cid == winner_cid)
            )
    except Exception:  # noqa: BLE001 — never break auction on bookkeeping
        logger.exception("record_auction_participation failed")

    # Multi-dim reporting fan-out (best-effort, never breaks auction).
    try:
        from app.routers.reporting import record_impression as _rep_impr
        await _rep_impr(
            r,
            brand_id=winner_brand_id,
            campaign_id=winner_cid,
            ad_id=ad_id or None,
            placement=body.slot,
            country=geo_country or None,
            city=geo_city or None,
            device=ctx_device or None,
            os_name=None,
            language=ctx_language or None,
            audience_id=audience_id or None,
            source_brand=body.context.current_brand or None,
            user_id=body.user_id or None,
            device_fingerprint=body.device_fingerprint,
            quality_score=winner_qs,
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("reporting fan-out (impression) failed: %s", exc)

    # ── Charge now if CPM ───────────────────────────────────────────────
    if winner_bid_strategy in CHARGE_ON_IMPRESSION:
        await _settle_charge(
            r,
            impression_token,
            winner.get("brand_id", ""),
            winner_cid,
            actual_charge,
            reason="cpm_impression",
        )

    creative = _safe_json_loads(winner.get("creative"), {})

    # Outbound webhook fan-out: notify the winning brand that they took
    # this slot. Best-effort — never block the auction critical path.
    try:
        from app.routers.webhooks_outbound import fan_out_webhook_to_brand
        await fan_out_webhook_to_brand(
            winner_brand_id,
            "auction.won",
            {
                "campaign_id": winner_cid,
                "winning_bid_cents": winner_bid,
                "actual_charge_cents": actual_charge,
                "slot": body.slot,
                "impression_token": impression_token,
                "bid_strategy": winner_bid_strategy,
                "quality_score": winner_qs,
            },
            r,
        )
    except Exception as _exc:  # pragma: no cover
        logger.debug("webhook fan-out (auction.won) failed: %s", _exc)

    return AuctionResponse(
        winner_campaign_id=winner_cid,
        winner_brand_id=winner.get("brand_id", ""),
        winning_bid_cents=winner_bid,
        actual_charge_cents=actual_charge,
        creative=creative,
        impression_token=impression_token,
        no_eligible_campaigns=False,
        eligible_count=len(ranked),
    )


# ── Charge settlement ────────────────────────────────────────────────────


async def _settle_charge(
    r: aioredis.Redis,
    impression_token: str,
    brand_id: str,
    campaign_id: str,
    cents: int,
    *,
    reason: str,
) -> dict[str, Any]:
    """Idempotent charge: set ``settled=1`` first, then apply spend."""
    imp_key = _imp_key(impression_token)
    # Atomic SETNX-style: only the first caller settles.
    set_ok = await r.hsetnx(imp_key, "settled_lock", "1")
    if not set_ok:
        # Already in-flight or done.
        already = await r.hget(imp_key, "settled")
        return {"ok": True, "already_settled": already == "1"}

    try:
        await _wallet_charge(
            r,
            brand_id=brand_id,
            cents=cents,
            reason=reason,
            reference_id=impression_token,
            campaign_id=campaign_id,
        )
        daily, total, new_status = await record_spend(r, campaign_id, cents)
        # PI controller: feed the charge into the per-campaign sliding
        # window so the next factor recompute (≤60s away) reflects it.
        # Best-effort — never block settlement on a pacing telemetry hiccup.
        try:
            await _pi_record_spend(r, campaign_id, cents)
        except Exception as exc:  # pragma: no cover
            logger.debug("pacing PI record_spend failed: %s", exc)
        await r.hset(
            imp_key,
            mapping={
                "settled": "1",
                "settled_at": str(_now()),
                "settled_reason": reason,
            },
        )

        # Multi-dim reporting fan-out for spend.
        try:
            from app.routers.reporting import record_spend as _rep_spend
            # Re-read impression for dims (the lock-only update means
            # dims set at /run are intact).
            imp_for_dims = await r.hgetall(imp_key)
            await _rep_spend(
                r,
                brand_id=brand_id,
                campaign_id=campaign_id or None,
                placement=imp_for_dims.get("slot") or None,
                country=imp_for_dims.get("dim_country") or None,
                device=imp_for_dims.get("dim_device") or None,
                spend_cents=int(cents),
            )
        except Exception as exc:  # pragma: no cover
            logger.debug("reporting fan-out (spend) failed: %s", exc)

        return {
            "ok": True,
            "charged_cents": cents,
            "daily_spent": daily,
            "total_spent": total,
            "campaign_status": new_status,
        }
    except Exception:
        # Release the lock so retries can settle.
        await r.hdel(imp_key, "settled_lock")
        raise


# ── Event Reporting ──────────────────────────────────────────────────────


@router.post("/report-impression")
async def report_impression(
    body: ImpressionReport,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Force a CPM-style charge for the impression token.

    For CPM the auction has already charged at /run; this endpoint is
    idempotent. For other strategies it's a no-op confirming the
    impression was actually rendered (recorded in stats).
    """
    imp = await r.hgetall(_imp_key(body.impression_token))
    if not imp:
        raise HTTPException(status_code=404, detail="impression token unknown / expired")

    strategy = imp.get("bid_strategy", "cpm")
    if strategy in CHARGE_ON_IMPRESSION:
        result = await _settle_charge(
            r,
            body.impression_token,
            imp["brand_id"],
            imp["campaign_id"],
            int(imp.get("actual_charge", 0)),
            reason="cpm_impression",
        )
        return result

    # Non-CPM: just acknowledge.
    await r.hset(_imp_key(body.impression_token), mapping={"rendered_at": str(_now())})
    return {"ok": True, "no_charge": True, "bid_strategy": strategy}


@router.post("/report-click")
async def report_click(
    body: ClickReport,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    imp = await r.hgetall(_imp_key(body.impression_token))
    if not imp:
        raise HTTPException(status_code=404, detail="impression token unknown / expired")

    # Idempotent: store click flag.
    click_already = await r.exists(CLICK_KEY.format(token=body.impression_token))
    if not click_already:
        await r.set(
            CLICK_KEY.format(token=body.impression_token),
            json.dumps({
                "user_id": body.user_id,
                "device_fingerprint": body.device_fingerprint,
                "at": _now(),
            }),
            ex=IMPRESSION_TTL,
        )
        cid = imp.get("campaign_id", "")
        await r.hincrby(_sk(cid), "clicks", 1)
        await adjust_quality_score(r, cid, click=True)

        # Multi-dim reporting fan-out.
        try:
            from app.routers.reporting import record_click as _rep_click
            await _rep_click(
                r,
                brand_id=imp.get("brand_id", ""),
                campaign_id=cid or None,
                ad_id=imp.get("dim_ad_id") or None,
                placement=imp.get("slot") or None,
                country=imp.get("dim_country") or None,
                device=imp.get("dim_device") or None,
                os_name=None,
                language=imp.get("dim_language") or None,
                source_brand=imp.get("source_brand") or None,
                user_id=body.user_id,
                device_fingerprint=body.device_fingerprint,
            )
        except Exception as exc:  # pragma: no cover
            logger.debug("reporting fan-out (click) failed: %s", exc)

    strategy = imp.get("bid_strategy", "cpm")
    if strategy in CHARGE_ON_CLICK:
        # Anti-fraud gate.
        fscore = await _fraud_score(
            r,
            user_id=body.user_id,
            device_fingerprint=body.device_fingerprint,
            source_brand=imp.get("source_brand") or None,
            target_brand=imp.get("brand_id"),
        )
        if fscore > FRAUD_REJECT_THRESHOLD:
            logger.warning(
                "click rejected by anti-fraud token=%s score=%d",
                body.impression_token, fscore,
            )
            return {"ok": True, "rejected": "fraud", "fraud_score": fscore}

        result = await _settle_charge(
            r,
            body.impression_token,
            imp["brand_id"],
            imp["campaign_id"],
            int(imp.get("actual_charge", 0)),
            reason=f"{strategy}_click",
        )
        return result

    return {"ok": True, "click_recorded": True, "no_charge": True}


@router.post("/report-conversion")
async def report_conversion(
    body: ConversionReport,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    imp = await r.hgetall(_imp_key(body.impression_token))
    if not imp:
        raise HTTPException(status_code=404, detail="impression token unknown / expired")

    # ── Enforce attribution window stored on the impression token ───────
    # When unset (legacy tokens), fall back to the system default.
    try:
        window_seconds = int(imp.get("attribution_window_seconds", "0") or "0")
    except (TypeError, ValueError):
        window_seconds = 0
    if window_seconds <= 0:
        window_seconds = DEFAULT_ATTRIBUTION_WINDOW_SECONDS
    try:
        created_at = float(imp.get("created_at", "0") or "0")
    except (TypeError, ValueError):
        created_at = 0.0
    if created_at > 0 and (_now() - created_at) > window_seconds:
        return {
            "ok": False,
            "rejected": "outside_attribution_window",
            "window_seconds": window_seconds,
            "age_seconds": int(_now() - created_at),
        }

    # Idempotent on conversion key.
    conv_key = CONVERSION_KEY.format(token=body.impression_token)
    already = await r.exists(conv_key)
    if already:
        return {"ok": True, "already_converted": True}

    await r.set(
        conv_key,
        json.dumps({
            "user_id": body.user_id,
            "value_cents": body.conversion_value_cents,
            "at": _now(),
        }),
        ex=IMPRESSION_TTL,
    )
    cid = imp.get("campaign_id", "")
    pipe = r.pipeline()
    pipe.hincrby(_sk(cid), "conversions", 1)
    pipe.hincrby(_sk(cid), "revenue_cents", body.conversion_value_cents)
    await pipe.execute()
    await adjust_quality_score(r, cid, conversion=True)

    # Multi-dim reporting fan-out (view-through if no click recorded).
    try:
        from app.routers.reporting import record_conversion as _rep_conv
        click_seen = await r.exists(CLICK_KEY.format(token=body.impression_token))
        await _rep_conv(
            r,
            brand_id=imp.get("brand_id", ""),
            campaign_id=cid or None,
            ad_id=imp.get("dim_ad_id") or None,
            placement=imp.get("slot") or None,
            country=imp.get("dim_country") or None,
            device=imp.get("dim_device") or None,
            source_brand=imp.get("source_brand") or None,
            user_id=body.user_id,
            value_cents=body.conversion_value_cents,
            view_through=not bool(click_seen),
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("reporting fan-out (conversion) failed: %s", exc)

    # Anti-fraud check before charging.
    fscore = await _fraud_score(
        r,
        user_id=body.user_id,
        device_fingerprint=None,
        source_brand=imp.get("source_brand") or None,
        target_brand=imp.get("brand_id"),
    )
    if fscore > FRAUD_REJECT_THRESHOLD:
        logger.warning(
            "conversion rejected by anti-fraud token=%s score=%d",
            body.impression_token, fscore,
        )
        return {"ok": True, "rejected": "fraud", "fraud_score": fscore}

    strategy = imp.get("bid_strategy", "cpm")
    if strategy in CHARGE_ON_CONVERSION:
        # CPA: flat actual_charge.
        # CPS: percent-of-order when bid_percent_bps > 0 (GMV revenue share);
        # else legacy fixed cents fallback. The percent path is capped by
        # max_bid_cents to protect the merchant from runaway orders.
        base_charge = int(imp.get("actual_charge", 0))
        if strategy == "cps":
            try:
                cps_bps = int(imp.get("bid_percent_bps", "0") or "0")
            except (TypeError, ValueError):
                cps_bps = 0
            if cps_bps > 0:
                # commission = conversion_value × bps / 10000
                commission = int(
                    body.conversion_value_cents * cps_bps / 10000
                )
                # Cap by max_bid_cents (merchant-set ceiling). Falls back
                # to commission itself when max_bid is unset.
                try:
                    max_bid_cap = int(imp.get("max_bid_cents", "0") or "0")
                except (TypeError, ValueError):
                    max_bid_cap = 0
                if max_bid_cap > 0:
                    commission = min(commission, max_bid_cap)
                # Sanity: never charge more than the order itself.
                charge = max(0, min(commission, body.conversion_value_cents))
            else:
                # Legacy: fixed cents per conversion, capped by order value.
                charge = min(base_charge, body.conversion_value_cents)
        else:
            charge = base_charge

        result = await _settle_charge(
            r,
            body.impression_token,
            imp["brand_id"],
            imp["campaign_id"],
            charge,
            reason=f"{strategy}_conversion",
        )
        result["conversion_value_cents"] = body.conversion_value_cents

        # Outbound webhook fan-out (best effort — never block on subscribers).
        try:
            from app.routers.webhooks_outbound import fan_out_webhook_to_brand
            await fan_out_webhook_to_brand(
                imp.get("brand_id", ""),
                "conversion.attributed",
                {
                    "campaign_id": cid,
                    "impression_token": body.impression_token,
                    "user_id": body.user_id,
                    "conversion_value_cents": body.conversion_value_cents,
                    "charged_cents": charge,
                    "bid_strategy": strategy,
                },
                r,
            )
        except Exception as _exc:  # pragma: no cover
            logger.debug("webhook fan-out (conversion) failed: %s", _exc)

        return result

    # Conversion recorded but no charge (e.g. CPM/CPC where impression was
    # the chargeable event). Still notify subscribed merchants.
    try:
        from app.routers.webhooks_outbound import fan_out_webhook_to_brand
        await fan_out_webhook_to_brand(
            imp.get("brand_id", ""),
            "conversion.attributed",
            {
                "campaign_id": cid,
                "impression_token": body.impression_token,
                "user_id": body.user_id,
                "conversion_value_cents": body.conversion_value_cents,
                "charged_cents": 0,
                "bid_strategy": strategy,
            },
            r,
        )
    except Exception as _exc:  # pragma: no cover
        logger.debug("webhook fan-out (conversion no-charge) failed: %s", _exc)

    return {
        "ok": True,
        "conversion_recorded": True,
        "no_charge": True,
        "conversion_value_cents": body.conversion_value_cents,
    }


# ── CPE: Cost-Per-Engagement reporting ───────────────────────────────────


@router.post("/report-engagement")
async def report_engagement(
    body: EngagementReport,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Settle a CPE charge when a meaningful engagement event fires.

    Idempotent per (impression_token, engagement_type): the same token may
    fire multiple engagement_types (e.g. ``game_play_30s`` then
    ``voucher_claim``) and each charges once. Non-CPE strategies record the
    event but do not charge.
    """
    imp = await r.hgetall(_imp_key(body.impression_token))
    if not imp:
        raise HTTPException(
            status_code=404, detail="impression token unknown / expired"
        )

    if body.engagement_type not in ENGAGEMENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"engagement_type must be one of {sorted(ENGAGEMENT_TYPES)}",
        )

    eng_key = ENGAGEMENT_KEY.format(
        token=body.impression_token, etype=body.engagement_type
    )
    already = await r.exists(eng_key)
    if already:
        return {
            "ok": True,
            "already_reported": True,
            "engagement_type": body.engagement_type,
        }

    await r.set(
        eng_key,
        json.dumps({
            "user_id": body.user_id,
            "value_seconds": body.value_seconds,
            "device_fingerprint": body.device_fingerprint,
            "at": _now(),
        }),
        ex=IMPRESSION_TTL,
    )

    # Bump engagement counter on campaign stats (separate from clicks /
    # conversions — engagements are their own funnel step).
    cid = imp.get("campaign_id", "")
    if cid:
        await r.hincrby(_sk(cid), "engagements", 1)

    # Multi-dim reporting fan-out.
    try:
        from app.routers.reporting import record_engagement as _rep_eng
        # Map value_seconds → completion ratio (best-effort: 30s = 1.0).
        completion = None
        if body.value_seconds is not None:
            try:
                completion = min(1.0, max(0.0, body.value_seconds / 30.0))
            except Exception:
                completion = None
        await _rep_eng(
            r,
            brand_id=imp.get("brand_id", ""),
            campaign_id=cid or None,
            placement=imp.get("slot") or None,
            country=imp.get("dim_country") or None,
            device=imp.get("dim_device") or None,
            user_id=body.user_id,
            completion=completion,
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("reporting fan-out (engagement) failed: %s", exc)

    strategy = imp.get("bid_strategy", "cpm")
    if strategy in CHARGE_ON_ENGAGEMENT:
        # Anti-fraud gate (reuse conversion-style check; engagement events
        # have similar fraud surface to clicks/conversions).
        fscore = await _fraud_score(
            r,
            user_id=body.user_id,
            device_fingerprint=body.device_fingerprint,
            source_brand=imp.get("source_brand") or None,
            target_brand=imp.get("brand_id"),
        )
        if fscore > FRAUD_REJECT_THRESHOLD:
            logger.warning(
                "engagement rejected by anti-fraud token=%s type=%s score=%d",
                body.impression_token, body.engagement_type, fscore,
            )
            return {
                "ok": True,
                "rejected": "fraud",
                "fraud_score": fscore,
                "engagement_type": body.engagement_type,
            }

        result = await _settle_charge(
            r,
            body.impression_token,
            imp["brand_id"],
            imp["campaign_id"],
            int(imp.get("actual_charge", 0)),
            reason="cpe_engagement",
        )
        result["engagement_type"] = body.engagement_type
        return result

    return {
        "ok": True,
        "engagement_recorded": True,
        "no_charge": True,
        "engagement_type": body.engagement_type,
        "bid_strategy": strategy,
    }


# ── Diagnostics ──────────────────────────────────────────────────────────


@router.get("/impression/{impression_token}")
async def impression_detail(
    impression_token: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Inspect a single impression token (used by ops + tests)."""
    imp = await r.hgetall(_imp_key(impression_token))
    if not imp:
        raise HTTPException(status_code=404, detail="impression token unknown / expired")
    click = await r.get(CLICK_KEY.format(token=impression_token))
    conv = await r.get(CONVERSION_KEY.format(token=impression_token))
    return {
        "impression": imp,
        "click": _safe_json_loads(click, None),
        "conversion": _safe_json_loads(conv, None),
    }


# ── Admin: reserve price ─────────────────────────────────────────────────


class ReservePriceBody(BaseModel):
    slot: Literal["main", "banner", "interstitial", "push", "geofence"]
    reserve_cents: int = Field(ge=0)


@router.post("/admin/reserve-price")
async def set_reserve_price(
    body: ReservePriceBody,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Set the per-slot reserve price (admin only — wire your auth upstream)."""
    if body.reserve_cents == 0:
        await r.delete(RESERVE_KEY.format(slot=body.slot))
    else:
        await r.set(RESERVE_KEY.format(slot=body.slot), body.reserve_cents)
    return {
        "ok": True,
        "slot": body.slot,
        "reserve_cents": body.reserve_cents,
    }


@router.get("/admin/reserve-price")
async def get_reserve_prices(
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return current reserve floors for every recognized slot."""
    out: dict[str, int] = {}
    for slot in VALID_RESERVE_SLOTS:
        out[slot] = await _get_reserve_cents(r, slot)
    return {"reserves": out}


# ── Admin: pacing inspection ─────────────────────────────────────────────


@router.get("/admin/pacing/{campaign_id}")
async def admin_pacing(
    campaign_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Inspect PI pacing state for a single campaign.

    Surfaces the schedule-window check plus the live PI controller state
    (setpoint / actual / cumulative error / current factor). The factor
    drives both rank multiplication and probabilistic skipping in the
    auction; values below 1.0 mean the controller is braking.
    """
    c = await r.hgetall(_ck(campaign_id))
    if not c:
        raise HTTPException(status_code=404, detail="campaign not found")

    sched = _safe_json_loads(c.get("schedule"), {})
    hours = sched.get("hours_local") or [0, 24]
    try:
        h_start, h_end = int(hours[0]), int(hours[1])
    except (TypeError, ValueError, IndexError):
        h_start, h_end = 0, 24

    current_hour = time.localtime(_now()).tm_hour
    in_window = _pi_in_schedule_window(c.get("schedule"), current_hour)

    try:
        daily_budget = int(c.get("daily_budget_cents", 0))
    except (TypeError, ValueError):
        daily_budget = 0
    daily_spent = await _read_daily_spend(r, campaign_id)

    # Apply PI within-window; outside window the auction skips the
    # campaign entirely so we surface factor=0 + a clear recommendation.
    factor = (
        await _pi_pacing_factor(r, c, current_hour)
        if in_window
        else 0.0
    )
    recommendation = _pacing_recommendation(factor)

    return {
        "campaign_id": campaign_id,
        "current_hour_local": current_hour,
        "window": {"start": h_start, "end": h_end},
        "in_window": in_window,
        "daily_budget_cents": daily_budget,
        "daily_spent_cents": daily_spent,
        "pacing_factor": factor,
        "recommendation": recommendation,
    }


@router.get("/admin/pacing/{campaign_id}/pi-state")
async def admin_pacing_pi_state(
    campaign_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return the full PI controller state for ops debugging.

    Reads (no recompute, no side effects) every Redis key the controller
    writes for this campaign — useful when investigating why a particular
    campaign is being throttled or starved by pacing.
    """
    c = await r.hgetall(_ck(campaign_id))
    if not c:
        raise HTTPException(status_code=404, detail="campaign not found")
    try:
        daily_budget = int(c.get("daily_budget_cents", 0))
    except (TypeError, ValueError):
        daily_budget = 0
    state = await _pi_get_state(r, campaign_id, daily_budget)
    state.update({
        "campaign_id": campaign_id,
        "daily_budget_cents": daily_budget,
    })
    return state


# ── Admin: explain auction (dry-run) ─────────────────────────────────────


class ExplainBody(BaseModel):
    user_id: str | None = None
    device_fingerprint: str
    geo: GeoContext | None = None
    context: AuctionContext = Field(default_factory=AuctionContext)
    objective_filter: Literal[
        "acquire", "sales", "awareness", "geo_visit",
        "engagement", "retention", "activation", "win_back",
    ] | None = None
    slot: Literal["main", "banner", "interstitial", "push", "geofence"] = "main"


@router.post("/admin/explain")
async def admin_explain(
    body: ExplainBody,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Dry-run: walks every active campaign, returns rank + reason per row.

    Does NOT create an impression token or charge anyone. Useful for support
    cases ("why didn't my campaign win at 3pm?").
    """
    active_ids = await r.smembers(ACTIVE_CAMPAIGNS_KEY)
    user_profile = await _load_user_profile(r, body.user_id)
    geo_ctx: dict[str, Any] = body.geo.model_dump() if body.geo else {}
    current_hour = (
        body.context.time_of_day
        if body.context.time_of_day is not None
        else time.localtime(_now()).tm_hour
    )
    reserve_cents = await _get_reserve_cents(r, body.slot)

    rows: list[dict[str, Any]] = []
    eligible: list[tuple[float, int, float, float, dict[str, str]]] = []

    for cid in active_ids:
        c = await r.hgetall(_ck(cid))
        if not c:
            continue
        row: dict[str, Any] = {
            "campaign_id": cid,
            "brand_id": c.get("brand_id", ""),
            "status": c.get("status"),
            "bid_optimization": (
                c.get("bid_optimization") or c.get("optimization") or "manual"
            ),
        }

        if c.get("status") not in AUCTION_ELIGIBLE_STATUSES:
            row["dropped"] = "status_ineligible"
            rows.append(row)
            continue
        if body.objective_filter and c.get("objective") != body.objective_filter:
            row["dropped"] = "objective_mismatch"
            rows.append(row)
            continue

        targeting = _safe_json_loads(c.get("targeting"), {})
        sched = _safe_json_loads(c.get("schedule"), {})

        if not _schedule_match(sched):
            row["dropped"] = "schedule"
            rows.append(row)
            continue
        if not _geo_match(targeting, geo_ctx):
            row["dropped"] = "geo"
            rows.append(row)
            continue
        if user_profile and not _user_matches_targeting(user_profile, targeting):
            row["dropped"] = "targeting"
            rows.append(row)
            continue
        if not user_profile and body.user_id:
            if body.user_id in (targeting.get("exclude_users") or []):
                row["dropped"] = "exclude_user"
                rows.append(row)
                continue
        # Existing-customer exclusion check (mirrors run_auction).
        target_audience = (
            c.get("target_audience") or "new_users_only"
        ).lower()
        cand_brand_id = c.get("brand_id", "")
        row["target_audience"] = target_audience
        existing_check = "skipped_all"
        if body.user_id and cand_brand_id and target_audience != "all":
            is_existing = await _is_existing_customer(
                r, body.user_id, cand_brand_id
            )
            if target_audience in ("new_users_only", "", None):
                if is_existing:
                    row["dropped"] = "existing_customer"
                    row["existing_customer_check"] = "failed_existing"
                    rows.append(row)
                    continue
                existing_check = "passed"
            elif target_audience == "retargeting_only":
                if not is_existing:
                    row["dropped"] = "not_existing_customer"
                    row["existing_customer_check"] = "failed_not_existing"
                    rows.append(row)
                    continue
                existing_check = "passed"
        row["existing_customer_check"] = existing_check

        if not await _has_budget(r, c):
            row["dropped"] = "budget_exhausted"
            rows.append(row)
            continue

        try:
            qs = float(c.get("quality_score", 0.5))
        except (TypeError, ValueError):
            qs = 0.0
        if qs <= 0:
            row["dropped"] = "quality_zero"
            rows.append(row)
            continue

        stats = await r.hgetall(_sk(cid))
        bid = _compute_auto_bid(c, stats)
        if bid <= 0:
            row["dropped"] = "auto_bid_zero"
            rows.append(row)
            continue

        pacing = await _pi_pacing_factor(r, c, current_hour)
        if pacing <= 0:
            row["dropped"] = "outside_schedule_window"
            row["pacing"] = pacing
            rows.append(row)
            continue

        rank = bid * qs * pacing
        row.update({
            "bid_cents": bid,
            "quality_score": qs,
            "pacing": pacing,
            "rank": rank,
        })
        rows.append(row)
        eligible.append((rank, bid, qs, pacing, c))

    eligible.sort(key=lambda x: -x[0])

    # Determine winner (with reserve + freq-cap), mirroring run_auction.
    winner_info: dict[str, Any] | None = None
    for idx, (rank, bid, qs, pacing, cand) in enumerate(eligible):
        if idx + 1 < len(eligible):
            runner_rank = eligible[idx + 1][0]
            cand_charge = min(int(runner_rank / qs) + 1, bid)
        else:
            cand_charge = max(1, bid // 2)
        cand_charge = max(1, cand_charge)

        if reserve_cents > 0 and cand_charge < reserve_cents:
            winner_info = {
                "campaign_id": None,
                "reason": "below_reserve",
                "would_have_been": cand.get("campaign_id"),
                "cand_charge_cents": cand_charge,
                "reserve_cents": reserve_cents,
            }
            break

        cand_brand = cand.get("brand_id", "")
        allow = True
        freq_details: dict[str, Any] = {}
        if cand_brand:
            try:
                allow, freq_details = await _freq_check_internal(
                    body.user_id,
                    cand_brand,
                    body.slot,
                    r,
                    device_fingerprint=body.device_fingerprint,
                )
            except Exception as exc:  # pragma: no cover
                logger.warning("freq_cap check raised: %s", exc)
                allow = True

        if not allow:
            # Mark in rows.
            for row in rows:
                if row.get("campaign_id") == cand.get("campaign_id"):
                    row["dropped"] = "frequency_cap"
                    row["freq_cap"] = freq_details
            continue

        winner_info = {
            "campaign_id": cand.get("campaign_id"),
            "brand_id": cand_brand,
            "actual_charge_cents": cand_charge,
            "winning_bid_cents": bid,
            "quality_score": qs,
            "pacing": pacing,
            "rank": rank,
        }
        break

    return {
        "slot": body.slot,
        "current_hour_local": current_hour,
        "reserve_threshold_cents": reserve_cents,
        "winner": winner_info,
        "all_eligible": [
            {
                "campaign_id": cand.get("campaign_id"),
                "brand_id": cand.get("brand_id", ""),
                "bid": bid,
                "qs": qs,
                "pacing": pacing,
                "rank": rank,
            }
            for (rank, bid, qs, pacing, cand) in eligible
        ],
        "all_candidates": rows,
    }


# ── Admin: existing-customer savings ─────────────────────────────────────


@router.get("/admin/savings/{brand_id}")
async def admin_savings(
    brand_id: str,
    date: str | None = None,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """How many auction impressions were skipped today because the user
    was already a customer of this brand — i.e. money the merchant did
    not waste buying back its own users.

    Estimated savings = ``skipped × average_CPA`` (cents). ``average_CPA``
    is derived from the brand's aggregate spend / conversions; falls back
    to a conservative 50¢ placeholder if no historical data exists.
    """
    day = date or _today_utc()
    key = AUCTION_SKIPPED_EXISTING_KEY.format(brand_id=brand_id, date=day)
    raw = await r.get(key)
    try:
        skipped = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        skipped = 0

    # Estimate average CPA from brand-aggregated stats. The brand stats
    # key shape is `brand:{bid}:stats` — best-effort lookup; missing data
    # falls back to a conservative 50¢ placeholder so the dashboard still
    # shows a non-zero figure.
    avg_cpa_cents = 50
    try:
        brand_stats = await r.hgetall(f"brand:{brand_id}:stats")
        spend = int(brand_stats.get("spend_cents", 0) or 0)
        convs = int(brand_stats.get("conversions", 0) or 0)
        if convs > 0 and spend > 0:
            avg_cpa_cents = max(1, spend // convs)
    except Exception:  # pragma: no cover
        pass

    return {
        "brand_id": brand_id,
        "date": day,
        "existing_customers_skipped": skipped,
        "average_cpa_cents": avg_cpa_cents,
        "estimated_savings_cents": skipped * avg_cpa_cents,
    }


# ── Diversity report (trailing-window per-brand share) ───────────────────


@router.get("/diversity-report/{brand_id}")
async def diversity_report(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Trailing-window share metrics for a brand.

    Surfaces auction-entered / auction-won counts and the share of the
    last ``DIVERSITY_WINDOW`` (=1000) platform auctions, along with the
    current floor threshold. Used by ops to verify diversity-floor
    behaviour and by brand portals to display competitive position.
    """
    entered = await _brand_entered_count(r, brand_id)
    won = await _brand_won_count(r, brand_id)
    total = await _trailing_total_auctions(r)
    trailing = min(total, DIVERSITY_WINDOW)
    floor_count = max(1, int(DIVERSITY_WINDOW * DIVERSITY_FLOOR_PCT / 100.0))
    won_share = (won / trailing) if trailing > 0 else 0.0
    return {
        "brand_id": brand_id,
        "window_size": DIVERSITY_WINDOW,
        "trailing_total_auctions": trailing,
        "entered": entered,
        "won": won,
        "won_share": round(won_share, 4),
        "floor_pct": DIVERSITY_FLOOR_PCT,
        "floor_count": floor_count,
        "below_floor": won < floor_count and trailing >= DIVERSITY_WINDOW,
    }
