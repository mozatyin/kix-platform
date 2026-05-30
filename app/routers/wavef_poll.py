"""Wave F quick-poll router — BRAME-style pre-game engagement widget.

Endpoints:
    POST /api/v1/wavef/poll/                    create poll
    GET  /api/v1/wavef/poll/{poll_id}           read poll meta
    POST /api/v1/wavef/poll/{poll_id}/vote      cast vote
    GET  /api/v1/wavef/poll/{poll_id}/results   read results
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field

import redis.asyncio as aioredis

from app.deps import get_current_user
from app.redis_client import get_redis
from app.services import wavef_poll as svc


router = APIRouter()


class CreatePollRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    question: str = Field(..., min_length=1, max_length=280)
    options: list[str] = Field(..., min_length=2, max_length=8)


class OptionItem(BaseModel):
    id: str
    label: str


class PollMeta(BaseModel):
    poll_id: str
    question: str
    brand_id: str
    created_at_ms: int
    options: list[OptionItem]


class VoteRequest(BaseModel):
    option_id: str = Field(..., min_length=1)


class VoteResponse(BaseModel):
    accepted: bool
    totals: dict[str, int]


class ResultsResponse(PollMeta):
    totals: dict[str, int]
    total_voters: int


@router.post("/", response_model=PollMeta)
async def create_poll(
    body: CreatePollRequest,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> PollMeta:
    try:
        res = await svc.create_poll(r, body.brand_id, body.question, body.options)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return PollMeta(**res)


@router.get("/{poll_id}", response_model=PollMeta)
async def read_poll(
    poll_id: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> PollMeta:
    res = await svc.get_poll(r, poll_id)
    if res is None:
        raise HTTPException(status_code=404, detail="poll not found")
    return PollMeta(**res)


@router.post("/{poll_id}/vote", response_model=VoteResponse)
async def vote(
    poll_id: str,
    body: VoteRequest,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> VoteResponse:
    try:
        res = await svc.vote(r, poll_id, current_user["sub"], body.option_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return VoteResponse(**res)


@router.get("/{poll_id}/results", response_model=ResultsResponse)
async def results(
    poll_id: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> ResultsResponse:
    res = await svc.results(r, poll_id)
    if res is None:
        raise HTTPException(status_code=404, detail="poll not found")
    return ResultsResponse(**res)
