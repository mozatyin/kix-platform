"""Wave F animation library router — Flarie prize-reveal pattern.

Endpoints:
    GET /api/v1/wavef/animations          list of registered primitives
    GET /api/v1/wavef/animations/{pid}    single primitive metadata
    GET /api/v1/wavef/animations/palette  ?brand_primary=#aabbcc

The actual CSS + ``KiXFx`` JS shim live under ``landing/sdk/animations/``
and ``landing/sdk/kix-fx.js`` and are served by the existing static-file
mount (no API call needed at render-time).

NEW file — no existing module touched.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.deps import get_current_user
from app.services import wavef_animation_library as svc


router = APIRouter()


class PrimitiveItem(BaseModel):
    id: str
    css_path: str
    js_entry: str
    default_ms: int
    reduced_motion_safe: bool
    description: str
    asset_present: bool = False


class ListResponse(BaseModel):
    items: list[PrimitiveItem]
    count: int
    fx_js_present: bool


class PaletteResponse(BaseModel):
    brand_primary: Optional[str] = None
    palette: list[str]


@router.get("", response_model=ListResponse)
async def list_endpoint(
    current_user: dict = Depends(get_current_user),
) -> ListResponse:
    items: list[PrimitiveItem] = []
    for d in svc.list_primitives():
        items.append(
            PrimitiveItem(
                **d, asset_present=svc.asset_exists(d["id"])
            )
        )
    return ListResponse(
        items=items, count=len(items), fx_js_present=svc.fx_js_exists()
    )


@router.get("/palette", response_model=PaletteResponse)
async def palette_endpoint(
    brand_primary: Optional[str] = Query(None, pattern=r"^#?[0-9a-fA-F]{6}$"),
    current_user: dict = Depends(get_current_user),
) -> PaletteResponse:
    bp = brand_primary
    if bp and not bp.startswith("#"):
        bp = "#" + bp
    return PaletteResponse(
        brand_primary=bp, palette=svc.palette_for(bp)
    )


@router.get("/{pid}", response_model=PrimitiveItem)
async def get_endpoint(
    pid: str,
    current_user: dict = Depends(get_current_user),
) -> PrimitiveItem:
    d = svc.get_primitive(pid)
    if not d:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="animation primitive not found",
        )
    return PrimitiveItem(**d, asset_present=svc.asset_exists(pid))
