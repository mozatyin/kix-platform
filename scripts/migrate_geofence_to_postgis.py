"""Backfill PostGIS ``geofences`` table from Redis store records.

Walks every ``store:*`` Redis HASH that came from
``POST /geofence/stores/register`` and upserts a matching row into the
PostgreSQL ``geofences`` table.

Does NOT touch the Redis side — keep the GEO sorted-set alive for the
dual-write soak. After 30 days of clean reads from PG, ops can drop the
``geofence:stores`` ZSET and remove the Redis fallback from the router.

Usage::

    .venv/bin/python -m scripts.migrate_geofence_to_postgis --dry-run
    .venv/bin/python -m scripts.migrate_geofence_to_postgis
    .venv/bin/python -m scripts.migrate_geofence_to_postgis --verify
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from typing import Any

from sqlalchemy import func, select

from app.database import async_session_factory
from app.models.geofence import Geofence
from app.redis_client import close_redis, get_redis, init_redis

logger = logging.getLogger("migrate_geofence")

SCAN_BATCH = 200


# ── Decoders ──────────────────────────────────────────────────────────────


def _str(v: Any, default: str | None = None) -> str | None:
    if v is None:
        return default
    if isinstance(v, bytes):
        return v.decode()
    return str(v)


def _float(v: Any) -> float | None:
    s = _str(v)
    if s is None or s == "":
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _int(v: Any, default: int) -> int:
    s = _str(v)
    if s is None or s == "":
        return default
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return default


def _decode_hash(raw: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in raw.items():
        kk = k.decode() if isinstance(k, bytes) else k
        vv = v.decode() if isinstance(v, bytes) else v
        out[kk] = vv
    return out


# ── Core migration ────────────────────────────────────────────────────────


async def migrate(dry_run: bool = False) -> dict[str, int]:
    """Walk ``store:*`` HASHes, upsert into ``geofences``.

    Skips records without valid lat/lng (defence against partial/corrupt
    rows). Counts are returned as a JSON-friendly dict.
    """
    r = await get_redis()
    session = async_session_factory()

    inserted = 0
    updated = 0
    skipped = 0

    try:
        cursor = 0
        while True:
            cursor, keys = await r.scan(
                cursor=cursor, match="store:*", count=SCAN_BATCH
            )
            for key in keys:
                key_s = key.decode() if isinstance(key, bytes) else key
                # Only the root store hash — skip nested keys like
                # store:{id}:visits, store:{id}:push_sent, etc.
                if key_s.count(":") != 1:
                    continue

                raw = await r.hgetall(key)
                if not raw:
                    continue
                raw = _decode_hash(raw)

                store_id = raw.get("store_id") or key_s.split(":", 1)[1]
                brand_id = raw.get("brand_id")
                name = raw.get("name") or "Unknown"
                lat = _float(raw.get("lat"))
                lng = _float(raw.get("lng"))

                if not brand_id or lat is None or lng is None:
                    skipped += 1
                    continue

                radius_m = _int(raw.get("radius_meters"), 500)

                # Compact the auxiliary attributes into metadata_json so
                # they survive without bloating the columnar schema.
                metadata = {
                    "brand_name": raw.get("brand_name") or "",
                    "associated_game_slug": raw.get("associated_game_slug")
                    or "",
                    "associated_recipe_id": raw.get("associated_recipe_id")
                    or "",
                    "associated_campaign_id": raw.get(
                        "associated_campaign_id"
                    )
                    or "",
                }
                push_cfg_raw = raw.get("push_config") or "{}"
                try:
                    metadata["push_config"] = json.loads(push_cfg_raw)
                except (TypeError, json.JSONDecodeError):
                    metadata["push_config"] = {}

                if dry_run:
                    inserted += 1
                    continue

                now = int(time.time())
                point_wkt = f"POINT({lng} {lat})"

                existing = await session.get(Geofence, store_id)
                if existing is None:
                    session.add(
                        Geofence(
                            id=store_id,
                            brand_id=brand_id,
                            store_id=store_id,
                            name=name,
                            location=point_wkt,
                            radius_meters=radius_m,
                            active=True,
                            metadata_json=metadata,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    inserted += 1
                else:
                    existing.brand_id = brand_id
                    existing.name = name
                    existing.location = point_wkt
                    existing.radius_meters = radius_m
                    existing.active = True
                    existing.metadata_json = metadata
                    existing.updated_at = now
                    updated += 1

            if cursor == 0:
                break

        if not dry_run:
            await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()

    return {
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
    }


async def verify() -> dict[str, int]:
    """Compare Redis ``store:*`` count vs PG ``geofences`` row count."""
    r = await get_redis()
    session = async_session_factory()

    redis_count = 0
    cursor = 0
    try:
        while True:
            cursor, keys = await r.scan(
                cursor=cursor, match="store:*", count=SCAN_BATCH
            )
            for key in keys:
                key_s = key.decode() if isinstance(key, bytes) else key
                if key_s.count(":") == 1:
                    redis_count += 1
            if cursor == 0:
                break

        result = await session.execute(
            select(func.count()).select_from(Geofence)
        )
        pg_count = int(result.scalar() or 0)
    finally:
        await session.close()

    return {
        "redis": redis_count,
        "pg": pg_count,
        "drift": redis_count - pg_count,
    }


# ── CLI ───────────────────────────────────────────────────────────────────


async def _amain(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="count rows without writing to PG",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="compare Redis and PG counts; non-zero drift exits 1",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    await init_redis()
    try:
        if args.verify:
            stats = await verify()
            print(json.dumps(stats, indent=2))
            return 0 if stats["drift"] == 0 else 1

        stats = await migrate(dry_run=args.dry_run)
        print(json.dumps(stats, indent=2))
        return 0
    finally:
        await close_redis()


def main() -> None:
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
