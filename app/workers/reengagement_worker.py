"""Re-engagement worker — Wave E Step 5.

Cron-driven (recommended: every 15 min). Each run:

  1. Walks all brand user-indexes.
  2. For each (brand, user), asks
     :func:`app.services.reengagement_orchestrator.evaluate_users`
     to either open a fresh cascade or tick an existing one.
  3. Honours suppression (quiet hours, freq cap, opt-out, just-redeemed).
  4. Writes structured audit + stats to Redis.

The worker is **idempotent** and **side-effect-safe**: a duplicate run
in the same minute won't double-send any cascade step (the freq cap +
``next_due_ts`` advance prevent it).

LLM usage: the orchestrator's ``craft_message`` may eventually call an
LLM for tone-shaping. This worker wraps each evaluation in
``wait_if_paused`` so a global Anthropic quota pause halts re-engagement
sends without trampling the long-running job.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from typing import Any, Iterable

import redis.asyncio as aioredis

from app.redis_client import close_redis, get_redis, init_redis
from app.services.reengagement_orchestrator import (
    cascade_stats,
    evaluate_users,
    send_cascade_step,
)

logger = logging.getLogger("reengagement_worker")

# Per-cycle budget — caps how many users we process to avoid runaway
# pages. A real deployment will set this from env at startup.
DEFAULT_USERS_PER_CYCLE = 500
DEFAULT_POLL_INTERVAL = 15 * 60  # 15 min


async def _wait_if_paused() -> None:
    """Best-effort hook into the LLM-quota guard."""
    try:
        from scripts.llm_quota_monitor import wait_if_paused  # type: ignore
        await wait_if_paused(max_wait_seconds=3600)
    except Exception as exc:  # noqa: BLE001 — optional dep
        logger.debug("wait_if_paused unavailable: %s", exc)


async def _discover_brands(r: aioredis.Redis) -> list[str]:
    """Discover brand ids registered for re-engagement.

    Looks at two sources:
      * ``reengagement:brands`` SET (explicit opt-in)
      * keys matching ``brand:*:users`` (any brand with users)
    """
    raw = await r.smembers("reengagement:brands")
    brands = {
        m.decode() if isinstance(m, (bytes, bytearray)) else str(m)
        for m in (raw or set())
    }

    cursor = 0
    while True:
        cursor, batch = await r.scan(cursor=cursor, match="brand:*:users", count=200)
        for key in batch:
            sk = key.decode() if isinstance(key, (bytes, bytearray)) else str(key)
            parts = sk.split(":")
            if len(parts) == 3:
                brands.add(parts[1])
        if cursor == 0:
            break
    return sorted(brands)


async def run_once(
    r: aioredis.Redis,
    *,
    brand_ids: Iterable[str] | None = None,
    users_per_cycle: int = DEFAULT_USERS_PER_CYCLE,
    now: float | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Process one scan cycle. Returns a structured report."""
    if now is None:
        now = time.time()

    await _wait_if_paused()

    if brand_ids is None:
        brand_ids = await _discover_brands(r)
    brand_ids = list(brand_ids)

    reports: list[dict[str, Any]] = []
    total_started = 0
    total_advanced = 0

    for bid in brand_ids:
        raw_users = await r.smembers(f"brand:{bid}:users")
        users = sorted(
            m.decode() if isinstance(m, (bytes, bytearray)) else str(m)
            for m in (raw_users or set())
        )[:users_per_cycle]

        report = await evaluate_users(
            r,
            brand_id=bid,
            cohort=users,
            now=now,
            dry_run=dry_run,
        )
        reports.append(report)
        total_started += report.get("started_count", 0)
        total_advanced += report.get("advanced_count", 0)

    return {
        "brands_processed": len(brand_ids),
        "started_total": total_started,
        "advanced_total": total_advanced,
        "reports": reports,
        "ran_at": now,
        "dry_run": dry_run,
    }


async def tick_user(
    r: aioredis.Redis,
    *,
    brand_id: str,
    user_id: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Public entrypoint for ticking a single user's cascade on demand.

    Mostly used by the admin test-cascade endpoint and unit tests.
    """
    await _wait_if_paused()
    return await send_cascade_step(
        r,
        brand_id=brand_id,
        user_id=user_id,
        now=now,
    )


# ── CLI runner ─────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="reengagement_worker")
    p.add_argument("--once", action="store_true", help="run one cycle and exit")
    p.add_argument("--dry-run", action="store_true", help="report only, no sends")
    p.add_argument("--brand", action="append", default=None,
                   help="restrict to brand id (may be passed multiple times)")
    p.add_argument(
        "--interval", type=int, default=DEFAULT_POLL_INTERVAL,
        help="poll interval seconds (default 900)",
    )
    p.add_argument(
        "--max-users", type=int, default=DEFAULT_USERS_PER_CYCLE,
        help="per-brand user cap per cycle (default 500)",
    )
    p.add_argument(
        "--print-stats", action="store_true",
        help="dump cascade_stats for each brand after each cycle",
    )
    return p.parse_args(argv)


async def main() -> None:  # pragma: no cover — entrypoint
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    args = _parse_args(sys.argv[1:])
    await init_redis()
    try:
        r = await get_redis()
        if args.once:
            report = await run_once(
                r,
                brand_ids=args.brand,
                users_per_cycle=args.max_users,
                dry_run=args.dry_run,
            )
            print(json.dumps(report, default=str)[:4000])
            if args.print_stats:
                for bid in (args.brand or await _discover_brands(r)):
                    print(json.dumps(await cascade_stats(r, bid), default=str))
            return

        logger.info(
            "reengagement_worker started: interval=%ss max_users=%d",
            args.interval, args.max_users,
        )
        while True:
            try:
                report = await run_once(
                    r,
                    brand_ids=args.brand,
                    users_per_cycle=args.max_users,
                    dry_run=args.dry_run,
                )
                logger.info(
                    "cycle: brands=%d started=%d advanced=%d",
                    report.get("brands_processed", 0),
                    report.get("started_total", 0),
                    report.get("advanced_total", 0),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("cycle failed: %s", exc)
            await asyncio.sleep(args.interval)
    finally:
        await close_redis()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
