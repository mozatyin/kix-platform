"""Billing cron worker — scans subscriptions hourly and charges due ones.

Implements the day-91 auto-charge flow (Apple Music style: 90-day trial,
then auto-charge on the stored default payment method) plus a dunning
sequence for failed charges:

    charge_failed ─► dunning_state=grace (3 days)
                  ├─ reminder email
                  ├─ retry on next cron tick (still within grace)
                  └─ grace expires ─► downgrade_to_free + email

Cancel-at-period-end is honoured here too: if ``cancel_pending=true`` and
``next_charge_at`` has passed, the subscription transitions to FREE.

Usage:
    .venv/bin/python -m app.workers.billing_cron --once   # single sweep
    .venv/bin/python -m app.workers.billing_cron          # continuous loop
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import time
from typing import Any

from app.redis_client import close_redis, get_redis, init_redis

logger = logging.getLogger("billing_cron")

CHECK_INTERVAL_SECONDS = 3600  # hourly sweep
GRACE_DAYS = 3                 # 3-day dunning grace before downgrade
SCAN_BATCH = 100
HISTORY_MAX_LEN = 500


# ── Helpers ────────────────────────────────────────────────────────────────


def _truthy(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    s = v.decode() if isinstance(v, bytes) else str(v)
    return s.lower() in ("1", "true", "yes", "on")


def _float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v.decode() if isinstance(v, bytes) else v)
    except (TypeError, ValueError):
        return default


def _str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    return v.decode() if isinstance(v, bytes) else str(v)


# ── Main sweep ─────────────────────────────────────────────────────────────


async def run_once() -> dict[str, int]:
    """One pass over every ``brand:*:subscription`` hash.

    Returns counters: ``scanned`` / ``charged`` / ``failed`` / ``downgraded``.
    Safe to call repeatedly; cron loop wraps this with a sleep.
    """
    r = await get_redis()

    cursor: int = 0
    processed = 0
    charged = 0
    failed = 0
    downgraded = 0

    while True:
        cursor, keys = await r.scan(
            cursor=cursor, match="brand:*:subscription", count=SCAN_BATCH
        )
        for key in keys:
            key_s = _str(key)
            # Skip history list keys etc. — only HASHes at the exact pattern.
            # `brand:{id}:subscription:history` would not match our pattern
            # because `*` in glob excludes ":" only via lack of brace-expand;
            # to be safe we filter explicitly:
            if key_s.count(":") != 2:
                continue

            try:
                sub = await r.hgetall(key)
            except Exception:  # noqa: BLE001
                logger.exception("hgetall failed for %s", key_s)
                continue
            if not sub:
                continue

            tier = _str(sub.get("tier"), "free")
            if tier == "free":
                continue

            next_charge_at = _float(sub.get("next_charge_at"))
            auto_renew = _truthy(sub.get("auto_renew"), default=True)
            cancel_pending = _truthy(sub.get("cancel_pending"), default=False)

            now = time.time()
            if next_charge_at <= 0:
                continue
            if next_charge_at > now:
                continue  # not due yet

            brand_id = key_s.split(":")[1]

            # 1) Cancel-at-period-end takes priority over renewal.
            if cancel_pending:
                await _downgrade_to_free(
                    r, brand_id, "user_cancelled_at_period_end"
                )
                downgraded += 1
                processed += 1
                continue

            # 2) Auto-renew off → just expire to FREE silently.
            if not auto_renew:
                await _downgrade_to_free(r, brand_id, "auto_renew_disabled")
                downgraded += 1
                processed += 1
                continue

            payment_method_id = _str(sub.get("payment_method_id"))
            if not payment_method_id:
                await _enter_dunning(r, brand_id, "no_payment_method")
                failed += 1
                processed += 1
                continue

            # 3) Compute charge amount from tier × billing cadence.
            from app.routers.brand_subscriptions import (
                TIERS,
                _tier_price_cents,
            )

            billing = _str(sub.get("billing"), "monthly")
            if billing not in ("monthly", "annual"):
                billing = "monthly"

            if tier not in TIERS:
                # Unknown tier — sanity downgrade.
                await _downgrade_to_free(r, brand_id, "invalid_tier")
                downgraded += 1
                processed += 1
                continue

            charge_amount = _tier_price_cents(tier, billing)

            # 4) Free trial month? Then $0 amount — treat as success and
            # advance the cycle without hitting the gateway.
            if charge_amount <= 0:
                await _mark_renewed(r, brand_id, billing, amount=0)
                await _audit(
                    r,
                    brand_id,
                    "AUTO_RENEW_FREE_CYCLE",
                    {"tier": tier, "billing": billing},
                )
                charged += 1
                processed += 1
                continue

            ok = await _attempt_charge(
                r, brand_id, payment_method_id, charge_amount
            )

            if ok:
                await _mark_renewed(r, brand_id, billing, amount=charge_amount)
                await _audit(
                    r,
                    brand_id,
                    "AUTO_RENEW_SUCCESS",
                    {
                        "tier": tier,
                        "billing": billing,
                        "amount_cents": charge_amount,
                    },
                )
                charged += 1
            else:
                await _enter_dunning(r, brand_id, "charge_failed")
                failed += 1

            processed += 1

        if cursor == 0:
            break

    logger.info(
        "billing_cron sweep complete: scanned=%d charged=%d failed=%d downgraded=%d",
        processed,
        charged,
        failed,
        downgraded,
    )
    return {
        "scanned": processed,
        "charged": charged,
        "failed": failed,
        "downgraded": downgraded,
    }


# ── Charge + lifecycle helpers ─────────────────────────────────────────────


async def _attempt_charge(
    r, brand_id: str, payment_method_id: str, amount_cents: int
) -> bool:
    """Call the payment-methods gateway stub. Returns True on success."""
    try:
        from app.routers.payment_methods import _gateway_charge

        reference_id = f"sub_auto_{brand_id}_{int(time.time())}"
        result = await _gateway_charge(
            payment_method_id, amount_cents, "CNY", reference_id, r
        )
        return bool(result.get("success"))
    except Exception:  # noqa: BLE001
        logger.exception("gateway charge raised for brand=%s", brand_id)
        return False


async def _mark_renewed(r, brand_id: str, billing: str, amount: int) -> None:
    """Advance ``next_charge_at`` by one billing cycle + clear dunning."""
    key = f"brand:{brand_id}:subscription"
    cycle = 30 * 86400 if billing == "monthly" else 365 * 86400
    now = time.time()
    await r.hset(
        key,
        mapping={
            "next_charge_at": str(now + cycle),
            "last_charged_at": str(now),
            "last_charge_amount_cents": str(amount),
            "first_year_free": "false",  # trial consumed
            "dunning_state": "none",
            "dunning_attempts": "0",
            "dunning_reason": "",
        },
    )


async def _enter_dunning(r, brand_id: str, reason: str) -> None:
    """Start or advance the dunning sequence for ``brand_id``."""
    key = f"brand:{brand_id}:subscription"
    sub = await r.hgetall(key) or {}

    state = _str(sub.get("dunning_state"), "none")
    attempts = int(_float(sub.get("dunning_attempts"), 0))
    now = time.time()

    if state in ("none", "", "downgraded"):
        grace_until = now + GRACE_DAYS * 86400
        await r.hset(
            key,
            mapping={
                "dunning_state": "grace",
                "dunning_attempts": "1",
                "dunning_started_at": str(now),
                "dunning_grace_until": str(grace_until),
                "dunning_reason": reason,
            },
        )
        await _audit(
            r,
            brand_id,
            "DUNNING_START",
            {"reason": reason, "grace_until": grace_until},
        )
        await _queue_email(
            r,
            brand_id,
            "dunning_start",
            {"reason": reason, "grace_days": GRACE_DAYS},
        )
        return

    if state == "grace":
        attempts += 1
        grace_until = _float(sub.get("dunning_grace_until"), 0.0)
        if now > grace_until:
            await _downgrade_to_free(r, brand_id, "dunning_grace_expired")
            return
        await r.hset(
            key,
            mapping={
                "dunning_attempts": str(attempts),
                "dunning_last_attempt_at": str(now),
                "dunning_reason": reason,
            },
        )
        await _audit(
            r,
            brand_id,
            "DUNNING_REMINDER",
            {"reason": reason, "attempts": attempts},
        )
        await _queue_email(
            r,
            brand_id,
            "dunning_reminder",
            {"attempts": attempts, "reason": reason},
        )


async def _downgrade_to_free(r, brand_id: str, reason: str) -> None:
    key = f"brand:{brand_id}:subscription"
    now = time.time()
    await r.hset(
        key,
        mapping={
            "tier": "free",
            "billing": "monthly",
            "auto_renew": "false",
            "cancel_pending": "false",
            "dunning_state": "downgraded",
            "downgraded_at": str(now),
            "downgraded_reason": reason,
        },
    )
    await _audit(r, brand_id, "DOWNGRADE_TO_FREE", {"reason": reason})
    await _queue_email(
        r, brand_id, "downgrade_notice", {"reason": reason}
    )


# ── Side-effect sinks (audit + email queue) ────────────────────────────────


async def _audit(r, brand_id: str, event: str, details: dict[str, Any]) -> None:
    key = f"brand:{brand_id}:subscription:history"
    record = {"event": event, "ts": time.time(), **details}
    try:
        await r.lpush(key, _json.dumps(record))
        await r.ltrim(key, 0, HISTORY_MAX_LEN - 1)
    except Exception:  # noqa: BLE001
        logger.exception("audit write failed for brand=%s event=%s", brand_id, event)


async def _queue_email(
    r, brand_id: str, template: str, vars: dict[str, Any]
) -> None:
    payload = {
        "brand_id": brand_id,
        "template": template,
        "vars": vars,
        "queued_at": time.time(),
    }
    try:
        await r.lpush("email:outbound:queue", _json.dumps(payload))
    except Exception:  # noqa: BLE001
        logger.exception(
            "email queue write failed for brand=%s tmpl=%s", brand_id, template
        )


# ── Entrypoint ─────────────────────────────────────────────────────────────


async def main() -> None:
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    await init_redis()
    try:
        if "--once" in sys.argv:
            result = await run_once()
            print(_json.dumps(result))
            return
        while True:
            try:
                result = await run_once()
                logger.info("cycle=%s", result)
            except Exception:  # noqa: BLE001
                logger.exception("billing_cron cycle failed")
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
    finally:
        await close_redis()


if __name__ == "__main__":
    asyncio.run(main())
