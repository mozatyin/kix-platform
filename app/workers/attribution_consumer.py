"""Consume ``events:attribution`` Redis stream.

Events handled (written across multiple routers):

* ``track.click``         — update CTR stats (click count + per-brand)
* ``track.conversion``    — update CVR + fire commission credit
* ``track.view_through``  — update view-through conversion stats
* ``visit_completed``     — legacy alias emitted by reservations.honored;
                             treated as a verified visit + CTR-style credit

Same operational primitives as the other consumers:
* Redis Streams consumer group with at-least-once delivery
* XTRIM MAXLEN ~1M to bound memory
* DLQ at ``events:attribution:dlq`` after 5 delivery attempts
* XPENDING + XCLAIM reclaim of crashed-worker entries (>60s idle)
* Backpressure-aware skip (in overload only conversion events run)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import time
from typing import Any

import redis.asyncio as aioredis

from app.redis_client import close_redis, get_redis, init_redis

logger = logging.getLogger("attribution_consumer")

# ── Tunables ──────────────────────────────────────────────────────────────
STREAM = "events:attribution"
GROUP = "attribution_workers"
CONSUMER_NAME = f"worker_{socket.gethostname()}_{os.getpid()}"

BATCH_SIZE = 100
BLOCK_MS = 5_000
MAX_LEN_APPROX = 1_000_000
DLQ_STREAM = f"{STREAM}:dlq"
DLQ_MAX_LEN = 100_000
MAX_DELIVERIES = 5
RECLAIM_IDLE_MS = 60_000
BACKPRESSURE_LAG = 10_000


# ── Helpers ───────────────────────────────────────────────────────────────


async def _ensure_group(r: aioredis.Redis) -> None:
    try:
        await r.xgroup_create(STREAM, GROUP, id="$", mkstream=True)
        logger.info("created consumer group %s on %s", GROUP, STREAM)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            logger.debug("xgroup_create non-fatal: %s", exc)


def _safe_loads(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _today_bucket() -> str:
    return time.strftime("%Y%m%d", time.gmtime())


async def _to_dlq(r: aioredis.Redis, fields: dict[str, str], reason: str) -> None:
    payload = dict(fields)
    payload["_dlq_reason"] = reason[:512]
    payload["_dlq_ts"] = str(time.time())
    try:
        await r.xadd(DLQ_STREAM, payload, maxlen=DLQ_MAX_LEN, approximate=True)
    except Exception as exc:  # pragma: no cover
        logger.warning("DLQ write failed: %s", exc)


# ── Event handlers ────────────────────────────────────────────────────────


async def _handle_click(r: aioredis.Redis, fields: dict[str, str]) -> None:
    """Increment CTR counters keyed by brand and (brand, campaign, day)."""
    brand_id = fields.get("brand_id") or fields.get("target_brand", "")
    campaign_id = fields.get("campaign_id", "")
    day = _today_bucket()
    if not brand_id:
        return
    try:
        await r.hincrby(f"brand:{brand_id}:ctr_stats", "clicks", 1)
        await r.hincrby(f"brand:{brand_id}:ctr_stats:{day}", "clicks", 1)
        if campaign_id:
            await r.hincrby(f"campaign:{campaign_id}:ctr_stats", "clicks", 1)
            await r.hincrby(f"campaign:{campaign_id}:ctr_stats:{day}", "clicks", 1)
    except Exception as exc:
        logger.debug("click counter best-effort: %s", exc)


async def _handle_conversion(r: aioredis.Redis, fields: dict[str, str]) -> None:
    """Increment CVR counters + fire commission credit hook."""
    brand_id = fields.get("brand_id") or fields.get("target_brand", "")
    source_brand = fields.get("source_brand", "")
    campaign_id = fields.get("campaign_id", "")
    value_cents = 0
    try:
        value_cents = int(fields.get("value_cents", 0) or 0)
    except (TypeError, ValueError):
        value_cents = 0
    day = _today_bucket()
    if not brand_id:
        return

    try:
        await r.hincrby(f"brand:{brand_id}:cvr_stats", "conversions", 1)
        await r.hincrby(f"brand:{brand_id}:cvr_stats:{day}", "conversions", 1)
        if value_cents:
            await r.hincrby(
                f"brand:{brand_id}:cvr_stats", "value_cents", value_cents
            )
            await r.hincrby(
                f"brand:{brand_id}:cvr_stats:{day}", "value_cents", value_cents
            )
        if campaign_id:
            await r.hincrby(
                f"campaign:{campaign_id}:cvr_stats", "conversions", 1
            )
            if value_cents:
                await r.hincrby(
                    f"campaign:{campaign_id}:cvr_stats", "value_cents", value_cents
                )
    except Exception as exc:
        logger.debug("cvr counter best-effort: %s", exc)

    # Commission credit hook — if a source brand referred this conversion,
    # accrue commission for them (1% of value as a placeholder; the real
    # rate lives in commission_program.py).
    if source_brand and value_cents:
        try:
            commission = max(1, value_cents // 100)
            await r.hincrby(
                f"brand:{source_brand}:commission_pending",
                "value_cents",
                commission,
            )
            await r.xadd(
                "events:commission",
                {
                    "event_type": "commission.accrued",
                    "source_brand": source_brand,
                    "target_brand": brand_id,
                    "value_cents": str(commission),
                    "ref_conversion_value_cents": str(value_cents),
                    "at": str(time.time()),
                },
                maxlen=1_000_000,
                approximate=True,
            )
        except Exception as exc:
            logger.debug("commission accrual best-effort: %s", exc)


async def _handle_view_through(r: aioredis.Redis, fields: dict[str, str]) -> None:
    """Update view-through conversion counters."""
    brand_id = fields.get("brand_id") or fields.get("target_brand", "")
    source_brand = fields.get("source_brand", "")
    day = _today_bucket()
    if not brand_id:
        return
    try:
        await r.hincrby(f"brand:{brand_id}:view_through_stats", "count", 1)
        await r.hincrby(
            f"brand:{brand_id}:view_through_stats:{day}", "count", 1
        )
        if source_brand:
            await r.hincrby(
                f"brand:{source_brand}:view_through_outgoing", "count", 1
            )
    except Exception as exc:
        logger.debug("view_through counter best-effort: %s", exc)


async def _handle_visit_completed(r: aioredis.Redis, fields: dict[str, str]) -> None:
    """Reservation-honored emits this; treat as a verified visit + counter."""
    brand_id = fields.get("brand_id", "")
    user_id = fields.get("user_id", "")
    day = _today_bucket()
    if not brand_id:
        return
    try:
        await r.hincrby(f"brand:{brand_id}:visit_stats", "visits", 1)
        await r.hincrby(f"brand:{brand_id}:visit_stats:{day}", "visits", 1)
        if user_id:
            await r.zadd(
                f"brand:{brand_id}:visitors",
                {user_id: time.time()},
            )
    except Exception as exc:
        logger.debug("visit counter best-effort: %s", exc)


_HANDLERS = {
    "track.click": _handle_click,
    "track.conversion": _handle_conversion,
    "track.view_through": _handle_view_through,
    "visit_completed": _handle_visit_completed,
}


async def process_event(
    r: aioredis.Redis, event_id: str, fields: dict[str, str], *, backpressured: bool
) -> None:
    event_type = fields.get("event_type", "")
    if not event_type:
        raise ValueError("missing_event_type")
    handler = _HANDLERS.get(event_type)
    if handler is None:
        logger.debug("skip unknown event_type=%s id=%s", event_type, event_id)
        return
    if backpressured and event_type != "track.conversion":
        logger.warning("backpressure skip: %s id=%s", event_type, event_id)
        return
    await handler(r, fields)


# ── Pending reclaim + dispatch ────────────────────────────────────────────


async def reclaim_stale(r: aioredis.Redis) -> int:
    try:
        summary = await r.xpending(STREAM, GROUP)
    except Exception:
        return 0
    if not summary:
        return 0
    pending_count = summary.get("pending", 0) if isinstance(summary, dict) else 0
    if not pending_count:
        return 0

    try:
        entries = await r.xpending_range(STREAM, GROUP, min="-", max="+", count=100)
    except Exception:
        return 0

    stale_ids = [
        e["message_id"]
        for e in entries
        if e.get("time_since_delivered", 0) >= RECLAIM_IDLE_MS
        and e.get("consumer") != CONSUMER_NAME
    ]
    if not stale_ids:
        return 0

    try:
        claimed = await r.xclaim(
            STREAM,
            GROUP,
            CONSUMER_NAME,
            min_idle_time=RECLAIM_IDLE_MS,
            message_ids=stale_ids,
        )
    except Exception as exc:
        logger.debug("xclaim failed: %s", exc)
        return 0

    for event_id, fields in claimed or []:
        await _dispatch_with_dlq(r, event_id, fields, reclaimed=True)
    return len(claimed or [])


async def _dispatch_with_dlq(
    r: aioredis.Redis,
    event_id: str,
    fields: dict[str, str],
    *,
    reclaimed: bool = False,
) -> tuple[bool, bool]:
    deliveries = 1
    try:
        pend = await r.xpending_range(
            STREAM, GROUP, min=event_id, max=event_id, count=1
        )
        if pend:
            deliveries = int(pend[0].get("times_delivered", 1))
    except Exception:
        pass

    if deliveries > MAX_DELIVERIES:
        await _to_dlq(r, fields, reason=f"exceeded_{MAX_DELIVERIES}_deliveries")
        await r.xack(STREAM, GROUP, event_id)
        logger.warning("→ DLQ id=%s deliveries=%d", event_id, deliveries)
        return False, True

    try:
        summary = await r.xpending(STREAM, GROUP)
        lag = summary.get("pending", 0) if isinstance(summary, dict) else 0
    except Exception:
        lag = 0
    backpressured = lag > BACKPRESSURE_LAG

    try:
        await process_event(r, event_id, fields, backpressured=backpressured)
        await r.xack(STREAM, GROUP, event_id)
        return True, False
    except Exception as exc:
        logger.exception(
            "process failed id=%s reclaimed=%s deliveries=%d: %s",
            event_id, reclaimed, deliveries, exc,
        )
        return False, False


# ── Loop ──────────────────────────────────────────────────────────────────


async def run_once() -> dict[str, int]:
    r = await get_redis()
    await _ensure_group(r)

    reclaimed = await reclaim_stale(r)

    messages = await r.xreadgroup(
        GROUP,
        CONSUMER_NAME,
        {STREAM: ">"},
        count=BATCH_SIZE,
        block=BLOCK_MS,
    )

    processed = 0
    failed = 0
    dlq = 0

    for _stream, events in messages or []:
        for event_id, fields in events:
            ok, sent = await _dispatch_with_dlq(r, event_id, fields)
            if ok:
                processed += 1
            else:
                if sent:
                    dlq += 1
                else:
                    failed += 1

    try:
        await r.xtrim(STREAM, maxlen=MAX_LEN_APPROX, approximate=True)
    except Exception as exc:
        logger.debug("xtrim non-fatal: %s", exc)

    return {
        "processed": processed,
        "failed": failed,
        "dlq": dlq,
        "reclaimed": reclaimed,
    }


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    await init_redis()
    try:
        if "--once" in sys.argv:
            result = await run_once()
            print(json.dumps(result))
            return
        logger.info(
            "attribution_consumer started: stream=%s group=%s consumer=%s",
            STREAM, GROUP, CONSUMER_NAME,
        )
        while True:
            try:
                result = await run_once()
                if any(result.values()):
                    logger.info("cycle: %s", result)
            except Exception as exc:
                logger.exception("cycle failed: %s", exc)
            await asyncio.sleep(0.1)
    finally:
        await close_redis()


if __name__ == "__main__":
    asyncio.run(main())
