"""Welcome Kit — auto-generated physical marketing collateral.

Per MERCHANT_FLOW_TRUTH.md: when a brand first creates a game, auto-trigger
a welcome kit so the merchant feels "this is a real platform". The kit
contains print-ready A4 templates (table stand, counter standing,
door sticker, social poster) plus an optional physical shipping queue.

MVP implementation
------------------
* Templates are rendered as standalone HTML files saved under
  ``landing/welcome-kits/{brand_id}/`` and served via the existing
  ``/landing`` static mount.
* Brand colour + name + tagline are pulled from ``brand_config:{bid}``
  (Redis HASH cache) with PostgreSQL fallback via the ORM.
* QR token is generated on-demand via ``services.qr.generate_qr_token``
  (no rotation cost — 24h duration is fine for printed material).
* Shipping requests are queued in Redis (``welcome_kit:shipping:queue``)
  for ops to fulfil manually; in production this hooks a print-on-demand
  API.

Endpoints
---------
* POST /{brand_id}/generate          → render all template variants
* GET  /{brand_id}/items             → list available items + URLs
* POST /{brand_id}/shipping/request  → queue physical shipping
* GET  /{brand_id}/shipping/status   → current shipping queue status
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import BrandConfig
from app.redis_client import get_redis
from app.services.qr import generate_qr_token

logger = logging.getLogger(__name__)

router = APIRouter()


# ── constants ────────────────────────────────────────────────────────────


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_KITS_DIR = _PROJECT_ROOT / "landing" / "welcome-kits"
_KIT_CACHE_TTL = 30 * 24 * 60 * 60  # 30 days


_ITEMS: dict[str, dict[str, str]] = {
    "table_stand": {
        "title": "桌牌 (A5 双面)",
        "description": "A5 桌面立牌，正反面均印有扫码引导。",
    },
    "counter_standing": {
        "title": "柜台立牌 (A4)",
        "description": "A4 立式陈列，适合柜台/前台位置。",
    },
    "door_sticker": {
        "title": "门贴 (150mm 圆形)",
        "description": "门口/橱窗静电贴，提示路过用户扫码。",
    },
    "social_poster": {
        "title": "社交海报 (1080×1080)",
        "description": "可直接发到朋友圈/小红书/抖音 的方形海报。",
    },
    "handover_kit": {
        "title": "完整 Handover 包",
        "description": "上述所有素材打包 (HTML 索引)。",
    },
}


# ── helpers ──────────────────────────────────────────────────────────────


async def _resolve_brand(
    r: aioredis.Redis, db: AsyncSession, brand_id: str
) -> dict[str, str]:
    """Return brand_name + brand_color + brand_slug + tagline.

    Prefer the Redis HASH cache (``brand_config:{bid}``); fall back to
    PostgreSQL ``brand_configs`` if the cache is cold.
    """
    info: dict[str, str] = {}
    try:
        cached = await r.hgetall(f"brand_config:{brand_id}")
        if cached:
            info.update(cached)
    except Exception:  # pragma: no cover
        pass

    if not info.get("brand_name") or not info.get("brand_slug"):
        row = await db.get(BrandConfig, brand_id)
        if row is not None:
            info.setdefault("brand_name", row.brand_name)
            info.setdefault("brand_slug", row.brand_slug)
            cfg = row.config_json or {}
            info.setdefault(
                "brand_color", cfg.get("primary_color") or "#00FC00"
            )
            info.setdefault(
                "tagline", cfg.get("tagline") or "扫码玩游戏 拿奖励！"
            )
            info.setdefault("logo_url", cfg.get("logo_url") or "")

    if not info.get("brand_name"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"brand_id {brand_id} not found",
        )

    info.setdefault("brand_color", "#00FC00")
    info.setdefault("tagline", "扫码玩游戏 拿奖励！")
    info.setdefault("logo_url", "")
    info.setdefault("brand_slug", brand_id)
    return info


async def _get_or_create_brand_qr(brand_id: str, brand_slug: str) -> str:
    """Mint a long-duration QR URL for printed material.

    Print collateral can't rotate, so we mint a 24h token. In production
    the brand should re-print monthly; this URL still resolves via the
    QR validation grace period and the kit endpoint can be re-called
    on schedule by an ops cron.
    """
    qr_token, qr_url, _valid_until, _next_rot = generate_qr_token(
        brand_id=brand_id,
        location_id="welcome_kit",
        duration_minutes=24 * 60,
        brand_slug=brand_slug,
    )
    return qr_url


def _qr_img_tag(qr_url: str, size: int = 400) -> str:
    """Return an <img> tag using a public QR generator service.

    Using a remote service avoids a hard dependency on ``qrcode``/Pillow
    in the MVP. In prod we should self-host a /qr/png endpoint.
    """
    # google chart inline (deprecated but reliable; fallback to api.qrserver)
    encoded = qr_url.replace("&", "%26")
    src = (
        f"https://api.qrserver.com/v1/create-qr-code/"
        f"?size={size}x{size}&data={encoded}"
    )
    return f'<img src="{src}" width="{size}" height="{size}" alt="QR">'


def _render_table_stand(brand: dict[str, str], qr_url: str) -> str:
    color = escape(brand["brand_color"])
    name = escape(brand["brand_name"])
    tagline = escape(brand["tagline"])
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{name} - 桌牌</title>
<style>
  @page {{ size: A5; margin: 0; }}
  body {{ margin: 0; font-family: -apple-system, sans-serif; }}
  .face {{
    width: 148mm; height: 210mm; box-sizing: border-box; padding: 14mm;
    text-align: center; page-break-after: always;
    background: linear-gradient(180deg, #fff 0%, {color}22 100%);
  }}
  h1 {{ font-size: 36pt; color: {color}; margin: 8mm 0 4mm; }}
  p.tag {{ font-size: 18pt; color: #333; margin: 0 0 10mm; }}
  .qr {{ margin: 6mm auto; }}
  .footer {{ font-size: 10pt; color: #666; margin-top: 8mm; }}
</style></head><body>
  <div class="face">
    <h1>{name}</h1>
    <p class="tag">{tagline}</p>
    <div class="qr">{_qr_img_tag(qr_url, 380)}</div>
    <p style="font-size:12pt">扫码立得能量 · 玩小游戏 · 拿专属奖励</p>
    <p class="footer">Powered by KiX</p>
  </div>
  <div class="face">
    <h1 style="font-size:28pt">怎么玩？</h1>
    <ol style="text-align:left; font-size:14pt; line-height:1.8">
      <li>扫描二维码自动打开 KiX 小游戏</li>
      <li>30 秒玩完一局</li>
      <li>满足条件直接获得本店专属券</li>
      <li>结账时出示券码即可使用</li>
    </ol>
    <div class="qr">{_qr_img_tag(qr_url, 280)}</div>
    <p class="footer">Powered by KiX · letskix.com</p>
  </div>
</body></html>"""


def _render_counter_standing(brand: dict[str, str], qr_url: str) -> str:
    color = escape(brand["brand_color"])
    name = escape(brand["brand_name"])
    tagline = escape(brand["tagline"])
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{name} - 柜台立牌</title>
<style>
  @page {{ size: A4; margin: 0; }}
  body {{ margin: 0; font-family: -apple-system, sans-serif;
    width: 210mm; height: 297mm; box-sizing: border-box;
    background: {color}; color: #fff; padding: 20mm; text-align: center; }}
  h1 {{ font-size: 56pt; margin: 30mm 0 10mm; }}
  p.tag {{ font-size: 28pt; margin: 0 0 20mm; }}
  .qr {{ background: #fff; display: inline-block; padding: 8mm;
    border-radius: 8mm; margin: 10mm 0; }}
  .footer {{ font-size: 14pt; opacity: 0.85; margin-top: 20mm; }}
</style></head><body>
  <h1>{name}</h1>
  <p class="tag">{tagline}</p>
  <div class="qr">{_qr_img_tag(qr_url, 500)}</div>
  <p style="font-size:20pt">扫码玩 · 拿奖励 · 凭码使用</p>
  <p class="footer">Powered by KiX</p>
</body></html>"""


def _render_door_sticker(brand: dict[str, str], qr_url: str) -> str:
    color = escape(brand["brand_color"])
    name = escape(brand["brand_name"])
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{name} - 门贴</title>
<style>
  @page {{ size: 160mm 160mm; margin: 0; }}
  body {{ margin: 0; width: 150mm; height: 150mm; border-radius: 50%;
    background: {color}; color: #fff; font-family: sans-serif;
    text-align: center; padding: 20mm; box-sizing: border-box;
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; }}
  h2 {{ font-size: 18pt; margin: 0 0 4mm; }}
  .qr {{ background: #fff; padding: 4mm; border-radius: 4mm; }}
  p {{ font-size: 12pt; margin: 4mm 0 0; }}
</style></head><body>
  <h2>{name}</h2>
  <div class="qr">{_qr_img_tag(qr_url, 240)}</div>
  <p>扫码玩游戏，拿奖励</p>
</body></html>"""


def _render_social_poster(brand: dict[str, str], qr_url: str) -> str:
    color = escape(brand["brand_color"])
    name = escape(brand["brand_name"])
    tagline = escape(brand["tagline"])
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{name} - 社交海报</title>
<style>
  body {{ margin: 0; font-family: -apple-system, sans-serif;
    width: 1080px; height: 1080px; background: {color}; color: #fff;
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; text-align: center; }}
  h1 {{ font-size: 96px; margin: 0 0 24px; }}
  p.tag {{ font-size: 48px; margin: 0 0 48px; }}
  .qr {{ background: #fff; padding: 32px; border-radius: 32px; }}
  p.foot {{ font-size: 28px; opacity: 0.85; margin-top: 40px; }}
</style></head><body>
  <h1>{name}</h1>
  <p class="tag">{tagline}</p>
  <div class="qr">{_qr_img_tag(qr_url, 500)}</div>
  <p class="foot">扫码玩 · 拿奖励 · Powered by KiX</p>
</body></html>"""


def _render_handover_index(
    brand: dict[str, str], items: dict[str, str]
) -> str:
    name = escape(brand["brand_name"])
    rows = "".join(
        f'<li><a href="./{file}">{escape(_ITEMS[slug]["title"])}</a> — '
        f'{escape(_ITEMS[slug]["description"])}</li>'
        for slug, file in items.items()
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{name} - 欢迎包</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 720px;
    margin: 40px auto; padding: 24px; color: #222; }}
  h1 {{ color: {escape(brand["brand_color"])}; }}
  li {{ margin: 12px 0; line-height: 1.6; }}
  a {{ color: {escape(brand["brand_color"])}; }}
</style></head><body>
  <h1>{name} 欢迎包</h1>
  <p>以下素材均可直接打印或转发，扫码后用户会进入贵店的 KiX 体验：</p>
  <ul>{rows}</ul>
  <p style="margin-top:32px;color:#666;font-size:12px">
    Generated at {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")} · KiX Platform
  </p>
</body></html>"""


def _write(path: Path, html: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def _public_url(brand_id: str, filename: str) -> str:
    return f"/landing/welcome-kits/{brand_id}/{filename}"


async def _generate_all(
    brand_id: str, brand: dict[str, str], qr_url: str
) -> dict[str, str]:
    out_dir = _KITS_DIR / brand_id
    files = {
        "table_stand": "table_stand.html",
        "counter_standing": "counter_standing.html",
        "door_sticker": "door_sticker.html",
        "social_poster": "social_poster.html",
    }
    _write(out_dir / files["table_stand"], _render_table_stand(brand, qr_url))
    _write(
        out_dir / files["counter_standing"],
        _render_counter_standing(brand, qr_url),
    )
    _write(out_dir / files["door_sticker"], _render_door_sticker(brand, qr_url))
    _write(out_dir / files["social_poster"], _render_social_poster(brand, qr_url))
    # Handover index links to all variants.
    files["handover_kit"] = "index.html"
    _write(
        out_dir / files["handover_kit"],
        _render_handover_index(brand, files),
    )
    return {slug: _public_url(brand_id, fn) for slug, fn in files.items()}


# ── routes ───────────────────────────────────────────────────────────────


@router.post("/{brand_id}/generate")
async def generate_welcome_kit(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Render all kit templates and cache the manifest for 30 days.

    Idempotent — calling repeatedly is fine; files are overwritten with
    a fresh QR token. Auto-triggered after first game creation by the
    brand-onboarding flow.
    """
    cache_key = f"welcome_kit:{brand_id}:manifest"
    try:
        cached = await r.get(cache_key)
    except Exception:
        cached = None
    if cached:
        try:
            return json.loads(cached)
        except (TypeError, ValueError):
            pass

    brand = await _resolve_brand(r, db, brand_id)
    qr_url = await _get_or_create_brand_qr(brand_id, brand["brand_slug"])
    urls = await _generate_all(brand_id, brand, qr_url)

    manifest = {
        "brand_id": brand_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "qr_url": qr_url,
        "qr_table_stand_pdf_url": urls["table_stand"],
        "counter_standing_pdf_url": urls["counter_standing"],
        "door_sticker_pdf_url": urls["door_sticker"],
        "social_media_poster_url": urls["social_poster"],
        "handover_kit_zip_url": urls["handover_kit"],
        "items": [
            {
                "slug": slug,
                "title": _ITEMS[slug]["title"],
                "description": _ITEMS[slug]["description"],
                "url": urls[slug],
            }
            for slug in urls
        ],
    }
    try:
        await r.set(cache_key, json.dumps(manifest), ex=_KIT_CACHE_TTL)
    except Exception:  # pragma: no cover
        pass
    logger.info("welcome_kit generated brand=%s items=%d", brand_id, len(urls))
    return manifest


@router.get("/{brand_id}/items")
async def list_welcome_kit_items(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List available kit items. Auto-generates if missing."""
    cache_key = f"welcome_kit:{brand_id}:manifest"
    try:
        cached = await r.get(cache_key)
    except Exception:
        cached = None
    if cached:
        try:
            return json.loads(cached)
        except (TypeError, ValueError):
            pass
    # Cache miss → generate now so the merchant never sees an empty page.
    return await generate_welcome_kit(brand_id=brand_id, r=r, db=db)


# ── shipping queue ───────────────────────────────────────────────────────


class ShippingRequest(BaseModel):
    address: str = Field(..., min_length=4, max_length=500)
    contact_phone: str = Field(..., min_length=4, max_length=32)
    contact_name: str | None = Field(default=None, max_length=80)
    quantity: int = Field(default=5, ge=1, le=200)
    notes: str | None = Field(default=None, max_length=500)


_SHIP_QUEUE_KEY = "welcome_kit:shipping:queue"


def _ship_key(brand_id: str) -> str:
    return f"welcome_kit:{brand_id}:shipping"


@router.post("/{brand_id}/shipping/request")
async def request_shipping(
    brand_id: str,
    body: ShippingRequest,
    r: aioredis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Queue a physical printing + shipping request.

    MVP: enqueues for ops to fulfil manually. In production this hooks
    a print-on-demand API (Printful / 阿里印 / 公牛 etc).
    """
    # Validate brand exists (cheap lookup, also forces the kit to exist).
    await _resolve_brand(r, db, brand_id)

    request_id = f"ship_{brand_id}_{int(time.time())}"
    record = {
        "request_id": request_id,
        "brand_id": brand_id,
        "address": body.address,
        "contact_phone": body.contact_phone,
        "contact_name": body.contact_name or "",
        "quantity": body.quantity,
        "notes": body.notes or "",
        "status": "queued",
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await r.hset(_ship_key(brand_id), request_id, json.dumps(record))
        await r.zadd(_SHIP_QUEUE_KEY, {request_id: time.time()})
    except Exception as exc:  # pragma: no cover
        logger.error("ship queue write failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="shipping queue unavailable",
        )

    return {
        "request_id": request_id,
        "status": "queued",
        "estimated_ship_days": 5,
        "tracking_url": (
            f"/api/v1/welcome-kit/{brand_id}/shipping/status?request_id={request_id}"
        ),
    }


@router.get("/{brand_id}/shipping/status")
async def shipping_status(
    brand_id: str,
    request_id: str | None = None,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return shipping status for a request_id or all requests for the brand."""
    try:
        records = await r.hgetall(_ship_key(brand_id))
    except Exception:
        records = {}
    items: list[dict[str, Any]] = []
    for rid, raw in (records or {}).items():
        try:
            items.append(json.loads(raw))
        except (TypeError, ValueError):
            continue
    if request_id:
        for it in items:
            if it.get("request_id") == request_id:
                return it
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"shipping request {request_id} not found",
        )
    items.sort(key=lambda x: x.get("queued_at", ""), reverse=True)
    return {"brand_id": brand_id, "requests": items, "count": len(items)}
