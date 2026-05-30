"""Audit log HTTP surface — admin search / export / retention dashboard.

All endpoints are admin-token gated (``X-Admin-Token`` header or
``admin_token`` query). The audit log is itself a PII attack surface
(it records who acted on whom, with IPs) so we never expose it to
merchants or end-users.

Endpoints
---------
* ``GET  /api/v1/audit/events`` — paginated search
* ``GET  /api/v1/audit/events/{event_id}`` — single event lookup
* ``POST /api/v1/audit/export`` — CSV download for regulator response
* ``GET  /api/v1/admin/audit/retention-status`` — per-region expiry stats

The router is additive — it does not touch any existing audit code in
``app/routers/compliance.py``. Once the Redis→PG importer has run and
the dual-write hooks are live, the legacy ``compliance:pii_audit:*``
LISTs can be deprecated independently.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.security import check_admin_token
from app.services import audit_log_service as svc

logger = logging.getLogger(__name__)

router = APIRouter()
admin_router = APIRouter()


# ── Auth helper ──────────────────────────────────────────────────────────


def _require_admin(admin_token: str | None) -> None:
    if not check_admin_token(admin_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "admin_token_invalid"},
        )


# ── Request / response models ────────────────────────────────────────────


class ExportBody(BaseModel):
    """POST body for ``/audit/export``.

    All filters are optional. ``from_ts`` / ``to_ts`` are ISO-8601
    strings (Pydantic coerces them to ``datetime``).
    """

    actor_id: str | None = Field(default=None, max_length=64)
    brand_id: str | None = Field(default=None, max_length=64)
    action: str | None = Field(default=None, max_length=64)
    jurisdiction: str | None = Field(default=None, max_length=8)
    from_ts: datetime | None = None
    to_ts: datetime | None = None
    max_rows: int = Field(default=100_000, ge=1, le=1_000_000)


# ── Search ───────────────────────────────────────────────────────────────


@router.get("/events")
async def list_events(
    actor_id: str | None = Query(default=None, max_length=64),
    brand_id: str | None = Query(default=None, max_length=64),
    action: str | None = Query(default=None, max_length=64),
    jurisdiction: str | None = Query(default=None, max_length=8),
    result: str | None = Query(default=None, max_length=32),
    from_ts: datetime | None = Query(default=None, alias="from"),
    to_ts: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    admin_token: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Paginated AND-filtered audit search.

    Returns ``{"events": [...], "limit": int, "offset": int}``. Ordered
    by ``ts DESC`` (most recent first) which matches the admin "what
    just happened?" mental model.
    """
    _require_admin(x_admin_token or admin_token)

    rows = await svc.query(
        db,
        actor_id=actor_id,
        brand_id=brand_id,
        action=action,
        jurisdiction=jurisdiction,
        result=result,
        from_ts=from_ts,
        to_ts=to_ts,
        limit=limit,
        offset=offset,
    )
    return {
        "events": [r.to_dict() for r in rows],
        "limit": limit,
        "offset": offset,
        "count": len(rows),
    }


@router.get("/events/{event_id}")
async def get_event(
    event_id: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    admin_token: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Single-event lookup. 404 when no event with that ID exists."""
    _require_admin(x_admin_token or admin_token)

    row = await svc.get_event(db, event_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "event_not_found", "event_id": event_id},
        )
    return row.to_dict()


# ── CSV export ───────────────────────────────────────────────────────────


@router.post("/export")
async def export_events(
    body: ExportBody,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    admin_token: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Stream a CSV of matching events. ``Content-Disposition: attachment``.

    Intended for regulator-response packs. Ordering is ascending so the
    CSV reads as a chronological narrative.
    """
    _require_admin(x_admin_token or admin_token)

    csv_text = await svc.export_csv(
        db,
        actor_id=body.actor_id,
        brand_id=body.brand_id,
        action=body.action,
        jurisdiction=body.jurisdiction,
        from_ts=body.from_ts,
        to_ts=body.to_ts,
        max_rows=body.max_rows,
    )

    filename = (
        f"audit-export-"
        f"{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.csv"
    )
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


# ── Retention dashboard (admin namespace) ────────────────────────────────


@admin_router.get("/retention-status")
async def retention_status(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    admin_token: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Per-jurisdiction summary: total / expired / earliest+latest expiry.

    Drives the admin dashboard tile that warns when a region has a
    growing backlog of expired rows still in the table (e.g. the purge
    cron is wedged).
    """
    _require_admin(x_admin_token or admin_token)
    return {"jurisdictions": await svc.retention_status(db)}
