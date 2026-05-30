"""Support SLA monitor — flags tickets that breached their first-response SLA.

Runs hourly. Walks the global ticket queue, computes per-ticket SLA status
using :func:`app.routers.support._is_sla_breached`, and for every *newly*
breached ticket:

  * Sets ``support:ticket:{tid}:sla_breach_alerted`` = "1" (idempotent so we
    only alert once per breach event).
  * Pushes a structured alert envelope onto ``support:alerts:queue`` — the
    Slack / email shipper consumes this. (We don't ship from inside the
    worker so the worker has zero hard external dependencies.)
  * Increments ``support:metrics:sla_breaches:total`` for the dashboard.

Public surface
--------------
``run_once(redis=None, *, now=None, dry_run=False)``
    One scan pass. Returns a structured report (counts + per-ticket
    actions). ``dry_run=True`` skips writes; ``now`` lets tests pin the
    clock.

This worker is **not** a forever-loop — schedule via cron / k8s CronJob.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import redis.asyncio as aioredis

from app.redis_client import get_redis
from app.routers.support import _decode_hash, _is_sla_breached, _SLA_SECONDS

logger = logging.getLogger(__name__)


def _alert_key(tid: str) -> str:
    return f"support:ticket:{tid}:sla_breach_alerted"


async def run_once(
    redis: aioredis.Redis | None = None,
    *,
    now: float | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Single scan: flag newly breached tickets, enqueue alert envelopes."""
    r = redis if redis is not None else await get_redis()
    now_ts = now if now is not None else time.time()

    raw_ids = await r.lrange("support:tickets:queue", 0, 4_999)
    scanned = 0
    breached_total = 0
    newly_alerted = 0
    alerts: list[dict[str, Any]] = []

    for raw in raw_ids:
        tid = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
        rec = _decode_hash(await r.hgetall(f"support:ticket:{tid}"))
        if not rec:
            continue
        scanned += 1
        if not _is_sla_breached(rec):
            continue
        breached_total += 1
        # Idempotency — only alert once per breach event.
        if await r.exists(_alert_key(tid)):
            continue

        prio = rec.get("priority", "p2")
        window = _SLA_SECONDS.get(prio, _SLA_SECONDS["p2"])
        try:
            created = float(rec.get("created_ts", "0") or 0)
        except ValueError:
            created = 0.0
        overdue_secs = max(0.0, now_ts - created - window)

        alert = {
            "event": "support_sla_breach",
            "ticket_id": tid,
            "brand_id": rec.get("brand_id", ""),
            "brand_name": rec.get("brand_name", ""),
            "subject": rec.get("subject", ""),
            "priority": prio,
            "assignee": rec.get("assignee", ""),
            "overdue_seconds": int(overdue_secs),
            "sla_window_seconds": window,
            "created_at": rec.get("created_at", ""),
            "ts": now_ts,
        }
        alerts.append(alert)

        if not dry_run:
            pipe = r.pipeline()
            pipe.set(_alert_key(tid), "1")
            pipe.lpush("support:alerts:queue", json.dumps(alert))
            pipe.ltrim("support:alerts:queue", 0, 999)
            pipe.incr("support:metrics:sla_breaches:total")
            await pipe.execute()
            newly_alerted += 1

        logger.warning(
            "support_sla_breach tid=%s brand=%s prio=%s overdue=%ds",
            tid, rec.get("brand_id", ""), prio, int(overdue_secs),
        )

    report = {
        "scanned": scanned,
        "breached_total": breached_total,
        "newly_alerted": newly_alerted,
        "dry_run": dry_run,
        "alerts": alerts,
    }
    logger.info("support_sla_worker report=%s", report)
    return report


if __name__ == "__main__":  # pragma: no cover — cron entrypoint
    asyncio.run(run_once())
