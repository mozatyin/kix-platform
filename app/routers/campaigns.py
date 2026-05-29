"""Campaign Manager — merchants create ad campaigns (Google-Ads style).

A *campaign* bundles:

  * **objective** — what the merchant pays for (acquire, sales, awareness,
    geo_visit).
  * **bid_strategy** — pricing model (cpa / cps / cpm / cpv).
  * **targeting** — geo + demographics + interests + lookalike + excludes.
  * **creative** — what gets shown to the user (recipe / game / voucher).
  * **schedule** — calendar window + per-day hour window + DOW mask.
  * **quality_score** — CTR/CVR-adjusted multiplier (0..1) used by auction
    rank.

The router exposes CRUD + stats + audience-preview. Campaign **status** is
re-derived on every read (cheap; no cron required):

    active  ←→  paused
       │
       ├─→ daily_budget_exhausted  (auto-resume next day)
       ├─→ total_budget_exhausted  (terminal)
       ├─→ scheduled               (start_at not yet reached)
       ├─→ ended                   (end_at passed)
       └─→ disapproved             (manual review reject)

Redis Schema
------------
  campaign:{cid}                            HASH  — full state (JSON fields)
  brand:{bid}:campaigns                     SET   — campaigns owned by brand
  campaigns:active                          SET   — fast lookup for auction
  campaign:{cid}:stats                      HASH  — impressions/clicks/...
  campaign:{cid}:budget_spent_today:{date}  STR   — INT, EX 86400
  campaign:{cid}:budget_spent_total         STR   — INT, persistent

All campaign-side mutations are best-effort idempotent: the auction
router writes spend via the same helpers so daily/total caps stay
consistent across processes.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ────────────────────────────────────────────────────────────

CAMPAIGN_KEY = "campaign:{cid}"
BRAND_CAMPAIGNS_KEY = "brand:{bid}:campaigns"
ACTIVE_CAMPAIGNS_KEY = "campaigns:active"
CAMPAIGN_STATS_KEY = "campaign:{cid}:stats"
CAMPAIGN_DAILY_SPEND_KEY = "campaign:{cid}:budget_spent_today:{date}"
CAMPAIGN_TOTAL_SPEND_KEY = "campaign:{cid}:budget_spent_total"

DAILY_SPEND_TTL = 86400 * 2  # keep yesterday around briefly for reporting

VALID_OBJECTIVES = {
    "acquire", "sales", "awareness", "geo_visit",
    # Engagement / retention lifecycle objectives — subscription &
    # community merchants need these to honestly declare campaign intent
    # (rather than declaring `acquire` for engagement spend).
    "engagement", "retention", "activation", "win_back",
}
VALID_BID_STRATEGIES = {"cpa", "cps", "cpm", "cpv", "cpc", "cpe"}

# Attribution window bounds (campaign-configurable). System default = 7 days
# (604800 s) when not set on the campaign.
ATTRIBUTION_WINDOW_MIN_DAYS = 1
ATTRIBUTION_WINDOW_MAX_DAYS = 90
ATTRIBUTION_WINDOW_DEFAULT_SECONDS = 7 * 86400

# CPS percent-of-order bid bounds (basis points). 1 bps = 0.01%; 5000 bps = 50%.
CPS_BPS_MIN = 1
CPS_BPS_MAX = 5000

# Valid engagement types for CPE charging.
ENGAGEMENT_TYPES = {
    "game_play_30s", "video_complete", "voucher_claim",
    "level_up", "streak_milestone",
}

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_DAILY_EXHAUSTED = "daily_budget_exhausted"
STATUS_TOTAL_EXHAUSTED = "total_budget_exhausted"
STATUS_SCHEDULED = "scheduled"
STATUS_ENDED = "ended"
STATUS_DISAPPROVED = "disapproved"
STATUS_PENDING_REVIEW = "pending_review"

# Statuses that participate in the auction.
AUCTION_ELIGIBLE_STATUSES = {STATUS_ACTIVE}

# ── AdGroup / Review / Quality keys ──────────────────────────────────────

CAMPAIGN_ADGROUPS_KEY = "campaign:{cid}:adgroups"
ADGROUP_KEY = "adgroup:{aid}"
ADGROUP_CREATIVES_KEY = "adgroup:{aid}:creatives"

REVIEW_QUEUE_KEY = "campaigns:review_queue"
APPROVAL_LOG_KEY = "campaigns:approval_log"
AUTO_APPROVE_RULES_KEY = "campaigns:auto_approve_rules"

# Admin bearer (env-overridable in real deploy). Module-level so tests can
# patch; kept hard-coded simple to match the no-new-deps spirit of this
# router.
ADMIN_TOKEN_DEFAULT = "admin-dev-token"

VALID_REJECT_REASONS = {"policy_violation", "low_quality", "spam", "other"}


# ── Pydantic Models ──────────────────────────────────────────────────────


class GeoTargeting(BaseModel):
    country: str | None = None
    city: str | None = None
    lat: float | None = None
    lng: float | None = None
    radius_km: float | None = 5.0


class Demographics(BaseModel):
    age_min: int | None = None
    age_max: int | None = None
    gender: str | None = None  # "m" | "f" | "x"


class Targeting(BaseModel):
    geo: GeoTargeting | None = None
    demographics: Demographics | None = None
    interests: list[str] = Field(default_factory=list)
    lookalike_user_id: str | None = None
    exclude_users: list[str] = Field(default_factory=list)


class Creative(BaseModel):
    recipe_id: str | None = None
    game_slug: str | None = None
    voucher_template_id: str | None = None
    share_card: str | None = None


class Schedule(BaseModel):
    start_at: float | None = None  # epoch seconds
    end_at: float | None = None
    hours_local: list[int] = Field(default_factory=list)  # [start_h, end_h]
    days_of_week: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])


class CampaignCreate(BaseModel):
    brand_id: str
    name: str
    objective: Literal[
        "acquire", "sales", "awareness", "geo_visit",
        "engagement", "retention", "activation", "win_back",
    ]
    bid_strategy: Literal["cpa", "cps", "cpm", "cpv", "cpc", "cpe"]
    max_bid_cents: int = Field(gt=0)
    # CPS percent-of-order bid (basis points). When set with bid_strategy=cps,
    # commission = conversion_value × bid_percent_bps / 10000 — i.e. true
    # GMV revenue share. When unset, CPS falls back to fixed max_bid_cents
    # (legacy semantics). Range 1..5000 = 0.01%..50%.
    bid_percent_bps: int | None = Field(default=None, ge=1, le=5000)
    daily_budget_cents: int = Field(gt=0)
    total_budget_cents: int = Field(gt=0)
    # Attribution window in days. None = use system default (7 days).
    # Auction stores this on the impression token so report_conversion
    # uses the campaign-specific window, not the global default.
    attribution_window_days: int | None = Field(default=None, ge=1, le=90)
    targeting: Targeting = Field(default_factory=Targeting)
    creative: Creative = Field(default_factory=Creative)
    schedule: Schedule = Field(default_factory=Schedule)
    quality_score: float = Field(default=0.5, ge=0.0, le=1.0)


class CampaignUpdate(BaseModel):
    name: str | None = None
    max_bid_cents: int | None = Field(default=None, gt=0)
    bid_percent_bps: int | None = Field(default=None, ge=1, le=5000)
    daily_budget_cents: int | None = Field(default=None, gt=0)
    total_budget_cents: int | None = Field(default=None, gt=0)
    attribution_window_days: int | None = Field(default=None, ge=1, le=90)
    targeting: Targeting | None = None
    creative: Creative | None = None
    schedule: Schedule | None = None
    quality_score: float | None = Field(default=None, ge=0.0, le=1.0)


class CampaignCreateResponse(BaseModel):
    campaign_id: str
    status: str


# ── AdGroup / Review / Quality models ────────────────────────────────────


class AdGroupCreate(BaseModel):
    name: str
    targeting_override: Targeting | None = None
    creative_variants: list[str] = Field(default_factory=list)


class AdGroupUpdate(BaseModel):
    name: str | None = None
    targeting_override: Targeting | None = None
    creative_variants: list[str] | None = None


class ApproveBody(BaseModel):
    admin_token: str
    notes: str | None = None


class RejectBody(BaseModel):
    admin_token: str
    reason: Literal["policy_violation", "low_quality", "spam", "other"]
    details: str | None = None


class AutoApproveRules(BaseModel):
    trusted_brands: list[str] = Field(default_factory=list)
    min_budget_cents: int = Field(default=0, ge=0)
    max_bid_cents: int = Field(default=10_000_000, gt=0)
    allow_categories: list[str] = Field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────────────────


def _now() -> float:
    return time.time()


def _today_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ck(cid: str) -> str:
    return CAMPAIGN_KEY.format(cid=cid)


def _sk(cid: str) -> str:
    return CAMPAIGN_STATS_KEY.format(cid=cid)


def _daily_spend_key(cid: str, date: str | None = None) -> str:
    return CAMPAIGN_DAILY_SPEND_KEY.format(
        cid=cid, date=date or _today_date()
    )


def _total_spend_key(cid: str) -> str:
    return CAMPAIGN_TOTAL_SPEND_KEY.format(cid=cid)


def _serialise_campaign(c: CampaignCreate, campaign_id: str) -> dict[str, str]:
    """Convert nested Pydantic → flat dict of strings for HSET."""
    payload = {
        "campaign_id": campaign_id,
        "brand_id": c.brand_id,
        "name": c.name,
        "objective": c.objective,
        "bid_strategy": c.bid_strategy,
        "max_bid_cents": str(c.max_bid_cents),
        # CPS percent-bid: 0 = unset (fall back to fixed max_bid_cents).
        # Storing 0 keeps the Redis HASH shape stable across all campaigns.
        "bid_percent_bps": str(c.bid_percent_bps or 0),
        "daily_budget_cents": str(c.daily_budget_cents),
        "total_budget_cents": str(c.total_budget_cents),
        # 0 = use system default attribution window (7d) in auction.
        "attribution_window_days": str(c.attribution_window_days or 0),
        "targeting": c.targeting.model_dump_json(),
        "creative": c.creative.model_dump_json(),
        "schedule": c.schedule.model_dump_json(),
        "quality_score": str(c.quality_score),
        "status": STATUS_ACTIVE,
        "created_at": str(_now()),
        "updated_at": str(_now()),
    }
    return payload


def _safe_json_loads(s: str | None, default: Any) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return default


async def _read_daily_spend(r: aioredis.Redis, cid: str) -> int:
    raw = await r.get(_daily_spend_key(cid))
    return int(raw) if raw else 0


async def _read_total_spend(r: aioredis.Redis, cid: str) -> int:
    raw = await r.get(_total_spend_key(cid))
    return int(raw) if raw else 0


async def _add_spend(
    r: aioredis.Redis, cid: str, cents: int
) -> tuple[int, int]:
    """Atomically increment daily + total spend. Returns (daily, total)."""
    daily_key = _daily_spend_key(cid)
    total_key = _total_spend_key(cid)
    pipe = r.pipeline()
    pipe.incrby(daily_key, cents)
    pipe.expire(daily_key, DAILY_SPEND_TTL)
    pipe.incrby(total_key, cents)
    res = await pipe.execute()
    return int(res[0]), int(res[2])


# ── Status Derivation ────────────────────────────────────────────────────


async def _derive_status(
    r: aioredis.Redis, raw: dict[str, str]
) -> str:
    """Recompute status from time + budget.

    Manual statuses (paused, disapproved) and the terminal
    total_budget_exhausted are honoured.
    """
    current = raw.get("status", STATUS_ACTIVE)

    # Terminal / manual / awaiting-review states win.
    if current in (
        STATUS_PAUSED,
        STATUS_DISAPPROVED,
        STATUS_TOTAL_EXHAUSTED,
        STATUS_PENDING_REVIEW,
    ):
        return current

    sched = _safe_json_loads(raw.get("schedule"), {})
    now = _now()

    start_at = sched.get("start_at")
    end_at = sched.get("end_at")

    if end_at and now > end_at:
        return STATUS_ENDED
    if start_at and now < start_at:
        return STATUS_SCHEDULED

    cid = raw.get("campaign_id", "")
    total_budget = int(raw.get("total_budget_cents", "0"))
    daily_budget = int(raw.get("daily_budget_cents", "0"))

    total_spent = await _read_total_spend(r, cid)
    daily_spent = await _read_daily_spend(r, cid)

    if total_budget > 0 and total_spent >= total_budget:
        return STATUS_TOTAL_EXHAUSTED
    if daily_budget > 0 and daily_spent >= daily_budget:
        return STATUS_DAILY_EXHAUSTED

    return STATUS_ACTIVE


async def _persist_status_change(
    r: aioredis.Redis,
    cid: str,
    old_status: str,
    new_status: str,
) -> None:
    """Write back the derived status and update the active set membership."""
    if old_status == new_status:
        return
    pipe = r.pipeline()
    pipe.hset(_ck(cid), mapping={"status": new_status, "updated_at": str(_now())})
    if new_status in AUCTION_ELIGIBLE_STATUSES:
        pipe.sadd(ACTIVE_CAMPAIGNS_KEY, cid)
    else:
        pipe.srem(ACTIVE_CAMPAIGNS_KEY, cid)
    await pipe.execute()


async def _load_and_refresh(
    r: aioredis.Redis, cid: str
) -> dict[str, str]:
    raw = await r.hgetall(_ck(cid))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"campaign {cid} not found",
        )
    new_status = await _derive_status(r, raw)
    if new_status != raw.get("status"):
        await _persist_status_change(r, cid, raw.get("status", ""), new_status)
        raw["status"] = new_status
    return raw


# ── Stats Helpers ────────────────────────────────────────────────────────


async def _read_stats(r: aioredis.Redis, cid: str) -> dict[str, Any]:
    raw = await r.hgetall(_sk(cid)) or {}
    impressions = int(raw.get("impressions", 0))
    clicks = int(raw.get("clicks", 0))
    conversions = int(raw.get("conversions", 0))
    spend = int(raw.get("spend_cents", 0))
    revenue = int(raw.get("revenue_cents", 0))

    ctr = (clicks / impressions) if impressions > 0 else 0.0
    cvr = (conversions / clicks) if clicks > 0 else 0.0
    cpa_actual = (spend / conversions) if conversions > 0 else 0.0
    roas = (revenue / spend) if spend > 0 else 0.0

    return {
        "impressions": impressions,
        "clicks": clicks,
        "conversions": conversions,
        "spend_cents": spend,
        "revenue_cents": revenue,
        "ctr": round(ctr, 6),
        "cvr": round(cvr, 6),
        "cpa_actual": round(cpa_actual, 2),
        "roas": round(roas, 4),
    }


def _to_response(raw: dict[str, str], stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "campaign_id": raw.get("campaign_id"),
        "brand_id": raw.get("brand_id"),
        "name": raw.get("name"),
        "objective": raw.get("objective"),
        "bid_strategy": raw.get("bid_strategy"),
        "max_bid_cents": int(raw.get("max_bid_cents", 0)),
        "bid_percent_bps": int(raw.get("bid_percent_bps", 0)),
        "daily_budget_cents": int(raw.get("daily_budget_cents", 0)),
        "total_budget_cents": int(raw.get("total_budget_cents", 0)),
        "attribution_window_days": int(raw.get("attribution_window_days", 0)),
        "targeting": _safe_json_loads(raw.get("targeting"), {}),
        "creative": _safe_json_loads(raw.get("creative"), {}),
        "schedule": _safe_json_loads(raw.get("schedule"), {}),
        "quality_score": float(raw.get("quality_score", 0.5)),
        "status": raw.get("status"),
        "created_at": float(raw.get("created_at", 0.0)),
        "updated_at": float(raw.get("updated_at", 0.0)),
        "stats": stats,
    }


# ── Endpoints: CRUD ──────────────────────────────────────────────────────


@router.post("/create", response_model=CampaignCreateResponse)
async def create_campaign(
    body: CampaignCreate,
    r: aioredis.Redis = Depends(get_redis),
) -> CampaignCreateResponse:
    """Create a new campaign and mark it active in the auction pool."""
    if body.daily_budget_cents > body.total_budget_cents:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="daily_budget_cents must be ≤ total_budget_cents",
        )
    if body.objective not in VALID_OBJECTIVES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"objective must be one of {sorted(VALID_OBJECTIVES)}",
        )
    if body.bid_strategy not in VALID_BID_STRATEGIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"bid_strategy must be one of {sorted(VALID_BID_STRATEGIES)}",
        )

    campaign_id = f"camp_{uuid4().hex[:16]}"
    payload = _serialise_campaign(body, campaign_id)

    # Check auto-approve rules.
    rules = await r.hgetall(AUTO_APPROVE_RULES_KEY) or {}
    auto_ok = _is_auto_approvable(body, rules)

    # Initial status decision:
    #   1. Future-dated campaigns are *scheduled* regardless of review (the
    #      reviewer can still approve them; once start_at lands we'll let
    #      the status-derivation flip to active).
    #   2. Auto-approvable → active (or scheduled if future-dated).
    #   3. Otherwise → pending_review, enqueued.
    is_future = bool(
        body.schedule.start_at and body.schedule.start_at > _now()
    )
    if auto_ok:
        payload["status"] = STATUS_SCHEDULED if is_future else STATUS_ACTIVE
    else:
        payload["status"] = STATUS_PENDING_REVIEW

    pipe = r.pipeline()
    pipe.hset(_ck(campaign_id), mapping=payload)
    pipe.sadd(BRAND_CAMPAIGNS_KEY.format(bid=body.brand_id), campaign_id)
    if payload["status"] == STATUS_ACTIVE:
        pipe.sadd(ACTIVE_CAMPAIGNS_KEY, campaign_id)
    if payload["status"] == STATUS_PENDING_REVIEW:
        # Bigger daily-budget campaigns surface first in the review queue.
        pipe.zadd(REVIEW_QUEUE_KEY, {campaign_id: float(body.daily_budget_cents)})
    # Pre-create stats hash so HGETALL is deterministic.
    pipe.hset(
        _sk(campaign_id),
        mapping={
            "impressions": 0,
            "clicks": 0,
            "conversions": 0,
            "spend_cents": 0,
            "revenue_cents": 0,
        },
    )
    await pipe.execute()

    logger.info(
        "campaign created cid=%s brand=%s obj=%s bid=%s status=%s auto=%s",
        campaign_id, body.brand_id, body.objective, body.bid_strategy,
        payload["status"], auto_ok,
    )
    return CampaignCreateResponse(
        campaign_id=campaign_id, status=payload["status"]
    )


@router.get("/{brand_id}")
async def list_brand_campaigns(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """List all campaigns for a brand with up-to-date status + stats."""
    cids = await r.smembers(BRAND_CAMPAIGNS_KEY.format(bid=brand_id))
    out: list[dict[str, Any]] = []
    for cid in cids:
        try:
            raw = await _load_and_refresh(r, cid)
        except HTTPException:
            continue
        stats = await _read_stats(r, cid)
        out.append(_to_response(raw, stats))
    out.sort(key=lambda c: c.get("created_at", 0.0), reverse=True)
    return {"brand_id": brand_id, "campaigns": out, "count": len(out)}


@router.get("/{campaign_id}/details")
async def campaign_details(
    campaign_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Full campaign body + real-time stats."""
    raw = await _load_and_refresh(r, campaign_id)
    stats = await _read_stats(r, campaign_id)
    daily_spent = await _read_daily_spend(r, campaign_id)
    total_spent = await _read_total_spend(r, campaign_id)
    resp = _to_response(raw, stats)
    resp["budget"] = {
        "daily_spent_cents": daily_spent,
        "daily_remaining_cents": max(
            0, int(raw.get("daily_budget_cents", 0)) - daily_spent
        ),
        "total_spent_cents": total_spent,
        "total_remaining_cents": max(
            0, int(raw.get("total_budget_cents", 0)) - total_spent
        ),
    }
    return resp


@router.post("/{campaign_id}/update")
async def update_campaign(
    campaign_id: str,
    body: CampaignUpdate,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Partial update; serialises nested fields back to JSON."""
    raw = await _load_and_refresh(r, campaign_id)

    patch: dict[str, str] = {}
    if body.name is not None:
        patch["name"] = body.name
    if body.max_bid_cents is not None:
        patch["max_bid_cents"] = str(body.max_bid_cents)
    if body.bid_percent_bps is not None:
        patch["bid_percent_bps"] = str(body.bid_percent_bps)
    if body.daily_budget_cents is not None:
        patch["daily_budget_cents"] = str(body.daily_budget_cents)
    if body.total_budget_cents is not None:
        patch["total_budget_cents"] = str(body.total_budget_cents)
    if body.attribution_window_days is not None:
        patch["attribution_window_days"] = str(body.attribution_window_days)
    if body.targeting is not None:
        patch["targeting"] = body.targeting.model_dump_json()
    if body.creative is not None:
        patch["creative"] = body.creative.model_dump_json()
    if body.schedule is not None:
        patch["schedule"] = body.schedule.model_dump_json()
    if body.quality_score is not None:
        patch["quality_score"] = str(body.quality_score)

    if not patch:
        return {"ok": True, "no_changes": True}

    patch["updated_at"] = str(_now())
    await r.hset(_ck(campaign_id), mapping=patch)

    # Status may need re-derivation (schedule / budget changed).
    raw.update(patch)
    new_status = await _derive_status(r, raw)
    await _persist_status_change(r, campaign_id, raw.get("status", ""), new_status)

    return {"ok": True, "status": new_status}


@router.post("/{campaign_id}/pause")
async def pause_campaign(
    campaign_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    await _load_and_refresh(r, campaign_id)
    pipe = r.pipeline()
    pipe.hset(_ck(campaign_id), mapping={"status": STATUS_PAUSED, "updated_at": str(_now())})
    pipe.srem(ACTIVE_CAMPAIGNS_KEY, campaign_id)
    await pipe.execute()
    return {"ok": True, "status": STATUS_PAUSED}


@router.post("/{campaign_id}/resume")
async def resume_campaign(
    campaign_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    raw = await r.hgetall(_ck(campaign_id))
    if not raw:
        raise HTTPException(status_code=404, detail="campaign not found")
    if raw.get("status") == STATUS_DISAPPROVED:
        raise HTTPException(
            status_code=409,
            detail="cannot resume disapproved campaign",
        )
    if raw.get("status") == STATUS_PENDING_REVIEW:
        raise HTTPException(
            status_code=409,
            detail="campaign is pending review — use admin approve",
        )
    # Force to active then let derivation pick the right state.
    await r.hset(_ck(campaign_id), mapping={"status": STATUS_ACTIVE})
    raw["status"] = STATUS_ACTIVE
    new_status = await _derive_status(r, raw)
    await _persist_status_change(r, campaign_id, "", new_status)
    return {"ok": True, "status": new_status}


@router.post("/{campaign_id}/delete")
async def delete_campaign(
    campaign_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    raw = await r.hgetall(_ck(campaign_id))
    if not raw:
        raise HTTPException(status_code=404, detail="campaign not found")
    brand_id = raw.get("brand_id", "")

    pipe = r.pipeline()
    pipe.delete(_ck(campaign_id))
    pipe.delete(_sk(campaign_id))
    pipe.delete(_total_spend_key(campaign_id))
    pipe.delete(_daily_spend_key(campaign_id))
    pipe.srem(ACTIVE_CAMPAIGNS_KEY, campaign_id)
    if brand_id:
        pipe.srem(BRAND_CAMPAIGNS_KEY.format(bid=brand_id), campaign_id)
    await pipe.execute()
    return {"ok": True, "deleted": campaign_id}


@router.get("/{campaign_id}/stats")
async def campaign_stats(
    campaign_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    if not await r.exists(_ck(campaign_id)):
        raise HTTPException(status_code=404, detail="campaign not found")
    stats = await _read_stats(r, campaign_id)
    daily = await _read_daily_spend(r, campaign_id)
    total = await _read_total_spend(r, campaign_id)
    return {
        "campaign_id": campaign_id,
        **stats,
        "daily_spend_cents": daily,
        "total_spend_cents": total,
    }


# ── Audience Preview ─────────────────────────────────────────────────────


@router.get("/{campaign_id}/audience-preview")
async def audience_preview(
    campaign_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Estimate reachable users matching this campaign's targeting.

    Implementation note: KiX does not yet maintain a global user profile
    index. This endpoint returns a *heuristic* estimate based on the
    targeting envelope (geo/demographics/interests) plus a sampling of
    known users from Redis (``user:*`` keys). Real production would back
    this with a search index (e.g. OpenSearch/PG).
    """
    raw = await _load_and_refresh(r, campaign_id)
    targeting = _safe_json_loads(raw.get("targeting"), {})

    # Cheap sample scan: pull up to limit user keys.
    sampled: list[str] = []
    cursor = 0
    scanned = 0
    while scanned < limit * 5:  # over-sample then filter
        cursor, batch = await r.scan(cursor=cursor, match="user:*", count=200)
        for k in batch:
            # Filter to leaf profiles, not sub-namespaced keys.
            if k.count(":") == 1:
                sampled.append(k.split(":", 1)[1])
        scanned += len(batch)
        if cursor == 0:
            break

    # Score each user against targeting.
    matches: list[dict[str, Any]] = []
    for uid in sampled[: limit * 3]:
        prof_raw = await r.hgetall(f"user:{uid}")
        if not prof_raw:
            continue
        if _user_matches_targeting(prof_raw, targeting):
            matches.append({
                "user_id": uid,
                "country": prof_raw.get("country"),
                "city": prof_raw.get("city"),
                "age": prof_raw.get("age"),
                "gender": prof_raw.get("gender"),
            })
            if len(matches) >= limit:
                break

    # Estimate reachable via density extrapolation.
    sampled_n = max(1, len(sampled))
    match_rate = len(matches) / sampled_n
    estimated_reach = int(match_rate * sampled_n * 10)  # crude scale-up

    return {
        "campaign_id": campaign_id,
        "sample": matches,
        "sample_size": len(matches),
        "scanned_users": len(sampled),
        "match_rate": round(match_rate, 4),
        "estimated_reach": estimated_reach,
    }


# ── Shared targeting matcher (imported by auction router) ────────────────


def _user_matches_targeting(
    user_profile: dict[str, Any],
    targeting: dict[str, Any],
) -> bool:
    """Pure function — used by audience preview and by the auction.

    `user_profile` is the flat ``user:{uid}`` HASH dict (string values).
    `targeting` is the parsed JSON envelope.
    """
    if not targeting:
        return True

    # Exclude list — hard filter.
    uid = user_profile.get("user_id") or user_profile.get("id")
    if uid and uid in (targeting.get("exclude_users") or []):
        return False

    # Geo.
    geo = targeting.get("geo") or {}
    if geo.get("country"):
        if user_profile.get("country") != geo["country"]:
            return False
    if geo.get("city"):
        if user_profile.get("city") != geo["city"]:
            return False

    # Demographics.
    demo = targeting.get("demographics") or {}
    if demo.get("age_min") is not None:
        try:
            if int(user_profile.get("age", 0)) < int(demo["age_min"]):
                return False
        except (ValueError, TypeError):
            return False
    if demo.get("age_max") is not None:
        try:
            if int(user_profile.get("age", 999)) > int(demo["age_max"]):
                return False
        except (ValueError, TypeError):
            return False
    if demo.get("gender") and user_profile.get("gender"):
        if user_profile["gender"] != demo["gender"]:
            return False

    # Interests — at least one overlap if specified.
    wanted = set(targeting.get("interests") or [])
    if wanted:
        have_raw = user_profile.get("interests", "")
        have = (
            set(_safe_json_loads(have_raw, []))
            if have_raw.startswith("[")
            else set(s.strip() for s in have_raw.split(",") if s.strip())
        )
        if not (wanted & have):
            return False

    return True


# ── Public helpers consumed by auction.py ────────────────────────────────


async def adjust_quality_score(
    r: aioredis.Redis,
    campaign_id: str,
    *,
    impression: bool = False,
    click: bool = False,
    conversion: bool = False,
) -> float:
    """Recompute QS = α·CTR + β·CVR + baseline, clamped to [0.05, 1.0].

    Called by the auction router after impression / click / conversion
    events. Cheap O(1).
    """
    stats = await r.hgetall(_sk(campaign_id)) or {}
    impressions = int(stats.get("impressions", 0)) + (1 if impression else 0)
    clicks = int(stats.get("clicks", 0)) + (1 if click else 0)
    conversions = int(stats.get("conversions", 0)) + (1 if conversion else 0)

    ctr = (clicks / impressions) if impressions > 0 else 0.0
    cvr = (conversions / clicks) if clicks > 0 else 0.0

    # Weighted: baseline 0.3, CTR up to +0.4, CVR up to +0.3.
    qs = 0.3 + min(ctr * 8.0, 0.4) + min(cvr * 6.0, 0.3)
    qs = max(0.05, min(1.0, qs))

    await r.hset(_ck(campaign_id), mapping={"quality_score": str(qs)})
    return qs


async def record_spend(
    r: aioredis.Redis,
    campaign_id: str,
    cents: int,
) -> tuple[int, int, str]:
    """Atomically apply spend and refresh status. Returns (daily, total, status)."""
    daily, total = await _add_spend(r, campaign_id, cents)
    # Update spend on stats hash for reporting.
    await r.hincrby(_sk(campaign_id), "spend_cents", cents)
    raw = await r.hgetall(_ck(campaign_id))
    if raw:
        new_status = await _derive_status(r, raw)
        await _persist_status_change(r, campaign_id, raw.get("status", ""), new_status)
        return daily, total, new_status
    return daily, total, ""


# ═════════════════════════════════════════════════════════════════════════
#  AdGroup Hierarchy
# ═════════════════════════════════════════════════════════════════════════
#
# Google-Ads-style layer between Campaign and Creative. Targeting at the
# adgroup level *overrides* (i.e. wins on per-key basis) the campaign's
# targeting. Multiple creatives per adgroup → A/B-able by the auction.


def _agk(aid: str) -> str:
    return ADGROUP_KEY.format(aid=aid)


def _ag_creatives_k(aid: str) -> str:
    return ADGROUP_CREATIVES_KEY.format(aid=aid)


def _campaign_adgroups_k(cid: str) -> str:
    return CAMPAIGN_ADGROUPS_KEY.format(cid=cid)


def _serialise_adgroup(
    body: AdGroupCreate, adgroup_id: str, campaign_id: str
) -> dict[str, str]:
    return {
        "adgroup_id": adgroup_id,
        "campaign_id": campaign_id,
        "name": body.name,
        "targeting_override": (
            body.targeting_override.model_dump_json()
            if body.targeting_override is not None
            else ""
        ),
        "created_at": str(_now()),
        "updated_at": str(_now()),
    }


def _merge_targeting(
    campaign_t: dict[str, Any], override: dict[str, Any] | None
) -> dict[str, Any]:
    """Adgroup-level keys win — more specific beats less specific.

    Pure helper, exported for the auction router.
    """
    if not override:
        return campaign_t or {}
    merged = dict(campaign_t or {})
    for k, v in override.items():
        if v is None:
            continue
        if isinstance(v, list) and not v:
            # Empty list means "no override" — keep campaign-level.
            continue
        if isinstance(v, dict):
            # Per-key shallow merge for geo / demographics sub-objects.
            base = dict(merged.get(k) or {})
            for sk, sv in v.items():
                if sv is not None:
                    base[sk] = sv
            merged[k] = base
        else:
            merged[k] = v
    return merged


@router.post("/{campaign_id}/adgroups/create")
async def create_adgroup(
    campaign_id: str,
    body: AdGroupCreate,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Create an AdGroup under a campaign."""
    if not await r.exists(_ck(campaign_id)):
        raise HTTPException(status_code=404, detail="campaign not found")

    adgroup_id = f"ag_{uuid4().hex[:14]}"
    payload = _serialise_adgroup(body, adgroup_id, campaign_id)

    pipe = r.pipeline()
    pipe.hset(_agk(adgroup_id), mapping=payload)
    pipe.sadd(_campaign_adgroups_k(campaign_id), adgroup_id)
    if body.creative_variants:
        pipe.rpush(_ag_creatives_k(adgroup_id), *body.creative_variants)
    await pipe.execute()

    logger.info(
        "adgroup created aid=%s cid=%s variants=%d",
        adgroup_id, campaign_id, len(body.creative_variants),
    )
    return {"adgroup_id": adgroup_id, "campaign_id": campaign_id}


@router.get("/{campaign_id}/adgroups")
async def list_adgroups(
    campaign_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    if not await r.exists(_ck(campaign_id)):
        raise HTTPException(status_code=404, detail="campaign not found")
    aids = await r.smembers(_campaign_adgroups_k(campaign_id))
    out: list[dict[str, Any]] = []
    for aid in aids:
        raw = await r.hgetall(_agk(aid))
        if not raw:
            continue
        creatives = await r.lrange(_ag_creatives_k(aid), 0, -1)
        out.append({
            "adgroup_id": raw.get("adgroup_id"),
            "campaign_id": raw.get("campaign_id"),
            "name": raw.get("name"),
            "targeting_override": _safe_json_loads(
                raw.get("targeting_override"), None
            ),
            "creative_variants": creatives,
            "created_at": float(raw.get("created_at", 0.0)),
            "updated_at": float(raw.get("updated_at", 0.0)),
        })
    out.sort(key=lambda x: x.get("created_at", 0.0), reverse=True)
    return {"campaign_id": campaign_id, "adgroups": out, "count": len(out)}


@router.post("/{campaign_id}/adgroups/{adgroup_id}/update")
async def update_adgroup(
    campaign_id: str,
    adgroup_id: str,
    body: AdGroupUpdate,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    raw = await r.hgetall(_agk(adgroup_id))
    if not raw or raw.get("campaign_id") != campaign_id:
        raise HTTPException(status_code=404, detail="adgroup not found")

    patch: dict[str, str] = {}
    if body.name is not None:
        patch["name"] = body.name
    if body.targeting_override is not None:
        patch["targeting_override"] = body.targeting_override.model_dump_json()

    pipe = r.pipeline()
    if patch:
        patch["updated_at"] = str(_now())
        pipe.hset(_agk(adgroup_id), mapping=patch)
    if body.creative_variants is not None:
        # Replace-list semantics.
        pipe.delete(_ag_creatives_k(adgroup_id))
        if body.creative_variants:
            pipe.rpush(_ag_creatives_k(adgroup_id), *body.creative_variants)
    await pipe.execute()
    return {"ok": True, "adgroup_id": adgroup_id}


@router.delete("/{campaign_id}/adgroups/{adgroup_id}")
async def delete_adgroup(
    campaign_id: str,
    adgroup_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    raw = await r.hgetall(_agk(adgroup_id))
    if not raw or raw.get("campaign_id") != campaign_id:
        raise HTTPException(status_code=404, detail="adgroup not found")

    pipe = r.pipeline()
    pipe.delete(_agk(adgroup_id))
    pipe.delete(_ag_creatives_k(adgroup_id))
    pipe.srem(_campaign_adgroups_k(campaign_id), adgroup_id)
    await pipe.execute()
    return {"ok": True, "deleted": adgroup_id}


async def resolve_adgroup_targeting(
    r: aioredis.Redis,
    campaign_id: str,
    adgroup_id: str | None,
) -> dict[str, Any]:
    """Helper for the auction: campaign targeting merged with adgroup override.

    Exported (not under_score-prefixed) so auction.py can import it.
    """
    raw = await r.hgetall(_ck(campaign_id))
    if not raw:
        return {}
    campaign_t = _safe_json_loads(raw.get("targeting"), {}) or {}
    if not adgroup_id:
        return campaign_t
    ag_raw = await r.hgetall(_agk(adgroup_id))
    override = _safe_json_loads(ag_raw.get("targeting_override"), None)
    return _merge_targeting(campaign_t, override)


# ═════════════════════════════════════════════════════════════════════════
#  Manual Review Queue
# ═════════════════════════════════════════════════════════════════════════


def _is_auto_approvable(
    body: CampaignCreate, rules: dict[str, str]
) -> bool:
    """Decide if a fresh campaign skips manual review.

    `rules` is the flat Redis HASH (string values).

    MVP default (permissive): when no auto-approve rules are configured
    (empty/unset hash), every new campaign is auto-approved. Admins opt
    INTO restrictive review by writing a rules hash via
    ``POST /admin/auto-approve-rules`` — only then are trusted_brands /
    max_bid / min_budget gates enforced.
    """
    if not rules:
        # No rules configured → permissive default: auto-approve all.
        return True

    # Rules ARE set → apply restrictive logic. An explicit empty
    # trusted_brands list means "every brand passes the trust gate"
    # (the gate is opt-in); a populated list restricts to listed brands.
    trusted = _safe_json_loads(rules.get("trusted_brands"), []) or []
    if trusted and body.brand_id not in trusted:
        return False
    try:
        max_bid_cap = int(rules.get("max_bid_cents", "0"))
    except (TypeError, ValueError):
        max_bid_cap = 0
    if max_bid_cap > 0 and body.max_bid_cents > max_bid_cap:
        return False
    try:
        min_budget = int(rules.get("min_budget_cents", "0"))
    except (TypeError, ValueError):
        min_budget = 0
    if min_budget > 0 and body.daily_budget_cents < min_budget:
        return False
    return True


def _check_admin(token: str) -> None:
    """Centralised admin-token check. Lift to env-config in production."""
    import os
    expected = os.getenv("KIX_ADMIN_TOKEN", ADMIN_TOKEN_DEFAULT)
    if token != expected:
        raise HTTPException(status_code=403, detail="invalid admin token")


async def _log_approval(
    r: aioredis.Redis,
    *,
    campaign_id: str,
    action: str,
    actor_token_hash: str,
    extra: dict[str, Any] | None = None,
) -> None:
    entry = {
        "campaign_id": campaign_id,
        "action": action,
        "at": _now(),
        "actor": actor_token_hash,
        **(extra or {}),
    }
    await r.lpush(APPROVAL_LOG_KEY, json.dumps(entry))
    await r.ltrim(APPROVAL_LOG_KEY, 0, 9999)  # cap at 10k entries


def _token_fingerprint(token: str) -> str:
    """Short non-reversible fingerprint, for the audit log."""
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


@router.get("/admin/review-queue")
async def review_queue(
    status_filter: Literal["pending", "approved", "rejected"] = Query(
        default="pending", alias="status"
    ),
    limit: int = Query(default=100, ge=1, le=1000),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return campaigns in the review pipeline, high-budget first.

    * ``pending``  — currently in pending_review status (from the sorted set).
    * ``approved`` — derived from the approval log.
    * ``rejected`` — disapproved campaigns (status=disapproved).
    """
    items: list[dict[str, Any]] = []

    if status_filter == "pending":
        # Highest priority (== largest score == biggest daily_budget) first.
        cids_with_scores = await r.zrevrange(
            REVIEW_QUEUE_KEY, 0, limit - 1, withscores=True
        )
        for cid, score in cids_with_scores:
            raw = await r.hgetall(_ck(cid))
            if not raw:
                # Stale queue entry — clean it up best-effort.
                await r.zrem(REVIEW_QUEUE_KEY, cid)
                continue
            if raw.get("status") != STATUS_PENDING_REVIEW:
                # Drifted out of pending — keep the queue clean.
                await r.zrem(REVIEW_QUEUE_KEY, cid)
                continue
            items.append({
                "campaign_id": cid,
                "brand_id": raw.get("brand_id"),
                "name": raw.get("name"),
                "objective": raw.get("objective"),
                "max_bid_cents": int(raw.get("max_bid_cents", 0)),
                "daily_budget_cents": int(raw.get("daily_budget_cents", 0)),
                "total_budget_cents": int(raw.get("total_budget_cents", 0)),
                "priority": float(score),
                "created_at": float(raw.get("created_at", 0.0)),
            })
    elif status_filter == "approved":
        entries = await r.lrange(APPROVAL_LOG_KEY, 0, limit * 4)
        seen: set[str] = set()
        for raw_entry in entries:
            try:
                e = json.loads(raw_entry)
            except (json.JSONDecodeError, TypeError):
                continue
            if e.get("action") != "approve":
                continue
            cid = e.get("campaign_id", "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            items.append(e)
            if len(items) >= limit:
                break
    else:  # rejected
        entries = await r.lrange(APPROVAL_LOG_KEY, 0, limit * 4)
        seen = set()
        for raw_entry in entries:
            try:
                e = json.loads(raw_entry)
            except (json.JSONDecodeError, TypeError):
                continue
            if e.get("action") != "reject":
                continue
            cid = e.get("campaign_id", "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            items.append(e)
            if len(items) >= limit:
                break

    return {
        "status": status_filter,
        "items": items,
        "count": len(items),
    }


@router.post("/{campaign_id}/admin/approve")
async def admin_approve(
    campaign_id: str,
    body: ApproveBody,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _check_admin(body.admin_token)
    raw = await r.hgetall(_ck(campaign_id))
    if not raw:
        raise HTTPException(status_code=404, detail="campaign not found")
    if raw.get("status") not in (STATUS_PENDING_REVIEW, STATUS_DISAPPROVED):
        raise HTTPException(
            status_code=409,
            detail=f"cannot approve from status {raw.get('status')}",
        )

    # Decide target status: scheduled if start_at is future, else active.
    sched = _safe_json_loads(raw.get("schedule"), {}) or {}
    start_at = sched.get("start_at")
    target = (
        STATUS_SCHEDULED
        if (start_at and start_at > _now())
        else STATUS_ACTIVE
    )

    pipe = r.pipeline()
    pipe.hset(_ck(campaign_id), mapping={
        "status": target,
        "updated_at": str(_now()),
    })
    if target == STATUS_ACTIVE:
        pipe.sadd(ACTIVE_CAMPAIGNS_KEY, campaign_id)
    pipe.zrem(REVIEW_QUEUE_KEY, campaign_id)
    await pipe.execute()

    await _log_approval(
        r,
        campaign_id=campaign_id,
        action="approve",
        actor_token_hash=_token_fingerprint(body.admin_token),
        extra={"notes": body.notes or "", "to_status": target},
    )
    logger.info("campaign approved cid=%s → %s", campaign_id, target)
    return {"ok": True, "campaign_id": campaign_id, "status": target}


@router.post("/{campaign_id}/admin/reject")
async def admin_reject(
    campaign_id: str,
    body: RejectBody,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _check_admin(body.admin_token)
    if body.reason not in VALID_REJECT_REASONS:
        raise HTTPException(status_code=422, detail="invalid reason")
    raw = await r.hgetall(_ck(campaign_id))
    if not raw:
        raise HTTPException(status_code=404, detail="campaign not found")

    pipe = r.pipeline()
    pipe.hset(_ck(campaign_id), mapping={
        "status": STATUS_DISAPPROVED,
        "updated_at": str(_now()),
    })
    pipe.srem(ACTIVE_CAMPAIGNS_KEY, campaign_id)
    pipe.zrem(REVIEW_QUEUE_KEY, campaign_id)
    await pipe.execute()

    await _log_approval(
        r,
        campaign_id=campaign_id,
        action="reject",
        actor_token_hash=_token_fingerprint(body.admin_token),
        extra={"reason": body.reason, "details": body.details or ""},
    )
    logger.info(
        "campaign rejected cid=%s reason=%s", campaign_id, body.reason
    )
    return {
        "ok": True,
        "campaign_id": campaign_id,
        "status": STATUS_DISAPPROVED,
        "reason": body.reason,
    }


@router.post("/{campaign_id}/submit-for-review")
async def submit_for_review(
    campaign_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Merchant-callable self-serve path out of ``pending_review``.

    Re-evaluates the current auto-approve rules against the campaign and,
    if the campaign would now pass, flips it to ``active`` (or
    ``scheduled`` for future-dated start) without admin involvement.
    Otherwise the campaign stays in ``pending_review`` and the merchant
    sees the reason. This is the MVP escape hatch so merchants are never
    blocked when default rules are permissive.
    """
    raw = await r.hgetall(_ck(campaign_id))
    if not raw:
        raise HTTPException(status_code=404, detail="campaign not found")
    if raw.get("status") != STATUS_PENDING_REVIEW:
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "status": raw.get("status"),
            "no_changes": True,
        }

    # Reconstruct just enough of CampaignCreate to re-run the rule check.
    try:
        body = CampaignCreate(
            brand_id=raw.get("brand_id", ""),
            name=raw.get("name", ""),
            objective=raw.get("objective", "acquire"),  # type: ignore[arg-type]
            bid_strategy=raw.get("bid_strategy", "cpa"),  # type: ignore[arg-type]
            max_bid_cents=int(raw.get("max_bid_cents", "1") or "1"),
            bid_percent_bps=(
                int(raw.get("bid_percent_bps", "0") or "0") or None
            ),
            daily_budget_cents=int(raw.get("daily_budget_cents", "1") or "1"),
            total_budget_cents=int(raw.get("total_budget_cents", "1") or "1"),
            attribution_window_days=(
                int(raw.get("attribution_window_days", "0") or "0") or None
            ),
            targeting=Targeting(**_safe_json_loads(raw.get("targeting"), {})),
            creative=Creative(**_safe_json_loads(raw.get("creative"), {})),
            schedule=Schedule(**_safe_json_loads(raw.get("schedule"), {})),
            quality_score=float(raw.get("quality_score", "0.5") or "0.5"),
        )
    except Exception as exc:  # noqa: BLE001 — defensive reconstruction
        logger.warning(
            "submit_for_review: cannot rebuild campaign body cid=%s err=%s",
            campaign_id, exc,
        )
        raise HTTPException(
            status_code=409,
            detail="campaign payload corrupted — admin review required",
        ) from exc

    rules = await r.hgetall(AUTO_APPROVE_RULES_KEY) or {}
    if not _is_auto_approvable(body, rules):
        return {
            "ok": False,
            "campaign_id": campaign_id,
            "status": STATUS_PENDING_REVIEW,
            "reason": "auto_approve_rules_not_satisfied",
        }

    sched = _safe_json_loads(raw.get("schedule"), {}) or {}
    start_at = sched.get("start_at")
    target = (
        STATUS_SCHEDULED
        if (start_at and start_at > _now())
        else STATUS_ACTIVE
    )

    pipe = r.pipeline()
    pipe.hset(_ck(campaign_id), mapping={
        "status": target,
        "updated_at": str(_now()),
    })
    if target == STATUS_ACTIVE:
        pipe.sadd(ACTIVE_CAMPAIGNS_KEY, campaign_id)
    pipe.zrem(REVIEW_QUEUE_KEY, campaign_id)
    await pipe.execute()

    await _log_approval(
        r,
        campaign_id=campaign_id,
        action="approve",
        actor_token_hash="self_serve",
        extra={"notes": "submit_for_review", "to_status": target},
    )
    logger.info(
        "campaign self-serve approved cid=%s → %s", campaign_id, target
    )
    return {"ok": True, "campaign_id": campaign_id, "status": target}


@router.post("/admin/auto-approve-rules")
async def set_auto_approve_rules(
    body: AutoApproveRules,
    admin_token: str = Query(...),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _check_admin(admin_token)
    mapping = {
        "trusted_brands": json.dumps(body.trusted_brands),
        "min_budget_cents": str(body.min_budget_cents),
        "max_bid_cents": str(body.max_bid_cents),
        "allow_categories": json.dumps(body.allow_categories),
        "updated_at": str(_now()),
    }
    # Replace-all semantics: drop the old hash before writing.
    pipe = r.pipeline()
    pipe.delete(AUTO_APPROVE_RULES_KEY)
    pipe.hset(AUTO_APPROVE_RULES_KEY, mapping=mapping)
    await pipe.execute()
    return {"ok": True, "rules": body.model_dump()}


@router.get("/admin/auto-approve-rules")
async def get_auto_approve_rules(
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    raw = await r.hgetall(AUTO_APPROVE_RULES_KEY) or {}
    if not raw:
        return {"rules": None}
    return {
        "rules": {
            "trusted_brands": _safe_json_loads(raw.get("trusted_brands"), []),
            "min_budget_cents": int(raw.get("min_budget_cents", 0)),
            "max_bid_cents": int(raw.get("max_bid_cents", 0)),
            "allow_categories": _safe_json_loads(
                raw.get("allow_categories"), []
            ),
            "updated_at": float(raw.get("updated_at", 0.0)),
        }
    }


# ═════════════════════════════════════════════════════════════════════════
#  Quality Score Transparency
# ═════════════════════════════════════════════════════════════════════════
#
# Decompose the opaque 0..1 quality_score into a 0..10 score with four
# sub-components and give the merchant concrete next-steps.

# Industry-level defaults (TODO: lift from a per-vertical config table).
_INDUSTRY_AVG_CTR = 0.025
_INDUSTRY_AVG_CVR = 0.030
_CREATIVE_REFRESH_WARN_DAYS = 14


async def _brand_avg_metrics(
    r: aioredis.Redis, brand_id: str, exclude_cid: str
) -> tuple[float, float]:
    """Average CTR and CVR across a brand's campaigns (excluding `exclude_cid`)."""
    cids = await r.smembers(BRAND_CAMPAIGNS_KEY.format(bid=brand_id))
    ctrs: list[float] = []
    cvrs: list[float] = []
    for cid in cids:
        if cid == exclude_cid:
            continue
        stats = await _read_stats(r, cid)
        if stats["impressions"] > 0:
            ctrs.append(stats["ctr"])
        if stats["clicks"] > 0:
            cvrs.append(stats["cvr"])
    avg_ctr = (sum(ctrs) / len(ctrs)) if ctrs else _INDUSTRY_AVG_CTR
    avg_cvr = (sum(cvrs) / len(cvrs)) if cvrs else _INDUSTRY_AVG_CVR
    return avg_ctr, avg_cvr


def _score_ctr(campaign_ctr: float, brand_avg_ctr: float) -> float:
    """0..3 score: 1.0× benchmark → 1.5, 2× → 3.0, 0× → 0."""
    if brand_avg_ctr <= 0:
        return 1.5 if campaign_ctr > 0 else 0.0
    ratio = campaign_ctr / brand_avg_ctr
    return round(min(3.0, ratio * 1.5), 2)


def _score_cvr(campaign_cvr: float, brand_avg_cvr: float) -> float:
    if brand_avg_cvr <= 0:
        return 1.5 if campaign_cvr > 0 else 0.0
    ratio = campaign_cvr / brand_avg_cvr
    return round(min(3.0, ratio * 1.5), 2)


async def _score_creative(
    r: aioredis.Redis, cid: str, raw: dict[str, str]
) -> tuple[float, int, float]:
    """Returns (score 0..2, n_variants, days_since_refresh)."""
    # Variants come from any adgroups under this campaign (if present) plus
    # the campaign-level creative as a single fallback variant.
    aids = await r.smembers(_campaign_adgroups_k(cid))
    variants: set[str] = set()
    for aid in aids:
        creatives = await r.lrange(_ag_creatives_k(aid), 0, -1)
        for c in creatives:
            if c:
                variants.add(c)
    creative = _safe_json_loads(raw.get("creative"), {}) or {}
    for v in (
        creative.get("recipe_id"),
        creative.get("game_slug"),
        creative.get("voucher_template_id"),
    ):
        if v:
            variants.add(str(v))
    n = len(variants)

    updated_at = float(raw.get("updated_at", 0.0))
    days_since = max(0.0, (_now() - updated_at) / 86400.0) if updated_at else 999.0

    # Variety score: 0 variants → 0; 1 → 0.6; 2 → 1.2; 3+ → 1.6
    variety = min(1.6, n * 0.6)
    # Freshness penalty: lose up to 0.4 for stale creative.
    if days_since > _CREATIVE_REFRESH_WARN_DAYS:
        freshness = 0.0
    elif days_since > 7:
        freshness = 0.2
    else:
        freshness = 0.4
    return round(min(2.0, variety + freshness), 2), n, round(days_since, 1)


def _score_targeting(targeting: dict[str, Any]) -> tuple[float, int]:
    """Score how specific the targeting is. Returns (score 0..2, specificity_count)."""
    spec = 0
    geo = targeting.get("geo") or {}
    if geo.get("country"):
        spec += 1
    if geo.get("city"):
        spec += 1
    if geo.get("lat") and geo.get("lng"):
        spec += 1
    demo = targeting.get("demographics") or {}
    if demo.get("age_min") is not None or demo.get("age_max") is not None:
        spec += 1
    if demo.get("gender"):
        spec += 1
    if targeting.get("interests"):
        spec += 1
    if targeting.get("lookalike_user_id"):
        spec += 1
    # 0 facets → 0, 1 → 0.4, 2 → 0.8, ... cap at 2.0 (5+ facets).
    return round(min(2.0, spec * 0.4), 2), spec


def _improvement_hints(
    *,
    ctr_score: float,
    cvr_score: float,
    creative_score: float,
    targeting_score: float,
    n_variants: int,
    days_since_refresh: float,
    targeting_specificity: int,
    brand_avg_ctr: float,
    brand_avg_cvr: float,
    campaign_ctr: float,
    campaign_cvr: float,
) -> list[str]:
    hints: list[str] = []
    if ctr_score < 1.5:
        hints.append(
            f"CTR ({campaign_ctr:.2%}) is below brand average "
            f"({brand_avg_ctr:.2%}) — try a stronger headline/thumbnail."
        )
    if cvr_score < 1.5:
        hints.append(
            f"CVR ({campaign_cvr:.2%}) is below brand average "
            f"({brand_avg_cvr:.2%}) — simplify the post-click flow."
        )
    if n_variants <= 1:
        hints.append(
            "Add A/B variants — single-creative campaigns can't be optimised."
        )
    if days_since_refresh > _CREATIVE_REFRESH_WARN_DAYS:
        hints.append(
            f"Refresh creative — same asset for "
            f"{int(days_since_refresh)} days (>14d shows ad fatigue)."
        )
    if targeting_specificity == 0:
        hints.append("Add at least one targeting facet — wide-open targeting wastes budget.")
    elif targeting_specificity <= 1:
        hints.append("Narrow targeting — add geo / demographics / interest filters.")
    if creative_score < 1.0:
        hints.append("Increase creative variety: aim for 3+ variants per adgroup.")
    if not hints:
        hints.append("All quality dimensions look healthy — keep monitoring.")
    return hints


@router.get("/{campaign_id}/quality")
async def campaign_quality(
    campaign_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Decomposed quality-score breakdown + concrete improvement hints."""
    raw = await _load_and_refresh(r, campaign_id)
    stats = await _read_stats(r, campaign_id)
    brand_id = raw.get("brand_id", "")
    brand_avg_ctr, brand_avg_cvr = await _brand_avg_metrics(
        r, brand_id, campaign_id
    )

    campaign_ctr = stats["ctr"]
    campaign_cvr = stats["cvr"]

    ctr_score = _score_ctr(campaign_ctr, brand_avg_ctr)
    cvr_score = _score_cvr(campaign_cvr, brand_avg_cvr)
    creative_score, n_variants, days_since_refresh = await _score_creative(
        r, campaign_id, raw
    )
    targeting = _safe_json_loads(raw.get("targeting"), {}) or {}
    targeting_score, targeting_specificity = _score_targeting(targeting)

    # Cap at 10. Each sub-score already lives in its declared range
    # (ctr+cvr up to 3+3=6, creative up to 2, targeting up to 2 → 10).
    overall = round(
        min(10.0, ctr_score + cvr_score + creative_score + targeting_score),
        2,
    )

    hints = _improvement_hints(
        ctr_score=ctr_score,
        cvr_score=cvr_score,
        creative_score=creative_score,
        targeting_score=targeting_score,
        n_variants=n_variants,
        days_since_refresh=days_since_refresh,
        targeting_specificity=targeting_specificity,
        brand_avg_ctr=brand_avg_ctr,
        brand_avg_cvr=brand_avg_cvr,
        campaign_ctr=campaign_ctr,
        campaign_cvr=campaign_cvr,
    )

    return {
        "campaign_id": campaign_id,
        "overall_score": overall,
        "breakdown": {
            "ctr_score": ctr_score,
            "cvr_score": cvr_score,
            "creative_score": creative_score,
            "targeting_score": targeting_score,
        },
        "benchmark": {
            "campaign_ctr": campaign_ctr,
            "campaign_cvr": campaign_cvr,
            "brand_avg_ctr": round(brand_avg_ctr, 6),
            "brand_avg_cvr": round(brand_avg_cvr, 6),
            "industry_avg_ctr": _INDUSTRY_AVG_CTR,
            "industry_avg_cvr": _INDUSTRY_AVG_CVR,
        },
        "creative_meta": {
            "n_variants": n_variants,
            "days_since_refresh": days_since_refresh,
        },
        "targeting_meta": {
            "specificity_facets": targeting_specificity,
        },
        "improvement_hints": hints,
        # Also surface the legacy 0..1 quality_score so existing callers
        # have a migration window.
        "legacy_quality_score": float(raw.get("quality_score", 0.5)),
    }
