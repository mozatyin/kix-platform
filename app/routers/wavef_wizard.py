"""Wave F campaign-wizard router — Playable-style fast-launch."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

import redis.asyncio as aioredis

from app.deps import get_current_user
from app.redis_client import get_redis
from app.services import wavef_wizard as svc


router = APIRouter()


# ── Models ───────────────────────────────────────────────────────────────


class CreateDraftRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)


class PatchDraftRequest(BaseModel):
    step: Optional[str] = None
    mechanic_id: Optional[str] = None
    assets: Optional[dict] = None
    reward: Optional[dict] = None


class DraftOut(BaseModel):
    draft_id: str
    uid: str
    brand_id: str
    step: str
    mechanic_id: str
    assets: dict
    reward: dict
    created_at_ms: int
    updated_at_ms: int
    published: bool
    campaign_id: str


class PublishOut(BaseModel):
    published: bool
    campaign_id: str
    draft_id: str


# ── Routes ───────────────────────────────────────────────────────────────


@router.post("/drafts", response_model=DraftOut)
async def create_draft(
    body: CreateDraftRequest,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> DraftOut:
    res = await svc.create_draft(r, uid=current_user["sub"], brand_id=body.brand_id)
    return DraftOut(**res)


@router.get("/drafts", response_model=list[DraftOut])
async def list_drafts(
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> list[DraftOut]:
    rows = await svc.list_drafts(r, current_user["sub"])
    return [DraftOut(**d) for d in rows]


@router.get("/state/{draft_id}", response_model=DraftOut)
async def read_draft(
    draft_id: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> DraftOut:
    d = await svc.get_draft(r, draft_id)
    if d is None:
        raise HTTPException(status_code=404, detail="draft not found")
    return DraftOut(**d)


@router.post("/state/{draft_id}", response_model=DraftOut)
async def patch_state(
    draft_id: str,
    body: PatchDraftRequest = Body(...),
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> DraftOut:
    try:
        d = await svc.patch_draft(
            r,
            draft_id,
            step=body.step,
            mechanic_id=body.mechanic_id,
            assets=body.assets,
            reward=body.reward,
        )
    except ValueError as exc:
        msg = str(exc)
        code = 404 if "not found" in msg else 400
        raise HTTPException(status_code=code, detail=msg) from exc
    return DraftOut(**d)


@router.post("/{draft_id}/publish", response_model=PublishOut)
async def publish(
    draft_id: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> PublishOut:
    try:
        res = await svc.publish(r, draft_id)
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        msg = str(exc)
        code = 404 if "not found" in msg else 400
        raise HTTPException(status_code=code, detail=msg) from exc
    return PublishOut(**res)
