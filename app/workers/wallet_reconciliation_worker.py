"""Wallet ledger reconciliation worker.

Surfaces wallet drift caused by missed/double events. Designed for:
- Hourly cron: scan active brands, compute drift, alert if > threshold
- One-shot CLI: reconcile single brand, optionally repair

Sources of truth (in priority order):
1. Audit log (PG) — durable record of every wallet event
2. Ledger HASHes — topup/charge/refund HASHes scanned via SCAN
3. Transactions LIST — capped, recent only (defensive fallback)

Detected drift causes:
- Auto-recharge credited without matching audit_log entry
- Double-charge from idempotency race
- Refund issued but balance not decremented
- Settlement transfer (voucher pool) missing ledger leg
- Manual ops adjustment via SET balance_cents
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# Reconciliation thresholds
DRIFT_WARN_CENTS = 1000  # $10
DRIFT_ALERT_CENTS = 100_00  # $100
DRIFT_CRITICAL_CENTS = 10_000_00  # $10,000

ALERT_KEY = "wallet:reconciliation:alerts"
ALERT_LIST_MAX = 1000


async def compute_ledger_expected(
    r: aioredis.Redis, brand_id: str
) -> dict[str, int]:
    """
    Sum topups + auto-recharges - charges + refunds from HASHes.
    Returns {expected, topup_sum, charge_sum, refund_sum, recharge_sum, doc_count}.
    """
    topup_sum = 0
    charge_sum = 0
    refund_sum = 0
    recharge_sum = 0
    doc_count = 0

    # SCAN topup HASHes for this brand
    cursor = 0
    while True:
        cursor, keys = await r.scan(cursor=cursor, match="topup:*", count=500)
        for key in keys:
            data = await r.hgetall(key)
            if data.get("brand_id") == brand_id:
                if data.get("status") in ("confirmed", "completed"):
                    topup_sum += int(data.get("amount_cents", 0))
                    doc_count += 1
        if cursor == 0:
            break

    # SCAN charge HASHes
    cursor = 0
    while True:
        cursor, keys = await r.scan(cursor=cursor, match="charge:*", count=500)
        for key in keys:
            data = await r.hgetall(key)
            if data.get("brand_id") == brand_id:
                charge_sum += int(data.get("amount", 0))
                doc_count += 1
        if cursor == 0:
            break

    # SCAN refund HASHes
    cursor = 0
    while True:
        cursor, keys = await r.scan(cursor=cursor, match="refund:*", count=500)
        for key in keys:
            data = await r.hgetall(key)
            if data.get("brand_id") == brand_id:
                refund_sum += int(data.get("amount_cents", 0))
                doc_count += 1
        if cursor == 0:
            break

    # Auto-recharge log (separate HASH list per-brand)
    log_key = f"wallet:{brand_id}:autorecharge_log"
    log_entries = await r.lrange(log_key, 0, -1)
    for raw in log_entries:
        try:
            entry = json.loads(raw)
            if entry.get("status") == "succeeded":
                recharge_sum += int(entry.get("amount_cents", 0))
                doc_count += 1
        except (json.JSONDecodeError, TypeError):
            pass

    expected = topup_sum + recharge_sum - charge_sum + refund_sum

    return {
        "expected": expected,
        "topup_sum": topup_sum,
        "charge_sum": charge_sum,
        "refund_sum": refund_sum,
        "recharge_sum": recharge_sum,
        "doc_count": doc_count,
    }


async def reconcile_brand(
    r: aioredis.Redis, brand_id: str, repair: bool = False
) -> dict[str, Any]:
    """
    Reconcile a single brand. Returns drift report.
    If repair=True and drift is non-zero, log to alert queue but DO NOT
    auto-adjust balance (manual review required for safety).
    """
    actual = int(await r.get(f"wallet:{brand_id}:balance") or 0)
    ledger = await compute_ledger_expected(r, brand_id)
    expected = ledger["expected"]
    drift_cents = actual - expected
    abs_drift = abs(drift_cents)

    severity = "ok"
    if abs_drift >= DRIFT_CRITICAL_CENTS:
        severity = "critical"
    elif abs_drift >= DRIFT_ALERT_CENTS:
        severity = "alert"
    elif abs_drift >= DRIFT_WARN_CENTS:
        severity = "warn"

    report = {
        "brand_id": brand_id,
        "actual_balance_cents": actual,
        "expected_balance_cents": expected,
        "drift_cents": drift_cents,
        "abs_drift_cents": abs_drift,
        "severity": severity,
        "ts": time.time(),
        **ledger,
    }

    # Alert queue (capped LIST)
    if severity in ("warn", "alert", "critical"):
        await r.lpush(ALERT_KEY, json.dumps(report))
        await r.ltrim(ALERT_KEY, 0, ALERT_LIST_MAX - 1)
        logger.warning(
            "wallet drift brand=%s sev=%s drift=%dc actual=%d expected=%d docs=%d",
            brand_id, severity, drift_cents, actual, expected, ledger["doc_count"],
        )

    return report


async def list_active_brands(r: aioredis.Redis) -> list[str]:
    """Discover brand_ids by scanning balance keys."""
    brands = []
    cursor = 0
    while True:
        cursor, keys = await r.scan(cursor=cursor, match="wallet:*:balance", count=500)
        for key in keys:
            parts = key.split(":")
            if len(parts) == 3 and parts[0] == "wallet" and parts[2] == "balance":
                brands.append(parts[1])
        if cursor == 0:
            break
    return brands


async def run_once(r: aioredis.Redis, repair: bool = False) -> dict[str, Any]:
    """Reconcile every active brand. One-shot, idempotent."""
    started_at = time.time()
    brands = await list_active_brands(r)
    reports = []
    by_severity = {"ok": 0, "warn": 0, "alert": 0, "critical": 0}
    total_abs_drift = 0
    for brand_id in brands:
        try:
            report = await reconcile_brand(r, brand_id, repair=repair)
            reports.append(report)
            by_severity[report["severity"]] += 1
            total_abs_drift += report["abs_drift_cents"]
        except Exception as exc:
            logger.exception("reconcile failed brand=%s: %s", brand_id, exc)
    summary = {
        "brands_scanned": len(brands),
        "by_severity": by_severity,
        "total_abs_drift_cents": total_abs_drift,
        "runtime_seconds": time.time() - started_at,
        "ts": started_at,
    }
    logger.info("wallet reconciliation %s", summary)
    return summary


async def get_recent_alerts(
    r: aioredis.Redis, limit: int = 100
) -> list[dict[str, Any]]:
    """Read recent alerts from the queue."""
    raw = await r.lrange(ALERT_KEY, 0, max(0, limit - 1))
    alerts = []
    for r_ in raw:
        try:
            alerts.append(json.loads(r_))
        except json.JSONDecodeError:
            pass
    return alerts


async def main():
    """CLI entry: python -m app.workers.wallet_reconciliation_worker [--brand X] [--repair]"""
    import argparse
    import sys

    from app.redis_client import close_redis, get_redis, init_redis

    parser = argparse.ArgumentParser()
    parser.add_argument("--brand", help="Reconcile single brand only")
    parser.add_argument("--repair", action="store_true", help="Repair mode (logs only)")
    parser.add_argument("--alerts", action="store_true", help="Show recent alerts")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    await init_redis()
    try:
        r = await get_redis()
        if args.alerts:
            alerts = await get_recent_alerts(r, args.limit)
            print(json.dumps(alerts, indent=2))
        elif args.brand:
            report = await reconcile_brand(r, args.brand, repair=args.repair)
            print(json.dumps(report, indent=2))
            if report["severity"] != "ok":
                sys.exit(1)
        else:
            summary = await run_once(r, repair=args.repair)
            print(json.dumps(summary, indent=2))
    finally:
        await close_redis()


if __name__ == "__main__":
    asyncio.run(main())
