"""Outbound webhook delivery worker.

Polls ``webhook:delivery:queue`` (a ZSET, score = scheduled-at unix ts)
and POSTs signed payloads to merchant target URLs. Non-2xx responses or
transport exceptions are re-enqueued with exponential backoff per the
schedule defined in :mod:`app.routers.webhooks_outbound`
(1m → 5m → 30m → 2h → 24h, then ``failed_permanent``).

Usage
-----
::

    .venv/bin/python -m app.workers.webhook_worker --once
    .venv/bin/python -m app.workers.webhook_worker

Reads/writes the same Redis schema documented in
``app.routers.webhooks_outbound`` — keep this module thin and delegate the
actual signing + state transitions to the router so the contract has one
home.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from app.redis_client import close_redis, get_redis, init_redis
from app.routers.webhooks_outbound import drain_due_deliveries

logger = logging.getLogger("webhook_worker")

POLL_INTERVAL_SECONDS = 10
BATCH_SIZE = 50


async def run_once() -> dict[str, int]:
    r = await get_redis()
    return await drain_due_deliveries(r, batch_size=BATCH_SIZE)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="webhook_worker")
    p.add_argument("--once", action="store_true",
                   help="run one cycle and exit")
    p.add_argument("--interval", type=float, default=POLL_INTERVAL_SECONDS,
                   help="poll interval seconds (default %(default)s)")
    return p.parse_args(argv)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    args = _parse_args(sys.argv[1:])

    await init_redis()
    try:
        if args.once:
            result = await run_once()
            print(json.dumps(result))
            return
        logger.info(
            "webhook_worker started: polling every %ss", args.interval,
        )
        while True:
            try:
                result = await run_once()
                if result.get("due"):
                    logger.info("cycle: %s", result)
            except Exception as exc:  # noqa: BLE001
                logger.exception("cycle failed: %s", exc)
            await asyncio.sleep(args.interval)
    finally:
        await close_redis()


if __name__ == "__main__":
    asyncio.run(main())
