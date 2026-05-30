"""Wave F email/SMS capture-gate router — BRAME-style lead capture.

Endpoints (mounted at ``/api/v1/wavef/capture``):

* ``POST /submit``                   — record a contact with consent
* ``GET  /{campaign_id}/export``     — CSV download (admin-token)
* ``POST /optout``                   — add a contact to brand's optout

The service layer encrypts plaintext at rest, hashes the contact for
key lookup, and honours the per-brand opt-out set. See
``app/services/wavef_capture.py`` for the storage model.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

import redis.asyncio as aioredis

from app.deps import get_current_user
from app.redis_client import get_redis
from app.services import wavef_capture as svc


router = APIRouter()


# ── Models ───────────────────────────────────────────────────────────────


class SubmitRequest(BaseModel):
    campaign_id: str = Field(..., min_length=1)
    email: Optional[str] = None
    phone: Optional[str] = None
    sms_opt_in: bool = False
    marketing_opt_in: bool = False


class SubmitResponse(BaseModel):
    accepted: bool
    idempotent: bool = False
    reason: Optional[str] = None


class OptoutRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    email: Optional[str] = None
    phone: Optional[str] = None


class OptoutResponse(BaseModel):
    added: int


# ── Admin helper ─────────────────────────────────────────────────────────


def _require_admin(x_admin_token: Optional[str]) -> None:
    """Hand-rolled admin gate — matches existing wavef admin pattern.

    Reads ``KIX_ADMIN_TOKEN`` (default ``admin-dev-token`` for tests).
    """
    expected = os.environ.get("KIX_ADMIN_TOKEN", "admin-dev-token")
    if not x_admin_token or x_admin_token != expected:
        raise HTTPException(status_code=403, detail="admin token required")


# ── Routes ───────────────────────────────────────────────────────────────


@router.post("/submit", response_model=SubmitResponse)
async def submit(
    body: SubmitRequest,
    current_user: dict = Depends(get_current_user),
    r: aioredis.Redis = Depends(get_redis),
) -> SubmitResponse:
    """Capture a contact. Brand-id comes from the JWT, not the body."""
    brand_id = current_user.get("brand_id") or "unknown"
    try:
        res = await svc.submit(
            r,
            campaign_id=body.campaign_id,
            brand_id=brand_id,
            email=body.email,
            phone=body.phone,
            sms_opt_in=body.sms_opt_in,
            marketing_opt_in=body.marketing_opt_in,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SubmitResponse(**res)


@router.get("/{campaign_id}/export", response_class=PlainTextResponse)
async def export(
    campaign_id: str,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    r: aioredis.Redis = Depends(get_redis),
) -> PlainTextResponse:
    _require_admin(x_admin_token)
    rows = await svc.export_records(r, campaign_id)
    csv_text = svc.to_csv(rows)
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="capture_{campaign_id}.csv"'
            ),
        },
    )


@router.post("/optout", response_model=OptoutResponse)
async def optout(
    body: OptoutRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> OptoutResponse:
    """Public opt-out — no auth, to honour one-click unsubscribe links."""
    try:
        res = await svc.optout(
            r,
            brand_id=body.brand_id,
            email=body.email,
            phone=body.phone,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OptoutResponse(**res)
