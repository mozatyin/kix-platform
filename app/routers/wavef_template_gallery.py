"""Wave F campaign template gallery router — BRAME pattern.

Endpoints:
    GET  /api/v1/wavef/templates                 query: vertical, mechanic, region
    GET  /api/v1/wavef/templates/{tid}
    POST /api/v1/wavef/templates/{tid}/clone     body: {brand_id, overrides}

NEW file — no existing module touched.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

import redis.asyncio as aioredis

from app.deps import get_current_user
from app.redis_client import get_redis
from app.services import wavef_template_gallery as svc


router = APIRouter()


class TemplateItem(BaseModel):
    id: str
    vertical: str
    region: str = "global"
    mechanics: list[str] = Field(default_factory=list)
    reward_floor: float = 0.0
    duration_days: int = 0
    expected_kpis: dict = Field(default_factory=dict)
    default_terms_template: Optional[str] = None
    description: Optional[str] = None


class ListResponse(BaseModel):
    items: list[TemplateItem]
    count: int


class CloneRequest(BaseModel):
    brand_id: str
    overrides: Optional[dict] = None


class CloneResponse(BaseModel):
    campaign_id: str
    template_id: str
    brand_id: str
    merged: dict


def _to_item(d: dict) -> TemplateItem:
    return TemplateItem(
        id=d["id"],
        vertical=d.get("vertical", ""),
        region=d.get("region", "global"),
        mechanics=list(d.get("mechanics") or []),
        reward_floor=float(d.get("reward_floor", 0.0)),
        duration_days=int(d.get("duration_days", 0)),
        expected_kpis=dict(d.get("expected_kpis") or {}),
        default_terms_template=d.get("default_terms_template"),
        description=d.get("description"),
    )


@router.get("", response_model=ListResponse)
async def list_templates_endpoint(
    vertical: Optional[str] = Query(None),
    mechanic: Optional[str] = Query(None),
    region: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> ListResponse:
    items = await svc.list_templates(
        r, vertical=vertical, mechanic=mechanic, region=region
    )
    return ListResponse(
        items=[_to_item(d) for d in items], count=len(items)
    )


@router.get("/{tid}", response_model=TemplateItem)
async def get_template_endpoint(
    tid: str,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> TemplateItem:
    await svc.ensure_loaded(r)
    tpl = await svc.get_template(r, tid)
    if tpl is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="template not found"
        )
    return _to_item(tpl)


@router.post("/{tid}/clone", response_model=CloneResponse)
async def clone_template_endpoint(
    tid: str,
    body: CloneRequest,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> CloneResponse:
    await svc.ensure_loaded(r)
    try:
        res = await svc.clone_template(
            r, tid, brand_id=body.brand_id, overrides=body.overrides
        )
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="template not found"
        )
    return CloneResponse(**res)
