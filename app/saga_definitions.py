"""Pre-defined sagas built on :mod:`app.saga`.

These wire together domain-specific actions and compensations into
named sagas that the routers (or webhooks) can invoke.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import uuid4

import redis.asyncio as aioredis

from app.saga import SagaCoordinator, SagaResult, SagaStep

logger = logging.getLogger(__name__)


# ── Redis key helpers used by the action bodies ──────────────────────────
def _k_commission_reversal(reversal_id: str) -> str:
    return f"commission:reversal:{reversal_id}"


def _k_conv_commission(conversion_id: str) -> str:
    return f"conversion:{conversion_id}:commission"


def _k_subscription(brand_id: str) -> str:
    return f"subscription:{brand_id}"


def _k_voucher(voucher_id: str) -> str:
    return f"voucher:{voucher_id}"


# ─── Refund cascade saga ─────────────────────────────────────────────────
async def _do_wallet_refund(ctx: dict, r: aioredis.Redis) -> dict:
    """Action: refund the wallet charge via the disputes-grade helper."""
    from app.routers.disputes import _wallet_refund_internal

    brand_id = ctx["brand_id"]
    charge_id = ctx["charge_id"]
    amount_cents = ctx["refund_amount_cents"]
    reason = ctx.get("reason") or "saga_refund"

    ok, refund_id, err = await _wallet_refund_internal(
        r, brand_id, charge_id, amount_cents, reason=reason,
    )
    if not ok:
        raise RuntimeError(f"wallet_refund_failed:{err}")
    ctx["wallet_refund_id"] = refund_id
    return {"refund_id": refund_id, "amount_cents": amount_cents}


async def _undo_wallet_refund(ctx: dict, r: aioredis.Redis) -> None:
    """Compensate: re-debit the wallet (i.e. cancel the refund)."""
    refund_id = ctx.get("wallet_refund_id")
    if not refund_id:
        return  # nothing to undo
    try:
        from app.routers.wallet import (
            _k_balance as _wk_balance,
            _k_charge as _wk_charge,
            _k_refund as _wk_refund,
        )
    except Exception as exc:  # pragma: no cover
        logger.error("wallet import failed in compensate: %s", exc)
        raise

    brand_id = ctx["brand_id"]
    amount = int(ctx["refund_amount_cents"])
    charge_id = ctx["charge_id"]

    # Idempotency: mark the refund record as reversed before mutating
    # balance. If we crash midway, the next retry sees `reversed=1` and
    # skips.
    rkey = _wk_refund(refund_id)
    already_reversed = await r.hget(rkey, "reversed")
    if already_reversed == "1":
        return

    async with r.pipeline(transaction=True) as pipe:
        pipe.hset(rkey, mapping={"reversed": "1", "reversed_at": time.time()})
        pipe.decrby(_wk_balance(brand_id), amount)
        # Bring the charge back to a "completed" posture so it can be
        # re-collected by downstream settlement.
        pipe.hset(
            _wk_charge(charge_id),
            mapping={
                "status": "completed",
                "refunded_amount": 0,
            },
        )
        await pipe.execute()


async def _do_cleanup_attribution(ctx: dict, r: aioredis.Redis) -> dict:
    """Action: scrub the attribution event from the user journey."""
    from app.routers.disputes import _cascade_remove_attribution

    event_id = ctx.get("conversion_id") or ctx.get("attribution_event_id")
    if not event_id:
        return {"skipped": True, "reason": "no_event_id"}

    # Snapshot the original event so we can restore on compensate.
    snapshot = await r.hgetall(f"attr:{event_id}")
    if snapshot:
        ctx["_attribution_snapshot"] = dict(snapshot)

    await _cascade_remove_attribution(r, event_id)
    return {"event_id": event_id, "snapshot_keys": list(snapshot.keys())}


async def _restore_attribution(ctx: dict, r: aioredis.Redis) -> None:
    """Compensate: re-insert the attribution event we deleted."""
    snapshot = ctx.get("_attribution_snapshot")
    event_id = ctx.get("conversion_id") or ctx.get("attribution_event_id")
    if not snapshot or not event_id:
        return

    user_id = snapshot.get("user_id")
    device_fp = snapshot.get("device_fingerprint")

    async with r.pipeline(transaction=False) as pipe:
        pipe.hset(f"attr:{event_id}", mapping=snapshot)
        if user_id:
            pipe.lpush(f"user:{user_id}:attr_journey", event_id)
        if device_fp:
            pipe.lpush(f"device:{device_fp}:attr_journey", event_id)
        await pipe.execute()


async def _do_reverse_commission(ctx: dict, r: aioredis.Redis) -> dict:
    """Action: record a reversal entry against the conversion's commission.

    We do not double-charge the merchant — the wallet refund already
    returned funds. This step exists to keep the commission ledger
    consistent so analytics + payouts don't double-count.
    """
    conversion_id = (
        ctx.get("conversion_id") or ctx.get("attribution_event_id")
    )
    if not conversion_id:
        return {"skipped": True, "reason": "no_conversion_id"}

    reversal_id = uuid4().hex
    ctx["commission_reversal_id"] = reversal_id

    async with r.pipeline(transaction=True) as pipe:
        pipe.hset(
            _k_commission_reversal(reversal_id),
            mapping={
                "reversal_id": reversal_id,
                "conversion_id": conversion_id,
                "charge_id": ctx.get("charge_id") or "",
                "brand_id": ctx.get("brand_id") or "",
                "amount_cents": ctx["refund_amount_cents"],
                "reason": ctx.get("reason") or "saga_refund",
                "ts": time.time(),
                "status": "applied",
            },
        )
        pipe.hset(
            _k_conv_commission(conversion_id),
            mapping={
                "status": "reversed",
                "reversal_id": reversal_id,
            },
        )
        await pipe.execute()

    return {"reversal_id": reversal_id, "conversion_id": conversion_id}


async def _undo_reverse_commission(ctx: dict, r: aioredis.Redis) -> None:
    """Compensate: mark the reversal as cancelled, restore commission."""
    reversal_id = ctx.get("commission_reversal_id")
    conversion_id = (
        ctx.get("conversion_id") or ctx.get("attribution_event_id")
    )
    if not reversal_id or not conversion_id:
        return

    rkey = _k_commission_reversal(reversal_id)
    cancelled = await r.hget(rkey, "status")
    if cancelled == "cancelled":
        return  # idempotent

    async with r.pipeline(transaction=True) as pipe:
        pipe.hset(
            rkey,
            mapping={"status": "cancelled", "cancelled_at": time.time()},
        )
        pipe.hset(
            _k_conv_commission(conversion_id),
            mapping={"status": "applied", "reversal_id": ""},
        )
        await pipe.execute()


async def _emit_refund_event(ctx: dict, r: aioredis.Redis) -> dict:
    """Action: publish a refund event for downstream consumers.

    Idempotent: pushes onto a stream keyed by ``saga_id`` — re-runs
    append duplicates that downstream consumers must dedupe by
    ``saga_id``.
    """
    event = {
        "type": "refund.applied",
        "saga_id": ctx.get("_saga_id", "unknown"),
        "brand_id": ctx.get("brand_id"),
        "charge_id": ctx.get("charge_id"),
        "conversion_id": ctx.get("conversion_id"),
        "amount_cents": ctx.get("refund_amount_cents"),
        "ts": time.time(),
    }
    try:
        await r.xadd("events:refund", {k: str(v) for k, v in event.items()})
    except Exception as exc:
        logger.warning("refund event publish failed: %s", exc)
        # Treat as non-fatal — the wallet refund already won.
    return event


async def _noop_compensate(ctx: dict, r: aioredis.Redis) -> None:
    return None


async def refund_cascade_saga(
    *,
    r: aioredis.Redis,
    charge_id: str,
    brand_id: str,
    refund_amount_cents: int,
    conversion_id: str | None = None,
    reason: str | None = None,
) -> SagaResult:
    """Run the full refund cascade as a saga.

    Order matters:
      1. wallet_refund        (mutation; money moves)
      2. cleanup_attribution  (mutation; visible to product analytics)
      3. reverse_commission   (mutation; ledger correctness)
      4. fire_refund_event    (best-effort broadcast)
    """
    saga_id = f"saga_refund_{uuid4().hex[:16]}"
    coordinator = SagaCoordinator(r)

    context: dict[str, Any] = {
        "_saga_id": saga_id,
        "charge_id": charge_id,
        "brand_id": brand_id,
        "refund_amount_cents": int(refund_amount_cents),
        "conversion_id": conversion_id,
        "reason": reason,
    }

    steps = [
        SagaStep(
            name="wallet_refund",
            action=_do_wallet_refund,
            compensate=_undo_wallet_refund,
            timeout_seconds=20,
        ),
        SagaStep(
            name="cleanup_attribution",
            action=_do_cleanup_attribution,
            compensate=_restore_attribution,
            timeout_seconds=15,
        ),
        SagaStep(
            name="reverse_commission",
            action=_do_reverse_commission,
            compensate=_undo_reverse_commission,
            timeout_seconds=15,
        ),
        SagaStep(
            name="fire_refund_event",
            action=_emit_refund_event,
            compensate=_noop_compensate,  # event is idempotent
            timeout_seconds=10,
        ),
    ]

    return await coordinator.run(saga_id, steps, context)


# ─── Subscription upgrade saga ───────────────────────────────────────────
async def _do_charge_payment(ctx: dict, r: aioredis.Redis) -> dict:
    """Debit the brand wallet for the upgrade price."""
    try:
        from app.routers.wallet import (
            _k_balance as _wk_balance,
        )
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"wallet_import_failed:{exc}")

    brand_id = ctx["brand_id"]
    amount = int(ctx["upgrade_price_cents"])
    bkey = _wk_balance(brand_id)

    current = int(await r.get(bkey) or 0)
    if current < amount:
        raise RuntimeError("insufficient_funds")

    charge_id = f"sub_chg_{uuid4().hex[:16]}"
    ctx["subscription_charge_id"] = charge_id
    async with r.pipeline(transaction=True) as pipe:
        pipe.decrby(bkey, amount)
        pipe.hset(
            f"subscription:charge:{charge_id}",
            mapping={
                "charge_id": charge_id,
                "brand_id": brand_id,
                "amount": amount,
                "kind": "subscription_upgrade",
                "ts": time.time(),
                "status": "completed",
            },
        )
        await pipe.execute()
    return {"charge_id": charge_id, "amount_cents": amount}


async def _undo_charge_payment(ctx: dict, r: aioredis.Redis) -> None:
    try:
        from app.routers.wallet import _k_balance as _wk_balance
    except Exception:  # pragma: no cover
        return

    charge_id = ctx.get("subscription_charge_id")
    if not charge_id:
        return
    brand_id = ctx["brand_id"]
    amount = int(ctx["upgrade_price_cents"])
    ckey = f"subscription:charge:{charge_id}"

    refunded = await r.hget(ckey, "status")
    if refunded == "refunded":
        return  # idempotent

    async with r.pipeline(transaction=True) as pipe:
        pipe.incrby(_wk_balance(brand_id), amount)
        pipe.hset(
            ckey,
            mapping={
                "status": "refunded",
                "refunded_at": time.time(),
            },
        )
        await pipe.execute()


async def _do_upgrade_tier(ctx: dict, r: aioredis.Redis) -> dict:
    brand_id = ctx["brand_id"]
    new_tier = ctx["new_tier"]
    key = _k_subscription(brand_id)
    prev = await r.hget(key, "tier")
    ctx["previous_tier"] = prev or "free"
    await r.hset(
        key,
        mapping={
            "tier": new_tier,
            "upgraded_at": time.time(),
        },
    )
    return {"previous_tier": ctx["previous_tier"], "new_tier": new_tier}


async def _undo_upgrade_tier(ctx: dict, r: aioredis.Redis) -> None:
    brand_id = ctx["brand_id"]
    previous = ctx.get("previous_tier") or "free"
    await r.hset(
        _k_subscription(brand_id),
        mapping={
            "tier": previous,
            "downgraded_at": time.time(),
        },
    )


async def _do_enable_features(ctx: dict, r: aioredis.Redis) -> dict:
    brand_id = ctx["brand_id"]
    features = ctx.get("features") or []
    key = f"subscription:{brand_id}:features"
    if features:
        await r.sadd(key, *features)
    return {"enabled": features}


async def _undo_enable_features(ctx: dict, r: aioredis.Redis) -> None:
    brand_id = ctx["brand_id"]
    features = ctx.get("features") or []
    if not features:
        return
    key = f"subscription:{brand_id}:features"
    await r.srem(key, *features)


async def subscription_upgrade_saga(
    *,
    r: aioredis.Redis,
    brand_id: str,
    new_tier: str,
    upgrade_price_cents: int,
    features: list[str] | None = None,
) -> SagaResult:
    """Upgrade a brand subscription as an atomic 3-step saga.

    1. charge_payment   (mutation; wallet debit)
    2. upgrade_tier     (mutation; subscription record)
    3. enable_features  (mutation; feature flags set)
    """
    saga_id = f"saga_subup_{uuid4().hex[:16]}"
    coordinator = SagaCoordinator(r)

    context: dict[str, Any] = {
        "_saga_id": saga_id,
        "brand_id": brand_id,
        "new_tier": new_tier,
        "upgrade_price_cents": int(upgrade_price_cents),
        "features": list(features or []),
    }

    steps = [
        SagaStep(
            name="charge_payment",
            action=_do_charge_payment,
            compensate=_undo_charge_payment,
            timeout_seconds=20,
        ),
        SagaStep(
            name="upgrade_tier",
            action=_do_upgrade_tier,
            compensate=_undo_upgrade_tier,
            timeout_seconds=10,
        ),
        SagaStep(
            name="enable_features",
            action=_do_enable_features,
            compensate=_undo_enable_features,
            timeout_seconds=10,
        ),
    ]

    return await coordinator.run(saga_id, steps, context)
