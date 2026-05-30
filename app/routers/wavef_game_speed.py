"""Wave F game-speed router — Playable-style difficulty knob.

Endpoints:
    PUT /api/v1/wavef/game-speed/{campaign_id}              set difficulty
    GET /api/v1/wavef/game-speed/{campaign_id}              read difficulty
    GET /api/v1/wavef/game-speed/{campaign_id}/resolve      resolve for template
    GET /api/v1/wavef/game-speed/_/templates                supported templates

NEW file — no existing campaign router touched.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

import redis.asyncio as aioredis

from app.deps import get_current_user
from app.redis_client import get_redis
from app.services import wavef_game_speed as svc


router = APIRouter()


class SetDifficultyRequest(BaseModel):
    difficulty: int = Field(
        ..., ge=svc.DIFFICULTY_MIN, le=svc.DIFFICULTY_MAX
    )


class DifficultyResponse(BaseModel):
    campaign_id: str
    difficulty: int


class ResolveResponse(BaseModel):
    campaign_id: str
    template: str
    difficulty: int
    params: dict
    win_probability: float


class TemplatesResponse(BaseModel):
    templates: list[str]


@router.put("/{campaign_id}", response_model=DifficultyResponse)
async def set_endpoint(
    campaign_id: str,
    body: SetDifficultyRequest,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> DifficultyResponse:
    d = await svc.set_difficulty(r, campaign_id, body.difficulty)
    return DifficultyResponse(campaign_id=campaign_id, difficulty=d)


@router.get("/_/templates", response_model=TemplatesResponse)
async def templates_endpoint(
    current_user: dict = Depends(get_current_user),
) -> TemplatesResponse:
    return TemplatesResponse(templates=svc.supported_templates())


@router.get("/{campaign_id}", response_model=DifficultyResponse)
async def get_endpoint(
    campaign_id: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> DifficultyResponse:
    d = await svc.get_difficulty(r, campaign_id)
    return DifficultyResponse(campaign_id=campaign_id, difficulty=d)


@router.get("/{campaign_id}/resolve", response_model=ResolveResponse)
async def resolve_endpoint(
    campaign_id: str,
    template: str = Query(...),
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> ResolveResponse:
    res = await svc.resolve_session(r, campaign_id, template)
    return ResolveResponse(**res)
