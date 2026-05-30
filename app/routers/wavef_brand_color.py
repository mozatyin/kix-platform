"""Wave F brand-color router — Flarie/Playable-style logo-to-theme.

Endpoint:
    POST /api/v1/wavef/brand-color/extract   multipart logo upload
        returns {dominant: [...], palette: {primary, accent, text_on_primary}}

Auth: standard JWT (marketers upload their brand assets).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.deps import get_current_user
from app.services import wavef_brand_color as svc


router = APIRouter()


_MAX_BYTES = 8 * 1024 * 1024  # 8 MB


class PaletteSlice(BaseModel):
    primary: str
    accent: str
    text_on_primary: str


class ExtractResponse(BaseModel):
    dominant: list[str]
    palette: PaletteSlice


@router.post("/extract", response_model=ExtractResponse)
async def extract_palette_endpoint(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
) -> ExtractResponse:
    data = await file.read()
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="empty file",
        )
    if len(data) > _MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file > {_MAX_BYTES} bytes",
        )
    try:
        res = svc.extract_palette(data, k=3)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return ExtractResponse(
        dominant=res["dominant"],
        palette=PaletteSlice(**res["palette"]),
    )
