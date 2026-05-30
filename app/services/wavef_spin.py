"""Spin-the-wheel service — Wave F obvious-win #8.

Inspired by CataBoom. Configurable wheel with N slices; each slice has a
probability weight and a payload. Server is source of truth: it picks the
winning slice with seeded weighted RNG, returns slice_id; client animation
is purely cosmetic.

Redis schema::

    spin:cfg:{cid}                       HASH   {brand_id, slices_json,
                                                  daily_limit, created_at_ms}
    spin:cfg:{cid}:user:{uid}:spins      STRING int counter
    spin:cfg:{cid}:user:{uid}:today:{d}  STRING int per-day counter

NEW file.
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4


_SGT = timezone(timedelta(hours=8))


def _sgt_today_str() -> str:
    return datetime.now(_SGT).strftime("%Y-%m-%d")


def _k_meta(cid: str) -> str:
    return f"spin:cfg:{cid}"


def _k_user_spins(cid: str, uid: str) -> str:
    return f"spin:cfg:{cid}:user:{uid}:spins"


def _k_user_today(cid: str, uid: str, day: str) -> str:
    return f"spin:cfg:{cid}:user:{uid}:today:{day}"


async def _decode_hash(r, key: str) -> dict[str, str]:
    raw = await r.hgetall(key)
    out: dict[str, str] = {}
    for k, v in raw.items():
        k = k.decode() if isinstance(k, bytes) else k
        v = v.decode() if isinstance(v, bytes) else v
        out[k] = v
    return out


def _validate_slices(slices: list[dict]) -> None:
    if not slices or len(slices) < 2:
        raise ValueError("wheel needs at least 2 slices")
    if len(slices) > 16:
        raise ValueError("wheel supports at most 16 slices")
    total = 0
    for s in slices:
        if "label" not in s or "weight" not in s:
            raise ValueError("each slice needs label & weight")
        w = s["weight"]
        if not isinstance(w, (int, float)) or w < 0:
            raise ValueError("weight must be non-negative number")
        total += w
    if total <= 0:
        raise ValueError("at least one slice must have positive weight")


async def create_config(
    r,
    brand_id: str,
    slices: list[dict],
    daily_limit: int = 1,
) -> dict:
    if not brand_id:
        raise ValueError("brand_id required")
    _validate_slices(slices)
    if daily_limit < 1:
        raise ValueError("daily_limit must be >= 1")

    cid = uuid4().hex[:12]
    # Assign stable slice IDs.
    enriched = [
        {
            "id": f"slc{i}",
            "label": s["label"],
            "weight": float(s["weight"]),
            "payload": s.get("payload", {}),
        }
        for i, s in enumerate(slices)
    ]
    await r.hset(
        _k_meta(cid),
        mapping={
            "brand_id": brand_id,
            "slices_json": json.dumps(enriched),
            "daily_limit": str(daily_limit),
            "created_at_ms": str(int(time.time() * 1000)),
        },
    )
    return {
        "config_id": cid,
        "brand_id": brand_id,
        "daily_limit": daily_limit,
        "slices": enriched,
    }


async def get_config(r, cid: str) -> dict | None:
    raw = await _decode_hash(r, _k_meta(cid))
    if not raw:
        return None
    try:
        slices = json.loads(raw.get("slices_json", "[]"))
    except (json.JSONDecodeError, TypeError):
        slices = []
    return {
        "config_id": cid,
        "brand_id": raw.get("brand_id", ""),
        "daily_limit": int(raw.get("daily_limit", "1") or 1),
        "slices": slices,
    }


def pick(slices: list[dict], rng: random.Random) -> dict:
    """Server-authoritative weighted random pick.

    Slices with weight 0 are NEVER selected.
    """
    total = sum(s["weight"] for s in slices)
    if total <= 0:
        raise ValueError("no positive-weight slices to pick from")
    target = rng.random() * total
    cum = 0.0
    for s in slices:
        cum += s["weight"]
        if target < cum:
            return s
    # Floating-point fallback — return last positive-weight slice.
    for s in reversed(slices):
        if s["weight"] > 0:
            return s
    raise ValueError("unreachable")


async def spin(
    r,
    cid: str,
    uid: str,
    seed: int | None = None,
) -> dict:
    cfg = await get_config(r, cid)
    if cfg is None:
        raise ValueError("config not found")

    # Daily cap
    day = _sgt_today_str()
    today_key = _k_user_today(cid, uid, day)
    used = await r.incr(today_key)
    if used == 1:
        await r.expire(today_key, 48 * 3600)
    if used > cfg["daily_limit"]:
        # Roll back so re-attempts don't compound.
        await r.decr(today_key)
        raise ValueError("daily_limit_exceeded")

    rng = random.Random(seed) if seed is not None else random.SystemRandom()
    winning = pick(cfg["slices"], rng)
    await r.incr(_k_user_spins(cid, uid))

    return {
        "config_id": cid,
        "slice_id": winning["id"],
        "label": winning["label"],
        "payload": winning.get("payload", {}),
        "spins_used_today": used,
        "daily_limit": cfg["daily_limit"],
    }


async def user_spin_count(r, cid: str, uid: str) -> int:
    raw = await r.get(_k_user_spins(cid, uid))
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0
