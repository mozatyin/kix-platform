"""Geofence router — Location-Based Discovery for KiX gamification.

Each merchant has physical stores with GPS coordinates. When a KK app
user enters a store's geofence (e.g. 500m radius), the system pushes
that store's game to the user. User plays → wins voucher → walks in →
conversion.

Pipeline:
    Store register (lat/lng + push config + game/campaign association)
        → Redis GEOADD on "geofence:stores"
    User KK app pings /nearby with current location
        → GEOSEARCH returns nearby store list
    Client (or server-side trigger) calls /enter when user crosses radius
        → cooldown + hours-of-day + campaign liveness check
        → impression token issued, push payload returned
    User actually walks in → /visit (QR scan / manual / check-in)
        → cross-brand attribution lookup (7-day window)

All keys are brand-isolated. Anti-spam:
  * per (user|device) per store cooldown (default 60 min)
  * per user global cap: 5 pushes / hour
  * per store global cap: 200 pushes / hour
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis
from sqlalchemy import func as sa_func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, get_read_db
from app.models.geofence import Geofence
from app.redis_client import get_redis

try:
    # Optional: only imported when PostGIS is configured. We tolerate the
    # absence (e.g. in test environments that mock the DB) so the router
    # still loads cleanly. ``geoalchemy2`` is declared in pyproject.toml
    # so production environments will always have it.
    from geoalchemy2 import Geography as _PGGeography  # noqa: F401
    from geoalchemy2.functions import ST_DWithin

    _POSTGIS_AVAILABLE = True
except ImportError:  # pragma: no cover - safety net
    ST_DWithin = None  # type: ignore[assignment]
    _POSTGIS_AVAILABLE = False

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ─────────────────────────────────────────────────────────────

_USER_HOURLY_PUSH_CAP = 5
_STORE_HOURLY_PUSH_CAP = 200
_ATTR_LOOKBACK_SECONDS = 7 * 24 * 3600  # 7 days
_DEFAULT_COOLDOWN_MIN = 60
_DEFAULT_HOURS = [0, 24]
_DEFAULT_RADIUS_M = 500
_GEO_INDEX = "geofence:stores"


# ── Pydantic models ───────────────────────────────────────────────────────


class PushConfig(BaseModel):
    enabled: bool = True
    cooldown_minutes: int = Field(default=_DEFAULT_COOLDOWN_MIN, ge=0)
    hours_local: list[int] = Field(default_factory=lambda: list(_DEFAULT_HOURS))
    message_template: str = "你在 {brand_name} 附近！玩个游戏拿优惠券 ☕"


class StoreRegister(BaseModel):
    brand_id: str
    store_id: str
    name: str
    lat: float = Field(..., ge=-90.0, le=90.0)
    lng: float = Field(..., ge=-180.0, le=180.0)
    radius_meters: int = Field(default=_DEFAULT_RADIUS_M, ge=10, le=20_000)
    brand_name: str | None = None
    associated_game_slug: str | None = None
    associated_recipe_id: str | None = None
    associated_campaign_id: str | None = None
    push_config: PushConfig = Field(default_factory=PushConfig)


class Store(BaseModel):
    store_id: str
    brand_id: str
    name: str
    brand_name: str | None = None
    lat: float
    lng: float
    radius_meters: int
    associated_game_slug: str | None = None
    associated_recipe_id: str | None = None
    associated_campaign_id: str | None = None
    push_config: PushConfig


class NearbyRequest(BaseModel):
    user_id: str | None = None
    device_fingerprint: str
    lat: float = Field(..., ge=-90.0, le=90.0)
    lng: float = Field(..., ge=-180.0, le=180.0)
    max_distance_km: float = Field(default=5.0, gt=0.0, le=50.0)


class NearbyStore(BaseModel):
    store_id: str
    brand_id: str
    brand_name: str | None
    name: str
    distance_meters: float
    radius_meters: int
    inside_geofence: bool
    game_slug: str | None
    push_eligible: bool


class NearbyResponse(BaseModel):
    nearby_stores: list[NearbyStore]


class GeofenceEnterRequest(BaseModel):
    user_id: str | None = None
    device_fingerprint: str
    store_id: str


class PushPayload(BaseModel):
    title: str
    message: str
    game_slug: str | None
    deep_link: str


class GeofenceEnterResponse(BaseModel):
    push_eligible: bool
    reason: str | None = None
    payload: PushPayload | None = None
    impression_token: str | None = None


class VisitRequest(BaseModel):
    user_id: str
    store_id: str
    evidence: Literal["qr_scan", "manual", "check_in"]
    impression_token: str | None = None


class VisitResponse(BaseModel):
    ok: bool
    visit_id: str
    attributed_source_brand: str | None = None
    attributed_campaign_id: str | None = None


class HeatmapResponse(BaseModel):
    store_id: str
    from_ts: float
    to_ts: float
    enter_count: int
    visit_count: int
    push_sent: int
    push_clicked: int
    conversion_count: int


class RecentVisit(BaseModel):
    visit_id: str
    store_id: str
    brand_id: str
    ts: float
    evidence: str


class RecentVisitsResponse(BaseModel):
    user_id: str
    visits: list[RecentVisit]


class TestInterpolationRequest(BaseModel):
    template: str
    user_id: str | None = None
    brand_id: str
    store_id: str | None = None


class TestInterpolationResponse(BaseModel):
    interpolated: str
    placeholders: list[str]
    missing_placeholders: list[str]


# ── Internal helpers ──────────────────────────────────────────────────────


def _store_key(store_id: str) -> str:
    return f"store:{store_id}"


def _brand_stores_key(brand_id: str) -> str:
    return f"brand:{brand_id}:stores"


def _user_or_device(user_id: str | None, device_fp: str) -> str:
    return user_id if user_id else f"dev:{device_fp}"


def _cooldown_key(user_key: str, store_id: str) -> str:
    return f"geofence:cooldown:{user_key}:{store_id}"


def _user_hour_bucket_key(user_key: str) -> str:
    bucket = int(time.time() // 3600)
    return f"geofence:user_hour:{user_key}:{bucket}"


def _store_hour_bucket_key(store_id: str) -> str:
    bucket = int(time.time() // 3600)
    return f"geofence:store_hour:{store_id}:{bucket}"


def _impression_key(token: str) -> str:
    return f"impression:{token}"


async def _load_store(r: aioredis.Redis, store_id: str) -> dict[str, Any] | None:
    raw = await r.hgetall(_store_key(store_id))
    if not raw:
        return None
    return raw


def _store_from_hash(raw: dict[str, Any]) -> Store:
    push_cfg_raw = raw.get("push_config", "{}")
    try:
        push_cfg_dict = json.loads(push_cfg_raw) if isinstance(push_cfg_raw, str) else push_cfg_raw
    except json.JSONDecodeError:
        push_cfg_dict = {}
    return Store(
        store_id=raw["store_id"],
        brand_id=raw["brand_id"],
        name=raw["name"],
        brand_name=raw.get("brand_name") or None,
        lat=float(raw["lat"]),
        lng=float(raw["lng"]),
        radius_meters=int(raw.get("radius_meters", _DEFAULT_RADIUS_M)),
        associated_game_slug=raw.get("associated_game_slug") or None,
        associated_recipe_id=raw.get("associated_recipe_id") or None,
        associated_campaign_id=raw.get("associated_campaign_id") or None,
        push_config=PushConfig(**push_cfg_dict),
    )


async def _upsert_pg_geofence(
    db: AsyncSession,
    *,
    store_id: str,
    brand_id: str,
    name: str,
    lat: float,
    lng: float,
    radius_meters: int,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Mirror a store record into the PostGIS ``geofences`` table.

    Best-effort dual-write: on any SQLAlchemy failure we log + swallow so
    the Redis-backed primary path still completes. Once PG becomes source
    of truth this swallow can be tightened to fail-closed.

    The ``location`` column is filled via raw WKT (``POINT(lng lat)``);
    PostGIS coerces this to ``geography(POINT, 4326)`` at insert time.
    Note WKT order is ``lng lat`` (not ``lat lng``).
    """
    if db is None:
        return
    now = int(time.time())
    point_wkt = f"POINT({lng} {lat})"
    try:
        existing = await db.get(Geofence, store_id)
        if existing is None:
            db.add(
                Geofence(
                    id=store_id,
                    brand_id=brand_id,
                    store_id=store_id,
                    name=name,
                    location=point_wkt,
                    radius_meters=int(radius_meters),
                    active=True,
                    metadata_json=metadata or {},
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            existing.brand_id = brand_id
            existing.name = name
            existing.location = point_wkt
            existing.radius_meters = int(radius_meters)
            existing.active = True
            if metadata is not None:
                existing.metadata_json = metadata
            existing.updated_at = now
        await db.flush()
    except SQLAlchemyError as exc:
        logger.warning(
            "geofence pg dual-write failed store=%s: %s", store_id, exc
        )
        await db.rollback()


async def _delete_pg_geofence(db: AsyncSession, store_id: str) -> None:
    """Soft-delete by setting ``active=False`` (preserves analytics)."""
    if db is None:
        return
    try:
        existing = await db.get(Geofence, store_id)
        if existing is not None:
            existing.active = False
            existing.updated_at = int(time.time())
            await db.flush()
    except SQLAlchemyError as exc:
        logger.warning(
            "geofence pg soft-delete failed store=%s: %s", store_id, exc
        )
        await db.rollback()


async def find_geofences_near(
    lat: float,
    lng: float,
    max_distance_meters: int,
    db: AsyncSession,
) -> list[dict[str, Any]]:
    """Return geofences whose centre is within ``max_distance_meters``.

    PostGIS spatial query — backed by the GiST R-tree index, so this is
    ``O(log N)`` plus the candidates actually inside the bbox.

    The result is capped at 100 to bound payload size; callers that need
    more should narrow ``max_distance_meters`` or paginate by brand.
    """
    if not _POSTGIS_AVAILABLE or ST_DWithin is None:
        return []
    user_point = sa_func.ST_SetSRID(
        sa_func.ST_MakePoint(lng, lat), 4326
    ).cast(_PGGeography(geometry_type="POINT", srid=4326))

    query = (
        select(Geofence)
        .where(
            Geofence.active.is_(True),
            ST_DWithin(Geofence.location, user_point, max_distance_meters),
        )
        .limit(100)
    )
    result = await db.execute(query)
    return [g.to_dict() for g in result.scalars().all()]


async def is_inside_geofence(
    brand_id: str,
    lat: float,
    lng: float,
    db: AsyncSession,
) -> list[str]:
    """Return geofence IDs the (lat, lng) is inside for this brand.

    Uses each row's own ``radius_meters`` as the inclusion threshold so
    one query answers "which of my brand's stores is the user inside?".
    """
    if not _POSTGIS_AVAILABLE or ST_DWithin is None:
        return []
    user_point = sa_func.ST_SetSRID(
        sa_func.ST_MakePoint(lng, lat), 4326
    ).cast(_PGGeography(geometry_type="POINT", srid=4326))

    query = select(Geofence.id).where(
        Geofence.brand_id == brand_id,
        Geofence.active.is_(True),
        ST_DWithin(Geofence.location, user_point, Geofence.radius_meters),
    )
    result = await db.execute(query)
    return [row[0] for row in result.all()]


async def _campaign_is_active(r: aioredis.Redis, campaign_id: str) -> bool:
    """Look up a campaign hash and confirm it's active.

    The auction / campaign system stores campaign records under
    ``campaign:{id}`` with a ``status`` field. If the record is missing
    we treat the campaign as inactive (fail-closed for push delivery).
    """
    if not campaign_id:
        return True  # no campaign attached = no campaign gate
    c = await r.hgetall(f"campaign:{campaign_id}")
    if not c:
        return False
    return c.get("status") == "active"


# ── Template interpolation ────────────────────────────────────────────────


_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")


async def _interpolate_template(
    r: aioredis.Redis,
    template: str,
    *,
    user_id: str | None,
    device_fp: str | None,
    brand_id: str,
    store_id: str | None,
) -> str:
    """Replace ``{placeholder}`` tokens in template with user/brand data.

    Supported placeholders:
      {name}, {first_name}, {tier}, {brand_name}, {store_name},
      {last_visit}, {xp}, {streak}, {visit_count}, {member_since},
      {custom.<key>} — from user:{uid}:attributes[:brand]

    Unknown placeholders are left untouched (rendered literally as ``{foo}``)
    so a merchant can spot typos. Missing/empty data renders as an empty
    string (except {name}/{first_name} which default to "贵宾").
    """
    if not template or "{" not in template:
        return template

    ctx: dict[str, str] = {
        "brand_name": "",
        "store_name": "",
        "name": "贵宾",
        "first_name": "贵宾",
        "tier": "",
        "last_visit": "",
        "xp": "0",
        "streak": "0",
        "visit_count": "0",
        "member_since": "",
    }

    # Brand name: storefront → brand_config
    try:
        bn = await r.hget(f"storefront:{brand_id}", "display_name")
        if not bn:
            bn = await r.hget(f"brand_config:{brand_id}", "brand_name")
        if bn:
            ctx["brand_name"] = bn
    except aioredis.RedisError:
        pass

    # Store name
    if store_id:
        try:
            sn = await r.hget(_store_key(store_id), "name")
            if sn:
                ctx["store_name"] = sn
        except aioredis.RedisError:
            pass

    if user_id:
        # name + first_name
        try:
            name = await r.hget(f"user:{user_id}:profile", "name")
            if name:
                ctx["name"] = name
                # First word: split on whitespace OR for CJK names just keep
                # whole thing (no whitespace) — that's the desired behaviour.
                parts = name.split()
                ctx["first_name"] = parts[0] if parts else name
        except aioredis.RedisError:
            pass

        # tier + xp via primitives helpers
        try:
            from app.routers.primitives import (  # noqa: PLC0415
                _read_tier_config,
                _read_user_xp,
            )
            xp, _resolved = await _read_user_xp(r, user_id, brand_id)
            ctx["xp"] = str(xp)
            tier_cfg = await _read_tier_config(r, brand_id)
            if tier_cfg:
                current_tier: str | None = None
                for t in sorted(tier_cfg, key=lambda x: x.get("xp_min", 0)):
                    try:
                        if xp >= int(t.get("xp_min", 0)):
                            current_tier = t.get("name")
                    except (TypeError, ValueError):
                        continue
                if current_tier:
                    ctx["tier"] = current_tier
        except Exception as exc:  # noqa: BLE001
            logger.debug("tier/xp lookup failed for %s: %s", user_id, exc)

        # streak
        try:
            streak = await r.get(f"user:{user_id}:streak:{brand_id}")
            if streak is not None:
                ctx["streak"] = str(streak)
        except aioredis.RedisError:
            pass

        # visit_count (all-brand across user's visits zset)
        try:
            vc = await r.zcard(f"user:{user_id}:visits")
            ctx["visit_count"] = str(vc or 0)
        except aioredis.RedisError:
            pass

        # last_visit
        try:
            last_visits = await r.zrevrange(
                f"user:{user_id}:visits", 0, 0, withscores=True,
            )
            if last_visits:
                _vid, score = last_visits[0]
                days_ago = int((time.time() - float(score)) / 86400)
                ctx["last_visit"] = (
                    f"{days_ago} 天前" if days_ago > 0 else "今天"
                )
        except (aioredis.RedisError, TypeError, ValueError, IndexError):
            pass

        # member_since
        try:
            first_touch_ts = await r.get(
                f"user:{user_id}:first_brand_touch:{brand_id}"
            )
            if first_touch_ts:
                days = int((time.time() - float(first_touch_ts)) / 86400)
                if days < 30:
                    ctx["member_since"] = f"{days} 天"
                elif days < 365:
                    ctx["member_since"] = f"{days // 30} 个月"
                else:
                    ctx["member_since"] = f"{days // 365} 年"
        except (aioredis.RedisError, TypeError, ValueError):
            pass

        # custom.* attributes — global hash first, then brand-scoped override
        try:
            attrs = await r.hgetall(f"user:{user_id}:attributes")
            for k, v in (attrs or {}).items():
                ctx[f"custom.{k}"] = v
            battrs = await r.hgetall(f"user:{user_id}:attributes:{brand_id}")
            for k, v in (battrs or {}).items():
                ctx[f"custom.{k}"] = v
        except aioredis.RedisError:
            pass

    def _repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in ctx:
            return str(ctx[key])
        return m.group(0)  # leave unknown placeholder untouched

    return _PLACEHOLDER_RE.sub(_repl, template)


def _list_template_placeholders(template: str) -> list[str]:
    return sorted(set(_PLACEHOLDER_RE.findall(template or "")))


# ── Endpoints: store registration ─────────────────────────────────────────


@router.post("/stores/register", response_model=dict)
async def register_store(
    payload: StoreRegister,
    r: aioredis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Register a physical store with its geo coordinates + push config.

    Idempotent on ``store_id``: re-registering overwrites the record.
    Also indexes the store in the global Redis GEO sorted-set AND in the
    PostGIS ``geofences`` table (dual-write migration window).
    """
    store_id = payload.store_id
    brand_id = payload.brand_id

    record = {
        "store_id": store_id,
        "brand_id": brand_id,
        "name": payload.name,
        "brand_name": payload.brand_name or "",
        "lat": str(payload.lat),
        "lng": str(payload.lng),
        "radius_meters": str(payload.radius_meters),
        "associated_game_slug": payload.associated_game_slug or "",
        "associated_recipe_id": payload.associated_recipe_id or "",
        "associated_campaign_id": payload.associated_campaign_id or "",
        "push_config": payload.push_config.model_dump_json(),
        "created_at": str(time.time()),
    }
    await r.hset(_store_key(store_id), mapping=record)
    await r.sadd(_brand_stores_key(brand_id), store_id)

    # Index in GEO sorted-set. redis-py accepts (lng, lat, member).
    await r.geoadd(_GEO_INDEX, (payload.lng, payload.lat, store_id))

    # NEW: dual-write to PostGIS so the spatial index is in place when
    # we cut reads over. Failures are logged + swallowed so Redis stays
    # source of truth during the migration window.
    await _upsert_pg_geofence(
        db,
        store_id=store_id,
        brand_id=brand_id,
        name=payload.name,
        lat=payload.lat,
        lng=payload.lng,
        radius_meters=payload.radius_meters,
        metadata={
            "brand_name": payload.brand_name or "",
            "associated_game_slug": payload.associated_game_slug or "",
            "associated_recipe_id": payload.associated_recipe_id or "",
            "associated_campaign_id": payload.associated_campaign_id or "",
            "push_config": payload.push_config.model_dump(),
        },
    )

    logger.info(
        "geofence.register_store brand=%s store=%s @ (%.5f,%.5f) r=%dm",
        brand_id, store_id, payload.lat, payload.lng, payload.radius_meters,
    )
    return {"store_id": store_id, "ok": True}


@router.get("/stores/{brand_id}", response_model=list[Store])
async def list_brand_stores(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> list[Store]:
    """List all registered stores for a brand."""
    store_ids = await r.smembers(_brand_stores_key(brand_id))
    out: list[Store] = []
    for sid in store_ids:
        raw = await _load_store(r, sid)
        if raw:
            try:
                out.append(_store_from_hash(raw))
            except (KeyError, ValueError) as exc:
                logger.warning("malformed store %s: %s", sid, exc)
    out.sort(key=lambda s: s.store_id)
    return out


@router.delete("/stores/{store_id}", response_model=dict)
async def delete_store(
    store_id: str,
    r: aioredis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete a store. Removes the hash, brand index entry, and GEO entry.

    Does not delete historical event streams (enter/visit zsets) so that
    analytics can still be queried for terminated stores. The PostGIS
    mirror is soft-deleted (``active=False``) for the same reason.
    """
    raw = await _load_store(r, store_id)
    if not raw:
        raise HTTPException(status_code=404, detail="store_not_found")
    brand_id = raw.get("brand_id", "")
    await r.delete(_store_key(store_id))
    if brand_id:
        await r.srem(_brand_stores_key(brand_id), store_id)
    await r.zrem(_GEO_INDEX, store_id)
    # Soft-delete in PG so spatial queries stop returning the store but
    # historical analytics joins still resolve.
    await _delete_pg_geofence(db, store_id)
    logger.info("geofence.delete_store store=%s brand=%s", store_id, brand_id)
    return {"store_id": store_id, "deleted": True}


# ── Endpoints: live geo queries ───────────────────────────────────────────


@router.post("/nearby", response_model=NearbyResponse)
async def nearby(
    payload: NearbyRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> NearbyResponse:
    """Return stores within ``max_distance_km`` of the caller's GPS.

    Used by the KK app (or any SDK client) to drive the "stores around
    you" carousel and to decide when to fire ``/enter``.
    """
    try:
        # GEOSEARCH returns list of [member, distance, [lng,lat]] when
        # withdist + withcoord are requested.
        results = await r.geosearch(
            name=_GEO_INDEX,
            longitude=payload.lng,
            latitude=payload.lat,
            radius=payload.max_distance_km,
            unit="km",
            withcoord=True,
            withdist=True,
            sort="ASC",
            count=20,
        )
    except aioredis.RedisError as exc:
        logger.exception("geosearch failed: %s", exc)
        raise HTTPException(status_code=500, detail="geo_query_failed") from exc

    user_key = _user_or_device(payload.user_id, payload.device_fingerprint)
    out: list[NearbyStore] = []
    for row in results:
        # redis-py shape: [member, dist_km, (lng, lat)]
        try:
            member, dist_km, _coord = row[0], float(row[1]), row[2]
        except (IndexError, TypeError, ValueError):
            continue
        raw = await _load_store(r, member)
        if not raw:
            continue
        distance_m = dist_km * 1000.0
        radius_m = int(raw.get("radius_meters", _DEFAULT_RADIUS_M))
        inside = distance_m <= radius_m

        # Push-eligible if inside AND no cooldown AND push enabled.
        push_eligible = False
        if inside:
            push_cfg_raw = raw.get("push_config", "{}")
            try:
                push_cfg = json.loads(push_cfg_raw)
            except json.JSONDecodeError:
                push_cfg = {}
            if push_cfg.get("enabled", True):
                cd_key = _cooldown_key(user_key, member)
                if not await r.exists(cd_key):
                    push_eligible = True

        out.append(NearbyStore(
            store_id=member,
            brand_id=raw.get("brand_id", ""),
            brand_name=raw.get("brand_name") or None,
            name=raw.get("name", ""),
            distance_meters=round(distance_m, 1),
            radius_meters=radius_m,
            inside_geofence=inside,
            game_slug=raw.get("associated_game_slug") or None,
            push_eligible=push_eligible,
        ))
    return NearbyResponse(nearby_stores=out)


# ── Endpoints: PostGIS spatial queries (preparation, opt-in) ─────────────


class PostGISNearbyRequest(BaseModel):
    lat: float = Field(..., ge=-90.0, le=90.0)
    lng: float = Field(..., ge=-180.0, le=180.0)
    max_distance_meters: int = Field(default=5000, ge=10, le=50_000)


class PostGISInsideRequest(BaseModel):
    brand_id: str
    lat: float = Field(..., ge=-90.0, le=90.0)
    lng: float = Field(..., ge=-180.0, le=180.0)


@router.post("/postgis/nearby", response_model=list[dict])
async def postgis_nearby(
    payload: PostGISNearbyRequest,
    db: AsyncSession = Depends(get_read_db),
) -> list[dict]:
    """Spatial query against PostGIS — preparation endpoint.

    Returns the same shape as the Redis ``/nearby`` carousel but goes
    through the R-tree GiST index. Off by default in routing config until
    the dual-write soak completes.
    """
    return await find_geofences_near(
        payload.lat, payload.lng, payload.max_distance_meters, db
    )


@router.post("/postgis/inside", response_model=list[str])
async def postgis_inside(
    payload: PostGISInsideRequest,
    db: AsyncSession = Depends(get_read_db),
) -> list[str]:
    """Returns the list of geofence IDs the (lat, lng) is inside for
    this brand, using each row's own ``radius_meters``.
    """
    return await is_inside_geofence(
        payload.brand_id, payload.lat, payload.lng, db
    )


# ── Endpoints: geofence enter (push trigger) ──────────────────────────────


async def _handle_geofence_enter(
    user_id: str | None,
    device_fp: str,
    store_id: str,
    r: aioredis.Redis,
) -> GeofenceEnterResponse:
    """Core geofence-enter logic.

    Returns ``push_eligible=False`` (with ``reason``) when:
      * store doesn't exist (404 raised upstream)
      * push disabled
      * within per-user-per-store cooldown
      * outside configured local hours
      * user global hourly cap exceeded
      * store global hourly cap exceeded
      * attached campaign is not active

    On success, issues an impression token, records the enter event,
    and returns the push payload + token.
    """
    raw = await _load_store(r, store_id)
    if not raw:
        raise HTTPException(status_code=404, detail="store_not_found")

    push_cfg_raw = raw.get("push_config", "{}")
    try:
        push_cfg = json.loads(push_cfg_raw)
    except json.JSONDecodeError:
        push_cfg = {}

    if not push_cfg.get("enabled", True):
        return GeofenceEnterResponse(push_eligible=False, reason="push_disabled")

    user_key = _user_or_device(user_id, device_fp)
    cd_key = _cooldown_key(user_key, store_id)
    if await r.exists(cd_key):
        return GeofenceEnterResponse(push_eligible=False, reason="cooldown")

    # Hours-of-day check (local hour; we use server hour as approximation —
    # production should pass a tz offset or use store's tz).
    current_hour = datetime.now().hour
    hours = push_cfg.get("hours_local", _DEFAULT_HOURS)
    try:
        h_start, h_end = int(hours[0]), int(hours[1])
    except (IndexError, TypeError, ValueError):
        h_start, h_end = 0, 24
    if not (h_start <= current_hour < h_end):
        return GeofenceEnterResponse(push_eligible=False, reason="out_of_hours")

    # Global anti-spam caps (per-user and per-store)
    user_bucket_key = _user_hour_bucket_key(user_key)
    user_count = await r.get(user_bucket_key)
    if user_count is not None and int(user_count) >= _USER_HOURLY_PUSH_CAP:
        return GeofenceEnterResponse(push_eligible=False, reason="user_rate_limit")

    store_bucket_key = _store_hour_bucket_key(store_id)
    store_count = await r.get(store_bucket_key)
    if store_count is not None and int(store_count) >= _STORE_HOURLY_PUSH_CAP:
        return GeofenceEnterResponse(push_eligible=False, reason="store_rate_limit")

    # Campaign liveness gate (auction integration point)
    campaign_id = raw.get("associated_campaign_id") or ""
    if campaign_id and not await _campaign_is_active(r, campaign_id):
        return GeofenceEnterResponse(
            push_eligible=False, reason="campaign_not_active"
        )

    # All gates passed — set cooldown, increment hour buckets.
    cooldown_min = int(push_cfg.get("cooldown_minutes", _DEFAULT_COOLDOWN_MIN))
    if cooldown_min > 0:
        await r.set(cd_key, "1", ex=60 * cooldown_min)

    # Hour-bucket counters expire after 2h so they self-clean.
    pipe = r.pipeline()
    pipe.incr(user_bucket_key)
    pipe.expire(user_bucket_key, 7200)
    pipe.incr(store_bucket_key)
    pipe.expire(store_bucket_key, 7200)
    await pipe.execute()

    # Issue impression token
    impression_token = uuid4().hex
    brand_id = raw.get("brand_id", "")
    await r.hset(_impression_key(impression_token), mapping={
        "type": "geofence_push",
        "brand_id": brand_id,
        "store_id": store_id,
        "campaign_id": campaign_id,
        "user_id": user_id or "",
        "device_fp": device_fp,
        "ts": str(time.time()),
    })
    # Expire orphan impressions after 14 days
    await r.expire(_impression_key(impression_token), 14 * 24 * 3600)

    # Record enter event for heatmap
    await r.zadd(
        f"store:{store_id}:enter_events",
        {impression_token: time.time()},
    )
    await r.zadd(
        f"store:{store_id}:push_sent",
        {impression_token: time.time()},
    )

    # Build payload — interpolate {name}/{tier}/{brand_name}/... at push time
    # so per-user personalisation (chef recognition, VIP welcome) renders
    # correctly instead of being sent literally.
    store_name = raw.get("name", "")
    raw_msg_tmpl = push_cfg.get("message_template", "玩个游戏拿优惠券！")
    raw_title_tmpl = push_cfg.get("title_template") or f"你在 {store_name} 附近"
    try:
        msg = await _interpolate_template(
            r, raw_msg_tmpl,
            user_id=user_id, device_fp=device_fp,
            brand_id=brand_id, store_id=store_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("template interpolation failed (msg): %s", exc)
        msg = raw_msg_tmpl
    try:
        title = await _interpolate_template(
            r, raw_title_tmpl,
            user_id=user_id, device_fp=device_fp,
            brand_id=brand_id, store_id=store_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("template interpolation failed (title): %s", exc)
        title = raw_title_tmpl

    game_slug = raw.get("associated_game_slug") or None
    deep_link = (
        f"/landing/play.html?brand={brand_id}&store={store_id}"
        f"&push={impression_token}"
    )
    if game_slug:
        deep_link += f"&game={game_slug}"

    return GeofenceEnterResponse(
        push_eligible=True,
        payload=PushPayload(
            title=title,
            message=msg,
            game_slug=game_slug,
            deep_link=deep_link,
        ),
        impression_token=impression_token,
    )


@router.post("/enter", response_model=GeofenceEnterResponse)
async def enter_geofence(
    payload: GeofenceEnterRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> GeofenceEnterResponse:
    """Triggered when the client detects the user crossed a store radius.

    Server-side gates: cooldown / hours / rate-limits / campaign liveness.
    On success, returns push payload (or the caller can hand it to FCM /
    APNs / Web Push directly).
    """
    return await _handle_geofence_enter(
        payload.user_id,
        payload.device_fingerprint,
        payload.store_id,
        r,
    )


# ── Endpoints: visit + attribution ────────────────────────────────────────


async def _try_attribute_visit(
    r: aioredis.Redis,
    user_id: str,
    target_brand: str,
) -> tuple[str | None, str | None]:
    """Walk the user's attribution journey looking for a recent cross-brand
    push whose ``target_brand`` matches the store we just visited.

    Returns ``(source_brand, campaign_id)`` if a match was found within
    the 7-day lookback window, else ``(None, None)``.

    Side-effect on match: increments the cross-brand campaign's
    ``conversions`` counter and pushes a ``conversion`` event onto the
    attribution event log for downstream auction billing.
    """
    journey = await r.lrange(f"user:{user_id}:attr_journey", 0, 50)
    now = time.time()
    for event_id in journey:
        event = await r.hgetall(f"attr:{event_id}")
        if not event:
            continue
        try:
            ts = float(event.get("timestamp", 0))
        except (TypeError, ValueError):
            continue
        if now - ts > _ATTR_LOOKBACK_SECONDS:
            # Journey is reverse-chronological; once we cross the window
            # we can stop scanning.
            break
        src = event.get("source_brand")
        tgt = event.get("target_brand")
        campaign = event.get("campaign_id") or None
        if src and tgt == target_brand:
            # Fire conversion side-effect (best-effort; never block visit).
            try:
                if campaign:
                    await r.hincrby(f"campaign:{campaign}", "conversions", 1)
                conv_id = uuid4().hex
                await r.hset(f"attr:{conv_id}", mapping={
                    "type": "conversion",
                    "user_id": user_id,
                    "source_brand": src,
                    "target_brand": target_brand,
                    "campaign_id": campaign or "",
                    "origin_event_id": event_id,
                    "timestamp": str(now),
                })
                await r.lpush(f"user:{user_id}:attr_journey", conv_id)
                await r.ltrim(f"user:{user_id}:attr_journey", 0, 199)
            except aioredis.RedisError as exc:
                logger.warning("attribution side-effect failed: %s", exc)
            return src, campaign
    return None, None


@router.post("/visit", response_model=VisitResponse)
async def record_visit(
    payload: VisitRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> VisitResponse:
    """Record a confirmed physical visit (QR scan / manual / check-in).

    Performs cross-brand attribution lookup (7-day window) against the
    user's recent ``attr_journey`` to detect conversions driven by
    cross-brand pushes.
    """
    raw = await _load_store(r, payload.store_id)
    if not raw:
        raise HTTPException(status_code=404, detail="store_not_found")
    brand_id = raw.get("brand_id", "")

    visit_id = uuid4().hex
    now = time.time()
    await r.hset(f"visit:{visit_id}", mapping={
        "visit_id": visit_id,
        "user_id": payload.user_id,
        "store_id": payload.store_id,
        "brand_id": brand_id,
        "ts": str(now),
        "evidence": payload.evidence,
        "impression_token": payload.impression_token or "",
    })
    await r.zadd(f"store:{payload.store_id}:visits", {visit_id: now})
    await r.zadd(f"user:{payload.user_id}:visits", {visit_id: now})
    await r.zadd(f"brand:{brand_id}:visits", {visit_id: now})

    # If the visit was driven by a geofence impression, mark it as clicked.
    if payload.impression_token:
        imp_raw = await r.hgetall(_impression_key(payload.impression_token))
        if imp_raw:
            await r.hset(
                _impression_key(payload.impression_token),
                "converted_visit_id",
                visit_id,
            )
            await r.zadd(
                f"store:{payload.store_id}:push_clicked",
                {payload.impression_token: now},
            )

    # Attribution: cross-brand conversion lookup
    src_brand, campaign_id = await _try_attribute_visit(
        r, payload.user_id, brand_id,
    )
    if src_brand:
        await r.zadd(
            f"store:{payload.store_id}:conversions",
            {visit_id: now},
        )

    logger.info(
        "geofence.visit user=%s store=%s brand=%s evidence=%s attr=%s",
        payload.user_id, payload.store_id, brand_id,
        payload.evidence, src_brand or "-",
    )

    return VisitResponse(
        ok=True,
        visit_id=visit_id,
        attributed_source_brand=src_brand,
        attributed_campaign_id=campaign_id,
    )


# ── Endpoints: analytics / heatmap ────────────────────────────────────────


@router.get(
    "/stores/{store_id}/heatmap",
    response_model=HeatmapResponse,
)
async def store_heatmap(
    store_id: str,
    from_ts: float = Query(default=0.0, alias="from"),
    to_ts: float | None = Query(default=None, alias="to"),
    r: aioredis.Redis = Depends(get_redis),
) -> HeatmapResponse:
    """Per-store funnel counts: enters, pushes sent, push clicks (visits
    keyed to an impression), confirmed visits, conversions (cross-brand).
    """
    raw = await _load_store(r, store_id)
    if not raw:
        raise HTTPException(status_code=404, detail="store_not_found")

    end = to_ts if to_ts is not None else time.time()
    start = from_ts

    async def _count(key: str) -> int:
        try:
            return int(await r.zcount(key, start, end))
        except aioredis.RedisError:
            return 0

    enter_count = await _count(f"store:{store_id}:enter_events")
    push_sent = await _count(f"store:{store_id}:push_sent")
    push_clicked = await _count(f"store:{store_id}:push_clicked")
    visit_count = await _count(f"store:{store_id}:visits")
    conversion_count = await _count(f"store:{store_id}:conversions")

    return HeatmapResponse(
        store_id=store_id,
        from_ts=start,
        to_ts=end,
        enter_count=enter_count,
        visit_count=visit_count,
        push_sent=push_sent,
        push_clicked=push_clicked,
        conversion_count=conversion_count,
    )


@router.get(
    "/user/{user_id}/recent-visits",
    response_model=RecentVisitsResponse,
)
async def user_recent_visits(
    user_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    r: aioredis.Redis = Depends(get_redis),
) -> RecentVisitsResponse:
    """User's recent physical visits — for retargeting and recommendations."""
    # ZRANGE with REV by default in zrevrange (newest first)
    try:
        visit_ids = await r.zrevrange(f"user:{user_id}:visits", 0, limit - 1)
    except aioredis.RedisError as exc:
        logger.warning("recent_visits zrange failed: %s", exc)
        visit_ids = []

    out: list[RecentVisit] = []
    for vid in visit_ids:
        v = await r.hgetall(f"visit:{vid}")
        if not v:
            continue
        try:
            out.append(RecentVisit(
                visit_id=v.get("visit_id", vid),
                store_id=v.get("store_id", ""),
                brand_id=v.get("brand_id", ""),
                ts=float(v.get("ts", 0)),
                evidence=v.get("evidence", "manual"),
            ))
        except (TypeError, ValueError):
            continue
    return RecentVisitsResponse(user_id=user_id, visits=out)


# ── Endpoints: admin / preview ────────────────────────────────────────────


@router.post(
    "/admin/test-interpolation",
    response_model=TestInterpolationResponse,
)
async def test_interpolation(
    payload: TestInterpolationRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> TestInterpolationResponse:
    """Preview how a push template will render for a given user/brand/store.

    Useful for merchants to validate ``{name}``/``{tier}``/``{custom.X}``
    expansion *before* saving the template to a store's ``push_config``.

    ``missing_placeholders`` lists tokens whose data could not be resolved
    (so they would render as the literal ``{token}``) — i.e. anything that
    survives ``_interpolate_template`` unchanged in the output.
    """
    placeholders = _list_template_placeholders(payload.template)
    interpolated = await _interpolate_template(
        r,
        payload.template,
        user_id=payload.user_id,
        device_fp=None,
        brand_id=payload.brand_id,
        store_id=payload.store_id,
    )
    # Anything still wrapped in {…} in the output is "missing" (unknown key
    # OR an empty-string resolution where the merchant probably expected a
    # value). We treat only literally-surviving placeholders as missing.
    surviving = set(_PLACEHOLDER_RE.findall(interpolated))
    missing = sorted(p for p in placeholders if p in surviving)
    return TestInterpolationResponse(
        interpolated=interpolated,
        placeholders=placeholders,
        missing_placeholders=missing,
    )
