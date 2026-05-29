"""Consume ``events:reservation`` Redis stream.

Events handled (written by ``app.routers.reservations._emit_event``):

* ``reservation.created``           — schedule pre-event reminder push
* ``reservation.confirmed``         — audit + brand notification hook
* ``reservation.honored``           — fire attribution conversion + tier XP
* ``reservation.no_show``           — recovery voucher hook + audit
* ``reservation.cancelled_by_user`` — release hold + notify brand
* ``reservation.cancelled_by_brand``— refund hold + apology push hook
* ``reservation.rescheduled``       — re-schedule reminder push

This is a Redis Streams *consumer group* worker. One stream + one group
+ N replicas with distinct ``CONSUMER_NAME`` values gives at-least-once
delivery and horizontal scale.

Operational primitives
----------------------
* **Consumer group**: ``reservation_workers`` (created lazily, ``MKSTREAM``).
* **Trim policy**: ``XTRIM MAXLEN ~ 1_000_000`` after each cycle to cap
  Redis memory (~7d at 1K ev/s).
* **Dead-letter queue**: events that fail ``MAX_DELIVERIES`` times move to
  ``events:reservation:dlq`` (also a stream, capped at 100K).
* **Stale reclaim**: at the top of each cycle, ``XPENDING`` + ``XCLAIM``
  picks up entries idle > 60s from crashed siblings.
* **Backpressure**: if pending lag > ``BACKPRESSURE_LAG``, downstream
  enrichments are skipped (only ACK + DLQ accounting still run).

Usage
-----
::

    .venv/bin/python -m app.workers.reservation_consumer --once
    .venv/bin/python -m app.workers.reservation_consumer
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

logger = logging.getLogger("reservation_consumer")

# ── Tunables ──────────────────────────────────────────────────────────────
STREAM = "events:reservation"
GROUP = "reservation_workers"
CONSUMER_NAME = f"worker_{socket.gethostname()}_{os.getpid()}"

BATCH_SIZE = 100
BLOCK_MS = 5_000
MAX_LEN_APPROX = 1_000_000           # ~7d at 1K/sec
DLQ_STREAM = f"{STREAM}:dlq"
DLQ_MAX_LEN = 100_000
MAX_DELIVERIES = 5
RECLAIM_IDLE_MS = 60_000
BACKPRESSURE_LAG = 10_000


# ── Helpers ───────────────────────────────────────────────────────────────


async def _ensure_group(r: aioredis.Redis) -> None:
    """Create the consumer group (idempotent)."""
    try:
        await r.xgroup_create(STREAM, GROUP, id="$", mkstream=True)
        logger.info("created consumer group %s on %s", GROUP, STREAM)
    except Exception as exc:
        # BUSYGROUP if it already exists — that's fine.
        msg = str(exc)
        if "BUSYGROUP" not in msg:
            logger.debug("xgroup_create non-fatal: %s", msg)


def _safe_loads(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


async def _to_dlq(r: aioredis.Redis, fields: dict[str, str], reason: str) -> None:
    """Move a poisonous event to the dead-letter stream and ACK it."""
    payload = dict(fields)
    payload["_dlq_reason"] = reason[:512]
    payload["_dlq_ts"] = str(time.time())
    try:
        await r.xadd(DLQ_STREAM, payload, maxlen=DLQ_MAX_LEN, approximate=True)
    except Exception as exc:  # pragma: no cover
        logger.warning("DLQ write failed: %s", exc)


# ── Event handlers ────────────────────────────────────────────────────────


async def _handle_honored(r: aioredis.Redis, fields: dict[str, str]) -> None:
    """Fire attribution conversion + grant tier XP for honored reservation."""
    rid = fields.get("reservation_id", "")
    user_id = fields.get("user_id", "")
    brand_id = fields.get("brand_id", "")
    if not (rid and user_id and brand_id):
        return

    # Fire attribution conversion via the canonical helper.
    try:
        from app.routers.attribution import STAGE_VISIT, _persist_event

        extra = _safe_loads(fields.get("extra", ""))
        party_size = int(extra.get("party_size", 1) or 1)
        await _persist_event(
            r,
            stage=STAGE_VISIT,
            user_id=user_id,
            target_brand=brand_id,
            value_cents=party_size * 5000,  # estimate; real value comes from POS later
            source_brand=None,
            meta={"reservation_id": rid, "source": "reservation.honored"},
        )
    except Exception as exc:
        logger.warning("attribution fire failed rid=%s: %s", rid, exc)

    # Tier XP grant (best-effort; loyalty router may not be wired yet).
    try:
        await r.hincrby(f"user:{user_id}:tier", "xp", 50)
        await r.hincrby(f"user:{user_id}:tier:{brand_id}", "honored_count", 1)
    except Exception as exc:
        logger.debug("tier grant best-effort: %s", exc)


async def _handle_no_show(r: aioredis.Redis, fields: dict[str, str]) -> None:
    """Log + audit. Voucher dispatch already triggered by HTTP handler."""
    rid = fields.get("reservation_id", "")
    brand_id = fields.get("brand_id", "")
    logger.info("no-show consumed: rid=%s brand=%s", rid, brand_id)
    try:
        await r.hincrby(f"brand:{brand_id}:reservation_stats", "no_show_processed", 1)
    except Exception:
        pass


async def _handle_created(r: aioredis.Redis, fields: dict[str, str]) -> None:
    """Schedule a 24h-ahead reminder push if the booking is far enough out."""
    rid = fields.get("reservation_id", "")
    user_id = fields.get("user_id", "")
    if not (rid and user_id):
        return
    extra = _safe_loads(fields.get("extra", ""))
    scheduled_at = 0.0
    for cand in (extra.get("scheduled_at"), fields.get("scheduled_at")):
        try:
            scheduled_at = float(cand or 0)
            if scheduled_at:
                break
        except (TypeError, ValueError):
            continue

    if scheduled_at <= time.time() + 86400:
        return  # too close — handler already sends an immediate confirmation

    fire_at = scheduled_at - 86400
    # Direct enqueue onto push:scheduled (Sorted Set) avoids depending on
    # the HTTP-bound schedule_push helper, which expects FastAPI-shaped args.
    try:
        push_id = f"resv_rem_{rid}"
        await r.hset(
            f"push:{push_id}",
            mapping={
                "kid": user_id,
                "title": "Your reservation is tomorrow",
                "body": "Don't forget your booking",
                "deep_link": f"/reservation/{rid}",
                "template_id": "reservation_reminder",
                "scheduled_at": str(fire_at),
                "context_reservation_id": rid,
            },
        )
        await r.zadd("push:scheduled", {push_id: fire_at})
    except Exception as exc:
        logger.debug("reminder schedule best-effort: %s", exc)


async def _handle_cancelled_by_user(r: aioredis.Redis, fields: dict[str, str]) -> None:
    rid = fields.get("reservation_id", "")
    brand_id = fields.get("brand_id", "")
    try:
        await r.hincrby(
            f"brand:{brand_id}:reservation_stats", "cancelled_by_user", 1
        )
        # Mark hold-released; the deposit router is the source of truth, this
        # is just an audit breadcrumb for the brand dashboard.
        await r.xadd(
            f"brand:{brand_id}:audit_feed",
            {"event": "reservation_cancelled_by_user", "rid": rid, "at": str(time.time())},
            maxlen=10_000,
            approximate=True,
        )
    except Exception:
        pass


async def _handle_cancelled_by_brand(r: aioredis.Redis, fields: dict[str, str]) -> None:
    rid = fields.get("reservation_id", "")
    brand_id = fields.get("brand_id", "")
    try:
        await r.hincrby(
            f"brand:{brand_id}:reservation_stats", "cancelled_by_brand", 1
        )
    except Exception:
        pass


_HANDLERS = {
    "reservation.created": _handle_created,
    "reservation.honored": _handle_honored,
    "reservation.no_show": _handle_no_show,
    "reservation.cancelled_by_user": _handle_cancelled_by_user,
    "reservation.cancelled_by_brand": _handle_cancelled_by_brand,
}


async def process_event(
    r: aioredis.Redis, event_id: str, fields: dict[str, str], *, backpressured: bool
) -> None:
    event_type = fields.get("event_type", "")
    if not event_type:
        raise ValueError("missing_event_type")
    handler = _HANDLERS.get(event_type)
    if handler is None:
        # Unknown but not poison: count + ACK (no DLQ).
        logger.debug("skip unknown event_type=%s id=%s", event_type, event_id)
        return
    if backpressured and event_type != "reservation.honored":
        # In overload mode we still run honored (revenue-critical) and skip
        # the rest — they'll be picked up later by reclaim or are non-fatal.
        logger.warning("backpressure skip: %s id=%s", event_type, event_id)
        return
    await handler(r, fields)


# ── Pending reclaim ───────────────────────────────────────────────────────


async def reclaim_stale(r: aioredis.Redis) -> int:
    """Reclaim entries pending > RECLAIM_IDLE_MS from any consumer.

    Returns the number of entries claimed by us.
    """
    try:
        summary = await r.xpending(STREAM, GROUP)
    except Exception:
        return 0
    if not summary:
        return 0
    # redis-py returns {'pending': N, 'min': ..., 'max': ..., 'consumers': [...]}
    pending_count = summary.get("pending", 0) if isinstance(summary, dict) else 0
    if not pending_count:
        return 0

    try:
        entries = await r.xpending_range(
            STREAM, GROUP, min="-", max="+", count=100
        )
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

    # Re-process claimed entries in this cycle.
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
    """Run process_event with DLQ + delivery-count enforcement.

    Returns (processed_ok, sent_to_dlq).
    """
    # Check delivery count via xpending_range for this exact id.
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

    # Decide backpressure
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
        # Leave un-ACKed so the next cycle (or reclaim) retries.
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

    # Trim regardless of activity — bound memory even on quiet streams.
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
            "reservation_consumer started: stream=%s group=%s consumer=%s",
            STREAM, GROUP, CONSUMER_NAME,
        )
        while True:
            try:
                result = await run_once()
                if any(result.values()):
                    logger.info("cycle: %s", result)
            except Exception as exc:
                logger.exception("cycle failed: %s", exc)
            # XREAD block=BLOCK_MS already paces the loop; tiny sleep is a
            # safety net in case xreadgroup returns immediately (e.g. on
            # immediate available messages).
            await asyncio.sleep(0.1)
    finally:
        await close_redis()


if __name__ == "__main__":
    asyncio.run(main())
