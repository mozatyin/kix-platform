"""Saga HTTP surface — run cross-module compensating transactions.

Endpoints
---------
- ``POST /api/v1/saga/refund-cascade``: kick off the refund saga
- ``POST /api/v1/saga/subscription-upgrade``: kick off the upgrade saga
- ``GET  /api/v1/saga/{saga_id}``: read saga status + journal
- ``POST /api/v1/saga/admin/retry-failed``: re-run rolled-back sagas
  from the last 24h (admin only)
"""

from __future__ import annotations

import logging
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.redis_client import get_redis
from app.saga import FAILED_INDEX_KEY, SagaCoordinator
from app.saga_definitions import (
    refund_cascade_saga,
    subscription_upgrade_saga,
)
from app.security import check_admin_token

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Request models ───────────────────────────────────────────────────────
class RefundCascadeRequest(BaseModel):
    transaction_id: str = Field(..., description="charge_id to refund")
    refund_amount_cents: int = Field(..., gt=0)
    brand_id: str = Field(..., min_length=1)
    conversion_id: str | None = None
    reason: str | None = None


class SubscriptionUpgradeRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    new_tier: str = Field(..., min_length=1)
    upgrade_price_cents: int = Field(..., ge=0)
    features: list[str] | None = None


class AdminRetryRequest(BaseModel):
    admin_token: str
    limit: int = Field(50, ge=1, le=500)


# ── POST /refund-cascade ─────────────────────────────────────────────────
@router.post("/refund-cascade")
async def http_refund_cascade(
    body: RefundCascadeRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Run the refund-cascade saga end-to-end.

    Returns 200 with the SagaResult on success, 502 with the result on
    a clean rollback, and 500 if compensation itself failed (admin
    intervention required).
    """
    result = await refund_cascade_saga(
        r=r,
        charge_id=body.transaction_id,
        brand_id=body.brand_id,
        refund_amount_cents=body.refund_amount_cents,
        conversion_id=body.conversion_id,
        reason=body.reason,
    )

    if result.success:
        return {"ok": True, **result.to_dict()}

    if result.compensation_failures:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "saga_failed_compensation_failed",
                **result.to_dict(),
            },
        )

    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "error": "saga_failed_rolled_back",
            **result.to_dict(),
        },
    )


# ── POST /subscription-upgrade ───────────────────────────────────────────
@router.post("/subscription-upgrade")
async def http_subscription_upgrade(
    body: SubscriptionUpgradeRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    result = await subscription_upgrade_saga(
        r=r,
        brand_id=body.brand_id,
        new_tier=body.new_tier,
        upgrade_price_cents=body.upgrade_price_cents,
        features=body.features,
    )

    if result.success:
        return {"ok": True, **result.to_dict()}

    if result.compensation_failures:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "saga_failed_compensation_failed",
                **result.to_dict(),
            },
        )

    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "error": "saga_failed_rolled_back",
            **result.to_dict(),
        },
    )


# ── GET /{saga_id} ───────────────────────────────────────────────────────
@router.get("/{saga_id}")
async def http_get_saga(
    saga_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    coordinator = SagaCoordinator(r)
    snapshot = await coordinator.get_status(saga_id)
    if not snapshot.get("found"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "saga_not_found", "saga_id": saga_id},
        )
    return snapshot


# ── POST /admin/retry-failed ─────────────────────────────────────────────
@router.post("/admin/retry-failed")
async def http_admin_retry_failed(
    body: AdminRetryRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """List failed sagas from the last 24h.

    NOTE: This endpoint does NOT auto-replay actions — replaying a saga
    blindly can double-charge or double-credit. Instead it surfaces the
    failed sagas so an operator can inspect each journal and decide
    whether to retry, hand-fix, or close. Use ``GET /api/v1/saga/{id}``
    on each surfaced ID for the full picture.
    """
    if not check_admin_token(body.admin_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "invalid_admin_token"},
        )

    saga_ids = await r.zrevrange(FAILED_INDEX_KEY, 0, body.limit - 1)
    coordinator = SagaCoordinator(r)
    items: list[dict] = []
    for sid in saga_ids:
        snap = await coordinator.get_status(sid)
        if snap.get("found"):
            items.append(
                {
                    "saga_id": sid,
                    "meta": snap.get("meta", {}),
                }
            )
    return {"ok": True, "count": len(items), "items": items}
