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

VALID_OBJECTIVES = {"acquire", "sales", "awareness", "geo_visit"}
VALID_BID_STRATEGIES = {"cpa", "cps", "cpm", "cpv", "cpc"}

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_DAILY_EXHAUSTED = "daily_budget_exhausted"
STATUS_TOTAL_EXHAUSTED = "total_budget_exhausted"
STATUS_SCHEDULED = "scheduled"
STATUS_ENDED = "ended"
STATUS_DISAPPROVED = "disapproved"

# Statuses that participate in the auction.
AUCTION_ELIGIBLE_STATUSES = {STATUS_ACTIVE}


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
    objective: Literal["acquire", "sales", "awareness", "geo_visit"]
    bid_strategy: Literal["cpa", "cps", "cpm", "cpv", "cpc"]
    max_bid_cents: int = Field(gt=0)
    daily_budget_cents: int = Field(gt=0)
    total_budget_cents: int = Field(gt=0)
    targeting: Targeting = Field(default_factory=Targeting)
    creative: Creative = Field(default_factory=Creative)
    schedule: Schedule = Field(default_factory=Schedule)
    quality_score: float = Field(default=0.5, ge=0.0, le=1.0)


class CampaignUpdate(BaseModel):
    name: str | None = None
    max_bid_cents: int | None = Field(default=None, gt=0)
    daily_budget_cents: int | None = Field(default=None, gt=0)
    total_budget_cents: int | None = Field(default=None, gt=0)
    targeting: Targeting | None = None
    creative: Creative | None = None
    schedule: Schedule | None = None
    quality_score: float | None = Field(default=None, ge=0.0, le=1.0)


class CampaignCreateResponse(BaseModel):
    campaign_id: str
    status: str


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
    return {
        "campaign_id": campaign_id,
        "brand_id": c.brand_id,
        "name": c.name,
        "objective": c.objective,
        "bid_strategy": c.bid_strategy,
        "max_bid_cents": str(c.max_bid_cents),
        "daily_budget_cents": str(c.daily_budget_cents),
        "total_budget_cents": str(c.total_budget_cents),
        "targeting": c.targeting.model_dump_json(),
        "creative": c.creative.model_dump_json(),
        "schedule": c.schedule.model_dump_json(),
        "quality_score": str(c.quality_score),
        "status": STATUS_ACTIVE,
        "created_at": str(_now()),
        "updated_at": str(_now()),
    }


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

    # Terminal / manual states win.
    if current in (STATUS_PAUSED, STATUS_DISAPPROVED, STATUS_TOTAL_EXHAUSTED):
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
        "daily_budget_cents": int(raw.get("daily_budget_cents", 0)),
        "total_budget_cents": int(raw.get("total_budget_cents", 0)),
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

    # Initial status — scheduled if start_at in future.
    if body.schedule.start_at and body.schedule.start_at > _now():
        payload["status"] = STATUS_SCHEDULED

    pipe = r.pipeline()
    pipe.hset(_ck(campaign_id), mapping=payload)
    pipe.sadd(BRAND_CAMPAIGNS_KEY.format(bid=body.brand_id), campaign_id)
    if payload["status"] == STATUS_ACTIVE:
        pipe.sadd(ACTIVE_CAMPAIGNS_KEY, campaign_id)
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
        "campaign created cid=%s brand=%s obj=%s bid=%s",
        campaign_id, body.brand_id, body.objective, body.bid_strategy,
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
    if body.daily_budget_cents is not None:
        patch["daily_budget_cents"] = str(body.daily_budget_cents)
    if body.total_budget_cents is not None:
        patch["total_budget_cents"] = str(body.total_budget_cents)
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
