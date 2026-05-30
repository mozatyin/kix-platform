"""Sweepstakes service — Wave F obvious-win #1.

Inspired by Merkle Promotions / HelloWorld / ePrize (Starbucks for Life).

Provides a minimal sweepstakes draw:
  - enter(campaign_id, user_id): add user to draw pool (multi-entry supported)
  - count(campaign_id): total entries
  - draw(campaign_id, n_winners): pull N random winners (atomic)
  - winners(campaign_id): list past winners

Redis schema:
    sweepstakes:{cid}:entries     ZSET   score=insert_ts_ms, member=entry_id
    sweepstakes:{cid}:entry_meta  HASH   entry_id -> json({user_id, ts, method})
    sweepstakes:{cid}:winners     LIST   json({entry_id, user_id, drawn_at})

NEW file — no existing router or service touched.
"""

from __future__ import annotations

import json
import random
import time
from typing import Literal
from uuid import uuid4

import redis.asyncio as aioredis


EntryMethod = Literal["voucher", "amoe", "purchase", "social"]


def _key_entries(cid: str) -> str:
    return f"sweepstakes:{cid}:entries"


def _key_meta(cid: str) -> str:
    return f"sweepstakes:{cid}:entry_meta"


def _key_winners(cid: str) -> str:
    return f"sweepstakes:{cid}:winners"


async def enter(
    r: aioredis.Redis,
    campaign_id: str,
    user_id: str,
    method: EntryMethod = "voucher",
) -> dict:
    """Record an entry. Returns {entry_id, ts_ms, total_entries}."""
    entry_id = uuid4().hex
    ts_ms = int(time.time() * 1000)
    meta = {"user_id": user_id, "ts_ms": ts_ms, "method": method}
    pipe = r.pipeline(transaction=True)
    pipe.zadd(_key_entries(campaign_id), {entry_id: ts_ms})
    pipe.hset(_key_meta(campaign_id), entry_id, json.dumps(meta))
    pipe.zcard(_key_entries(campaign_id))
    res = await pipe.execute()
    return {
        "entry_id": entry_id,
        "ts_ms": ts_ms,
        "method": method,
        "total_entries": int(res[2]),
    }


async def count(r: aioredis.Redis, campaign_id: str) -> int:
    """Number of entries in pool."""
    return int(await r.zcard(_key_entries(campaign_id)))


async def draw(
    r: aioredis.Redis,
    campaign_id: str,
    n_winners: int = 1,
    seed: int | None = None,
) -> list[dict]:
    """Pick n random winners atomically.

    Removes winning entries from the pool so a single entry can't win twice.
    Records winners in a LIST for audit. If pool < n_winners, draws as many
    as available.
    """
    rng = random.Random(seed)
    entries = await r.zrange(_key_entries(campaign_id), 0, -1)
    if not entries:
        return []
    n = min(n_winners, len(entries))
    picks = rng.sample(entries, k=n)

    winners: list[dict] = []
    drawn_at_ms = int(time.time() * 1000)
    for entry_id in picks:
        meta_raw = await r.hget(_key_meta(campaign_id), entry_id)
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        winner = {
            "entry_id": entry_id if isinstance(entry_id, str) else entry_id.decode(),
            "user_id": meta.get("user_id"),
            "drawn_at_ms": drawn_at_ms,
            "method": meta.get("method"),
        }
        winners.append(winner)
        pipe = r.pipeline(transaction=True)
        pipe.zrem(_key_entries(campaign_id), entry_id)
        pipe.hdel(_key_meta(campaign_id), entry_id)
        pipe.rpush(_key_winners(campaign_id), json.dumps(winner))
        await pipe.execute()
    return winners


async def winners(r: aioredis.Redis, campaign_id: str, limit: int = 100) -> list[dict]:
    """Return historical winners (most-recent last)."""
    raw = await r.lrange(_key_winners(campaign_id), -limit, -1)
    out: list[dict] = []
    for item in raw:
        try:
            out.append(json.loads(item))
        except (json.JSONDecodeError, TypeError):
            continue
    return out
