"""Flash promo service — Wave F obvious-win #7.

Inspired by Gamify / BRAME "happy hour" + "midnight flash" patterns.
A flash window is a short, server-published time slice during which a
campaign is eligible for a bonus payload. Each user may claim once per
window.

Redis schema:
    flash:window:{wid}        HASH  {brand_id, campaign_id, starts_at,
                                     ends_at, bonus_json, created_ms}
    flash:active:{brand_id}   ZSET  score=starts_at, member=wid
    flash:claim:{wid}:{uid}   STRING "1"  TTL 24h
    flash:claims:{wid}        LIST  json({uid, ts_ms})

NEW file — no existing module touched.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional
from uuid import uuid4

import redis.asyncio as aioredis


# ── Domain errors ────────────────────────────────────────────────────────


class FlashError(Exception):
    """Base class for flash-promo errors."""


class WindowNotFound(FlashError):
    pass


class OutOfWindow(FlashError):
    pass


class AlreadyClaimed(FlashError):
    pass


# ── Keys ─────────────────────────────────────────────────────────────────


def _k_window(wid: str) -> str:
    return f"flash:window:{wid}"


def _k_active(brand_id: str) -> str:
    return f"flash:active:{brand_id}"


def _k_claim(wid: str, uid: str) -> str:
    return f"flash:claim:{wid}:{uid}"


def _k_claims(wid: str) -> str:
    return f"flash:claims:{wid}"


# ── Time helper (overridable for tests) ──────────────────────────────────


def _now() -> int:
    return int(time.time())


# ── Public API ───────────────────────────────────────────────────────────


async def create_window(
    r: aioredis.Redis,
    *,
    brand_id: str,
    campaign_id: str,
    starts_at: int,
    duration_s: int,
    bonus_payload: Optional[dict] = None,
) -> dict:
    """Publish a new flash window. Returns window record (incl. wid)."""
    if duration_s <= 0:
        raise ValueError("duration_s must be > 0")
    if starts_at <= 0:
        raise ValueError("starts_at must be > 0")
    wid = uuid4().hex
    ends_at = starts_at + int(duration_s)
    bonus_json = json.dumps(bonus_payload or {})
    payload = {
        "brand_id": brand_id,
        "campaign_id": campaign_id,
        "starts_at": str(int(starts_at)),
        "ends_at": str(ends_at),
        "bonus_json": bonus_json,
        "created_ms": str(int(time.time() * 1000)),
    }
    pipe = r.pipeline(transaction=True)
    pipe.hset(_k_window(wid), mapping=payload)
    pipe.zadd(_k_active(brand_id), {wid: int(starts_at)})
    # Auto-expire the active index entry well after the window closes.
    pipe.expireat(_k_window(wid), ends_at + 86_400 * 30)
    await pipe.execute()
    return {
        "window_id": wid,
        "brand_id": brand_id,
        "campaign_id": campaign_id,
        "starts_at": int(starts_at),
        "ends_at": ends_at,
        "bonus_payload": bonus_payload or {},
    }


async def get_window(r: aioredis.Redis, window_id: str) -> Optional[dict]:
    raw = await r.hgetall(_k_window(window_id))
    if not raw:
        return None
    try:
        bonus = json.loads(raw.get("bonus_json", "{}") or "{}")
    except json.JSONDecodeError:
        bonus = {}
    return {
        "window_id": window_id,
        "brand_id": raw.get("brand_id", ""),
        "campaign_id": raw.get("campaign_id", ""),
        "starts_at": int(raw.get("starts_at", 0)),
        "ends_at": int(raw.get("ends_at", 0)),
        "bonus_payload": bonus,
    }


async def active_windows(
    r: aioredis.Redis,
    brand_id: str,
    *,
    now: Optional[int] = None,
) -> list[dict]:
    """Return windows currently active (starts_at ≤ now < ends_at)."""
    now_ts = now if now is not None else _now()
    # Filter by starts_at ≤ now using ZRANGEBYSCORE.
    wids: list[Any] = await r.zrangebyscore(
        _k_active(brand_id), min=0, max=now_ts
    )
    out: list[dict] = []
    for wid_raw in wids:
        wid = wid_raw if isinstance(wid_raw, str) else wid_raw.decode()
        w = await get_window(r, wid)
        if not w:
            continue
        if w["starts_at"] <= now_ts < w["ends_at"]:
            out.append(w)
    return out


async def claim(
    r: aioredis.Redis,
    window_id: str,
    user_id: str,
    *,
    now: Optional[int] = None,
) -> dict:
    """One-time per-user claim during the window. Raises on misuse."""
    w = await get_window(r, window_id)
    if not w:
        raise WindowNotFound(window_id)
    now_ts = now if now is not None else _now()
    if not (w["starts_at"] <= now_ts < w["ends_at"]):
        raise OutOfWindow(window_id)
    ok = await r.set(_k_claim(window_id, user_id), "1", nx=True, ex=86_400)
    if not ok:
        raise AlreadyClaimed(window_id)
    rec = {"uid": user_id, "ts_ms": int(time.time() * 1000)}
    try:
        await r.rpush(_k_claims(window_id), json.dumps(rec))
        await r.ltrim(_k_claims(window_id), -2000, -1)
    except Exception:  # pragma: no cover — audit-only
        pass
    return {
        "window_id": window_id,
        "user_id": user_id,
        "bonus_payload": w["bonus_payload"],
        "claimed_at": now_ts,
    }


async def claim_count(r: aioredis.Redis, window_id: str) -> int:
    return int(await r.llen(_k_claims(window_id)))
