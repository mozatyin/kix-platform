"""Wave F mechanic-registry router — CataBoom-style mechanic catalog.

Read-only catalog of game mechanics: their categories, parameters,
payload types and supported regions. Used by the template gallery and
the campaign wizard to populate dropdowns and validate config.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.services import wavef_mechanics as svc


router = APIRouter()


@router.get("")
async def list_mechanics(
    category: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    region: Optional[str] = Query(default=None),
) -> dict:
    """List mechanics with optional filter facets.

    Mounted as ``GET /api/v1/wavef/mechanics`` (also ``/`` for trailing
    slash) — uniform list-response envelope.
    """
    items = svc.list_mechanics(category=category, status=status, region=region)
    return {
        "items": items,
        "count": len(items),
        "total": len(svc.REGISTRY),
        "has_more": False,
        "limit": len(items),
        "offset": 0,
    }


@router.get("/")
async def list_mechanics_slash(
    category: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    region: Optional[str] = Query(default=None),
) -> dict:
    return await list_mechanics(category=category, status=status, region=region)


@router.get("/{mechanic_id}")
async def get_mechanic(mechanic_id: str) -> dict:
    m = svc.get_mechanic(mechanic_id)
    if m is None:
        raise HTTPException(status_code=404, detail="mechanic not found")
    return m


@router.get("/{mechanic_id}/schema")
async def get_schema(mechanic_id: str) -> dict:
    schema = svc.get_schema(mechanic_id)
    if schema is None:
        raise HTTPException(status_code=404, detail="mechanic not found")
    return schema
