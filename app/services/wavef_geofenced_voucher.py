"""Geofenced voucher service — Wave F obvious-win #6.

Inspired by Gamify (KFC Shrimp Attack). Vouchers may carry an optional
``redeem_radius_m`` + anchor (lat,lng); at redeem time the device's
location must be inside the circle or redemption is refused with
``GEO_DENIED`` to drive offline foot-traffic.

Redis schema (additive, namespaced — does not touch existing voucher
records):

    wavef:geo_voucher:{vid}     HASH  {radius_m, anchor_lat, anchor_lng,
                                       owner_brand_id, created_ms}
    wavef:geo_voucher:audit:{vid}  LIST json({uid, lat_t, lng_t, dist_m,
                                              allowed, ts_ms})

Lat/lng truncated to 4 decimals (~11 m) on logging for privacy per spec.

NEW file — no existing voucher router/service touched.
"""

from __future__ import annotations

import json
import math
import time
from typing import Optional

import redis.asyncio as aioredis


# ── Haversine ────────────────────────────────────────────────────────────
# Per spec §16: "geofence util" — we implement the helper here rather than
# import a non-exported private from app.routers.geofence to keep this
# module standalone and testable.
_EARTH_RADIUS_M = 6_371_008.8


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two lat/lng points in metres."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return _EARTH_RADIUS_M * c


# ── Keys ─────────────────────────────────────────────────────────────────


def _k_voucher(vid: str) -> str:
    return f"wavef:geo_voucher:{vid}"


def _k_audit(vid: str) -> str:
    return f"wavef:geo_voucher:audit:{vid}"


def _truncate(v: float) -> float:
    """Reduce lat/lng to 4 decimals for privacy-preserving logging."""
    return round(float(v), 4)


# ── Public API ───────────────────────────────────────────────────────────


async def set_geofence(
    r: aioredis.Redis,
    voucher_id: str,
    *,
    anchor_lat: float,
    anchor_lng: float,
    radius_m: int,
    brand_id: Optional[str] = None,
) -> dict:
    """Attach a redeem geofence to an existing voucher.

    Additive: the host voucher record is untouched; we store the geo
    metadata in a parallel namespaced hash.
    """
    if radius_m <= 0:
        raise ValueError("radius_m must be > 0")
    if not (-90.0 <= anchor_lat <= 90.0):
        raise ValueError("anchor_lat out of range")
    if not (-180.0 <= anchor_lng <= 180.0):
        raise ValueError("anchor_lng out of range")
    now_ms = int(time.time() * 1000)
    payload = {
        "radius_m": str(int(radius_m)),
        "anchor_lat": str(float(anchor_lat)),
        "anchor_lng": str(float(anchor_lng)),
        "created_ms": str(now_ms),
    }
    if brand_id:
        payload["owner_brand_id"] = brand_id
    await r.hset(_k_voucher(voucher_id), mapping=payload)
    return {
        "voucher_id": voucher_id,
        "radius_m": int(radius_m),
        "anchor_lat": float(anchor_lat),
        "anchor_lng": float(anchor_lng),
    }


async def get_geofence(
    r: aioredis.Redis,
    voucher_id: str,
) -> Optional[dict]:
    raw = await r.hgetall(_k_voucher(voucher_id))
    if not raw:
        return None
    try:
        return {
            "voucher_id": voucher_id,
            "radius_m": int(raw["radius_m"]),
            "anchor_lat": float(raw["anchor_lat"]),
            "anchor_lng": float(raw["anchor_lng"]),
            "owner_brand_id": raw.get("owner_brand_id"),
            "created_ms": int(raw.get("created_ms", 0)),
        }
    except (KeyError, ValueError, TypeError):
        return None


async def check_geo(
    r: aioredis.Redis,
    voucher_id: str,
    user_lat: Optional[float],
    user_lng: Optional[float],
    *,
    user_id: Optional[str] = None,
) -> dict:
    """Validate a redeem-time location against the voucher geofence.

    Returns ``{allowed: bool, reason: str|None, distance_m: float|None,
    radius_m: int|None}``. When the voucher has no geofence record, the
    call is allowed (backward-compatible per spec §27).
    """
    fence = await get_geofence(r, voucher_id)
    if fence is None:
        return {
            "allowed": True,
            "reason": None,
            "distance_m": None,
            "radius_m": None,
        }
    if user_lat is None or user_lng is None:
        await _audit(r, voucher_id, user_id, None, None, None, False)
        return {
            "allowed": False,
            "reason": "GEO_DENIED",
            "distance_m": None,
            "radius_m": fence["radius_m"],
        }
    dist = haversine_m(
        fence["anchor_lat"], fence["anchor_lng"],
        float(user_lat), float(user_lng),
    )
    allowed = dist <= float(fence["radius_m"])
    await _audit(r, voucher_id, user_id, user_lat, user_lng, dist, allowed)
    return {
        "allowed": allowed,
        "reason": None if allowed else "GEO_DENIED",
        "distance_m": dist,
        "radius_m": fence["radius_m"],
    }


async def _audit(
    r: aioredis.Redis,
    voucher_id: str,
    user_id: Optional[str],
    lat: Optional[float],
    lng: Optional[float],
    dist: Optional[float],
    allowed: bool,
) -> None:
    rec = {
        "uid": user_id,
        "lat_t": _truncate(lat) if lat is not None else None,
        "lng_t": _truncate(lng) if lng is not None else None,
        "dist_m": round(dist, 1) if dist is not None else None,
        "allowed": bool(allowed),
        "ts_ms": int(time.time() * 1000),
    }
    try:
        await r.rpush(_k_audit(voucher_id), json.dumps(rec))
        await r.ltrim(_k_audit(voucher_id), -500, -1)  # keep last 500
    except Exception:  # pragma: no cover — audit must never fail flow
        pass


async def audit_log(
    r: aioredis.Redis,
    voucher_id: str,
    limit: int = 100,
) -> list[dict]:
    raw = await r.lrange(_k_audit(voucher_id), -limit, -1)
    out: list[dict] = []
    for item in raw:
        try:
            out.append(json.loads(item))
        except (json.JSONDecodeError, TypeError):
            continue
    return out
