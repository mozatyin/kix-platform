"""Stripe webhook receiver.

Handles:
- payment_intent.succeeded         → credit wallet, audit
- payment_intent.payment_failed    → start dunning
- customer.subscription.deleted    → downgrade brand
- invoice.payment_succeeded        → audit
- charge.refunded                  → audit + reverse wallet credit

Signature verification uses ``STRIPE_WEBHOOK_SECRET``. Handler failures
return 500 so Stripe retries — exactly-once side-effects matter more than
"always 200" for money flows.

Idempotency (two-phase, crash-safe):
    Phase 1 — SET NX state="processing" with a 60s TTL (a "claim").
    Phase 2 — on success, promote to state="completed" with 24h TTL.
    On crash, the 60s TTL releases the claim and Stripe's retry re-runs it.
    Concurrent deliveries: loser sees "processing" → 503 (Stripe retries),
    or "completed" → duplicate=True (no-op).

Mount in main.py::

    from app.routers import stripe_webhook
    app.include_router(
        stripe_webhook.router,
        prefix="/api/v1/webhooks/stripe",
        tags=["stripe_webhook"],
    )
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import redis.asyncio as aioredis
import stripe
from fastapi import APIRouter, Depends, HTTPException, Request

from app.redis_client import get_redis

logger = logging.getLogger(__name__)
router = APIRouter()

STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
EVENT_DEDUP_TTL_SECONDS = 24 * 3600
EVENT_PROCESSING_TTL_SECONDS = 60  # Phase-1 claim; expires if instance crashes.
AUDIT_LIST_MAX = 10_000

# Two-phase idempotency state values stored at _k_event_seen(event_id):
#   "processing" → an instance is currently handling this event (TTL 60s)
#   "completed"  → handler ran to completion (TTL 24h, hard duplicate)
EVENT_STATE_PROCESSING = "processing"
EVENT_STATE_COMPLETED = "completed"


def _k_event_seen(event_id: str) -> str:
    return f"stripe_webhook:seen:{event_id}"


def _k_event_log(brand_id: str) -> str:
    return f"stripe_webhook:brand:{brand_id}:events"


def _k_audit() -> str:
    return "stripe_webhook:audit"


def _decode_state(value: Any) -> str:
    """Redis client may return bytes or str depending on decode_responses."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


async def _log_event(
    r: aioredis.Redis, brand_id: str | None, event_type: str, payload: dict[str, Any]
) -> None:
    if not brand_id:
        return
    try:
        await r.rpush(
            _k_event_log(brand_id),
            json.dumps(
                {"event_type": event_type, "ts": time.time(), "payload": payload},
                ensure_ascii=False,
            ),
        )
        await r.ltrim(_k_event_log(brand_id), -500, -1)
    except Exception as exc:  # noqa: BLE001 — never break the handler
        logger.warning("event log failed brand=%s: %s", brand_id, exc)


@router.post("")
async def webhook(
    request: Request, r: aioredis.Redis = Depends(get_redis)
) -> dict[str, Any]:
    """Receive + verify + dispatch Stripe events."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        # Misconfigured deploy — refuse loudly rather than silently trust.
        logger.error("STRIPE_WEBHOOK_SECRET not configured; refusing event")
        raise HTTPException(503, "webhook_not_configured")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError as exc:
        logger.warning("invalid stripe payload: %s", exc)
        raise HTTPException(400, "invalid_payload") from exc
    except stripe.error.SignatureVerificationError as exc:
        logger.warning("invalid stripe signature: %s", exc)
        raise HTTPException(401, "invalid_signature") from exc

    event_id = event.get("id", "")
    event_type = event.get("type", "")
    obj = event["data"]["object"]

    # Forensic audit log — every received & signature-valid event, before any
    # idempotency / handler logic. Best-effort: never break the handler.
    try:
        await r.lpush(
            _k_audit(),
            json.dumps(
                {
                    "event_id": event_id,
                    "type": event_type,
                    "received_at": time.time(),
                    "ip": request.client.host if request.client else None,
                },
                ensure_ascii=False,
            ),
        )
        await r.ltrim(_k_audit(), 0, AUDIT_LIST_MAX - 1)
    except Exception as exc:  # noqa: BLE001 — audit must never break delivery
        logger.warning("audit log failed event=%s: %s", event_id, exc)

    # ── Two-phase idempotency ────────────────────────────────────────────
    # Phase 1: atomically claim the event with state="processing" + short TTL.
    # If the handler crashes mid-flight, the short TTL releases the claim so
    # Stripe's retry can re-process. If two instances race, only one wins SET
    # NX; the other inspects state and either reports duplicate or asks
    # Stripe to retry (503).
    seen_key = _k_event_seen(event_id) if event_id else ""
    if seen_key:
        claimed = await r.set(
            seen_key,
            EVENT_STATE_PROCESSING,
            ex=EVENT_PROCESSING_TTL_SECONDS,
            nx=True,
        )
        if not claimed:
            state = _decode_state(await r.get(seen_key))
            if state == EVENT_STATE_COMPLETED:
                return {
                    "received": True,
                    "event_type": event_type,
                    "duplicate": True,
                }
            if state == EVENT_STATE_PROCESSING:
                # Another instance is mid-flight. Tell Stripe to retry; the
                # winning instance will mark "completed" before the retry
                # window closes.
                raise HTTPException(503, "event_already_being_processed")
            # Stale TTL or unknown sentinel — try to re-claim race-safely.
            claimed = await r.set(
                seen_key,
                EVENT_STATE_PROCESSING,
                ex=EVENT_PROCESSING_TTL_SECONDS,
                nx=True,
            )
            if not claimed:
                return {
                    "received": True,
                    "event_type": event_type,
                    "duplicate": True,
                }

    handlers = {
        "payment_intent.succeeded": _handle_payment_succeeded,
        "payment_intent.payment_failed": _handle_payment_failed,
        "customer.subscription.deleted": _handle_subscription_deleted,
        "invoice.payment_succeeded": _handle_invoice_paid,
        "charge.refunded": _handle_charge_refunded,
    }
    handler = handlers.get(event_type)
    if handler:
        try:
            await handler(r, obj, event)
        except Exception as exc:  # noqa: BLE001
            # Phase-2 (failure): release the claim so Stripe's retry can
            # pick the event up cleanly. This trades "200 OK on bug" for
            # "exactly-once side-effects" — preferable for money flows.
            if seen_key:
                try:
                    await r.delete(seen_key)
                except Exception as del_exc:  # noqa: BLE001
                    logger.warning(
                        "failed to release claim event=%s: %s", event_id, del_exc
                    )
            logger.exception("Webhook handler %s failed: %s", event_type, exc)
            raise HTTPException(500, "handler_failed") from exc

    # Phase 2 (success): promote claim → "completed" with the long dedup TTL.
    if seen_key:
        try:
            await r.set(seen_key, EVENT_STATE_COMPLETED, ex=EVENT_DEDUP_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to mark completed event=%s: %s", event_id, exc)

    return {"received": True, "event_type": event_type}


# ── Handlers ─────────────────────────────────────────────────────────────
async def _handle_payment_succeeded(
    r: aioredis.Redis, obj: dict[str, Any], event: dict[str, Any]
) -> None:
    """Credit the brand wallet + write an audit trail entry."""
    metadata = obj.get("metadata", {}) or {}
    brand_id = metadata.get("brand_id")
    ref_id = metadata.get("reference_id")
    if not brand_id:
        logger.info(
            "payment_intent.succeeded with no brand_id metadata; intent=%s",
            obj.get("id"),
        )
        return

    amount = int(obj.get("amount") or 0)
    if amount <= 0:
        return

    # Credit wallet (best-effort import — wallet router owns the key shape).
    try:
        from app.routers.wallet import _k_balance
        await r.incrby(_k_balance(brand_id), amount)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "wallet credit failed brand=%s amount=%s: %s", brand_id, amount, exc
        )

    await r.lpush(
        f"wallet:{brand_id}:transactions",
        json.dumps(
            {
                "type": "stripe_charge",
                "amount": amount,
                "gateway_tx_id": obj.get("id"),
                "reference_id": ref_id,
                "ts": time.time(),
            },
            ensure_ascii=False,
        ),
    )
    await r.ltrim(f"wallet:{brand_id}:transactions", 0, 10_000)
    await _log_event(
        r,
        brand_id,
        "payment_intent.succeeded",
        {"amount": amount, "intent_id": obj.get("id"), "reference_id": ref_id},
    )


async def _handle_payment_failed(
    r: aioredis.Redis, obj: dict[str, Any], event: dict[str, Any]
) -> None:
    """Trigger dunning on the brand."""
    metadata = obj.get("metadata", {}) or {}
    brand_id = metadata.get("brand_id")
    if not brand_id:
        return

    err = obj.get("last_payment_error") or {}
    decline_code = err.get("code") or err.get("decline_code") or "unknown"

    # Optional dunning hook — billing_cron may not exist yet in this branch.
    try:
        from app.workers.billing_cron import _enter_dunning  # type: ignore[import-not-found]
        await _enter_dunning(r, brand_id, reason=f"stripe_decline:{decline_code}")
    except ImportError:
        # Persist a minimal dunning marker so a future cron can pick it up.
        await r.hset(
            f"brand:{brand_id}:dunning",
            mapping={
                "state": "pending",
                "reason": f"stripe_decline:{decline_code}",
                "intent_id": obj.get("id") or "",
                "ts": time.time(),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("enter_dunning failed brand=%s: %s", brand_id, exc)

    await _log_event(
        r,
        brand_id,
        "payment_intent.payment_failed",
        {"intent_id": obj.get("id"), "decline_code": decline_code},
    )


async def _handle_subscription_deleted(
    r: aioredis.Redis, obj: dict[str, Any], event: dict[str, Any]
) -> None:
    """Downgrade the brand on subscription cancellation."""
    metadata = obj.get("metadata", {}) or {}
    brand_id = metadata.get("brand_id")
    if not brand_id:
        # Try the customer → brand mapping as a fallback.
        customer_id = obj.get("customer")
        if customer_id:
            brand_id = await r.get(f"stripe_customer:{customer_id}:brand_id")
    if not brand_id:
        return

    await r.set(f"brand_subscription:{brand_id}:status", "cancelled")
    await r.hset(
        f"brand_subscription:{brand_id}",
        mapping={
            "status": "cancelled",
            "cancelled_at": time.time(),
            "stripe_subscription_id": obj.get("id") or "",
        },
    )
    await _log_event(
        r,
        brand_id,
        "customer.subscription.deleted",
        {"subscription_id": obj.get("id")},
    )


async def _handle_invoice_paid(
    r: aioredis.Redis, obj: dict[str, Any], event: dict[str, Any]
) -> None:
    """Audit invoice payments — wallet credit is handled by payment_intent.succeeded."""
    metadata = obj.get("metadata", {}) or {}
    brand_id = metadata.get("brand_id")
    if not brand_id:
        customer_id = obj.get("customer")
        if customer_id:
            brand_id = await r.get(f"stripe_customer:{customer_id}:brand_id")
    await _log_event(
        r,
        brand_id,
        "invoice.payment_succeeded",
        {
            "invoice_id": obj.get("id"),
            "amount_paid": obj.get("amount_paid"),
            "subscription_id": obj.get("subscription"),
        },
    )


async def _handle_charge_refunded(
    r: aioredis.Redis, obj: dict[str, Any], event: dict[str, Any]
) -> None:
    """Reverse the wallet credit and audit the refund."""
    metadata = obj.get("metadata", {}) or {}
    brand_id = metadata.get("brand_id")
    if not brand_id:
        return

    amount_refunded = int(obj.get("amount_refunded") or 0)
    if amount_refunded > 0:
        try:
            from app.routers.wallet import _k_balance
            await r.decrby(_k_balance(brand_id), amount_refunded)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "wallet refund debit failed brand=%s amount=%s: %s",
                brand_id, amount_refunded, exc,
            )
        await r.lpush(
            f"wallet:{brand_id}:transactions",
            json.dumps(
                {
                    "type": "stripe_refund",
                    "amount": -amount_refunded,
                    "gateway_tx_id": obj.get("id"),
                    "ts": time.time(),
                },
                ensure_ascii=False,
            ),
        )
        await r.ltrim(f"wallet:{brand_id}:transactions", 0, 10_000)

    await _log_event(
        r,
        brand_id,
        "charge.refunded",
        {"charge_id": obj.get("id"), "amount_refunded": amount_refunded},
    )
