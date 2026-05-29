"""Billing cron worker — scans subscriptions hourly and charges due ones.

Migrated from a full ``SCAN brand:*:subscription`` over Redis (O(N) over
every key in the database, the Trinity-F bottleneck) to an indexed
PostgreSQL range query on ``brand_subscriptions.next_charge_at``. At
1M brands the cron now touches only the due rows instead of walking
the full key-space.

Implements the day-91 auto-charge flow (Apple Music style: 90-day trial,
then auto-charge on the stored default payment method) plus a dunning
sequence for failed charges:

    charge_failed ─► dunning_state=grace (3 days)
                  ├─ reminder email
                  ├─ retry on next cron tick (still within grace)
                  └─ grace expires ─► downgrade_to_free + email

Cancel-at-period-end is honoured here too: if ``cancel_pending=True``
and ``next_charge_at`` has passed, the subscription transitions to FREE.

Redis still receives a mirrored write of every state change so the
existing portal/SDK code (which still reads from Redis during the
migration window) sees the same data.

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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.models.subscription import BrandSubscription, SubscriptionHistory
from app.redis_client import close_redis, get_redis, init_redis

logger = logging.getLogger("billing_cron")

CHECK_INTERVAL_SECONDS = 3600  # hourly sweep
GRACE_DAYS = 3                 # 3-day dunning grace before downgrade
BATCH_LIMIT = 1000             # max subs processed per sweep
HISTORY_MAX_LEN = 500


# ── Main sweep ─────────────────────────────────────────────────────────────


async def run_once(
    db: AsyncSession | None = None,
    r: Any | None = None,
) -> dict[str, int]:
    """One pass over every subscription whose ``next_charge_at`` has passed.

    Returns counters: ``scanned`` / ``charged`` / ``failed`` / ``downgraded``.
    Safe to call repeatedly; cron loop wraps this with a sleep.

    Both ``db`` and ``r`` are optional — the worker entrypoint passes
    ``None`` so the function creates its own session/connection. Callers
    in tests or admin endpoints can pass live handles to share the
    transaction context.
    """
    owns_db = db is None
    if db is None:
        db = async_session_factory()
    if r is None:
        r = await get_redis()

    processed = 0
    charged = 0
    failed = 0
    downgraded = 0

    try:
        now = time.time()
        stmt = (
            select(BrandSubscription)
            .where(BrandSubscription.next_charge_at <= int(now))
            .where(BrandSubscription.tier != "free")
            .order_by(BrandSubscription.next_charge_at)
            .limit(BATCH_LIMIT)
        )
        result = await db.execute(stmt)
        due: list[BrandSubscription] = list(result.scalars().all())

        for sub in due:
            brand_id = sub.brand_id

            # 1) Cancel-at-period-end takes priority over renewal.
            if sub.cancel_pending:
                await _downgrade_to_free(
                    db, r, brand_id, "user_cancelled_at_period_end"
                )
                downgraded += 1
                processed += 1
                continue

            # 2) Auto-renew off → just expire to FREE silently.
            if not sub.auto_renew:
                await _downgrade_to_free(
                    db, r, brand_id, "auto_renew_disabled"
                )
                downgraded += 1
                processed += 1
                continue

            payment_method_id = sub.payment_method_id or ""
            if not payment_method_id:
                await _enter_dunning(db, r, sub, "no_payment_method")
                failed += 1
                processed += 1
                continue

            # 3) Compute charge amount from tier × billing cadence.
            from app.routers.brand_subscriptions import (
                TIERS,
                _tier_price_cents,
            )

            billing = sub.billing if sub.billing in ("monthly", "annual") else "monthly"

            if sub.tier not in TIERS:
                # Unknown tier — sanity downgrade.
                await _downgrade_to_free(db, r, brand_id, "invalid_tier")
                downgraded += 1
                processed += 1
                continue

            charge_amount = _tier_price_cents(sub.tier, billing)

            # 4) Free trial month? Then $0 amount — treat as success and
            # advance the cycle without hitting the gateway.
            if charge_amount <= 0:
                await _mark_renewed(db, r, sub, billing, amount=0)
                await _audit(
                    db,
                    r,
                    brand_id,
                    "AUTO_RENEW_FREE_CYCLE",
                    {"tier": sub.tier, "billing": billing},
                )
                charged += 1
                processed += 1
                continue

            ok = await _attempt_charge(
                r, brand_id, payment_method_id, charge_amount
            )

            if ok:
                await _mark_renewed(db, r, sub, billing, amount=charge_amount)
                await _audit(
                    db,
                    r,
                    brand_id,
                    "AUTO_RENEW_SUCCESS",
                    {
                        "tier": sub.tier,
                        "billing": billing,
                        "amount_cents": charge_amount,
                    },
                )
                charged += 1
            else:
                await _enter_dunning(db, r, sub, "charge_failed")
                failed += 1

            processed += 1

        if owns_db:
            await db.commit()
    except Exception:
        if owns_db:
            await db.rollback()
        raise
    finally:
        if owns_db:
            await db.close()

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


async def _mark_renewed(
    db: AsyncSession,
    r,
    sub: BrandSubscription,
    billing: str,
    amount: int,
) -> None:
    """Advance ``next_charge_at`` by one billing cycle + clear dunning."""
    cycle = 30 * 86400 if billing == "monthly" else 365 * 86400
    now = time.time()
    next_charge = int(now + cycle)

    sub.next_charge_at = next_charge
    sub.last_charged_at = int(now)
    sub.last_charge_amount_cents = amount
    sub.first_year_free = False  # trial consumed
    sub.dunning_state = "none"
    sub.dunning_attempts = 0
    sub.dunning_reason = None

    # Mirror to Redis for legacy readers
    await r.hset(
        f"brand:{sub.brand_id}:subscription",
        mapping={
            "next_charge_at": str(next_charge),
            "last_charged_at": str(now),
            "last_charge_amount_cents": str(amount),
            "first_year_free": "false",
            "dunning_state": "none",
            "dunning_attempts": "0",
            "dunning_reason": "",
        },
    )


async def _enter_dunning(
    db: AsyncSession, r, sub: BrandSubscription, reason: str
) -> None:
    """Start or advance the dunning sequence for ``brand_id``."""
    brand_id = sub.brand_id
    state = sub.dunning_state or "none"
    attempts = int(sub.dunning_attempts or 0)
    now = time.time()

    if state in ("none", "", "downgraded"):
        grace_until = int(now + GRACE_DAYS * 86400)
        sub.dunning_state = "grace"
        sub.dunning_attempts = 1
        sub.dunning_grace_until = grace_until
        sub.dunning_reason = reason
        await r.hset(
            f"brand:{brand_id}:subscription",
            mapping={
                "dunning_state": "grace",
                "dunning_attempts": "1",
                "dunning_started_at": str(now),
                "dunning_grace_until": str(grace_until),
                "dunning_reason": reason,
            },
        )
        await _audit(
            db,
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
        grace_until = float(sub.dunning_grace_until or 0)
        if now > grace_until:
            await _downgrade_to_free(
                db, r, brand_id, "dunning_grace_expired"
            )
            return
        sub.dunning_attempts = attempts
        sub.dunning_reason = reason
        await r.hset(
            f"brand:{brand_id}:subscription",
            mapping={
                "dunning_attempts": str(attempts),
                "dunning_last_attempt_at": str(now),
                "dunning_reason": reason,
            },
        )
        await _audit(
            db,
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


async def _downgrade_to_free(
    db: AsyncSession, r, brand_id: str, reason: str
) -> None:
    now = time.time()
    row = await db.get(BrandSubscription, brand_id)
    if row is not None:
        row.tier = "free"
        row.billing = "monthly"
        row.auto_renew = False
        row.cancel_pending = False
        row.dunning_state = "downgraded"
        # Stash reason in metadata_json so we keep history without
        # adding ad-hoc columns.
        meta = dict(row.metadata_json or {})
        meta["downgraded_at"] = now
        meta["downgraded_reason"] = reason
        row.metadata_json = meta
    await r.hset(
        f"brand:{brand_id}:subscription",
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
    await _audit(db, r, brand_id, "DOWNGRADE_TO_FREE", {"reason": reason})
    await _queue_email(
        r, brand_id, "downgrade_notice", {"reason": reason}
    )


# ── Side-effect sinks (audit + email queue) ────────────────────────────────


async def _audit(
    db: AsyncSession,
    r,
    brand_id: str,
    event: str,
    details: dict[str, Any],
) -> None:
    now = time.time()
    db.add(
        SubscriptionHistory(
            brand_id=brand_id,
            event=event,
            from_tier=details.get("from_tier"),
            to_tier=details.get("to_tier"),
            charge_amount_cents=details.get("amount_cents"),
            metadata_json=details,
            ts=int(now),
        )
    )
    key = f"brand:{brand_id}:subscription:history"
    record = {"event": event, "ts": now, **details}
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
