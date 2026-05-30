"""Campaign Arc Scheduler — daily cron that emits drop events.

Once per day this worker:

  1. Scans every active arc.
  2. Refreshes its status (scheduled → active → redemption_only → ended).
  3. For arcs currently in-play, idempotently emits the day's drop event
     (consumed by the push-notification pipeline → "Day 14 of Monopoly,
     a new piece is available!").
  4. Tracks engagement decay: participants who haven't played in
     ``DECAY_THRESHOLD_DAYS`` days get a low-engagement flag the push
     engine can use to send a re-engagement nudge.

The worker is single-tenant and side-effect-only — it never mutates arc
configuration. It is safe to run multiple times in the same day
(emission tracking via ``arc:{arc_id}:emitted_drops``).

Usage::

    .venv/bin/python -m app.workers.campaign_arc_scheduler --once
    .venv/bin/python -m app.workers.campaign_arc_scheduler             # loop
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from typing import Any

import redis.asyncio as aioredis

from app.redis_client import close_redis, get_redis, init_redis
from app.services.campaign_arc import (
    ARC_EMITTED_DROPS_KEY,
    ARC_LEADERBOARD_KEY,
    ARC_PARTICIPANTS_KEY,
    ARC_USER_KEY,
    DAY_SECONDS,
    load_arc,
    refresh_status,
)

logger = logging.getLogger("campaign_arc_scheduler")

# How often to wake (seconds). Real prod = once/day; here every hour so
# late-starting arcs get picked up within a reasonable window.
CHECK_INTERVAL_SECONDS = 3600
DECAY_THRESHOLD_DAYS = 3
# Push notification event stream — consumed by push_worker.
DROP_EVENT_STREAM = "arc_drop_events"
DECAY_EVENT_STREAM = "arc_decay_events"
# Cap one stream so it can never grow unbounded.
STREAM_MAXLEN = 10_000


# ── Single-arc tick ────────────────────────────────────────────────────


async def emit_today_drop(r: aioredis.Redis, arc_id: str) -> dict[str, Any]:
    """Emit today's drop for ``arc_id`` exactly once.

    Returns a status dict describing what happened so the caller / tests
    can assert. Idempotency: the ``emitted_drops`` SET tracks emitted
    day_index values; re-runs in the same day return ``skipped_emitted``.
    """
    arc = await load_arc(r, arc_id)
    if arc is None:
        return {"arc_id": arc_id, "result": "missing"}

    new_status = await refresh_status(r, arc)
    if new_status not in ("active",):
        return {
            "arc_id": arc_id,
            "result": "not_in_play",
            "status": new_status,
        }

    day_index = arc.current_day_index()
    if day_index < 0 or day_index >= arc.duration_days:
        return {"arc_id": arc_id, "result": "out_of_window"}

    emitted_key = ARC_EMITTED_DROPS_KEY.format(arc_id=arc_id)
    # SADD returns 1 if newly added, 0 if already present — perfect
    # idempotency primitive.
    added = await r.sadd(emitted_key, str(day_index))
    if not added:
        return {
            "arc_id": arc_id,
            "result": "skipped_emitted",
            "day_index": day_index,
        }

    drop = arc.daily_drops[day_index] if day_index < len(arc.daily_drops) else {}
    payload = {
        "arc_id": arc_id,
        "brand_id": arc.brand_id,
        "arc_name": arc.name,
        "day_index": day_index,
        "day_label": f"Day {day_index + 1} of {arc.duration_days}",
        "arc_type": arc.arc_type,
        "drop": drop,
        "ts": time.time(),
    }
    # Emit to the push stream. xadd MAXLEN keeps the stream bounded so the
    # cron can't leak unbounded memory if push_worker is offline.
    try:
        await r.xadd(
            DROP_EVENT_STREAM,
            {"data": json.dumps(payload)},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as exc:  # pragma: no cover — never crash the cron
        logger.warning("xadd %s failed: %s", DROP_EVENT_STREAM, exc)

    return {
        "arc_id": arc_id,
        "result": "emitted",
        "day_index": day_index,
        "drop": drop,
    }


async def detect_engagement_decay(
    r: aioredis.Redis,
    arc_id: str,
    threshold_days: int = DECAY_THRESHOLD_DAYS,
) -> dict[str, Any]:
    """Flag participants who haven't played in ``threshold_days`` days.

    The push worker reads the decay stream and sends a re-engagement
    nudge ("come back, Day 14 just dropped").
    """
    arc = await load_arc(r, arc_id)
    if arc is None or arc.status not in ("active",):
        return {"arc_id": arc_id, "result": "skip", "decayed": 0}

    now = time.time()
    cutoff = now - threshold_days * DAY_SECONDS
    decayed = 0

    participants = await r.smembers(
        ARC_PARTICIPANTS_KEY.format(arc_id=arc_id)
    )
    for uid in participants:
        raw = await r.hgetall(
            ARC_USER_KEY.format(arc_id=arc_id, uid=uid)
        )
        last_play = float(raw.get("last_play_at", "0") or 0)
        if last_play and last_play < cutoff:
            try:
                await r.xadd(
                    DECAY_EVENT_STREAM,
                    {
                        "data": json.dumps({
                            "arc_id": arc_id,
                            "user_id": uid,
                            "last_play_at": last_play,
                            "days_idle": int((now - last_play) // DAY_SECONDS),
                            "ts": now,
                        })
                    },
                    maxlen=STREAM_MAXLEN,
                    approximate=True,
                )
                decayed += 1
            except Exception as exc:  # pragma: no cover
                logger.warning("xadd decay failed: %s", exc)
    return {"arc_id": arc_id, "result": "ok", "decayed": decayed}


# ── Discovery ─────────────────────────────────────────────────────────


async def discover_active_arc_ids(r: aioredis.Redis) -> list[str]:
    """Scan every ``arc:{arc_id}`` HASH and return ids that aren't ended.

    Uses SCAN with MATCH so we never block on KEYS, and filters by status
    in Python because Redis can't filter HASH fields with SCAN directly.
    """
    out: list[str] = []
    cursor = 0
    while True:
        cursor, batch = await r.scan(cursor=cursor, match="arc:*", count=200)
        for key in batch:
            # Skip namespaced sub-keys (arc:{id}:foo) — only top-level
            # ``arc:{id}`` HASHes.
            if key.count(":") != 1:
                continue
            arc_id = key.split(":", 1)[1]
            st = await r.hget(key, "status")
            if st in (None, "ended"):
                continue
            out.append(arc_id)
        if cursor == 0:
            break
    return out


# ── Sweeps ────────────────────────────────────────────────────────────


async def run_sweep(r: aioredis.Redis) -> dict[str, Any]:
    """One full sweep: refresh every arc, emit today's drops, decay flag."""
    arc_ids = await discover_active_arc_ids(r)
    emitted = 0
    skipped = 0
    decayed_total = 0
    for arc_id in arc_ids:
        em = await emit_today_drop(r, arc_id)
        if em.get("result") == "emitted":
            emitted += 1
        else:
            skipped += 1
        dec = await detect_engagement_decay(r, arc_id)
        decayed_total += int(dec.get("decayed", 0))
    summary = {
        "ok": True,
        "arcs_scanned": len(arc_ids),
        "drops_emitted": emitted,
        "drops_skipped": skipped,
        "decay_flags": decayed_total,
        "ts": time.time(),
    }
    logger.info("arc_scheduler sweep %s", summary)
    return summary


async def run_loop(once: bool = False) -> None:
    await init_redis()
    try:
        while True:
            r = await get_redis()
            try:
                await run_sweep(r)
            except Exception as exc:  # pragma: no cover
                logger.exception("arc_scheduler sweep failed: %s", exc)
            if once:
                return
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
    finally:
        await close_redis()


# ── CLI ───────────────────────────────────────────────────────────────


def _main() -> None:  # pragma: no cover
    parser = argparse.ArgumentParser(description="Campaign arc scheduler")
    parser.add_argument(
        "--once", action="store_true", help="run a single sweep and exit"
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    asyncio.run(run_loop(once=args.once))


if __name__ == "__main__":  # pragma: no cover
    _main()
