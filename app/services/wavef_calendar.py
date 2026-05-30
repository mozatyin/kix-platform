"""Calendar daily-reveal campaign service — Wave F obvious-win #7.

Inspired by tms McDonald's MONOPOLY. Brands run an N-day campaign; each
calendar day at 00:00 SGT a new "piece" is revealed (prize, bonus, clue,
or collect-set item). Drives daily return visits.

Redis schema::

    cal:cmp:{cid}                          HASH   {brand_id, name,
                                                    start_date, ttl_days,
                                                    days_json, created_at_ms}
    cal:cmp:{cid}:claim:{uid}:{day}        STRING "1"   per-user-per-day
    cal:cmp:{cid}:claim_count              HASH   {day_int -> int}

NEW file.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4


_SGT = timezone(timedelta(hours=8))


def _sgt_today() -> date:
    return datetime.now(_SGT).date()


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _k_meta(cid: str) -> str:
    return f"cal:cmp:{cid}"


def _k_claim(cid: str, uid: str, day: int) -> str:
    return f"cal:cmp:{cid}:claim:{uid}:{day}"


def _k_claim_count(cid: str) -> str:
    return f"cal:cmp:{cid}:claim_count"


async def _decode_hash(r, key: str) -> dict[str, str]:
    raw = await r.hgetall(key)
    out: dict[str, str] = {}
    for k, v in raw.items():
        k = k.decode() if isinstance(k, bytes) else k
        v = v.decode() if isinstance(v, bytes) else v
        out[k] = v
    return out


async def create_campaign(
    r,
    brand_id: str,
    name: str,
    start_date: str,
    days: list[dict],
) -> dict:
    """Create a calendar campaign.

    ``days`` is a list of {day:int (1-indexed), item_type:str, payload:dict}.
    """
    if not brand_id:
        raise ValueError("brand_id required")
    if not days:
        raise ValueError("at least one day required")
    if len(days) > 366:
        raise ValueError("max 366 days per campaign")

    # Validate day indices unique & 1..N
    seen = set()
    for d in days:
        if "day" not in d or "item_type" not in d:
            raise ValueError("each day needs day & item_type")
        if d["day"] in seen:
            raise ValueError(f"duplicate day index {d['day']}")
        if d["day"] < 1:
            raise ValueError("day index must be >= 1")
        seen.add(d["day"])

    # Validate start_date format
    _parse_date(start_date)

    cid = uuid4().hex[:12]
    ttl_days = max(d["day"] for d in days)
    now_ms = int(time.time() * 1000)
    await r.hset(
        _k_meta(cid),
        mapping={
            "brand_id": brand_id,
            "name": name,
            "start_date": start_date,
            "ttl_days": str(ttl_days),
            "days_json": json.dumps(days),
            "created_at_ms": str(now_ms),
        },
    )
    # TTL: campaign + 30d grace
    await r.expire(_k_meta(cid), (ttl_days + 30) * 24 * 3600)
    return {
        "campaign_id": cid,
        "brand_id": brand_id,
        "name": name,
        "start_date": start_date,
        "ttl_days": ttl_days,
        "days": days,
    }


async def get_campaign(r, cid: str) -> dict | None:
    raw = await _decode_hash(r, _k_meta(cid))
    if not raw:
        return None
    try:
        days = json.loads(raw.get("days_json", "[]"))
    except (json.JSONDecodeError, TypeError):
        days = []
    return {
        "campaign_id": cid,
        "brand_id": raw.get("brand_id", ""),
        "name": raw.get("name", ""),
        "start_date": raw.get("start_date", ""),
        "ttl_days": int(raw.get("ttl_days", "0") or 0),
        "days": days,
    }


def _day_index(start: date, today: date) -> int:
    return (today - start).days + 1


async def today_piece(r, cid: str, today: date | None = None) -> dict | None:
    """Return today's revealed piece, or None if before start / after end."""
    meta = await get_campaign(r, cid)
    if not meta:
        return None
    today = today or _sgt_today()
    start = _parse_date(meta["start_date"])
    idx = _day_index(start, today)
    if idx < 1 or idx > meta["ttl_days"]:
        return None
    day_entry = next((d for d in meta["days"] if d["day"] == idx), None)
    if not day_entry:
        return None
    return {
        "campaign_id": cid,
        "day": idx,
        "date": today.strftime("%Y-%m-%d"),
        "item_type": day_entry["item_type"],
        "payload": day_entry.get("payload", {}),
    }


async def claim_today(
    r,
    cid: str,
    uid: str,
    today: date | None = None,
) -> dict:
    """Atomically claim today's piece; one per user per day."""
    piece = await today_piece(r, cid, today=today)
    if not piece:
        raise ValueError("no piece available today")
    today = today or _sgt_today()
    day = piece["day"]
    meta = await get_campaign(r, cid)
    if not meta:
        raise ValueError("campaign not found")
    ttl = (meta["ttl_days"] + 30) * 24 * 3600
    set_ok = await r.set(_k_claim(cid, uid, day), "1", nx=True, ex=ttl)
    if not set_ok:
        raise ValueError("already_claimed")
    await r.hincrby(_k_claim_count(cid), str(day), 1)
    return {
        "claimed": True,
        "day": day,
        "item_type": piece["item_type"],
        "payload": piece["payload"],
    }


async def timeline(
    r,
    cid: str,
    uid: str,
    today: date | None = None,
) -> dict | None:
    """Return the days revealed so far plus claim status for ``uid``."""
    meta = await get_campaign(r, cid)
    if not meta:
        return None
    today = today or _sgt_today()
    start = _parse_date(meta["start_date"])
    today_idx = _day_index(start, today)
    revealed: list[dict] = []
    for d in meta["days"]:
        if d["day"] > today_idx:
            continue
        if d["day"] < 1:
            continue
        claimed = bool(await r.exists(_k_claim(cid, uid, d["day"])))
        revealed.append(
            {
                "day": d["day"],
                "item_type": d["item_type"],
                "payload": d.get("payload", {}),
                "claimed": claimed,
            }
        )
    revealed.sort(key=lambda x: x["day"])
    return {
        "campaign_id": cid,
        "brand_id": meta["brand_id"],
        "today_day_index": today_idx,
        "ttl_days": meta["ttl_days"],
        "revealed": revealed,
    }
