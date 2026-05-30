"""Voucher Pools router — cross-brand network voucher pooling.

Mounted by ``app/main.py`` at the prefix ``/api/v1/voucher-pools``.

The endpoints are intentionally thin: every operation delegates to
:mod:`app.services.voucher_pool`. Keeping logic out of the router makes
the settlement worker, internal tools, and the test suite share the
same execution path. The router's only jobs are:

  * request validation via Pydantic,
  * mapping service-layer error sentinels onto HTTP status codes,
  * audit-log fire-and-forget for the "interesting" mutations
    (create / join / leave / redeem).

This is the user-visible API for KiX's killer differentiator: a voucher
won by playing a game at Toast Box becomes redeemable at Ya Kun in the
same district. See ``app/services/voucher_pool.py`` for the data model.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
import redis.asyncio as aioredis

from app.redis_client import get_redis
from app.services import voucher_pool as vp
from app.services.audit_log_service import record_event_fire_and_forget

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Request / response models ────────────────────────────────────────────


class CreatePoolRequest(BaseModel):
    brand_ids: list[str] = Field(
        ..., min_length=1, max_length=500,
        description="Founding member brand IDs",
    )
    district: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=200)
    rules: dict[str, Any] = Field(default_factory=dict)
    discoverable: bool = True

    @field_validator("brand_ids")
    @classmethod
    def _validate_members(cls, v: list[str]) -> list[str]:
        cleaned = [b for b in v if isinstance(b, str) and b.strip()]
        if not cleaned:
            raise ValueError("brand_ids must contain at least one non-empty entry")
        return cleaned


class JoinRequest(BaseModel):
    brand_id: str = Field(..., min_length=1, max_length=128)


class IssueVoucherRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    source_brand_id: str = Field(..., min_length=1, max_length=128)
    amount_cents: int = Field(..., gt=0, le=1_000_000_00)
    currency: str = Field(default="SGD", min_length=3, max_length=8)
    expires_at: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RedeemRequest(BaseModel):
    voucher_id: str = Field(..., min_length=1, max_length=128)
    target_brand_id: str = Field(..., min_length=1, max_length=128)
    transaction_id: str = Field(..., min_length=1, max_length=128)
    target_context: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=128)


# ── Pool CRUD ────────────────────────────────────────────────────────────


@router.post(
    "/create",
    summary="Create a new cross-brand voucher pool",
    status_code=status.HTTP_201_CREATED,
)
async def create_pool_endpoint(
    body: CreatePoolRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    try:
        out = await vp.create_pool(
            r,
            brand_ids=body.brand_ids,
            district=body.district,
            name=body.name,
            rules=body.rules,
            discoverable=body.discoverable,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_request", "message": str(exc)},
        )
    await record_event_fire_and_forget(
        actor_id="system",
        actor_type="service",
        action="voucher_pool.create",
        target_type="voucher_pool",
        target_id=out["pool_id"],
        payload={
            "name": out["name"],
            "district": out["district"],
            "members": out["members"],
        },
        result="ok",
    )
    return out


@router.get(
    "/discovery",
    summary="Public list of pools (newest first)",
)
async def discovery_endpoint(
    limit: int = Query(default=50, ge=1, le=200),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Discovery is declared **before** ``GET /{pool_id}`` so the literal
    ``/discovery`` path doesn't get swallowed by the path-param matcher."""
    pools = await vp.discovery(r, limit=limit)
    return {"count": len(pools), "pools": pools}


@router.get(
    "/voucher/{voucher_id}/redemption-options",
    summary="List the shops where this voucher is accepted",
)
async def redemption_options_endpoint(
    voucher_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Declared early so ``/voucher/...`` cannot be eaten by other
    path-param routes registered below."""
    out = await vp.redemption_options(r, voucher_id)
    if not out.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=out,
        )
    return out


@router.get(
    "/{pool_id}",
    summary="Get a pool's config + membership",
)
async def get_pool_endpoint(
    pool_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    pool = await vp.get_pool(r, pool_id)
    if not pool:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "pool_not_found", "pool_id": pool_id},
        )
    return pool


@router.post(
    "/{pool_id}/join",
    summary="Brand opts into an existing pool",
)
async def join_pool_endpoint(
    pool_id: str,
    body: JoinRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    try:
        out = await vp.join_pool(r, pool_id, body.brand_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "pool_not_found", "message": str(exc)},
        )
    await record_event_fire_and_forget(
        actor_id=body.brand_id,
        actor_type="brand",
        action="voucher_pool.join",
        target_type="voucher_pool",
        target_id=pool_id,
        brand_id=body.brand_id,
        result="ok",
    )
    return out


@router.post(
    "/{pool_id}/leave",
    summary="Brand exits a pool (outstanding vouchers stay redeemable)",
)
async def leave_pool_endpoint(
    pool_id: str,
    body: JoinRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    out = await vp.leave_pool(r, pool_id, body.brand_id)
    await record_event_fire_and_forget(
        actor_id=body.brand_id,
        actor_type="brand",
        action="voucher_pool.leave",
        target_type="voucher_pool",
        target_id=pool_id,
        brand_id=body.brand_id,
        result="ok",
    )
    return out


# ── Voucher issue / redeem ───────────────────────────────────────────────


@router.post(
    "/{pool_id}/issue-voucher",
    summary="Issue a pool-redeemable voucher to a user",
    status_code=status.HTTP_201_CREATED,
)
async def issue_voucher_endpoint(
    pool_id: str,
    body: IssueVoucherRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    try:
        out = await vp.issue_pooled_voucher(
            r,
            user_id=body.user_id,
            source_brand_id=body.source_brand_id,
            pool_id=pool_id,
            amount_cents=body.amount_cents,
            currency=body.currency,
            expires_at=body.expires_at,
            metadata=body.metadata,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "pool_not_found", "message": str(exc)},
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "brand_not_in_pool", "message": str(exc)},
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_request", "message": str(exc)},
        )
    return out


@router.post(
    "/{pool_id}/redeem",
    summary="Redeem a pooled voucher at a target brand",
)
async def redeem_voucher_endpoint(
    pool_id: str,
    body: RedeemRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    out = await vp.record_redemption(
        r,
        voucher_id=body.voucher_id,
        target_brand_id=body.target_brand_id,
        transaction_id=body.transaction_id,
        target_context=body.target_context,
        idempotency_key=body.idempotency_key,
    )
    if not out.get("ok"):
        # 409 on contention, 400 on validation rejection. Distinguish
        # explicitly so retry semantics on the client are sane.
        reason = out.get("reason", "")
        if reason == "contention":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=out,
            )
        if reason == "voucher_not_found" or reason == "pool_not_found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=out,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=out,
        )

    # Audit only first-write events, never replays — keeps the audit
    # log readable for compliance + drives the SapFix dashboards.
    if not out.get("idempotent"):
        await record_event_fire_and_forget(
            actor_id=body.target_brand_id,
            actor_type="brand",
            action="voucher_pool.redeem",
            target_type="pooled_voucher",
            target_id=body.voucher_id,
            brand_id=body.target_brand_id,
            payload={
                "pool_id": out.get("pool_id"),
                "source_brand_id": out.get("source_brand_id"),
                "credit_amount_cents": out.get("credit_amount_cents"),
                "transaction_id": body.transaction_id,
            },
            result="ok",
        )
    return out


# ── Settlement view + discovery ──────────────────────────────────────────


@router.get(
    "/{brand_id}/net-position",
    summary="Settlement view: net position across all pools",
)
async def net_position_endpoint(
    brand_id: str,
    pool_id: str | None = Query(
        default=None,
        description="Restrict to a single pool",
    ),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    if pool_id:
        try:
            return await vp.net_position(r, pool_id=pool_id, brand_id=brand_id)
        except LookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "pool_not_found", "message": str(exc)},
            )
    return await vp.compute_pool_value(r, brand_id)


# ── Settlement admin (typically only callable by the cron worker) ────────


@router.post(
    "/{pool_id}/settle",
    summary="(Internal) compute + persist a settlement snapshot",
)
async def settle_endpoint(
    pool_id: str,
    week: int | None = Body(default=None, embed=True),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    try:
        return await vp.snapshot_settlement(r, pool_id, week=week)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "pool_not_found", "message": str(exc)},
        )


@router.get(
    "/{pool_id}/settlement/{week}",
    summary="Read a persisted settlement snapshot",
)
async def get_settlement_endpoint(
    pool_id: str,
    week: int,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    snap = await vp.get_settlement_snapshot(r, pool_id, week)
    if snap is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "snapshot_not_found", "pool_id": pool_id, "week": week},
        )
    return snap
