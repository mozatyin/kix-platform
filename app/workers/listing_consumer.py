"""Consume ``events:listing`` Redis stream.

Events handled (written by ``app.routers.listings._emit_event``):

* ``listing.created``         — index for search (text + category + brand)
* ``listing.updated``         — re-index (refresh text/price)
* ``listing.sold``            — fire attribution conversion, buyer notify,
                                 commission credit hook
* ``listing.promoted``        — charge wallet for boost + bump ranking
* ``listing.removed``         — remove from indices
* ``listing.offer_created``   — notify seller (best-effort)
* ``listing.offer_accepted``  — treated as sold (downstream of mark-sold)
* ``listing.offer_countered`` — notify buyer
* ``listing.offer_rejected``  — notify buyer

Same operational primitives as ``reservation_consumer``: consumer group,
XTRIM, DLQ, XPENDING-based stale reclaim, backpressure-aware skips.
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

logger = logging.getLogger("listing_consumer")

# ── Tunables ──────────────────────────────────────────────────────────────
STREAM = "events:listing"
GROUP = "listing_workers"
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


async def _to_dlq(r: aioredis.Redis, fields: dict[str, str], reason: str) -> None:
    payload = dict(fields)
    payload["_dlq_reason"] = reason[:512]
    payload["_dlq_ts"] = str(time.time())
    try:
        await r.xadd(DLQ_STREAM, payload, maxlen=DLQ_MAX_LEN, approximate=True)
    except Exception as exc:  # pragma: no cover
        logger.warning("DLQ write failed: %s", exc)


# ── Search index seam ─────────────────────────────────────────────────────


async def _index_listing(r: aioredis.Redis, lid: str, fields: dict[str, str]) -> None:
    """Add the listing to the search indices.

    MVP uses sorted-set indices keyed by brand/category — the actual full-
    text search system swaps in here. We're not building search now, only
    making sure listings are *findable* by the indices the rest of the
    platform already reads.
    """
    extra = _safe_loads(fields.get("extra", ""))
    brand_id = fields.get("brand_id", "")
    category = extra.get("category", "")
    ts = float(fields.get("at", time.time()))
    try:
        if brand_id:
            await r.zadd(f"index:brand:{brand_id}:listings", {lid: ts})
        if category:
            await r.zadd(f"index:category:{category}:listings", {lid: ts})
        await r.zadd("index:listings:recent", {lid: ts})
    except Exception as exc:
        logger.debug("index add best-effort: %s", exc)


async def _deindex_listing(r: aioredis.Redis, lid: str, fields: dict[str, str]) -> None:
    extra = _safe_loads(fields.get("extra", ""))
    brand_id = fields.get("brand_id", "")
    category = extra.get("category", "")
    try:
        if brand_id:
            await r.zrem(f"index:brand:{brand_id}:listings", lid)
        if category:
            await r.zrem(f"index:category:{category}:listings", lid)
        await r.zrem("index:listings:recent", lid)
    except Exception as exc:
        logger.debug("index rem best-effort: %s", exc)


# ── Event handlers ────────────────────────────────────────────────────────


async def _handle_created(r: aioredis.Redis, fields: dict[str, str]) -> None:
    lid = fields.get("listing_id", "")
    if not lid:
        return
    await _index_listing(r, lid, fields)


async def _handle_updated(r: aioredis.Redis, fields: dict[str, str]) -> None:
    lid = fields.get("listing_id", "")
    if not lid:
        return
    # Re-index (idempotent — zadd overwrites timestamp).
    await _index_listing(r, lid, fields)


async def _handle_sold(r: aioredis.Redis, fields: dict[str, str]) -> None:
    """Remove from active indices, fire attribution + buyer notification."""
    lid = fields.get("listing_id", "")
    brand_id = fields.get("brand_id", "")
    seller_id = fields.get("user_id", "")
    if not lid:
        return

    await _deindex_listing(r, lid, fields)

    extra = _safe_loads(fields.get("extra", ""))
    buyer_id = extra.get("buyer_user_id", "")
    sale_price = int(extra.get("sale_price_cents", 0) or 0)

    # Attribution conversion (best-effort).
    try:
        from app.routers.attribution import STAGE_CONVERSION, _persist_event

        if buyer_id and brand_id:
            await _persist_event(
                r,
                stage=STAGE_CONVERSION,
                user_id=buyer_id,
                target_brand=brand_id,
                value_cents=sale_price,
                meta={"listing_id": lid, "source": "listing.sold"},
            )
    except Exception as exc:
        logger.warning("attribution fire failed lid=%s: %s", lid, exc)

    # Commission credit hook: bump brand's pending payout counter.
    try:
        if brand_id and sale_price:
            await r.hincrby(f"brand:{brand_id}:payout_pending", "gross_cents", sale_price)
            await r.hincrby(f"brand:{brand_id}:payout_pending", "items_sold", 1)
    except Exception:
        pass

    # Buyer notification audit (real push is fanned out via push_engine
    # by HTTP handlers; here we just leave a breadcrumb).
    try:
        if buyer_id:
            await r.xadd(
                f"user:{buyer_id}:inbox_feed",
                {"event": "listing_sold", "listing_id": lid, "at": str(time.time())},
                maxlen=1_000,
                approximate=True,
            )
    except Exception:
        pass

    # Seller stats
    try:
        if seller_id:
            await r.hincrby(f"user:{seller_id}:seller_stats", "sold_count", 1)
            if sale_price:
                await r.hincrby(
                    f"user:{seller_id}:seller_stats", "gross_cents", sale_price
                )
    except Exception:
        pass


async def _handle_promoted(r: aioredis.Redis, fields: dict[str, str]) -> None:
    """Boost ranking. Wallet charge is the producer's responsibility."""
    lid = fields.get("listing_id", "")
    if not lid:
        return
    extra = _safe_loads(fields.get("extra", ""))
    boost_score = float(extra.get("boost_score", 1.0) or 1.0)
    boost_until = float(extra.get("boost_until", time.time() + 86400) or 0)
    try:
        await r.zadd(
            "index:listings:boosted",
            {lid: boost_until},
        )
        await r.hset(
            f"listing:{lid}:boost",
            mapping={"score": str(boost_score), "until": str(boost_until)},
        )
    except Exception as exc:
        logger.debug("boost write best-effort: %s", exc)


async def _handle_removed(r: aioredis.Redis, fields: dict[str, str]) -> None:
    lid = fields.get("listing_id", "")
    if not lid:
        return
    await _deindex_listing(r, lid, fields)
    try:
        await r.zrem("index:listings:boosted", lid)
    except Exception:
        pass


async def _handle_offer(r: aioredis.Redis, fields: dict[str, str]) -> None:
    """Audit-only fanout for offer_* events; real push handled elsewhere."""
    lid = fields.get("listing_id", "")
    if not lid:
        return
    try:
        await r.hincrby(f"listing:{lid}:counters", fields.get("event_type", "offer"), 1)
    except Exception:
        pass


_HANDLERS = {
    "listing.created": _handle_created,
    "listing.updated": _handle_updated,
    "listing.sold": _handle_sold,
    "listing.promoted": _handle_promoted,
    "listing.removed": _handle_removed,
    "listing.offer_created": _handle_offer,
    "listing.offer_accepted": _handle_sold,  # offer accept is effectively a sale
    "listing.offer_countered": _handle_offer,
    "listing.offer_rejected": _handle_offer,
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
    if backpressured and event_type not in ("listing.sold", "listing.offer_accepted"):
        # In overload mode, only revenue-critical events run; indexing
        # catches up on subsequent reclaim.
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
            "listing_consumer started: stream=%s group=%s consumer=%s",
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
