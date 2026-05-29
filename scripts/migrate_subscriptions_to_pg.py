"""Bulk-migrate brand subscription state from Redis to PostgreSQL.

One-shot tool that walks every ``brand:*:subscription`` Redis HASH and
upserts a matching row into ``brand_subscriptions``. Does NOT delete the
Redis keys — keep them around for the dual-write safety margin until
PG is verified as source of truth.

Usage::

    .venv/bin/python -m scripts.migrate_subscriptions_to_pg --dry-run
    .venv/bin/python -m scripts.migrate_subscriptions_to_pg
    .venv/bin/python -m scripts.migrate_subscriptions_to_pg --verify

``--verify`` re-counts both stores and reports any drift.
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
from app.models.subscription import BrandSubscription, SubscriptionHistory
from app.redis_client import close_redis, get_redis, init_redis

logger = logging.getLogger("migrate_subs")

SCAN_BATCH = 200


# ── Decoders ───────────────────────────────────────────────────────────────


def _str(v: Any, default: str | None = None) -> str | None:
    if v is None:
        return default
    if isinstance(v, bytes):
        return v.decode()
    return str(v)


def _int(v: Any, default: int | None = None) -> int | None:
    s = _str(v)
    if s is None or s in ("", "None"):
        return default
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return default


def _bool(v: Any, default: bool = False) -> bool:
    s = _str(v)
    if s is None:
        return default
    return s.lower() in ("1", "true", "yes", "on")


def _decode_hash(raw: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in raw.items():
        kk = k.decode() if isinstance(k, bytes) else k
        vv = v.decode() if isinstance(v, bytes) else v
        out[kk] = vv
    return out


def _redis_row_to_fields(brand_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    now = int(time.time())
    started_at = _int(raw.get("started_at"), now) or now
    expires_at = _int(raw.get("expires_at"), started_at) or started_at
    next_charge = _int(raw.get("next_charge_at"), expires_at) or expires_at

    return {
        "brand_id": brand_id,
        "tier": (raw.get("tier") or "free") if raw.get("tier") in (
            "free",
            "starter",
            "growth",
            "enterprise",
        ) else "free",
        "billing": raw.get("billing") if raw.get("billing") in (
            "monthly",
            "annual",
        ) else "monthly",
        "started_at": started_at,
        "expires_at": expires_at,
        "next_charge_at": next_charge,
        "auto_renew": _bool(raw.get("auto_renew"), False),
        "payment_method_id": raw.get("payment_method_id") or None,
        "first_year_free": _bool(raw.get("first_year_free"), False),
        "cancel_pending": _bool(raw.get("cancel_pending"), False),
        "pending_tier": raw.get("pending_tier") or None,
        "pending_effective_at": _int(raw.get("pending_effective_at")),
        "renew_to_tier": raw.get("renew_to_tier") or None,
        "dunning_state": raw.get("dunning_state") or "none",
        "dunning_attempts": _int(raw.get("dunning_attempts"), 0) or 0,
        "dunning_grace_until": _int(raw.get("dunning_grace_until")),
        "dunning_reason": raw.get("dunning_reason") or None,
        "last_charged_at": _int(raw.get("last_charged_at")),
        "last_charge_amount_cents": _int(
            raw.get("last_charge_amount_cents")
        ),
        "metadata_json": {},
    }


# ── Core migration ─────────────────────────────────────────────────────────


async def migrate(dry_run: bool = False) -> dict[str, int]:
    r = await get_redis()
    session = async_session_factory()

    inserted = 0
    updated = 0
    skipped = 0
    history_copied = 0

    try:
        cursor = 0
        while True:
            cursor, keys = await r.scan(
                cursor=cursor, match="brand:*:subscription", count=SCAN_BATCH
            )
            for key in keys:
                key_s = key.decode() if isinstance(key, bytes) else key
                # Skip nested keys like brand:{id}:subscription:history
                if key_s.count(":") != 2:
                    continue

                raw = await r.hgetall(key)
                if not raw:
                    continue
                raw = _decode_hash(raw)
                brand_id = key_s.split(":")[1]

                fields = _redis_row_to_fields(brand_id, raw)

                if dry_run:
                    inserted += 1
                    continue

                existing = await session.get(BrandSubscription, brand_id)
                if existing is None:
                    session.add(BrandSubscription(**fields))
                    inserted += 1
                else:
                    for k, v in fields.items():
                        if k == "brand_id":
                            continue
                        setattr(existing, k, v)
                    updated += 1

                # Mirror the history list (best-effort, capped at 500).
                history_key = f"brand:{brand_id}:subscription:history"
                events = await r.lrange(history_key, 0, 499)
                for blob in events:
                    text = blob.decode() if isinstance(blob, bytes) else blob
                    try:
                        ev = json.loads(text)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    session.add(
                        SubscriptionHistory(
                            brand_id=brand_id,
                            event=str(ev.get("event") or "UNKNOWN")[:64],
                            from_tier=ev.get("from_tier"),
                            to_tier=ev.get("to_tier"),
                            charge_amount_cents=ev.get(
                                "charge_amount_cents"
                            ),
                            metadata_json=ev,
                            ts=int(ev.get("ts") or time.time()),
                        )
                    )
                    history_copied += 1

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
        "history_copied": history_copied,
    }


async def verify() -> dict[str, int]:
    """Compare Redis vs PG counts and surface any drift."""
    r = await get_redis()
    session = async_session_factory()

    redis_count = 0
    cursor = 0
    try:
        while True:
            cursor, keys = await r.scan(
                cursor=cursor, match="brand:*:subscription", count=SCAN_BATCH
            )
            for key in keys:
                key_s = key.decode() if isinstance(key, bytes) else key
                if key_s.count(":") == 2:
                    redis_count += 1
            if cursor == 0:
                break

        result = await session.execute(
            select(func.count()).select_from(BrandSubscription)
        )
        pg_count = int(result.scalar() or 0)
    finally:
        await session.close()

    return {
        "redis": redis_count,
        "pg": pg_count,
        "drift": redis_count - pg_count,
    }


# ── CLI ────────────────────────────────────────────────────────────────────


async def _amain(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="count keys without writing to PG",
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
