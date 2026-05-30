"""Enterprise Portal — Conversion Pixel management.

Portal-facing wrapper around the existing ``pixel.py`` event tracker.
Unlike the public-ingest ``/api/v1/pixel/event`` endpoints, these are
**brand-scoped management** routes: create / list / inspect / test-fire
/ install-snippet — i.e. the controls behind the Settings → Conversion
Pixels view.

Endpoints
---------
- ``POST /api/v1/portal/pixels/{bid}/create`` — create pixel
- ``GET  /api/v1/portal/pixels/{bid}`` — list pixels for a brand
- ``GET  /api/v1/portal/pixels/{bid}/{pixel_id}`` — details + stats
- ``GET  /api/v1/portal/pixels/{bid}/{pixel_id}/install-snippet`` —
  install snippet for shopify / wordpress / custom
- ``POST /api/v1/portal/pixels/{bid}/{pixel_id}/test-event`` — fire a
  test event for end-to-end verification
- ``GET  /api/v1/portal/pixels/{bid}/{pixel_id}/events`` — recent events

Storage
-------
We keep portal pixel state in its own namespace so it composes cleanly
with the legacy ``pixel:{pixel_id}`` keys used by ``pixel.py``:

  ``pixel:brand:{bid}:list``     ZSET (score = created_at, member = pixel_id)
  ``pixel:{pixel_id}:meta``      HASH (name, created_at, brand_id, …)
  ``pixel:{pixel_id}:events``    LIST (JSON, capped at 500)

Auth model
----------
Mirrors ``portal_settings.py`` — Bearer JWT *or* ``X-Owner-Id`` header.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, Field

from app.api_standards import error_response, list_response, not_found
from app.config import settings
from app.i18n.context import get_current_locale
from app.i18n.formatting import format_datetime
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()
_bearer = HTTPBearer(auto_error=False)


# ── Storage keys ─────────────────────────────────────────────────────────

BRAND_PIXEL_LIST_KEY = "pixel:brand:{bid}:list"
PIXEL_META_KEY = "pixel:{pixel_id}:meta"
PIXEL_EVENTS_KEY = "pixel:{pixel_id}:events"
PIXEL_EVENTS_MAX = 500


# ── Auth ─────────────────────────────────────────────────────────────────


def _authorise(
    brand_id: str,
    credentials: HTTPAuthorizationCredentials | None,
    x_owner_id: str | None,
) -> str:
    if x_owner_id and x_owner_id == brand_id:
        return f"owner:{brand_id}"
    if credentials and credentials.credentials:
        try:
            payload = jwt.decode(
                credentials.credentials,
                settings.jwt_secret,
                algorithms=[settings.jwt_algorithm],
            )
        except JWTError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "invalid_token", "reason": str(exc)},
            ) from exc
        if payload.get("brand_id") in (brand_id, "all"):
            return f"jwt:{payload.get('sub', 'unknown')}"
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "forbidden", "reason": "brand_id_mismatch"},
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "auth_required"},
    )


# ── Helpers ──────────────────────────────────────────────────────────────


def _now() -> float:
    return time.time()


def _ts(epoch: float | int | None) -> dict[str, Any] | None:
    if not epoch:
        return None
    try:
        dt = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None
    return {
        "epoch_seconds": int(float(epoch)),
        "iso8601": dt.isoformat(),
        "formatted_display": format_datetime(dt, locale=get_current_locale()),
    }


def _new_pixel_id() -> str:
    return f"px_{secrets.token_urlsafe(12)}"


_INSTALL_INTEGRATIONS: frozenset[str] = frozenset(
    {"shopify", "wordpress", "custom"}
)


def _snippet_for(
    pixel_id: str, integration: str, sdk_url: str
) -> dict[str, Any]:
    """Return language + code for the chosen integration."""
    if integration == "shopify":
        code = (
            "{% comment %} KiX Conversion Pixel — paste into "
            "themes → checkout.liquid {% endcomment %}\n"
            f"<script async src=\"{sdk_url}\" "
            f"data-pixel-id=\"{pixel_id}\"></script>\n"
            "<script>\n"
            "  window.kix = window.kix || function(){"
            "(window.kix.q=window.kix.q||[]).push(arguments)};\n"
            f"  kix('init', '{pixel_id}');\n"
            "  kix('track', 'Purchase', {\n"
            "    value: {{ checkout.total_price | money_without_currency }},\n"
            "    currency: '{{ checkout.currency }}',\n"
            "    order_id: '{{ checkout.order_id }}'\n"
            "  });\n"
            "</script>"
        )
        return {"language": "liquid", "code": code}
    if integration == "wordpress":
        code = (
            "<?php\n"
            "// Paste into your theme's functions.php\n"
            "add_action('wp_footer', function () {\n"
            f"  echo '<script async src=\"{sdk_url}\" "
            f"data-pixel-id=\"{pixel_id}\"></script>';\n"
            "  echo '<script>window.kix=window.kix||function(){"
            "(window.kix.q=window.kix.q||[]).push(arguments)};';\n"
            f"  echo \"kix('init','{pixel_id}');\";\n"
            "  echo '</script>';\n"
            "});\n"
            "?>"
        )
        return {"language": "php", "code": code}
    # custom HTML/JS
    code = (
        f"<script async src=\"{sdk_url}\" "
        f"data-pixel-id=\"{pixel_id}\"></script>\n"
        "<script>\n"
        "  window.kix = window.kix || function(){"
        "(window.kix.q=window.kix.q||[]).push(arguments)};\n"
        f"  kix('init', '{pixel_id}');\n"
        "  // Fire a conversion when ready:\n"
        "  // kix('track', 'Purchase', {value: 1999, currency: 'SGD'});\n"
        "</script>"
    )
    return {"language": "html", "code": code}


async def _ensure_pixel(
    r: aioredis.Redis, brand_id: str, pixel_id: str
) -> dict[str, str]:
    """Return raw meta or 404."""
    raw = await r.hgetall(PIXEL_META_KEY.format(pixel_id=pixel_id))
    if not raw or raw.get("brand_id") != brand_id:
        raise not_found("pixel", pixel_id)
    return raw


# ── Pydantic ─────────────────────────────────────────────────────────────


class CreatePixelRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    website_url: str | None = Field(default=None, max_length=2048)


class TestEventRequest(BaseModel):
    event_type: str = Field(default="Purchase", max_length=64)
    value_cents: int = Field(default=1_999, ge=0)
    currency: str = Field(default="SGD", max_length=3, min_length=3)
    metadata: dict[str, Any] | None = None


def _serialise_pixel(
    pixel_id: str, raw: dict[str, str]
) -> dict[str, Any]:
    return {
        "pixel_id": pixel_id,
        "brand_id": raw.get("brand_id"),
        "name": raw.get("name"),
        "description": raw.get("description") or "",
        "website_url": raw.get("website_url") or "",
        "status": raw.get("status", "active"),
        "events_total": int(raw.get("events_total", 0) or 0),
        "last_event_at": _ts(float(raw.get("last_event_at", 0) or 0)),
        "created_at": _ts(float(raw.get("created_at", 0) or 0)),
    }


# ── 1. Create pixel ──────────────────────────────────────────────────────


@router.post(
    "/pixels/{bid}/create",
    tags=["portal-pixels"],
    status_code=status.HTTP_201_CREATED,
    summary="Create a new conversion pixel for a brand",
    responses={
        201: {
            "description": "Newly minted pixel + install snippet",
            "content": {
                "application/json": {
                    "example": {
                        "pixel_id": "px_AbCDeF1234",
                        "brand_id": "b_demo",
                        "name": "Main store",
                        "install_snippet": {
                            "language": "html", "code": "<script ...>"
                        },
                    }
                }
            },
        }
    },
)
async def create_pixel(
    bid: str,
    body: CreatePixelRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Mint a fresh pixel_id, index it by brand, return + install snippet."""
    actor = _authorise(bid, credentials, x_owner_id)
    pixel_id = _new_pixel_id()
    ts = _now()
    meta = {
        "pixel_id": pixel_id,
        "brand_id": bid,
        "name": body.name,
        "description": body.description or "",
        "website_url": body.website_url or "",
        "status": "active",
        "events_total": "0",
        "created_at": str(ts),
        "created_by": actor,
    }
    pipe = r.pipeline()
    pipe.hset(PIXEL_META_KEY.format(pixel_id=pixel_id), mapping=meta)
    pipe.zadd(BRAND_PIXEL_LIST_KEY.format(bid=bid), {pixel_id: ts})
    await pipe.execute()

    sdk_url = "/landing/pixel-sdk.js"  # static SDK shipped with landing
    return {
        **_serialise_pixel(pixel_id, meta),
        "install_snippet": _snippet_for(pixel_id, "custom", sdk_url),
    }


# ── 2. List pixels ───────────────────────────────────────────────────────


@router.get(
    "/pixels/{bid}",
    tags=["portal-pixels"],
    summary="List conversion pixels for a brand",
)
async def list_pixels(
    bid: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _authorise(bid, credentials, x_owner_id)
    total = int(await r.zcard(BRAND_PIXEL_LIST_KEY.format(bid=bid)))
    ids = await r.zrevrange(
        BRAND_PIXEL_LIST_KEY.format(bid=bid),
        offset,
        offset + limit - 1,
    )
    items: list[dict[str, Any]] = []
    for pid in ids:
        raw = await r.hgetall(PIXEL_META_KEY.format(pixel_id=pid))
        if not raw:
            continue
        items.append(_serialise_pixel(pid, raw))
    envelope = list_response(items=items, total=total, limit=limit, offset=offset)
    return {**envelope, "brand_id": bid}


# ── 3. Pixel detail + recent events ──────────────────────────────────────


@router.get(
    "/pixels/{bid}/{pixel_id}",
    tags=["portal-pixels"],
    summary="Pixel details + recent events",
)
async def pixel_detail(
    bid: str,
    pixel_id: str,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _authorise(bid, credentials, x_owner_id)
    raw = await _ensure_pixel(r, bid, pixel_id)
    raw_events = await r.lrange(PIXEL_EVENTS_KEY.format(pixel_id=pixel_id), 0, 9)
    events: list[dict[str, Any]] = []
    for blob in raw_events:
        try:
            entry = json.loads(blob)
        except (TypeError, ValueError):
            continue
        entry["received_at"] = _ts(entry.get("received_at"))
        events.append(entry)
    return {
        **_serialise_pixel(pixel_id, raw),
        "recent_events": events,
    }


# ── 4. Install snippet ───────────────────────────────────────────────────


@router.get(
    "/pixels/{bid}/{pixel_id}/install-snippet",
    tags=["portal-pixels"],
    summary="Code snippet to install the pixel on a host platform",
)
async def install_snippet(
    bid: str,
    pixel_id: str,
    integration: str = Query("custom"),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _authorise(bid, credentials, x_owner_id)
    await _ensure_pixel(r, bid, pixel_id)
    if integration not in _INSTALL_INTEGRATIONS:
        raise error_response(
            422,
            "invalid_integration",
            f"integration must be one of {sorted(_INSTALL_INTEGRATIONS)}",
            requested=integration,
        )
    sdk_url = "/landing/pixel-sdk.js"
    return {
        "pixel_id": pixel_id,
        "integration": integration,
        **_snippet_for(pixel_id, integration, sdk_url),
    }


# ── 5. Test event ────────────────────────────────────────────────────────


@router.post(
    "/pixels/{bid}/{pixel_id}/test-event",
    tags=["portal-pixels"],
    summary="Fire a synthetic test event for end-to-end verification",
)
async def fire_test_event(
    bid: str,
    pixel_id: str,
    body: TestEventRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Append a synthetic event with ``is_test=True`` so the UI flows turn green."""
    _authorise(bid, credentials, x_owner_id)
    await _ensure_pixel(r, bid, pixel_id)
    ts = _now()
    event_id = f"ev_{secrets.token_urlsafe(10)}"
    entry = {
        "event_id": event_id,
        "pixel_id": pixel_id,
        "brand_id": bid,
        "event_type": body.event_type,
        "value_cents": int(body.value_cents),
        "currency": body.currency.upper(),
        "metadata": body.metadata or {},
        "is_test": True,
        "received_at": ts,
    }
    pipe = r.pipeline()
    pipe.lpush(PIXEL_EVENTS_KEY.format(pixel_id=pixel_id), json.dumps(entry))
    pipe.ltrim(
        PIXEL_EVENTS_KEY.format(pixel_id=pixel_id),
        0,
        PIXEL_EVENTS_MAX - 1,
    )
    pipe.hincrby(PIXEL_META_KEY.format(pixel_id=pixel_id), "events_total", 1)
    pipe.hset(
        PIXEL_META_KEY.format(pixel_id=pixel_id), "last_event_at", str(ts)
    )
    await pipe.execute()
    return {"ok": True, "event": {**entry, "received_at": _ts(ts)}}


# ── 6. Recent events ─────────────────────────────────────────────────────


@router.get(
    "/pixels/{bid}/{pixel_id}/events",
    tags=["portal-pixels"],
    summary="Recent events captured by this pixel",
)
async def list_events(
    bid: str,
    pixel_id: str,
    limit: int = Query(100, ge=1, le=PIXEL_EVENTS_MAX),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _authorise(bid, credentials, x_owner_id)
    await _ensure_pixel(r, bid, pixel_id)
    raw_events = await r.lrange(
        PIXEL_EVENTS_KEY.format(pixel_id=pixel_id), 0, limit - 1
    )
    events: list[dict[str, Any]] = []
    for blob in raw_events:
        try:
            entry = json.loads(blob)
        except (TypeError, ValueError):
            continue
        entry["received_at"] = _ts(entry.get("received_at"))
        events.append(entry)
    return {
        "pixel_id": pixel_id,
        "brand_id": bid,
        "items": events,
        "count": len(events),
    }


__all__ = ["router"]
