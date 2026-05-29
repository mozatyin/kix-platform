"""Admin endpoints for multi-tenant isolation introspection + ops.

Endpoints:

  GET  /api/v1/admin/tenant/{brand_id}/usage
       → today's request count + total ms

  GET  /api/v1/admin/tenant/{brand_id}/limits
       → current tier + RPM limit

  POST /api/v1/admin/tenant/{brand_id}/circuit/reset
       → force-close a circuit breaker for one operation

Auth: shared pre-shared admin token (matches the convention used by
``brand_subscriptions.admin_run_billing_cron``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.config import settings
from app.middleware.tenant_isolation import (
    _TIER_RPM_LIMITS,
    DEFAULT_RPM_LIMIT,
    reset_circuit,
)
from app.redis_client import get_redis
from app.security import constant_time_eq

router = APIRouter()


def _date_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _check_admin(token: str) -> None:
    if not constant_time_eq(token, settings.jwt_secret):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "admin_token_invalid"},
        )


# ── Models ────────────────────────────────────────────────────────────────


class TenantUsageResponse(BaseModel):
    brand_id: str
    date: str
    requests: int
    total_ms: int
    avg_ms: float


class TenantLimitsResponse(BaseModel):
    brand_id: str
    tier: str
    rpm_limit: int


class CircuitResetRequest(BaseModel):
    admin_token: str = Field(..., min_length=8, max_length=512)
    operation: str = Field(..., min_length=1, max_length=128)


class CircuitResetResponse(BaseModel):
    brand_id: str
    operation: str
    reset: bool


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.get(
    "/tenant/{brand_id}/usage",
    response_model=TenantUsageResponse,
)
async def get_tenant_usage(
    brand_id: str,
    admin_token: str,
    r: Any = Depends(get_redis),
) -> TenantUsageResponse:
    """Return today's request count + cumulative ms for one brand."""
    _check_admin(admin_token)

    usage_key = f"tenant:usage:{brand_id}:{_date_today()}"
    raw = await r.hgetall(usage_key) or {}

    requests = int(raw.get("requests", 0) or 0)
    total_ms = int(raw.get("total_ms", 0) or 0)
    avg_ms = float(total_ms / requests) if requests else 0.0

    return TenantUsageResponse(
        brand_id=brand_id,
        date=_date_today(),
        requests=requests,
        total_ms=total_ms,
        avg_ms=round(avg_ms, 2),
    )


@router.get(
    "/tenant/{brand_id}/limits",
    response_model=TenantLimitsResponse,
)
async def get_tenant_limits(
    brand_id: str,
    admin_token: str,
    r: Any = Depends(get_redis),
) -> TenantLimitsResponse:
    """Return the brand's current tier and its derived RPM limit."""
    _check_admin(admin_token)

    try:
        from app.routers.brand_subscriptions import _get_brand_tier
        tier = await _get_brand_tier(r, brand_id)
    except Exception:
        tier = "free"

    return TenantLimitsResponse(
        brand_id=brand_id,
        tier=tier,
        rpm_limit=_TIER_RPM_LIMITS.get(tier, DEFAULT_RPM_LIMIT),
    )


@router.post(
    "/tenant/{brand_id}/circuit/reset",
    response_model=CircuitResetResponse,
)
async def post_tenant_circuit_reset(
    brand_id: str,
    body: CircuitResetRequest,
    r: Any = Depends(get_redis),
) -> CircuitResetResponse:
    """Manually clear the circuit breaker for one ``(brand_id, operation)``."""
    _check_admin(body.admin_token)
    await reset_circuit(brand_id, body.operation, r)
    return CircuitResetResponse(
        brand_id=brand_id,
        operation=body.operation,
        reset=True,
    )
