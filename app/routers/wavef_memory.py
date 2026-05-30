"""Wave F memory-match router — CataBoom-style concentration template.

Endpoints::

    POST /api/v1/wavef/memory/sessions
    POST /api/v1/wavef/memory/sessions/{sid}/flip
    POST /api/v1/wavef/memory/sessions/{sid}/complete
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import redis.asyncio as aioredis

from app.deps import get_current_user
from app.redis_client import get_redis
from app.services import wavef_memory as svc


router = APIRouter()


class CreateSessionRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    difficulty: int = Field(default=1, ge=1, le=3)


class CreateSessionResponse(BaseModel):
    session_id: str
    brand_id: str
    user_id: str
    difficulty: int
    grid_size: int
    deck_layout_masked: list[str]


class FlipRequest(BaseModel):
    position: int = Field(..., ge=0)


class FlipResponse(BaseModel):
    session_id: str
    position: int
    tile_face: int
    second_flip_result: str | None = None
    matched: bool
    flip_count: int
    matched_positions: list[int]
    all_matched: bool


class CompleteRequest(BaseModel):
    flips: int = Field(..., ge=1)
    time_ms: int = Field(..., ge=0)


class CompleteResponse(BaseModel):
    session_id: str
    won: bool
    already_completed: bool
    flip_count: int
    time_ms: int | None = None
    score: int


@router.post("/sessions", response_model=CreateSessionResponse)
async def create_session(
    body: CreateSessionRequest,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> CreateSessionResponse:
    try:
        res = await svc.create_session(
            r,
            body.brand_id,
            current_user["sub"],
            difficulty=body.difficulty,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return CreateSessionResponse(**res)


@router.post("/sessions/{sid}/flip", response_model=FlipResponse)
async def flip(
    sid: str,
    body: FlipRequest,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> FlipResponse:
    try:
        res = await svc.flip(r, sid, current_user["sub"], body.position)
    except ValueError as exc:
        msg = str(exc)
        if msg == "session not found":
            raise HTTPException(status_code=404, detail=msg) from exc
        if msg == "not your session":
            raise HTTPException(status_code=403, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    return FlipResponse(**res)


@router.post("/sessions/{sid}/complete", response_model=CompleteResponse)
async def complete(
    sid: str,
    body: CompleteRequest,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> CompleteResponse:
    try:
        res = await svc.complete(
            r, sid, current_user["sub"], body.flips, body.time_ms,
        )
    except ValueError as exc:
        msg = str(exc)
        if msg == "session not found":
            raise HTTPException(status_code=404, detail=msg) from exc
        if msg == "not your session":
            raise HTTPException(status_code=403, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    return CompleteResponse(**res)
