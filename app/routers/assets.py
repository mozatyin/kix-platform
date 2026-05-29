"""Asset CDN — merchant logo / image / video storage with optimisation.

The Asset CDN is KiX's authoritative store for **brand-facing**
artifacts: a merchant's logo, hero banner, video ad, audio jingle,
PDF certificate, etc. It sits in front of S3 / Aliyun OSS (or a local
filesystem in dev) and adds:

* Stable ``asset_id`` (``ast_xxx``) plus rich metadata in Redis
* Brand-scoped enumeration (``brand:{bid}:assets``)
* Variant generation (resize / re-encode / WebP)
* CDN-friendly public URL or signed URL for private assets
* Usage tracking so we can refuse to delete an asset that a live
  campaign or voucher still references

Why this is separate from ``media``
-----------------------------------
``media`` is the **regulated** media registry (KYC scans, medical
photos, voice recordings) with consent + PIPL audit baked in. The
Asset CDN is for **public marketing content** — what the brand
*wants* to show to users — and is therefore optimised for speed and
CDN delivery rather than consent gating. The two share nothing.

Redis schema
------------
    asset:{ast_id}                   HASH  asset metadata
    brand:{bid}:assets               ZSET  score=created_at, member=ast_id
    asset:{ast_id}:variants          HASH  variant_name → variant_url
    asset:{ast_id}:usage             SET   "campaign:c1", "voucher:v2", ...

Asset types
-----------
``logo`` (512×512), ``hero_image`` (1920×1080), ``thumbnail`` (300×300),
``video`` (mp4 ≤ 50 MB), ``gif`` (≤ 5 MB), ``audio`` (mp3 ≤ 10 MB),
``document`` (pdf, legal/compliance), ``icon`` (64×64).
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, HttpUrl
import redis.asyncio as aioredis

from app.redis_client import get_redis
from app.services.asset_storage import (
    ASSET_SIZE_LIMITS,
    ASSET_TARGET_DIMS,
    AssetStorage,
    detect_image_dimensions,
    get_storage,
    optimize_image,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ─────────────────────────────────────────────────────────────

ASSET_ID_PREFIX = "ast_"

VALID_ASSET_TYPES: set[str] = {
    "logo",
    "hero_image",
    "thumbnail",
    "video",
    "gif",
    "audio",
    "document",
    "icon",
}

# MIME type → asset type compatibility check. We are permissive on
# subtype (svg+xml, jpeg vs jpg, etc) but reject obvious mismatches
# (e.g. uploading a video as a "logo").
_TYPE_MIME_PREFIX: dict[str, tuple[str, ...]] = {
    "logo": ("image/",),
    "hero_image": ("image/",),
    "thumbnail": ("image/",),
    "icon": ("image/",),
    "gif": ("image/gif",),
    "video": ("video/",),
    "audio": ("audio/",),
    "document": ("application/pdf", "application/octet-stream"),
}

# Allowed referencing namespaces for usage tracking.
VALID_USAGE_NAMESPACES: set[str] = {
    "campaign",
    "voucher",
    "storefront",
    "creative",
    "module",
    "welcome_kit",
    "partnership",
}

# Cap how many concurrent usage records we keep before we refuse new
# ones — guards against runaway integrations that never call /release.
USAGE_RECORD_CAP = 10_000


# ── Pydantic models ───────────────────────────────────────────────────────


class AssetUploadFromUrlRequest(BaseModel):
    brand_id: str = Field(..., min_length=1, max_length=128)
    asset_type: Literal[
        "logo", "hero_image", "thumbnail", "video", "gif", "audio", "document", "icon"
    ]
    source_url: HttpUrl
    name: str = Field(..., min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = Field(default=None, max_length=32)


class AssetUploadResponse(BaseModel):
    asset_id: str
    brand_id: str
    asset_type: str
    name: str
    description: str | None
    cdn_url: str
    storage_key: str
    content_type: str
    size_bytes: int
    dimensions: dict[str, int] | None
    backend: str
    created_at: int


class AssetMetadata(BaseModel):
    asset_id: str
    brand_id: str
    asset_type: str
    name: str
    description: str | None = None
    cdn_url: str
    storage_key: str
    content_type: str
    size_bytes: int
    dimensions: dict[str, int] | None = None
    backend: str
    tags: list[str] | None = None
    status: str = "active"
    created_at: int
    deleted_at: int | None = None
    delete_reason: str | None = None
    variants: dict[str, str] | None = None
    usage_count: int = 0


class AssetVariantSpec(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    size: str | None = Field(default=None, pattern=r"^\d{1,5}x\d{1,5}$")
    format: Literal["webp", "jpeg", "jpg", "png", "gif"] = "webp"
    quality: int = Field(default=82, ge=1, le=100)


class AssetOptimizeRequest(BaseModel):
    variants: list[AssetVariantSpec] = Field(..., min_length=1, max_length=10)


class AssetUsageRecordRequest(BaseModel):
    used_in: Literal[
        "campaign",
        "voucher",
        "storefront",
        "creative",
        "module",
        "welcome_kit",
        "partnership",
    ]
    reference_id: str = Field(..., min_length=1, max_length=256)


class AssetUsageReleaseRequest(BaseModel):
    used_in: Literal[
        "campaign",
        "voucher",
        "storefront",
        "creative",
        "module",
        "welcome_kit",
        "partnership",
    ]
    reference_id: str = Field(..., min_length=1, max_length=256)


class AssetDeleteRequest(BaseModel):
    by: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=512)
    force: bool = False


# ── Helpers ───────────────────────────────────────────────────────────────


def _asset_key(aid: str) -> str:
    return f"asset:{aid}"


def _brand_assets_key(bid: str) -> str:
    return f"brand:{bid}:assets"


def _variants_key(aid: str) -> str:
    return f"asset:{aid}:variants"


def _usage_key(aid: str) -> str:
    return f"asset:{aid}:usage"


def _new_asset_id() -> str:
    return f"{ASSET_ID_PREFIX}{uuid4().hex[:20]}"


_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str, fallback: str) -> str:
    """Sanitise a user-supplied filename for use as a storage key fragment.

    Strips path separators and oddball characters; falls back to the
    *fallback* (typically the asset_id) if the result is empty.
    """
    if not name:
        return fallback
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    cleaned = _SAFE_FILENAME.sub("_", base).strip("._-")
    return cleaned or fallback


def _build_storage_key(
    brand_id: str, asset_type: str, asset_id: str, original_name: str
) -> str:
    """Build a CDN-friendly storage key.

    Shape: ``{brand_id}/{asset_type}/{asset_id}_{safe_original_name}``
    so a human browsing the bucket can spot ownership at a glance.
    """
    safe_brand = _SAFE_FILENAME.sub("_", brand_id)[:64]
    safe = _safe_filename(original_name, asset_id)
    return f"{safe_brand}/{asset_type}/{asset_id}_{safe}"


async def _load_asset(r: aioredis.Redis, aid: str) -> dict[str, Any]:
    raw = await r.hgetall(_asset_key(aid))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"asset_id '{aid}' not found",
        )
    return raw


def _hydrate(
    raw: dict[str, Any],
    variants: dict[str, str] | None = None,
    usage_count: int | None = None,
) -> dict[str, Any]:
    """Coerce HASH string values back to a typed shape for responses."""
    try:
        dims = json.loads(raw["dimensions"]) if raw.get("dimensions") else None
    except (json.JSONDecodeError, TypeError):
        dims = None
    try:
        tags = json.loads(raw["tags"]) if raw.get("tags") else None
    except (json.JSONDecodeError, TypeError):
        tags = None

    return {
        "asset_id": raw.get("asset_id"),
        "brand_id": raw.get("brand_id"),
        "asset_type": raw.get("asset_type"),
        "name": raw.get("name"),
        "description": raw.get("description") or None,
        "cdn_url": raw.get("cdn_url"),
        "storage_key": raw.get("storage_key"),
        "content_type": raw.get("content_type"),
        "size_bytes": int(raw.get("size_bytes", 0) or 0),
        "dimensions": dims,
        "backend": raw.get("backend", "local"),
        "tags": tags,
        "status": raw.get("status", "active"),
        "created_at": int(raw.get("created_at", 0) or 0),
        "deleted_at": (
            int(raw["deleted_at"]) if raw.get("deleted_at") else None
        ),
        "delete_reason": raw.get("delete_reason") or None,
        "variants": variants,
        "usage_count": usage_count or 0,
    }


def _validate_mime(asset_type: str, content_type: str) -> None:
    """Reject obvious mime/asset_type mismatches."""
    allowed = _TYPE_MIME_PREFIX.get(asset_type, ())
    if not allowed:
        return
    ct = (content_type or "").lower()
    if not any(ct.startswith(p) for p in allowed):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"content_type '{content_type}' is not compatible with "
                f"asset_type '{asset_type}'. Expected one of: {list(allowed)}"
            ),
        )


def _validate_size(asset_type: str, size_bytes: int) -> None:
    limit = ASSET_SIZE_LIMITS.get(asset_type)
    if limit is None:
        return
    if size_bytes > limit:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"asset of type '{asset_type}' exceeds size limit: "
                f"{size_bytes} bytes > {limit} bytes ({limit // (1024 * 1024)} MB)"
            ),
        )


async def _persist_asset(
    r: aioredis.Redis,
    *,
    asset_id: str,
    brand_id: str,
    asset_type: str,
    name: str,
    description: str | None,
    cdn_url: str,
    storage_key: str,
    content_type: str,
    size_bytes: int,
    dimensions: tuple[int, int] | None,
    backend: str,
    tags: list[str] | None,
    created_at: int,
) -> None:
    """Atomic write of the asset HASH + brand index ZSET."""
    mapping: dict[str, str] = {
        "asset_id": asset_id,
        "brand_id": brand_id,
        "asset_type": asset_type,
        "name": name,
        "description": description or "",
        "cdn_url": cdn_url,
        "storage_key": storage_key,
        "content_type": content_type,
        "size_bytes": str(size_bytes),
        "backend": backend,
        "status": "active",
        "created_at": str(created_at),
    }
    if dimensions:
        mapping["dimensions"] = json.dumps({"w": dimensions[0], "h": dimensions[1]})
    if tags:
        mapping["tags"] = json.dumps(tags)

    pipe = r.pipeline()
    pipe.hset(_asset_key(asset_id), mapping=mapping)
    pipe.zadd(_brand_assets_key(brand_id), {asset_id: created_at})
    await pipe.execute()


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post("/upload", response_model=AssetUploadResponse)
async def upload_asset(
    file: UploadFile = File(...),
    brand_id: str = Form(..., min_length=1, max_length=128),
    asset_type: str = Form(...),
    name: str = Form(..., min_length=1, max_length=256),
    description: str | None = Form(default=None, max_length=2000),
    tags: str | None = Form(default=None, max_length=1024),
    r: aioredis.Redis = Depends(get_redis),
    storage: AssetStorage = Depends(get_storage),
) -> AssetUploadResponse:
    """Upload a brand asset (multipart/form-data).

    The pipeline is:

    1. Validate ``asset_type`` and that the file's MIME matches.
    2. Slurp the body and enforce per-type size limits.
    3. Mint a fresh ``asset_id`` and storage key.
    4. Hand the bytes to the configured storage backend (S3 / OSS / local).
    5. Persist metadata + brand index in Redis.

    The backend's returned URL is treated as the canonical ``cdn_url``;
    if a CDN domain is configured (``ASSET_CDN_DOMAIN``) the S3 backend
    will already have rewritten the URL to point at the CDN.
    """
    if asset_type not in VALID_ASSET_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid asset_type '{asset_type}'. "
                f"Allowed: {sorted(VALID_ASSET_TYPES)}"
            ),
        )

    content_type = file.content_type or "application/octet-stream"
    _validate_mime(asset_type, content_type)

    data = await file.read()
    size_bytes = len(data)
    if size_bytes == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="uploaded file is empty",
        )
    _validate_size(asset_type, size_bytes)

    aid = _new_asset_id()
    storage_key = _build_storage_key(brand_id, asset_type, aid, file.filename or name)

    cdn_url = await storage.put(
        storage_key,
        data,
        content_type,
        metadata={"brand_id": brand_id, "asset_id": aid, "asset_type": asset_type},
    )

    dims = detect_image_dimensions(data) if content_type.startswith("image/") else None

    parsed_tags: list[str] | None = None
    if tags:
        try:
            parsed_tags = [t.strip() for t in tags.split(",") if t.strip()][:32]
        except Exception:
            parsed_tags = None

    now = int(time.time())
    await _persist_asset(
        r,
        asset_id=aid,
        brand_id=brand_id,
        asset_type=asset_type,
        name=name,
        description=description,
        cdn_url=cdn_url,
        storage_key=storage_key,
        content_type=content_type,
        size_bytes=size_bytes,
        dimensions=dims,
        backend=storage.backend_name,
        tags=parsed_tags,
        created_at=now,
    )

    logger.info(
        "asset.upload aid=%s brand=%s type=%s size=%d backend=%s",
        aid,
        brand_id,
        asset_type,
        size_bytes,
        storage.backend_name,
    )

    return AssetUploadResponse(
        asset_id=aid,
        brand_id=brand_id,
        asset_type=asset_type,
        name=name,
        description=description,
        cdn_url=cdn_url,
        storage_key=storage_key,
        content_type=content_type,
        size_bytes=size_bytes,
        dimensions={"w": dims[0], "h": dims[1]} if dims else None,
        backend=storage.backend_name,
        created_at=now,
    )


@router.post("/upload-from-url", response_model=AssetUploadResponse)
async def upload_from_url(
    body: AssetUploadFromUrlRequest,
    r: aioredis.Redis = Depends(get_redis),
    storage: AssetStorage = Depends(get_storage),
) -> AssetUploadResponse:
    """Ingest an asset from an external URL.

    Useful for migrating existing CDN content into KiX without
    asking the merchant to re-upload. Fetches synchronously via
    httpx (best effort — gracefully degrades to a placeholder if the
    network call fails so dev flows aren't blocked).
    """
    # Lazy import — keeps httpx optional at module load time.
    try:
        import httpx  # type: ignore
    except ImportError:  # pragma: no cover
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="httpx not available; upload-from-url disabled",
        )

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(str(body.source_url))
            resp.raise_for_status()
            data = resp.content
            content_type = resp.headers.get("content-type", "application/octet-stream")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"failed to fetch source_url: {exc}",
        ) from exc

    _validate_mime(body.asset_type, content_type)
    _validate_size(body.asset_type, len(data))

    # Derive an original_name from the URL path so the storage key
    # carries a hint about the source file.
    parsed = urlparse(str(body.source_url))
    original_name = parsed.path.rsplit("/", 1)[-1] or body.name

    aid = _new_asset_id()
    storage_key = _build_storage_key(
        body.brand_id, body.asset_type, aid, original_name
    )

    cdn_url = await storage.put(
        storage_key,
        data,
        content_type,
        metadata={
            "brand_id": body.brand_id,
            "asset_id": aid,
            "asset_type": body.asset_type,
            "source_url": str(body.source_url),
        },
    )

    dims = detect_image_dimensions(data) if content_type.startswith("image/") else None

    now = int(time.time())
    await _persist_asset(
        r,
        asset_id=aid,
        brand_id=body.brand_id,
        asset_type=body.asset_type,
        name=body.name,
        description=body.description,
        cdn_url=cdn_url,
        storage_key=storage_key,
        content_type=content_type,
        size_bytes=len(data),
        dimensions=dims,
        backend=storage.backend_name,
        tags=body.tags,
        created_at=now,
    )

    logger.info(
        "asset.upload_from_url aid=%s brand=%s type=%s src=%s",
        aid,
        body.brand_id,
        body.asset_type,
        body.source_url,
    )

    return AssetUploadResponse(
        asset_id=aid,
        brand_id=body.brand_id,
        asset_type=body.asset_type,
        name=body.name,
        description=body.description,
        cdn_url=cdn_url,
        storage_key=storage_key,
        content_type=content_type,
        size_bytes=len(data),
        dimensions={"w": dims[0], "h": dims[1]} if dims else None,
        backend=storage.backend_name,
        created_at=now,
    )


@router.get("/brand/{brand_id}")
async def list_brand_assets(
    brand_id: str,
    asset_type: str | None = Query(default=None),
    include_deleted: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """List assets owned by a brand, newest-first.

    Pagination is offset-based over the brand ZSET so large brands
    can page through their library without scanning the whole set.
    """
    if asset_type is not None and asset_type not in VALID_ASSET_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid asset_type '{asset_type}'. "
                f"Allowed: {sorted(VALID_ASSET_TYPES)}"
            ),
        )

    total = await r.zcard(_brand_assets_key(brand_id))
    # zrevrange returns newest first because we score with created_at.
    ids = await r.zrevrange(
        _brand_assets_key(brand_id), offset, offset + limit - 1
    )

    items: list[dict[str, Any]] = []
    for aid in ids:
        raw = await r.hgetall(_asset_key(aid))
        if not raw:
            continue
        if not include_deleted and raw.get("status") == "deleted":
            continue
        if asset_type and raw.get("asset_type") != asset_type:
            continue
        items.append(_hydrate(raw))

    return {
        "brand_id": brand_id,
        "total": total,
        "count": len(items),
        "limit": limit,
        "offset": offset,
        "items": items,
    }


@router.get("/{asset_id}", response_model=AssetMetadata)
async def get_asset(
    asset_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> AssetMetadata:
    """Return metadata only — does NOT redirect to the binary."""
    raw = await _load_asset(r, asset_id)
    variants = await r.hgetall(_variants_key(asset_id)) or None
    usage_count = await r.scard(_usage_key(asset_id))
    return AssetMetadata(**_hydrate(raw, variants=variants, usage_count=usage_count))


@router.get("/{asset_id}/serve")
async def serve_asset(
    asset_id: str,
    signed: bool = Query(default=False),
    ttl: int = Query(default=3600, ge=60, le=86400),
    r: aioredis.Redis = Depends(get_redis),
    storage: AssetStorage = Depends(get_storage),
) -> RedirectResponse:
    """302 redirect to the asset's CDN URL.

    Set ``signed=true`` to get a time-limited signed URL — required
    for any backend bucket that isn't public-read (i.e. most prod
    setups). The S3 backend will use a presigned URL; the local
    backend returns a deterministic synthetic token.
    """
    raw = await _load_asset(r, asset_id)
    if raw.get("status") == "deleted":
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"asset '{asset_id}' has been deleted",
        )

    if signed:
        target = await storage.signed_url(raw["storage_key"], ttl_seconds=ttl)
    else:
        target = raw.get("cdn_url") or storage.public_url(raw["storage_key"])

    return RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)


@router.post("/{asset_id}/optimize")
async def optimize_asset(
    asset_id: str,
    body: AssetOptimizeRequest,
    r: aioredis.Redis = Depends(get_redis),
    storage: AssetStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Generate additional variants (sizes / formats) for an asset.

    Only meaningful for image assets; for video / audio we politely
    refuse rather than silently no-op so callers know to use a
    different pipeline (transcoding belongs in a worker, not here).
    """
    raw = await _load_asset(r, asset_id)
    asset_type = raw.get("asset_type", "")
    content_type = raw.get("content_type", "")

    if not content_type.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"optimize is only supported for images; asset_type="
                f"'{asset_type}' content_type='{content_type}'. "
                "Use a transcoding worker for video/audio."
            ),
        )

    # Pull the original bytes back through the storage layer. For
    # large libraries this should move to an async worker; for MVP a
    # synchronous round-trip is acceptable.
    try:
        data = await storage.get(raw["storage_key"])
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"asset '{asset_id}' has no backing object",
        )
    except NotImplementedError:
        # Storage backend in stub mode (e.g. S3 without boto3) — we
        # cannot actually read bytes, so just record the variant
        # specs against a placeholder URL.
        data = b""

    variants_spec = [v.model_dump() for v in body.variants]
    generated = await optimize_image(data, variants_spec) if data else {}

    variant_urls: dict[str, str] = {}
    for spec in body.variants:
        variant_name = spec.name or f"{spec.size or 'auto'}_{spec.format}"
        variant_key = f"{raw['storage_key']}.{variant_name}"
        variant_bytes = generated.get(variant_name) or generated.get("original") or b""
        if variant_bytes:
            url = await storage.put(
                variant_key,
                variant_bytes,
                f"image/{spec.format}",
                metadata={"parent_asset_id": asset_id, "variant": variant_name},
            )
        else:
            # Stub mode — surface a placeholder URL pointing at the
            # parent so callers can wire end-to-end even before
            # transcoding is real.
            url = raw.get("cdn_url", "")
        variant_urls[variant_name] = url

    if variant_urls:
        await r.hset(_variants_key(asset_id), mapping=variant_urls)

    return {
        "asset_id": asset_id,
        "variants": variant_urls,
        "count": len(variant_urls),
    }


@router.post("/{asset_id}/usage/record")
async def record_usage(
    asset_id: str,
    body: AssetUsageRecordRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Track that *something* now references this asset.

    The pair ``(used_in, reference_id)`` is stored as ``ns:id`` in a
    Redis SET, so duplicate records are idempotent. We refuse new
    records when the set grows past :data:`USAGE_RECORD_CAP` — at that
    point the integration is almost certainly leaking.
    """
    raw = await _load_asset(r, asset_id)
    if raw.get("status") == "deleted":
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"asset '{asset_id}' has been deleted",
        )

    current = await r.scard(_usage_key(asset_id))
    if current >= USAGE_RECORD_CAP:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"usage records for asset '{asset_id}' exceed cap "
                f"({USAGE_RECORD_CAP}); release stale references first."
            ),
        )

    member = f"{body.used_in}:{body.reference_id}"
    added = await r.sadd(_usage_key(asset_id), member)

    return {
        "asset_id": asset_id,
        "used_in": body.used_in,
        "reference_id": body.reference_id,
        "added": bool(added),
        "usage_count": current + (1 if added else 0),
    }


@router.post("/{asset_id}/usage/release")
async def release_usage(
    asset_id: str,
    body: AssetUsageReleaseRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Release a previously-recorded usage reference.

    Called when the referencing object (campaign / voucher / module)
    is deleted or no longer points at this asset. Returns ``removed``
    so callers can distinguish "we cleared it" from "it wasn't there".
    """
    await _load_asset(r, asset_id)
    member = f"{body.used_in}:{body.reference_id}"
    removed = await r.srem(_usage_key(asset_id), member)
    remaining = await r.scard(_usage_key(asset_id))
    return {
        "asset_id": asset_id,
        "used_in": body.used_in,
        "reference_id": body.reference_id,
        "removed": bool(removed),
        "usage_count": remaining,
    }


@router.get("/{asset_id}/usage")
async def list_usage(
    asset_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return every reference currently pointing at this asset."""
    await _load_asset(r, asset_id)
    members = await r.smembers(_usage_key(asset_id))
    refs: list[dict[str, str]] = []
    for m in members:
        if ":" in m:
            ns, rid = m.split(":", 1)
            refs.append({"used_in": ns, "reference_id": rid})
        else:  # pragma: no cover — defensive
            refs.append({"used_in": "unknown", "reference_id": m})
    return {
        "asset_id": asset_id,
        "count": len(refs),
        "references": refs,
    }


@router.post("/{asset_id}/delete")
async def delete_asset(
    asset_id: str,
    body: AssetDeleteRequest,
    r: aioredis.Redis = Depends(get_redis),
    storage: AssetStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Soft-delete an asset.

    Refuses to delete while the usage set is non-empty unless
    ``force=true`` is supplied — that way callers can't accidentally
    remove a logo that a live campaign is rendering. With ``force``
    we still soft-delete (status='deleted') so audit history is
    preserved; the backing object is hard-deleted from storage.
    """
    raw = await _load_asset(r, asset_id)
    if raw.get("status") == "deleted":
        return {"asset_id": asset_id, "status": "deleted", "noop": True}

    usage_count = await r.scard(_usage_key(asset_id))
    if usage_count > 0 and not body.force:
        members = await r.smembers(_usage_key(asset_id))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    f"asset '{asset_id}' is still referenced by "
                    f"{usage_count} object(s); release them or pass force=true"
                ),
                "references": sorted(members)[:20],
                "usage_count": usage_count,
            },
        )

    # Hard-delete the bucket object; metadata stays for audit.
    try:
        await storage.delete(raw["storage_key"])
    except Exception:  # pragma: no cover — best effort
        logger.exception("asset.delete_storage_failed aid=%s", asset_id)

    # Also clean variants from storage.
    variants = await r.hgetall(_variants_key(asset_id))
    if variants:
        for vname in variants:
            vkey = f"{raw['storage_key']}.{vname}"
            try:
                await storage.delete(vkey)
            except Exception:  # pragma: no cover
                pass
        await r.delete(_variants_key(asset_id))

    now = int(time.time())
    await r.hset(
        _asset_key(asset_id),
        mapping={
            "status": "deleted",
            "deleted_at": str(now),
            "delete_reason": body.reason,
            "deleted_by": body.by,
        },
    )

    logger.info(
        "asset.delete aid=%s by=%s reason=%s force=%s usage=%d",
        asset_id,
        body.by,
        body.reason,
        body.force,
        usage_count,
    )

    return {
        "asset_id": asset_id,
        "status": "deleted",
        "deleted_at": now,
        "force": body.force,
        "released_references": usage_count,
    }


@router.get("/_meta/types")
async def list_asset_types() -> dict[str, Any]:
    """Expose the asset-type catalogue (helpful for merchant UIs)."""
    return {
        "asset_types": sorted(VALID_ASSET_TYPES),
        "size_limits": ASSET_SIZE_LIMITS,
        "target_dimensions": {
            t: ({"w": d[0], "h": d[1]} if d else None)
            for t, d in ASSET_TARGET_DIMS.items()
        },
    }


# ── Public exports ────────────────────────────────────────────────────────

__all__ = [
    "router",
    "VALID_ASSET_TYPES",
    "VALID_USAGE_NAMESPACES",
]
