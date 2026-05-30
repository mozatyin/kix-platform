"""Campaign Arc Router — multi-week arc HTTP API.

Wraps :mod:`app.services.campaign_arc` with FastAPI endpoints:

  * ``POST /api/v1/campaign-arcs/create``            — merchant creates an arc
  * ``GET  /api/v1/campaign-arcs/{arc_id}``          — arc details + status
  * ``GET  /api/v1/campaign-arcs/{arc_id}/today``    — today's drop spec
  * ``POST /api/v1/campaign-arcs/{arc_id}/play``     — record a play event
  * ``GET  /api/v1/campaign-arcs/{arc_id}/progress`` — per-user progression
  * ``GET  /api/v1/campaign-arcs/{arc_id}/leaderboard`` — top-N players
  * ``POST /api/v1/campaign-arcs/{arc_id}/claim``    — claim a prize

Existing ``app/routers/campaigns.py`` keeps working unchanged — arcs are
purely additive. An arc may wrap any number of existing campaigns
through ``wrapped_campaign_ids`` so the ad-spend / auction flow continues
to run while the arc layer drives narrative.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis
from app.services.campaign_arc import (
    CampaignArc,
    VALID_ARC_TYPES,
    leaderboard as load_leaderboard,
    list_brand_arcs,
    load_arc,
    refresh_status,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Pydantic models ──────────────────────────────────────────────────────


class ArcCreate(BaseModel):
    brand_id: str
    name: str
    arc_type: Literal[
        "monopoly_collect_n",
        "advent_calendar",
        "tournament_bracket",
        "sweepstakes_entries",
    ]
    duration_days: int = Field(ge=1, le=365)
    prize_pool: dict[str, Any] = Field(default_factory=dict)
    redemption_window: dict[str, Any] | None = None
    legal_compliance: dict[str, Any] | None = None
    start_at: float | None = None
    wrapped_campaign_ids: list[str] = Field(default_factory=list)
    # Arc-type-specific:
    #   monopoly_collect_n: piece_set: [str], rare_piece_id: str, collect_n: int
    #   advent_calendar:    daily_rewards: [dict]
    #   tournament_bracket: bracket_size: int
    config: dict[str, Any] = Field(default_factory=dict)


class ArcPlayBody(BaseModel):
    user_id: str
    day_index: int | None = None  # default = today


class ArcClaimBody(BaseModel):
    user_id: str
    prize_id: str
    user_region: str | None = None  # ISO country for legal gating


# ── Helpers ──────────────────────────────────────────────────────────────


def _arc_to_response(arc: CampaignArc) -> dict[str, Any]:
    return {
        "arc_id": arc.arc_id,
        "brand_id": arc.brand_id,
        "name": arc.name,
        "arc_type": arc.arc_type,
        "duration_days": arc.duration_days,
        "status": arc.status,
        "start_at": arc.start_at,
        "prize_pool": arc.prize_pool,
        "redemption_window": arc.redemption_window,
        "legal_compliance": arc.legal_compliance,
        "wrapped_campaign_ids": arc.wrapped_campaign_ids,
        "config": arc.config,
        "current_day_index": arc.current_day_index(),
        "created_at": arc.created_at,
        "updated_at": arc.updated_at,
    }


async def _must_load(arc_id: str, r: aioredis.Redis) -> CampaignArc:
    arc = await load_arc(r, arc_id)
    if arc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"arc {arc_id} not found",
        )
    await refresh_status(r, arc)
    return arc


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("/create")
async def create_arc(
    body: ArcCreate,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Create a new multi-day arc and persist its daily_drops schedule."""
    if body.arc_type not in VALID_ARC_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"arc_type must be one of {sorted(VALID_ARC_TYPES)}, "
                f"got {body.arc_type}"
            ),
        )
    try:
        arc = CampaignArc.new(
            brand_id=body.brand_id,
            name=body.name,
            duration_days=body.duration_days,
            arc_type=body.arc_type,
            prize_pool=body.prize_pool,
            redemption_window=body.redemption_window,
            legal_compliance=body.legal_compliance,
            start_at=body.start_at,
            wrapped_campaign_ids=body.wrapped_campaign_ids,
            config=body.config,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        ) from e
    await arc.save(r)
    logger.info(
        "campaign_arc created arc_id=%s brand=%s type=%s days=%d",
        arc.arc_id, arc.brand_id, arc.arc_type, arc.duration_days,
    )
    return {
        "ok": True,
        "arc_id": arc.arc_id,
        "status": arc.status,
        "daily_drops_count": len(arc.daily_drops),
    }


@router.get("/brand/{brand_id}")
async def list_arcs_for_brand(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """List arcs owned by a brand (most-recent first)."""
    arcs = await list_brand_arcs(r, brand_id)
    for arc in arcs:
        await refresh_status(r, arc)
    return {
        "brand_id": brand_id,
        "arcs": [_arc_to_response(a) for a in arcs],
        "count": len(arcs),
    }


@router.get("/{arc_id}")
async def get_arc(
    arc_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Full arc details + status refresh."""
    arc = await _must_load(arc_id, r)
    resp = _arc_to_response(arc)
    resp["daily_drops"] = arc.daily_drops
    return resp


@router.get("/{arc_id}/today")
async def get_today_drop(
    arc_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Today's drop spec for client UI (Day N of M + drop + prize pool)."""
    arc = await _must_load(arc_id, r)
    return arc.get_today_play()


@router.post("/{arc_id}/play")
async def record_play(
    arc_id: str,
    body: ArcPlayBody,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Record a play; awards today's piece / door / ticket / round."""
    arc = await _must_load(arc_id, r)
    result = await arc.record_play(r, body.user_id, day_index=body.day_index)
    return {"arc_id": arc_id, **result}


@router.get("/{arc_id}/progress")
async def get_progress(
    arc_id: str,
    user_id: str = Query(..., min_length=1),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Per-user progression in the arc (pieces / doors / tickets / score)."""
    arc = await _must_load(arc_id, r)
    progress = await arc.compute_progression(r, user_id)
    return {"arc_id": arc_id, **progress}


@router.get("/{arc_id}/leaderboard")
async def get_leaderboard(
    arc_id: str,
    limit: int = Query(default=10, ge=1, le=1000),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Top-N players ordered by score."""
    arc = await _must_load(arc_id, r)
    top = await load_leaderboard(r, arc.arc_id, limit=limit)
    return {
        "arc_id": arc.arc_id,
        "leaderboard": top,
        "count": len(top),
    }


@router.post("/{arc_id}/claim")
async def claim_prize(
    arc_id: str,
    body: ArcClaimBody,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Claim a prize from the arc's prize pool.

    Gating: arc must be in redemption window, prize must be in pool and
    have capacity, region must not be excluded, user must hit progression
    threshold, and the same user can't double-claim.
    """
    arc = await _must_load(arc_id, r)
    ok, reason = await arc.can_user_claim(
        r, body.user_id, body.prize_id, user_region=body.user_region
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "claim_rejected",
                "reason": reason,
                "arc_id": arc_id,
                "prize_id": body.prize_id,
                "user_id": body.user_id,
            },
        )
    await arc.record_claim(r, body.user_id, body.prize_id)
    return {
        "ok": True,
        "arc_id": arc_id,
        "user_id": body.user_id,
        "prize_id": body.prize_id,
    }
