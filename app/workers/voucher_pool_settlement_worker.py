"""Weekly voucher-pool settlement worker.

Each settlement cycle (default: weekly):

  1. Enumerate every pool with at least one recorded flow this week.
  2. For each pool, compute the per-brand net position and a minimal
     transfer plan (see :func:`app.services.voucher_pool.snapshot_settlement`).
  3. Dispatch each leg through
     :func:`app.routers.payouts._inter_brand_transfer_impl` with reason
     ``joint_campaign_settlement`` so it lands in the same double-entry
     ledger the rest of the platform settles against.
  4. Persist the executed plan back onto the snapshot for audit.

Atomicity guarantees come from the underlying transfer impl: the
``WATCH/MULTI`` block in ``_inter_brand_transfer_impl`` either fully
commits both legs of an inter-brand transfer or fully aborts. Per-leg
idempotency keys are derived from ``(pool_id, week, from, to)`` so a
re-run of the worker (after a crash, or because the schedule re-fires)
never double-pays.

Usage::

    .venv/bin/python -m app.workers.voucher_pool_settlement_worker --once
    .venv/bin/python -m app.workers.voucher_pool_settlement_worker

The ``--once`` form is what the production cron invokes weekly.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from typing import Any

import redis.asyncio as aioredis

from app.redis_client import close_redis, get_redis, init_redis
from app.services import voucher_pool as vp

logger = logging.getLogger("voucher_pool_settlement_worker")

# How often to sweep when running in the long-lived loop form. The
# weekly cadence is enforced by ``_current_week()`` semantics — even if
# the loop ticks faster, an already-settled (pool, week) is a no-op
# because the per-leg idempotency keys collide.
LOOP_INTERVAL_SECONDS = 24 * 3600  # 1 day; idempotency makes this safe


def _idem_for_leg(
    pool_id: str, week: int, from_bid: str, to_bid: str
) -> str:
    """Deterministic idempotency key for one settlement leg.

    Re-running settlement for the same (pool, week) MUST collapse to
    the same key for each (from, to) pair so the inter-brand ledger
    short-circuits replays.
    """
    return f"pool_settle:{pool_id}:wk{week}:{from_bid}>{to_bid}"


async def _discover_pools_with_flow(r: aioredis.Redis) -> list[str]:
    """Find every pool that has at least one recorded flow edge.

    Cheaper than walking the full pool index: pools with zero activity
    contribute nothing to settle, so we skip them.
    """
    seen: set[str] = set()
    async for key in r.scan_iter(match="pool:*:flow:*"):
        k = key.decode() if isinstance(key, bytes) else key
        # key shape: pool:{pool_id}:flow:{src}:{dst}
        parts = k.split(":")
        if len(parts) == 5 and parts[0] == "pool" and parts[2] == "flow":
            seen.add(parts[1])
    return sorted(seen)


async def _execute_transfer_leg(
    r: aioredis.Redis,
    *,
    pool_id: str,
    week: int,
    from_brand_id: str,
    to_brand_id: str,
    amount_cents: int,
) -> dict[str, Any]:
    """Move ``amount_cents`` from one brand wallet to another.

    Delegates to the atomic transfer impl in ``app.routers.payouts`` so
    the ledger row + balance moves are a single WATCH/MULTI commit.
    Pure local import: keeps this worker free of FastAPI startup cost
    when invoked from cron.
    """
    from app.routers.payouts import _inter_brand_transfer_impl

    idem = _idem_for_leg(pool_id, week, from_brand_id, to_brand_id)
    try:
        return await _inter_brand_transfer_impl(
            r,
            from_brand_id=from_brand_id,
            to_brand_id=to_brand_id,
            amount_cents=int(amount_cents),
            reason="joint_campaign_settlement",
            reference_id=idem,
            idempotency_key=idem,
            metadata={
                "settlement_source": "voucher_pool",
                "pool_id": pool_id,
                "week": week,
            },
            allow_fx=False,
        )
    except Exception as exc:  # pragma: no cover — defensive: settle != crash
        logger.error(
            "settle leg failed pool=%s week=%d from=%s to=%s amt=%d: %s",
            pool_id, week, from_brand_id, to_brand_id, amount_cents, exc,
        )
        return {"error": str(exc), "leg_failed": True}


async def settle_pool(
    r: aioredis.Redis,
    pool_id: str,
    *,
    week: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run settlement for a single pool.

    Returns a structured report::

        {
            "pool_id", "week", "transfer_count",
            "executed": [...],   # one entry per dispatched leg
            "skipped": [...],    # already-applied legs (idempotent replay)
            "dry_run": bool,
        }
    """
    wk = week if week is not None else vp._current_week()
    snapshot = await vp.snapshot_settlement(r, pool_id, week=wk)
    plan = snapshot.get("transfer_plan", [])

    executed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    if dry_run or not plan:
        logger.info(
            "settle pool=%s week=%d dry_run=%s transfers=%d",
            pool_id, wk, dry_run, len(plan),
        )
        snapshot["executed_at"] = int(time.time()) if not dry_run else None
        snapshot["dry_run"] = dry_run
        return {
            "pool_id": pool_id,
            "week": wk,
            "transfer_count": len(plan),
            "executed": [],
            "skipped": plan if dry_run else [],
            "failed": [],
            "dry_run": dry_run,
        }

    for leg in plan:
        res = await _execute_transfer_leg(
            r,
            pool_id=pool_id,
            week=wk,
            from_brand_id=leg["from_brand_id"],
            to_brand_id=leg["to_brand_id"],
            amount_cents=int(leg["amount_cents"]),
        )
        record = {**leg, "result": res}
        if res.get("leg_failed"):
            failed.append(record)
        elif res.get("idempotent"):
            skipped.append(record)
        else:
            executed.append(record)

    # Persist execution result back onto the snapshot for auditability.
    snapshot["executed_at"] = int(time.time())
    snapshot["dry_run"] = False
    snapshot["executed"] = executed
    snapshot["skipped"] = skipped
    snapshot["failed"] = failed
    await r.hset(
        vp._k_pool_settlement(pool_id, wk),
        mapping={"data": vp._dumps(snapshot)},
    )

    logger.info(
        "settle pool=%s week=%d executed=%d skipped=%d failed=%d",
        pool_id, wk, len(executed), len(skipped), len(failed),
    )
    return {
        "pool_id": pool_id,
        "week": wk,
        "transfer_count": len(plan),
        "executed": executed,
        "skipped": skipped,
        "failed": failed,
        "dry_run": False,
    }


async def run_once(
    r: aioredis.Redis | None = None,
    *,
    week: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """One full sweep across every pool with recorded activity.

    The (optional) ``r`` argument lets tests inject a Redis they already
    own. In production the worker grabs the shared pool itself.
    """
    own_pool = r is None
    if own_pool:
        await init_redis()
        r = await get_redis()
    try:
        pools = await _discover_pools_with_flow(r)
        reports: list[dict[str, Any]] = []
        for pid in pools:
            reports.append(
                await settle_pool(r, pid, week=week, dry_run=dry_run)
            )
        return {
            "swept_at": int(time.time()),
            "pool_count": len(pools),
            "reports": reports,
            "dry_run": dry_run,
        }
    finally:
        if own_pool:
            await close_redis()


async def run_forever(*, dry_run: bool = False) -> None:  # pragma: no cover
    """Long-lived loop. In production, prefer ``--once`` from cron."""
    await init_redis()
    try:
        while True:
            try:
                report = await run_once(await get_redis(), dry_run=dry_run)
                logger.info(
                    "settlement sweep complete pools=%d",
                    report["pool_count"],
                )
            except Exception as exc:
                logger.exception("settlement sweep failed: %s", exc)
            await asyncio.sleep(LOOP_INTERVAL_SECONDS)
    finally:
        await close_redis()


def _main() -> None:  # pragma: no cover — CLI entry
    ap = argparse.ArgumentParser(description="Voucher pool settlement worker")
    ap.add_argument("--once", action="store_true", help="Single sweep then exit")
    ap.add_argument("--dry-run", action="store_true", help="Compute snapshots without dispatching transfers")
    ap.add_argument("--week", type=int, default=None, help="Settle this specific week bucket")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.once:
        asyncio.run(run_once(week=args.week, dry_run=args.dry_run))
    else:
        asyncio.run(run_forever(dry_run=args.dry_run))


if __name__ == "__main__":  # pragma: no cover
    _main()
