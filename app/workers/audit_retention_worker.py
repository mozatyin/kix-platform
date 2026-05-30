"""Audit-log retention worker — daily purge of expired evidence rows.

Each row in ``audit_log`` carries an optional ``retention_until``
timestamp computed from the row's ``jurisdiction`` against
``app/compliance_regional/*``. When that timestamp has passed the row
is past its regulator-mandated retention horizon and MUST be deleted
to satisfy data-minimisation (GDPR Art. 5(1)(e), PIPL §47).

This worker is a thin, cron-style driver around
``audit_log_service.purge_expired``. The service does the actual
DELETE using the partial index ``idx_audit_retention`` so the scan is
O(expired) not O(table); the worker's job is just to schedule the call
and emit metrics.

Schedule
--------
* Daily at 02:30 local infra time (off-peak)
* Catch-up safe — re-running missed days simply purges more rows in
  one pass; the partial index keeps the cost proportional to backlog.

Usage
-----
    .venv/bin/python -m app.workers.audit_retention_worker --once
    .venv/bin/python -m app.workers.audit_retention_worker
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from typing import Any

from app.compliance_regional import REGIONAL_RULES
from app.database import async_session_factory
from app.services import audit_log_service as svc

logger = logging.getLogger("audit_retention")

# 24h cadence — retention is daily, finer granularity is overkill.
CHECK_INTERVAL_SECONDS = 24 * 60 * 60


async def _refresh_retention_for_unset(session_factory: Any) -> int:
    """Backfill ``retention_until`` for rows that lack one but have a
    known jurisdiction.

    Rows imported from the legacy Redis lists land with
    ``retention_until=NULL`` (see ``migrate_audit_redis_to_pg``). Once a
    jurisdiction is tagged on them (manually or via downstream service),
    this worker fills in the retention horizon so subsequent sweeps can
    purge them.

    Returns the number of rows updated.
    """
    total = 0
    for region in REGIONAL_RULES.keys():
        async with session_factory() as db:
            n = await svc.apply_retention_policy(db, jurisdiction=region)
            if n:
                logger.info(
                    "retention backfill: jurisdiction=%s rows=%d", region, n
                )
            total += n
    return total


async def run_once(session_factory: Any | None = None) -> dict[str, int]:
    """Single sweep: backfill retention horizons, then purge expired."""
    sf = session_factory or async_session_factory

    t0 = time.time()
    backfilled = await _refresh_retention_for_unset(sf)

    async with sf() as db:
        purged = await svc.purge_expired(db)

    elapsed = time.time() - t0
    summary = {
        "backfilled": backfilled,
        "purged": purged,
        "elapsed_ms": int(elapsed * 1000),
    }
    logger.info("audit_retention sweep: %s", summary)
    return summary


async def run_forever(session_factory: Any | None = None) -> None:
    """Continuous loop — sleep CHECK_INTERVAL_SECONDS between sweeps."""
    sf = session_factory or async_session_factory
    while True:
        try:
            await run_once(sf)
        except Exception as exc:  # pragma: no cover — log + continue
            logger.exception("audit_retention sweep failed: %s", exc)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single sweep and exit (cron-style invocation).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.once:
        asyncio.run(run_once())
    else:
        asyncio.run(run_forever())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
