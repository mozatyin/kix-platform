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

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ────────────────────────────────────────────────────────────

IMPRESSION_KEY = "impression:{token}"
IMPRESSION_TTL = 86400 * 7  # 7 days

CLICK_KEY = "impression:{token}:click"
CONVERSION_KEY = "impression:{token}:conversion"

# Charge timing per bid_strategy.
CHARGE_ON_IMPRESSION = {"cpm"}
CHARGE_ON_CLICK = {"cpc", "cpv"}
CHARGE_ON_CONVERSION = {"cpa", "cps"}

# Anti-fraud reject threshold.
FRAUD_REJECT_THRESHOLD = 70


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
    objective_filter: Literal["acquire", "sales", "awareness", "geo_visit"] | None = None
    slot: Literal["main", "banner", "interstitial"] = "main"


class AuctionResponse(BaseModel):
    winner_campaign_id: str | None = None
    winner_brand_id: str | None = None
    winning_bid_cents: int = 0
    actual_charge_cents: int = 0
    creative: dict[str, Any] = Field(default_factory=dict)
    impression_token: str | None = None
    no_eligible_campaigns: bool = False
    eligible_count: int = 0


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

        if not await _has_budget(r, c):
            continue

        eligible.append(c)

    if not eligible:
        return AuctionResponse(no_eligible_campaigns=True, eligible_count=0)

    # ── Rank ────────────────────────────────────────────────────────────
    ranked: list[tuple[float, int, float, dict[str, str]]] = []
    for c in eligible:
        try:
            bid = int(c.get("max_bid_cents", 0))
            qs = float(c.get("quality_score", 0.5))
        except (ValueError, TypeError):
            continue
        if qs <= 0 or bid <= 0:
            continue
        rank = bid * qs
        ranked.append((rank, bid, qs, c))

    if not ranked:
        return AuctionResponse(no_eligible_campaigns=True, eligible_count=0)

    ranked.sort(key=lambda x: -x[0])
    winner_rank, winner_bid, winner_qs, winner = ranked[0]

    # ── Second-price (GSP) ──────────────────────────────────────────────
    if len(ranked) > 1:
        runner_rank = ranked[1][0]
        # Charge = ceil(runner_rank / winner_qs) + 1¢, capped by winner_bid.
        actual_charge = min(int(runner_rank / winner_qs) + 1, winner_bid)
    else:
        # Lone bidder pays a floor (50% of own bid) — keep merchants honest.
        actual_charge = max(1, winner_bid // 2)
    actual_charge = max(1, actual_charge)

    winner_cid = winner.get("campaign_id", "")
    winner_bid_strategy = winner.get("bid_strategy", "cpm")

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
        # CPA: flat actual_charge. CPS: charge as % of conversion value
        # (default: actual_charge but capped by conversion_value).
        base_charge = int(imp.get("actual_charge", 0))
        if strategy == "cps":
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
