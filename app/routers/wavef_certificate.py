"""Wave F game-completion certificate router.

Endpoints:
    POST /api/v1/wavef/certificate/render  -> JSON {svg, verification_code}
    POST /api/v1/wavef/certificate/svg     -> raw image/svg+xml
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from app.deps import get_current_user
from app.services import wavef_certificate as svc


router = APIRouter()


class RenderRequest(BaseModel):
    player_name: str = Field(..., min_length=1, max_length=64)
    brand_name: str = Field(..., min_length=1, max_length=64)
    game_name: str = Field(..., min_length=1, max_length=64)
    score: int = Field(..., ge=0)
    primary_color: str = Field("#1F6FEB", pattern=r"^#[0-9A-Fa-f]{6}$")
    accent_color: str = Field("#FFD23F", pattern=r"^#[0-9A-Fa-f]{6}$")


class RenderResponse(BaseModel):
    svg: str
    verification_code: str


@router.post("/render", response_model=RenderResponse)
async def render_json(
    body: RenderRequest,
    current_user: dict = Depends(get_current_user),
) -> RenderResponse:
    svg, code = svc.render_svg(
        player_name=body.player_name,
        brand_name=body.brand_name,
        game_name=body.game_name,
        score=body.score,
        primary_color=body.primary_color,
        accent_color=body.accent_color,
    )
    return RenderResponse(svg=svg, verification_code=code)


@router.post("/svg")
async def render_svg(
    body: RenderRequest,
    current_user: dict = Depends(get_current_user),
) -> Response:
    svg, code = svc.render_svg(
        player_name=body.player_name,
        brand_name=body.brand_name,
        game_name=body.game_name,
        score=body.score,
        primary_color=body.primary_color,
        accent_color=body.accent_color,
    )
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"X-Verification-Code": code},
    )
