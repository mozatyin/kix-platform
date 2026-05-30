"""Re-engagement HTTP surface — Wave E Step 5.

Thin FastAPI shell over :mod:`app.services.reengagement_orchestrator`.
Routes:

  POST /api/v1/reengagement/{brand_id}/start-cascade    — manual fire
  GET  /api/v1/reengagement/{brand_id}/cascade-stats    — counters
  GET  /api/v1/reengagement/{brand_id}/at-risk-cohort   — current cascades
  POST /api/v1/admin/reengagement/test-cascade          — force full flow

Admin auth follows the alpha_program / email_admin convention:
``X-Admin-Token`` header or ``?admin_token=`` query, validated against
``KIX_ADMIN_TOKEN`` env (fallback ``admin-dev-token`` in dev).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.redis_client import get_redis
from app.security import constant_time_eq
from app.services.reengagement_orchestrator import (
    CASCADE_BLUEPRINTS,
    at_risk_cohort,
    cascade_stats,
    send_cascade,
    start_cascade,
)

logger = logging.getLogger(__name__)
router = APIRouter()
admin_router = APIRouter()

ADMIN_TOKEN_DEFAULT = "admin-dev-token"


def _check_admin(token: Optional[str]) -> None:
    if not token:
        raise HTTPException(status_code=403, detail="admin_token_required")
    expected = os.getenv("KIX_ADMIN_TOKEN", ADMIN_TOKEN_DEFAULT)
    if not constant_time_eq(token, expected):
        raise HTTPException(status_code=403, detail="invalid_admin_token")


def _admin_token_from_request(request: Request) -> Optional[str]:
    qs = request.query_params.get("admin_token")
    if qs:
        return qs
    return request.headers.get("x-admin-token")


# ── Request models ───────────────────────────────────────────────────


class StartCascadeBody(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    cascade_type: str = Field(..., min_length=1, max_length=32)
    locale: str = Field(default="en-SG", max_length=16)
    brand_name: Optional[str] = Field(default=None, max_length=128)


class TestCascadeBody(BaseModel):
    admin_token: str = Field(..., min_length=1, max_length=512)
    brand_id: str = Field(..., min_length=1, max_length=128)
    user_id: str = Field(..., min_length=1, max_length=128)
    cascade_type: str = Field(default="light", min_length=1, max_length=32)
    locale: str = Field(default="en-SG", max_length=16)
    brand_name: Optional[str] = Field(default=None, max_length=128)
    fast_forward: bool = Field(
        default=True,
        description="Walk all cascade steps in one call (test mode).",
    )


# ── Public-ish endpoints (brand-scoped, no global admin token) ──────────
#
# These are scoped to a brand and intended to be called by the brand's
# own merchant portal. They should be gated by the merchant session
# middleware in production — for now they accept the same admin token
# header so unit tests + ops can hit them directly.


@router.post("/{brand_id}/start-cascade")
async def start_cascade_route(
    brand_id: str, body: StartCascadeBody, request: Request,
) -> dict[str, Any]:
    """Open a re-engagement cascade for one user. Idempotent."""
    _check_admin(_admin_token_from_request(request))
    if body.cascade_type not in CASCADE_BLUEPRINTS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown cascade_type: {body.cascade_type}",
        )
    r = await get_redis()
    result = await start_cascade(
        r,
        brand_id=brand_id,
        user_id=body.user_id,
        cascade_type=body.cascade_type,
    )
    return {"brand_id": brand_id, **result}


@router.get("/{brand_id}/cascade-stats")
async def cascade_stats_route(brand_id: str, request: Request) -> dict[str, Any]:
    """Aggregate counters: started / sent / suppressed / at-risk size."""
    _check_admin(_admin_token_from_request(request))
    r = await get_redis()
    return await cascade_stats(r, brand_id)


@router.get("/{brand_id}/at-risk-cohort")
async def at_risk_route(
    brand_id: str, request: Request, limit: int = 200,
) -> dict[str, Any]:
    """List users currently in a cascade."""
    _check_admin(_admin_token_from_request(request))
    r = await get_redis()
    return await at_risk_cohort(r, brand_id, limit=max(1, min(limit, 1000)))


# ── Admin endpoint (token in body, for ops scripts) ────────────────────


@admin_router.post("/test-cascade")
async def test_cascade_route(body: TestCascadeBody) -> dict[str, Any]:
    """Force a full cascade walk for a single user. Bypasses calendar gaps."""
    _check_admin(body.admin_token)
    if body.cascade_type not in CASCADE_BLUEPRINTS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown cascade_type: {body.cascade_type}",
        )
    r = await get_redis()
    blueprint = CASCADE_BLUEPRINTS[body.cascade_type]
    started = await start_cascade(
        r,
        brand_id=body.brand_id,
        user_id=body.user_id,
        cascade_type=body.cascade_type,
    )
    sent = await send_cascade(
        r,
        brand_id=body.brand_id,
        user_id=body.user_id,
        cascade_id=started.get("cascade_id"),
        locale=body.locale,
        brand_name=body.brand_name,
        max_steps=len(blueprint.steps) if body.fast_forward else 1,
        now=time.time(),
    )
    return {
        "brand_id": body.brand_id,
        "user_id": body.user_id,
        "cascade_started": started,
        "cascade_sent": sent,
    }
