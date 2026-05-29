"""Brand translation HTTP surface — merchant + admin endpoints.

Endpoints
---------
* ``GET  /api/v1/brands/{bid}/translations``
* ``GET  /api/v1/brands/{bid}/translations/{field}/{locale}``
* ``PUT  /api/v1/brands/{bid}/translations/{field}/{locale}``
* ``POST /api/v1/brands/{bid}/translations/auto``
* ``GET  /api/v1/admin/translations/review-queue``
* ``POST /api/v1/admin/translations/mark-reviewed``

Auth model
----------
Per-brand writes accept either:
* ``X-Owner-Id: <brand_id>`` — the brand owner editing their own copy.
* ``X-Admin-Token: <token>`` — platform admin override.

Admin-only endpoints check the token via :func:`app.security.check_admin_token`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.security import check_admin_token
from app.services import brand_translation_service as svc

logger = logging.getLogger(__name__)

router = APIRouter()
admin_router = APIRouter()


# ── Request models ──────────────────────────────────────────────────────


class PutTranslationBody(BaseModel):
    value: str = Field(..., min_length=1, max_length=8192)
    auto_translated: bool = False


class AutoTranslateBody(BaseModel):
    target_locales: list[str] = Field(
        default_factory=lambda: list(svc.SG_LAUNCH_LOCALES),
        description="BCP-47 locales to translate into",
    )
    source_locale: str = Field(default="en-US")
    source_fields: dict[str, str] | None = Field(
        default=None,
        description="Optional explicit source field map; falls back to DB",
    )


class MarkReviewedBody(BaseModel):
    admin_token: str = Field(..., min_length=8, max_length=512)
    brand_id: str = Field(..., min_length=1)
    field: str = Field(..., min_length=1)
    locale: str = Field(..., min_length=2, max_length=16)
    reviewer_id: str = Field(..., min_length=1)


# ── Authorisation helpers ───────────────────────────────────────────────


def _authorise_brand_write(
    brand_id: str,
    x_owner_id: str | None,
    x_admin_token: str | None,
) -> str:
    """Return the actor ID (owner or 'admin'), else raise 403.

    Per spec: brand owner OR admin token may write translations.
    """
    if x_admin_token and check_admin_token(x_admin_token):
        return "admin"
    if x_owner_id and x_owner_id == brand_id:
        return f"owner:{brand_id}"
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error": "forbidden", "reason": "owner_or_admin_required"},
    )


def _require_admin(admin_token: str | None) -> None:
    if not check_admin_token(admin_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "admin_token_invalid"},
        )


# ── Brand-scoped read/write ─────────────────────────────────────────────


@router.get("/{bid}/translations")
async def list_brand_translations(
    bid: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List every translation row for a brand."""
    rows = await svc.list_translations_for_brand(db, bid)
    return {"brand_id": bid, "count": len(rows), "translations": [r.to_dict() for r in rows]}


@router.get("/{bid}/translations/{field}/{locale}")
async def get_brand_translation(
    bid: str,
    field: str,
    locale: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Fetch a single translated field, walking the fallback chain."""
    if not svc.validate_locale(locale):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "invalid_locale", "locale": locale},
        )
    value = await svc.get_translation(db, bid, field, locale)
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "not_found",
                "brand_id": bid,
                "field": field,
                "locale": locale,
            },
        )
    return {
        "brand_id": bid,
        "field": field,
        "locale": locale,
        "value": value,
    }


@router.put("/{bid}/translations/{field}/{locale}")
async def put_brand_translation(
    bid: str,
    field: str,
    locale: str,
    body: PutTranslationBody,
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    x_admin_token: str | None = Header(None, alias="X-Admin-Token"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Upsert a translation (auth: brand owner OR admin)."""
    actor = _authorise_brand_write(bid, x_owner_id, x_admin_token)
    if not svc.validate_locale(locale):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "invalid_locale", "locale": locale},
        )
    # Admin writes auto-flag reviewed=True via reviewer_id; owner edits
    # likewise are considered reviewed (the owner IS the source of
    # truth for their own brand copy).
    reviewer = actor
    row = await svc.set_translation(
        db,
        brand_id=bid,
        field=field,
        locale=locale,
        value=body.value,
        auto=bool(body.auto_translated),
        reviewer=reviewer,
    )
    return {"ok": True, "translation": row.to_dict()}


@router.post("/{bid}/translations/auto")
async def auto_translate_brand(
    bid: str,
    body: AutoTranslateBody,
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    x_admin_token: str | None = Header(None, alias="X-Admin-Token"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Run LLM bulk-translate into one or more target locales."""
    _authorise_brand_write(bid, x_owner_id, x_admin_token)

    results: list[dict[str, Any]] = []
    for target in body.target_locales:
        if not svc.validate_locale(target):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "invalid_locale", "locale": target},
            )
        out = await svc.bulk_translate_brand(
            db,
            brand_id=bid,
            target_locale=target,
            source_fields=body.source_fields,
            source_locale=body.source_locale,
        )
        results.append(out)

    return {"ok": True, "brand_id": bid, "results": results}


# ── Admin-only review endpoints ─────────────────────────────────────────


@admin_router.get("/translations/review-queue")
async def review_queue(
    limit: int = Query(100, ge=1, le=1000),
    x_admin_token: str | None = Header(None, alias="X-Admin-Token"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Admin: list auto-translated, unreviewed rows (oldest first)."""
    _require_admin(x_admin_token)
    rows = await svc.list_review_queue(db, limit=limit)
    return {"count": len(rows), "items": [r.to_dict() for r in rows]}


@admin_router.post("/translations/mark-reviewed")
async def post_mark_reviewed(
    body: MarkReviewedBody,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Admin: flip ``reviewed=True`` after human approval."""
    _require_admin(body.admin_token)
    ok = await svc.mark_reviewed(
        db,
        brand_id=body.brand_id,
        field=body.field,
        locale=body.locale,
        reviewer_id=body.reviewer_id,
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "translation_not_found"},
        )
    return {"ok": True}
