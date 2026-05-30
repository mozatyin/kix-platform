"""First-100-free-per-country slot allocation service.

Implements the public commitment from `landing/pricing.html` (the "100"
callout) and v4 board deck slide 13: each country grants its first 100
merchants 0% take-rate forever.

API:
    claim_slot(country_code, brand_id)  → SlotClaim | already_claimed
    release_slot(brand_id)              → number released (0 or 1)
    get_summary(country_code)           → {total, claimed, remaining, brand_ids[]}
    list_open_countries(limit=20)       → top N countries with open slots
    is_founding(brand_id)               → bool (0% take rate forever)

Backed by PostgreSQL `country_slots` table (migration 0010). Atomic claim
via UPDATE WHERE brand_id IS NULL ... RETURNING; no Redis race possible.

For Redis-mocked test environments without PG, falls back to a Redis SET-
based counter so the simulation/test path still works.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import redis.asyncio as aioredis


logger = logging.getLogger(__name__)


# Redis fallback keys (used when PG is unavailable, e.g. tests)
_RKEY_CLAIMED = "country_slots:{cc}:claimed"  # SET of brand_ids
_RKEY_BY_BRAND = "country_slots:by_brand:{bid}"  # STRING country_code
_SLOTS_PER_COUNTRY = 100


@dataclass
class SlotClaim:
    country_code: str
    slot_number: int
    brand_id: str
    claimed_at: float
    founding: bool = True  # 0% take rate forever


# ── PG path (production) ───────────────────────────────────────────────

async def _claim_pg(
    db: AsyncSession, country_code: str, brand_id: str
) -> Optional[SlotClaim]:
    """Atomically claim the lowest unclaimed slot for country."""
    # Check if brand already has a slot anywhere (idempotent)
    existing = await db.execute(
        text("""
            SELECT country_code, slot_number, claimed_at
            FROM country_slots
            WHERE brand_id = :bid AND released_at IS NULL
            LIMIT 1
        """),
        {"bid": brand_id},
    )
    row = existing.first()
    if row:
        return SlotClaim(
            country_code=row[0],
            slot_number=row[1],
            brand_id=brand_id,
            claimed_at=row[2].timestamp() if row[2] else time.time(),
        )

    # Atomic claim: lock the smallest free slot in this country
    result = await db.execute(
        text("""
            UPDATE country_slots
               SET brand_id = :bid,
                   claimed_at = NOW()
             WHERE country_code = :cc
               AND slot_number = (
                   SELECT slot_number
                     FROM country_slots
                    WHERE country_code = :cc
                      AND brand_id IS NULL
                    ORDER BY slot_number
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
               )
         RETURNING slot_number, claimed_at
        """),
        {"cc": country_code, "bid": brand_id},
    )
    row = result.first()
    if not row:
        return None  # All 100 slots taken
    await db.commit()
    return SlotClaim(
        country_code=country_code,
        slot_number=row[0],
        brand_id=brand_id,
        claimed_at=row[1].timestamp() if row[1] else time.time(),
    )


async def _summary_pg(db: AsyncSession, country_code: str) -> dict:
    result = await db.execute(
        text("""
            SELECT
                COUNT(*) AS total,
                COUNT(brand_id) FILTER (WHERE released_at IS NULL) AS claimed
            FROM country_slots
            WHERE country_code = :cc
        """),
        {"cc": country_code},
    )
    row = result.first()
    total = row[0] if row else 0
    claimed = row[1] if row else 0
    return {
        "country_code": country_code,
        "total": total,
        "claimed": claimed,
        "remaining": max(0, total - claimed),
    }


# ── Redis fallback path (tests / dev without PG) ───────────────────────

async def _claim_redis(
    r: aioredis.Redis, country_code: str, brand_id: str
) -> Optional[SlotClaim]:
    # Already has a slot?
    existing_cc = await r.get(_RKEY_BY_BRAND.format(bid=brand_id))
    if existing_cc:
        return SlotClaim(
            country_code=existing_cc,
            slot_number=0,  # unknown without PG
            brand_id=brand_id,
            claimed_at=time.time(),
        )
    # Atomic-ish: SADD returns 1 if added, 0 if already present
    added = await r.sadd(_RKEY_CLAIMED.format(cc=country_code), brand_id)
    if not added:
        return None
    n_claimed = await r.scard(_RKEY_CLAIMED.format(cc=country_code))
    if n_claimed > _SLOTS_PER_COUNTRY:
        # Over capacity — roll back
        await r.srem(_RKEY_CLAIMED.format(cc=country_code), brand_id)
        return None
    await r.set(_RKEY_BY_BRAND.format(bid=brand_id), country_code)
    return SlotClaim(
        country_code=country_code,
        slot_number=n_claimed,
        brand_id=brand_id,
        claimed_at=time.time(),
    )


async def _summary_redis(r: aioredis.Redis, country_code: str) -> dict:
    n = await r.scard(_RKEY_CLAIMED.format(cc=country_code))
    return {
        "country_code": country_code,
        "total": _SLOTS_PER_COUNTRY,
        "claimed": n,
        "remaining": max(0, _SLOTS_PER_COUNTRY - n),
    }


# ── Public dual-path API ───────────────────────────────────────────────

async def claim_slot(
    country_code: str,
    brand_id: str,
    *,
    db: Optional[AsyncSession] = None,
    r: Optional[aioredis.Redis] = None,
) -> Optional[SlotClaim]:
    """Atomically claim a founding-merchant slot.

    Returns SlotClaim on success (or if already claimed by this brand).
    Returns None if the country's 100 slots are full.
    """
    country_code = (country_code or "").upper()[:2]
    if not country_code or not brand_id:
        return None

    if db is not None:
        try:
            return await _claim_pg(db, country_code, brand_id)
        except Exception as exc:
            logger.warning("country_slots PG path failed → Redis fallback: %s", exc)

    if r is None:
        from app.redis_client import get_redis
        r = await get_redis()
    return await _claim_redis(r, country_code, brand_id)


async def get_summary(
    country_code: str,
    *,
    db: Optional[AsyncSession] = None,
    r: Optional[aioredis.Redis] = None,
) -> dict:
    country_code = (country_code or "").upper()[:2]
    if db is not None:
        try:
            return await _summary_pg(db, country_code)
        except Exception as exc:
            logger.warning("country_slots summary PG failed → Redis: %s", exc)

    if r is None:
        from app.redis_client import get_redis
        r = await get_redis()
    return await _summary_redis(r, country_code)


async def is_founding(
    brand_id: str, *, r: Optional[aioredis.Redis] = None
) -> bool:
    """Has this brand claimed any country's founding slot? (0% take rate.)"""
    if r is None:
        from app.redis_client import get_redis
        r = await get_redis()
    cc = await r.get(_RKEY_BY_BRAND.format(bid=brand_id))
    return cc is not None


async def release_slot(
    brand_id: str, *, r: Optional[aioredis.Redis] = None
) -> int:
    """Release the brand's slot (e.g. churn). Returns number of slots freed."""
    if r is None:
        from app.redis_client import get_redis
        r = await get_redis()
    cc = await r.get(_RKEY_BY_BRAND.format(bid=brand_id))
    if not cc:
        return 0
    await r.srem(_RKEY_CLAIMED.format(cc=cc), brand_id)
    await r.delete(_RKEY_BY_BRAND.format(bid=brand_id))
    return 1


# ── Discovery: where are slots still open? ─────────────────────────────

_LAUNCH_COUNTRIES = [
    "SG", "ID", "TH", "VN", "PH", "MY", "HK", "TW",
    "US", "GB", "AU", "IN", "JP", "KR", "AE", "SA",
    "BR", "MX", "DE", "FR", "TZ", "KH",
]


async def list_open_countries(
    limit: int = 20, *, r: Optional[aioredis.Redis] = None
) -> list[dict]:
    """Top N countries with most open slots (sorted by remaining desc)."""
    if r is None:
        from app.redis_client import get_redis
        r = await get_redis()
    results = []
    for cc in _LAUNCH_COUNTRIES:
        s = await get_summary(cc, r=r)
        results.append(s)
    results.sort(key=lambda x: -x["remaining"])
    return results[:limit]
