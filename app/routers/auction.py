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
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis
from app.routers.audiences import campaign_audience_matches
from app.routers.campaigns import (
    ACTIVE_CAMPAIGNS_KEY,
    AUCTION_ELIGIBLE_STATUSES,
    CAMPAIGN_STATS_KEY,
    _ck,
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
    daily_budget = int(c.get("daily_budget_cents", 0))
    total_budget = int(c.get("total_budget_cents", 0))
    max_bid = int(c.get("max_bid_cents", 0))
    if max_bid <= 0:
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


# ── Pacing (don't burn the daily budget at 9am) ──────────────────────────


def _pacing_factor(
    campaign: dict[str, str],
    current_hour: int,
    daily_spent_cents: int,
) -> float:
    """Return a multiplier in [0, 1] to apply to rank based on burn rate.

    Returns ``0.0`` if the current hour is outside the campaign's local
    schedule window. Otherwise compares actual spend fraction against the
    expected fraction for the elapsed share of the active window:

      * overshooting > 1.2× expected → 0.3 (deprioritize hard)
      * undershooting < 0.5× expected → 1.0 (catch up)
      * else                          → 0.8 (normal-cruise)

    Wrap-around windows (e.g. 22→6) are supported.
    """
    sched = _safe_json_loads(campaign.get("schedule"), {})
    hours = sched.get("hours_local") or [0, 24]
    try:
        h_start, h_end = int(hours[0]), int(hours[1])
    except (TypeError, ValueError, IndexError):
        h_start, h_end = 0, 24

    # In-window check (handles wrap).
    if h_start <= h_end:
        if current_hour < h_start or current_hour >= h_end:
            return 0.0
        total_hours = max(1, h_end - h_start)
        elapsed_hours = current_hour - h_start + 1
    else:
        # window wraps midnight
        if not (current_hour >= h_start or current_hour < h_end):
            return 0.0
        total_hours = max(1, (24 - h_start) + h_end)
        if current_hour >= h_start:
            elapsed_hours = current_hour - h_start + 1
        else:
            elapsed_hours = (24 - h_start) + current_hour + 1

    expected_spend_pct = elapsed_hours / total_hours

    try:
        daily_budget = int(campaign.get("daily_budget_cents", 0))
    except (TypeError, ValueError):
        daily_budget = 0

    if daily_budget <= 0:
        # No budget cap → no pacing pressure.
        return 0.8

    actual_spend_pct = daily_spent_cents / daily_budget if daily_budget else 0

    if actual_spend_pct > expected_spend_pct * 1.2:
        return 0.3
    if actual_spend_pct < expected_spend_pct * 0.5:
        return 1.0
    return 0.8


def _pacing_recommendation(expected_pct: float, actual_pct: float) -> str:
    if actual_pct > expected_pct * 1.2:
        return "overpacing — reduce bids or pause until daily window catches up"
    if actual_pct < expected_pct * 0.5:
        return "underpacing — increase bids or broaden targeting"
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


# ── Auction Core ─────────────────────────────────────────────────────────


@router.post("/run", response_model=AuctionResponse)
async def run_auction(
    body: AuctionRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> AuctionResponse:
    """Run a single quality-adjusted Vickrey auction."""
    active_ids = await r.smembers(ACTIVE_CAMPAIGNS_KEY)
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
    ranked: list[tuple[float, int, float, float, dict[str, str]]] = []
    for c in eligible:
        cid = c.get("campaign_id", "")
        try:
            qs = float(c.get("quality_score", 0.5))
        except (ValueError, TypeError):
            continue
        if qs <= 0:
            continue

        # Smart bid (or manual max_bid_cents).
        stats = await r.hgetall(_sk(cid)) if cid else {}
        bid = _compute_auto_bid(c, stats)
        if bid <= 0:
            continue

        # Pacing factor — outside-window or schedule-mismatch returns 0.
        daily_spent = await _read_daily_spend(r, cid)
        pacing = _pacing_factor(c, current_hour, daily_spent)
        if pacing <= 0:
            continue

        rank = bid * qs * pacing
        ranked.append((rank, bid, qs, pacing, c))

    if not ranked:
        return AuctionResponse(no_eligible_campaigns=True, eligible_count=0)

    ranked.sort(key=lambda x: -x[0])

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
    impression_token = uuid4().hex
    await r.hset(
        _imp_key(impression_token),
        mapping={
            "campaign_id": winner_cid,
            "brand_id": winner.get("brand_id", ""),
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
        },
    )
    await r.expire(_imp_key(impression_token), IMPRESSION_TTL)

    # ── Bookkeeping: impression count + QS update ───────────────────────
    await r.hincrby(_sk(winner_cid), "impressions", 1)
    await adjust_quality_score(r, winner_cid, impression=True)

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
        await r.hset(
            imp_key,
            mapping={
                "settled": "1",
                "settled_at": str(_now()),
                "settled_reason": reason,
            },
        )
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
        return result

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
    """Inspect pacing for a single campaign: expected vs actual + factor."""
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

    # Same in-window math as _pacing_factor.
    in_window = True
    if h_start <= h_end:
        if current_hour < h_start or current_hour >= h_end:
            in_window = False
            total_hours = max(1, h_end - h_start)
            elapsed_hours = 0
        else:
            total_hours = max(1, h_end - h_start)
            elapsed_hours = current_hour - h_start + 1
    else:
        if not (current_hour >= h_start or current_hour < h_end):
            in_window = False
            total_hours = max(1, (24 - h_start) + h_end)
            elapsed_hours = 0
        else:
            total_hours = max(1, (24 - h_start) + h_end)
            if current_hour >= h_start:
                elapsed_hours = current_hour - h_start + 1
            else:
                elapsed_hours = (24 - h_start) + current_hour + 1

    expected_pct = elapsed_hours / total_hours if total_hours else 0.0

    try:
        daily_budget = int(c.get("daily_budget_cents", 0))
    except (TypeError, ValueError):
        daily_budget = 0
    daily_spent = await _read_daily_spend(r, campaign_id)
    actual_pct = (daily_spent / daily_budget) if daily_budget > 0 else 0.0

    factor = _pacing_factor(c, current_hour, daily_spent)
    recommendation = (
        "outside_schedule_window"
        if not in_window
        else _pacing_recommendation(expected_pct, actual_pct)
    )

    return {
        "campaign_id": campaign_id,
        "current_hour_local": current_hour,
        "window": {"start": h_start, "end": h_end},
        "in_window": in_window,
        "expected_pct": round(expected_pct, 4),
        "actual_pct": round(actual_pct, 4),
        "daily_budget_cents": daily_budget,
        "daily_spent_cents": daily_spent,
        "pacing_factor": factor,
        "recommendation": recommendation,
    }


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

        daily_spent = await _read_daily_spend(r, cid)
        pacing = _pacing_factor(c, current_hour, daily_spent)
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
