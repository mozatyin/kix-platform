"""PaymentIntent + SetupIntent (Stripe-style charge + card-save flows).

A ``SetupIntent`` saves a card for future use without moving any money. A
``PaymentIntent`` actually charges a card. Both attach to a ``Customer`` and
to a specific ``PaymentMethod`` on file.

Why two intents
---------------
Stripe split these for a reason: a SetupIntent goes through the same
authentication path (3-D Secure, regulatory step-up) as a real charge but
without committing funds, so the on-file ``PaymentMethod`` is guaranteed
*usable* before the next billing cycle. The legacy KiX wallet conflated the
two ("verify card by holding ¥1 then reversing"), which works but cannot
satisfy SCA / RBI mandates for off-session merchant-initiated charges.

State machine
-------------
SetupIntent:
    requires_payment_method  → requires_confirmation  → succeeded
                                                     → requires_action (3DS)
                                                     → canceled

PaymentIntent:
    requires_payment_method  → requires_confirmation  → processing
                                                     → succeeded
                                                     → requires_action
                                                     → canceled

Redis schema
------------
    setup_intent:{si_id}     HASH (state, customer_id, payment_method_id,
                                   client_secret, created_at, updated_at,
                                   ...)
    payment_intent:{pi_id}   HASH (state, customer_id, payment_method_id,
                                   amount, currency, description,
                                   metadata_json, gateway_tx_id,
                                   gateway_fee_cents, invoice_id?, ...)
    customer:{cus_id}:setup_intents     ZSET (score=created_at)
    customer:{cus_id}:payment_intents   ZSET (score=created_at)
    payment_intent:{pi_id}:audit         LIST
    setup_intent:{si_id}:audit           LIST
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Literal
from uuid import uuid4

import redis.asyncio as aioredis
import stripe
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from app.api_standards import (
    error_response,
    list_response,
    not_found,
    now_ts,
    validation_failed,
)
from app.redis_client import get_redis
from app.routers.customers import (
    _k_cust,
    _k_cust_pms,
    ensure_customer_exists,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Stripe SDK init ───────────────────────────────────────────────────────
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "sk_test_stub")


def _stripe_is_live() -> bool:
    return bool(stripe.api_key) and stripe.api_key != "sk_test_stub"


# ── Constants ────────────────────────────────────────────────────────────
AUDIT_MAX = 500
GATEWAY_FEE_BPS = 290
GATEWAY_FEE_FIXED_CENTS = 30
MAX_AMOUNT_CENTS = 1_000_000_00  # $1M per intent

SETUP_INTENT_STATES = {
    "requires_payment_method",
    "requires_confirmation",
    "requires_action",
    "processing",
    "succeeded",
    "canceled",
}

PAYMENT_INTENT_STATES = {
    "requires_payment_method",
    "requires_confirmation",
    "requires_action",
    "processing",
    "succeeded",
    "canceled",
    "requires_capture",
}


# ── Key helpers ──────────────────────────────────────────────────────────
def _k_si(si_id: str) -> str:
    return f"setup_intent:{si_id}"


def _k_pi(pi_id: str) -> str:
    return f"payment_intent:{pi_id}"


def _k_cust_sis(cus_id: str) -> str:
    return f"customer:{cus_id}:setup_intents"


def _k_cust_pis(cus_id: str) -> str:
    return f"customer:{cus_id}:payment_intents"


def _k_si_audit(si_id: str) -> str:
    return f"setup_intent:{si_id}:audit"


def _k_pi_audit(pi_id: str) -> str:
    return f"payment_intent:{pi_id}:audit"


def _mint_si() -> str:
    return f"seti_{uuid4().hex[:24]}"


def _mint_pi() -> str:
    return f"pi_{uuid4().hex[:24]}"


def _client_secret(intent_id: str) -> str:
    """Stripe-style client_secret for browser confirmation flows.

    Format mirrors Stripe: ``{intent_id}_secret_{random}``. Never echo this
    back to anyone except the customer-side caller — it is enough to confirm
    the intent.
    """
    return f"{intent_id}_secret_{uuid4().hex}"


async def _audit_si(
    r: aioredis.Redis, si_id: str, event: str, detail: dict | None = None
) -> None:
    entry = {"event": event, "ts": now_ts(), "detail": detail or {}}
    try:
        await r.rpush(_k_si_audit(si_id), json.dumps(entry, ensure_ascii=False))
        await r.ltrim(_k_si_audit(si_id), -AUDIT_MAX, -1)
    except Exception as exc:
        logger.warning("si audit failed si=%s: %s", si_id, exc)


async def _audit_pi(
    r: aioredis.Redis, pi_id: str, event: str, detail: dict | None = None
) -> None:
    entry = {"event": event, "ts": now_ts(), "detail": detail or {}}
    try:
        await r.rpush(_k_pi_audit(pi_id), json.dumps(entry, ensure_ascii=False))
        await r.ltrim(_k_pi_audit(pi_id), -AUDIT_MAX, -1)
    except Exception as exc:
        logger.warning("pi audit failed pi=%s: %s", pi_id, exc)


# ── Pydantic ─────────────────────────────────────────────────────────────
class SetupIntentCreateRequest(BaseModel):
    customer_id: str = Field(..., min_length=1, max_length=128)
    payment_method_id: str | None = Field(default=None, max_length=128)
    save_for_future_use: bool = True
    usage: Literal["off_session", "on_session"] = "off_session"
    metadata: dict[str, Any] | None = None


class SetupIntentResponse(BaseModel):
    setup_intent_id: str
    status: str
    customer_id: str
    payment_method_id: str | None = None
    usage: str
    client_secret: str | None = None
    next_action: dict[str, Any] | None = None
    last_error: dict[str, Any] | None = None
    created_at: int
    updated_at: int


class PaymentIntentCreateRequest(BaseModel):
    customer_id: str = Field(..., min_length=1, max_length=128)
    amount_cents: int = Field(..., gt=0, le=MAX_AMOUNT_CENTS)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    payment_method_id: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    metadata: dict[str, Any] | None = None
    off_session: bool = False
    confirm: bool = False
    capture_method: Literal["automatic", "manual"] = "automatic"
    invoice_id: str | None = Field(default=None, max_length=128)
    statement_descriptor: str | None = Field(default=None, max_length=22)


class PaymentIntentResponse(BaseModel):
    payment_intent_id: str
    status: str
    customer_id: str
    amount_cents: int
    amount_received_cents: int = 0
    currency: str
    payment_method_id: str | None = None
    description: str | None = None
    client_secret: str | None = None
    gateway_tx_id: str | None = None
    gateway_fee_cents: int = 0
    invoice_id: str | None = None
    metadata: dict[str, Any] | None = None
    next_action: dict[str, Any] | None = None
    last_error: dict[str, Any] | None = None
    created_at: int
    updated_at: int


# ── Helpers ──────────────────────────────────────────────────────────────
async def _load_si(si_id: str, r: aioredis.Redis) -> dict[str, Any] | None:
    raw = await r.hgetall(_k_si(si_id))
    return raw or None


async def _load_pi(pi_id: str, r: aioredis.Redis) -> dict[str, Any] | None:
    raw = await r.hgetall(_k_pi(pi_id))
    return raw or None


def _hydrate_si(si_id: str, raw: dict[str, Any]) -> SetupIntentResponse:
    md_json = raw.get("metadata_json")
    err_json = raw.get("last_error_json")
    na_json = raw.get("next_action_json")
    return SetupIntentResponse(
        setup_intent_id=si_id,
        status=raw.get("status", "requires_payment_method"),
        customer_id=raw["customer_id"],
        payment_method_id=raw.get("payment_method_id") or None,
        usage=raw.get("usage", "off_session"),
        client_secret=raw.get("client_secret") or None,
        next_action=json.loads(na_json) if na_json else None,
        last_error=json.loads(err_json) if err_json else None,
        created_at=int(raw.get("created_at", now_ts())),
        updated_at=int(raw.get("updated_at", now_ts())),
    )


def _hydrate_pi(pi_id: str, raw: dict[str, Any]) -> PaymentIntentResponse:
    md_json = raw.get("metadata_json")
    err_json = raw.get("last_error_json")
    na_json = raw.get("next_action_json")
    return PaymentIntentResponse(
        payment_intent_id=pi_id,
        status=raw.get("status", "requires_payment_method"),
        customer_id=raw["customer_id"],
        amount_cents=int(raw.get("amount", 0)),
        amount_received_cents=int(raw.get("amount_received", 0)),
        currency=raw.get("currency", "USD"),
        payment_method_id=raw.get("payment_method_id") or None,
        description=raw.get("description") or None,
        client_secret=raw.get("client_secret") or None,
        gateway_tx_id=raw.get("gateway_tx_id") or None,
        gateway_fee_cents=int(raw.get("gateway_fee_cents", 0)),
        invoice_id=raw.get("invoice_id") or None,
        metadata=json.loads(md_json) if md_json else None,
        next_action=json.loads(na_json) if na_json else None,
        last_error=json.loads(err_json) if err_json else None,
        created_at=int(raw.get("created_at", now_ts())),
        updated_at=int(raw.get("updated_at", now_ts())),
    )


async def _verify_pm_attached(
    customer_id: str, payment_method_id: str, r: aioredis.Redis
) -> None:
    is_member = await r.sismember(_k_cust_pms(customer_id), payment_method_id)
    if not is_member:
        raise validation_failed(
            "payment_method_id",
            f"{payment_method_id} not attached to {customer_id}",
        )


def _stripe_setup_intent_create(
    customer: dict[str, Any],
    req: SetupIntentCreateRequest,
) -> dict[str, Any]:
    """Mint a real Stripe SetupIntent or simulate."""
    if not _stripe_is_live() or not customer.get("stripe_customer_id"):
        return {
            "id": _mint_si(),
            "status": "requires_confirmation"
            if req.payment_method_id
            else "requires_payment_method",
            "client_secret": None,  # filled by caller w/ KiX-format secret
            "simulated": True,
        }
    try:
        kwargs: dict[str, Any] = {
            "customer": customer["stripe_customer_id"],
            "usage": req.usage,
        }
        if req.payment_method_id:
            kwargs["payment_method"] = req.payment_method_id
        if req.metadata:
            kwargs["metadata"] = req.metadata
        si = stripe.SetupIntent.create(**kwargs)
        return {
            "id": si["id"],
            "status": si["status"],
            "client_secret": si.get("client_secret"),
            "next_action": si.get("next_action"),
            "simulated": False,
        }
    except Exception as exc:
        logger.warning("stripe setup intent create failed: %s", exc)
        return {
            "id": _mint_si(),
            "status": "requires_payment_method",
            "client_secret": None,
            "simulated": True,
            "last_error": {"message": str(exc)},
        }


def _stripe_payment_intent_create(
    customer: dict[str, Any],
    req: PaymentIntentCreateRequest,
) -> dict[str, Any]:
    if not _stripe_is_live() or not customer.get("stripe_customer_id"):
        # Simulated: derive a deterministic-ish gateway fee and a status
        # consistent with the requested flags.
        if req.confirm and req.payment_method_id:
            status_v = "succeeded"
        elif req.payment_method_id:
            status_v = "requires_confirmation"
        else:
            status_v = "requires_payment_method"
        fee = (
            (req.amount_cents * GATEWAY_FEE_BPS) // 10_000
            + GATEWAY_FEE_FIXED_CENTS
            if status_v == "succeeded"
            else 0
        )
        return {
            "id": _mint_pi(),
            "status": status_v,
            "client_secret": None,
            "amount_received": req.amount_cents if status_v == "succeeded" else 0,
            "gateway_tx_id": f"sim_tx_{uuid4().hex}" if status_v == "succeeded" else None,
            "gateway_fee_cents": fee,
            "simulated": True,
        }
    try:
        kwargs: dict[str, Any] = {
            "amount": req.amount_cents,
            "currency": req.currency.lower(),
            "customer": customer["stripe_customer_id"],
            "capture_method": req.capture_method,
        }
        if req.payment_method_id:
            kwargs["payment_method"] = req.payment_method_id
        if req.description:
            kwargs["description"] = req.description
        if req.metadata:
            kwargs["metadata"] = req.metadata
        if req.statement_descriptor:
            kwargs["statement_descriptor"] = req.statement_descriptor
        if req.confirm:
            kwargs["confirm"] = True
            kwargs["off_session"] = req.off_session
        pi = stripe.PaymentIntent.create(**kwargs)
        return {
            "id": pi["id"],
            "status": pi["status"],
            "client_secret": pi.get("client_secret"),
            "amount_received": pi.get("amount_received", 0),
            "gateway_tx_id": (pi.get("latest_charge") or "") if pi["status"] == "succeeded" else None,
            "gateway_fee_cents": 0,
            "simulated": False,
            "next_action": pi.get("next_action"),
        }
    except stripe.error.CardError as exc:  # type: ignore[attr-defined]
        return {
            "id": _mint_pi(),
            "status": "requires_payment_method",
            "simulated": True,
            "last_error": {"code": getattr(exc, "code", "card_error"), "message": str(exc)},
        }
    except Exception as exc:
        logger.warning("stripe payment intent create failed: %s", exc)
        return {
            "id": _mint_pi(),
            "status": "requires_payment_method",
            "simulated": True,
            "last_error": {"code": "gateway_error", "message": str(exc)},
        }


# ── SetupIntent endpoints ────────────────────────────────────────────────
@router.post(
    "/setup",
    status_code=status.HTTP_201_CREATED,
    response_model=SetupIntentResponse,
)
async def create_setup_intent(
    req: SetupIntentCreateRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> SetupIntentResponse:
    customer = await ensure_customer_exists(req.customer_id, r)
    if req.payment_method_id:
        await _verify_pm_attached(req.customer_id, req.payment_method_id, r)

    gw = _stripe_setup_intent_create(customer, req)
    # Always use OUR id namespace for the canonical record (real Stripe id is
    # mirrored under `stripe_setup_intent_id` when live).
    si_id = _mint_si()
    ts = now_ts()
    state = gw["status"]
    secret = gw.get("client_secret") or _client_secret(si_id)
    err = gw.get("last_error")
    na = gw.get("next_action")

    md_json = json.dumps(req.metadata, ensure_ascii=False) if req.metadata else ""
    record: dict[str, Any] = {
        "customer_id": req.customer_id,
        "payment_method_id": req.payment_method_id or "",
        "usage": req.usage,
        "save_for_future_use": "1" if req.save_for_future_use else "0",
        "status": state,
        "client_secret": secret,
        "stripe_setup_intent_id": gw["id"] if not gw.get("simulated") else "",
        "metadata_json": md_json,
        "next_action_json": json.dumps(na, ensure_ascii=False) if na else "",
        "last_error_json": json.dumps(err, ensure_ascii=False) if err else "",
        "created_at": ts,
        "updated_at": ts,
    }

    pipe = r.pipeline()
    pipe.hset(_k_si(si_id), mapping=record)
    pipe.zadd(_k_cust_sis(req.customer_id), {si_id: ts})
    await pipe.execute()
    await _audit_si(
        r,
        si_id,
        "setup_intent.created",
        {"customer_id": req.customer_id, "status": state, "simulated": gw.get("simulated", False)},
    )
    return _hydrate_si(si_id, record)


@router.post(
    "/setup/{setup_intent_id}/confirm",
    response_model=SetupIntentResponse,
)
async def confirm_setup_intent(
    setup_intent_id: str,
    payment_method_id: str | None = None,
    r: aioredis.Redis = Depends(get_redis),
) -> SetupIntentResponse:
    raw = await _load_si(setup_intent_id, r)
    if not raw:
        raise not_found("setup_intent", setup_intent_id)
    if raw["status"] in {"succeeded", "canceled"}:
        # idempotent — already terminal, return as-is
        return _hydrate_si(setup_intent_id, raw)

    pm_id = payment_method_id or raw.get("payment_method_id") or ""
    if not pm_id:
        raise validation_failed("payment_method_id", "required to confirm setup_intent")
    await _verify_pm_attached(raw["customer_id"], pm_id, r)

    # Real Stripe path
    new_status = "succeeded"
    next_action: dict[str, Any] | None = None
    err: dict[str, Any] | None = None
    if _stripe_is_live() and raw.get("stripe_setup_intent_id"):
        try:
            si = stripe.SetupIntent.confirm(
                raw["stripe_setup_intent_id"], payment_method=pm_id
            )
            new_status = si["status"]
            next_action = si.get("next_action")
        except Exception as exc:
            logger.warning("stripe SetupIntent.confirm failed: %s", exc)
            new_status = "requires_payment_method"
            err = {"code": "confirm_error", "message": str(exc)}

    updates: dict[str, Any] = {
        "payment_method_id": pm_id,
        "status": new_status,
        "updated_at": now_ts(),
        "next_action_json": json.dumps(next_action, ensure_ascii=False) if next_action else "",
        "last_error_json": json.dumps(err, ensure_ascii=False) if err else "",
    }
    await r.hset(_k_si(setup_intent_id), mapping=updates)

    # On success, attach the PM to the customer (idempotent SADD)
    if new_status == "succeeded" and raw.get("save_for_future_use") == "1":
        await r.sadd(_k_cust_pms(raw["customer_id"]), pm_id)

    await _audit_si(r, setup_intent_id, "setup_intent.confirmed", {"status": new_status})
    raw2 = await _load_si(setup_intent_id, r)
    return _hydrate_si(setup_intent_id, raw2 or {**raw, **updates})


@router.post(
    "/setup/{setup_intent_id}/cancel",
    response_model=SetupIntentResponse,
)
async def cancel_setup_intent(
    setup_intent_id: str,
    reason: str | None = None,
    r: aioredis.Redis = Depends(get_redis),
) -> SetupIntentResponse:
    raw = await _load_si(setup_intent_id, r)
    if not raw:
        raise not_found("setup_intent", setup_intent_id)
    if raw["status"] in {"succeeded", "canceled"}:
        return _hydrate_si(setup_intent_id, raw)

    if _stripe_is_live() and raw.get("stripe_setup_intent_id"):
        try:
            stripe.SetupIntent.cancel(raw["stripe_setup_intent_id"])
        except Exception as exc:  # do not block local state transition
            logger.warning("stripe SetupIntent.cancel failed: %s", exc)

    updates = {"status": "canceled", "updated_at": now_ts()}
    await r.hset(_k_si(setup_intent_id), mapping=updates)
    await _audit_si(r, setup_intent_id, "setup_intent.canceled", {"reason": reason})
    return _hydrate_si(setup_intent_id, {**raw, **updates})


@router.get("/setup/{setup_intent_id}", response_model=SetupIntentResponse)
async def get_setup_intent(
    setup_intent_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> SetupIntentResponse:
    raw = await _load_si(setup_intent_id, r)
    if not raw:
        raise not_found("setup_intent", setup_intent_id)
    return _hydrate_si(setup_intent_id, raw)


# ── PaymentIntent endpoints ──────────────────────────────────────────────
@router.post(
    "/create",
    status_code=status.HTTP_201_CREATED,
    response_model=PaymentIntentResponse,
)
async def create_payment_intent(
    req: PaymentIntentCreateRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> PaymentIntentResponse:
    customer = await ensure_customer_exists(req.customer_id, r)
    pm_id = req.payment_method_id or customer.get("default_pm") or ""
    if pm_id:
        await _verify_pm_attached(req.customer_id, pm_id, r)

    # Enrich the request with the resolved pm_id for the gateway call
    eff_req = req.model_copy(update={"payment_method_id": pm_id or None})
    gw = _stripe_payment_intent_create(customer, eff_req)

    pi_id = _mint_pi()
    ts = now_ts()
    state = gw["status"]
    secret = gw.get("client_secret") or _client_secret(pi_id)
    err = gw.get("last_error")
    na = gw.get("next_action")

    md_json = json.dumps(req.metadata, ensure_ascii=False) if req.metadata else ""
    record: dict[str, Any] = {
        "customer_id": req.customer_id,
        "payment_method_id": pm_id,
        "amount": req.amount_cents,
        "amount_received": int(gw.get("amount_received", 0)),
        "currency": req.currency.upper(),
        "description": req.description or "",
        "status": state,
        "client_secret": secret,
        "stripe_payment_intent_id": gw["id"] if not gw.get("simulated") else "",
        "gateway_tx_id": gw.get("gateway_tx_id") or "",
        "gateway_fee_cents": int(gw.get("gateway_fee_cents", 0)),
        "capture_method": req.capture_method,
        "invoice_id": req.invoice_id or "",
        "metadata_json": md_json,
        "next_action_json": json.dumps(na, ensure_ascii=False) if na else "",
        "last_error_json": json.dumps(err, ensure_ascii=False) if err else "",
        "created_at": ts,
        "updated_at": ts,
    }

    pipe = r.pipeline()
    pipe.hset(_k_pi(pi_id), mapping=record)
    pipe.zadd(_k_cust_pis(req.customer_id), {pi_id: ts})
    await pipe.execute()
    await _audit_pi(
        r,
        pi_id,
        "payment_intent.created",
        {
            "customer_id": req.customer_id,
            "amount_cents": req.amount_cents,
            "status": state,
            "simulated": gw.get("simulated", False),
        },
    )
    return _hydrate_pi(pi_id, record)


@router.post(
    "/{payment_intent_id}/confirm",
    response_model=PaymentIntentResponse,
)
async def confirm_payment_intent(
    payment_intent_id: str,
    payment_method_id: str | None = None,
    off_session: bool = False,
    r: aioredis.Redis = Depends(get_redis),
) -> PaymentIntentResponse:
    raw = await _load_pi(payment_intent_id, r)
    if not raw:
        raise not_found("payment_intent", payment_intent_id)
    if raw["status"] in {"succeeded", "canceled"}:
        return _hydrate_pi(payment_intent_id, raw)

    pm_id = payment_method_id or raw.get("payment_method_id") or ""
    if not pm_id:
        raise validation_failed("payment_method_id", "required to confirm payment_intent")
    await _verify_pm_attached(raw["customer_id"], pm_id, r)

    new_status = "succeeded"
    amount_received = int(raw["amount"])
    gateway_tx_id = f"sim_tx_{uuid4().hex}"
    gateway_fee = (
        int(raw["amount"]) * GATEWAY_FEE_BPS
    ) // 10_000 + GATEWAY_FEE_FIXED_CENTS
    err: dict[str, Any] | None = None
    next_action: dict[str, Any] | None = None

    if _stripe_is_live() and raw.get("stripe_payment_intent_id"):
        try:
            pi = stripe.PaymentIntent.confirm(
                raw["stripe_payment_intent_id"],
                payment_method=pm_id,
                off_session=off_session,
            )
            new_status = pi["status"]
            amount_received = int(pi.get("amount_received", 0))
            gateway_tx_id = pi.get("latest_charge") or ""
            next_action = pi.get("next_action")
        except stripe.error.CardError as exc:  # type: ignore[attr-defined]
            new_status = "requires_payment_method"
            err = {"code": getattr(exc, "code", "card_error"), "message": str(exc)}
            amount_received = 0
            gateway_tx_id = ""
            gateway_fee = 0
        except Exception as exc:
            new_status = "requires_payment_method"
            err = {"code": "gateway_error", "message": str(exc)}
            amount_received = 0
            gateway_tx_id = ""
            gateway_fee = 0

    updates: dict[str, Any] = {
        "payment_method_id": pm_id,
        "status": new_status,
        "amount_received": amount_received,
        "gateway_tx_id": gateway_tx_id,
        "gateway_fee_cents": gateway_fee,
        "updated_at": now_ts(),
        "next_action_json": json.dumps(next_action, ensure_ascii=False) if next_action else "",
        "last_error_json": json.dumps(err, ensure_ascii=False) if err else "",
    }
    await r.hset(_k_pi(payment_intent_id), mapping=updates)
    await _audit_pi(
        r,
        payment_intent_id,
        "payment_intent.confirmed",
        {"status": new_status, "amount_received": amount_received},
    )

    raw2 = await _load_pi(payment_intent_id, r)
    return _hydrate_pi(payment_intent_id, raw2 or {**raw, **updates})


@router.post(
    "/{payment_intent_id}/cancel",
    response_model=PaymentIntentResponse,
)
async def cancel_payment_intent(
    payment_intent_id: str,
    reason: str | None = None,
    r: aioredis.Redis = Depends(get_redis),
) -> PaymentIntentResponse:
    raw = await _load_pi(payment_intent_id, r)
    if not raw:
        raise not_found("payment_intent", payment_intent_id)
    if raw["status"] in {"succeeded", "canceled"}:
        return _hydrate_pi(payment_intent_id, raw)

    if _stripe_is_live() and raw.get("stripe_payment_intent_id"):
        try:
            stripe.PaymentIntent.cancel(
                raw["stripe_payment_intent_id"],
                cancellation_reason=reason or "requested_by_customer",
            )
        except Exception as exc:
            logger.warning("stripe PaymentIntent.cancel failed: %s", exc)

    updates = {"status": "canceled", "updated_at": now_ts()}
    await r.hset(_k_pi(payment_intent_id), mapping=updates)
    await _audit_pi(r, payment_intent_id, "payment_intent.canceled", {"reason": reason})
    return _hydrate_pi(payment_intent_id, {**raw, **updates})


@router.post(
    "/{payment_intent_id}/capture",
    response_model=PaymentIntentResponse,
)
async def capture_payment_intent(
    payment_intent_id: str,
    amount_to_capture_cents: int | None = None,
    r: aioredis.Redis = Depends(get_redis),
) -> PaymentIntentResponse:
    """Capture a previously authorised (manual capture_method) PaymentIntent."""
    raw = await _load_pi(payment_intent_id, r)
    if not raw:
        raise not_found("payment_intent", payment_intent_id)
    if raw["status"] not in {"requires_capture"}:
        raise error_response(
            409,
            "invalid_state",
            f"payment_intent in state {raw['status']} cannot be captured",
            state=raw["status"],
        )
    capture_amt = amount_to_capture_cents or int(raw["amount"])
    if capture_amt > int(raw["amount"]):
        raise validation_failed(
            "amount_to_capture_cents",
            "cannot exceed original PaymentIntent amount",
        )

    gw_fee = (capture_amt * GATEWAY_FEE_BPS) // 10_000 + GATEWAY_FEE_FIXED_CENTS

    if _stripe_is_live() and raw.get("stripe_payment_intent_id"):
        try:
            stripe.PaymentIntent.capture(
                raw["stripe_payment_intent_id"],
                amount_to_capture=capture_amt,
            )
        except Exception as exc:
            logger.warning("stripe PaymentIntent.capture failed: %s", exc)
            updates = {
                "status": "requires_capture",
                "last_error_json": json.dumps({"message": str(exc)}),
                "updated_at": now_ts(),
            }
            await r.hset(_k_pi(payment_intent_id), mapping=updates)
            return _hydrate_pi(payment_intent_id, {**raw, **updates})

    updates = {
        "status": "succeeded",
        "amount_received": capture_amt,
        "gateway_fee_cents": gw_fee,
        "updated_at": now_ts(),
    }
    await r.hset(_k_pi(payment_intent_id), mapping=updates)
    await _audit_pi(
        r,
        payment_intent_id,
        "payment_intent.captured",
        {"amount_captured_cents": capture_amt},
    )
    return _hydrate_pi(payment_intent_id, {**raw, **updates})


@router.get("/{payment_intent_id}", response_model=PaymentIntentResponse)
async def get_payment_intent(
    payment_intent_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> PaymentIntentResponse:
    raw = await _load_pi(payment_intent_id, r)
    if not raw:
        raise not_found("payment_intent", payment_intent_id)
    return _hydrate_pi(payment_intent_id, raw)


@router.get("/customer/{customer_id}/list")
async def list_payment_intents_for_customer(
    customer_id: str,
    limit: int = 50,
    offset: int = 0,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    await ensure_customer_exists(customer_id, r)
    total = int(await r.zcard(_k_cust_pis(customer_id)))
    # newest-first
    ids = await r.zrevrange(_k_cust_pis(customer_id), offset, offset + limit - 1)
    items: list[dict[str, Any]] = []
    for pi_id in ids:
        raw = await _load_pi(pi_id, r)
        if raw:
            items.append(_hydrate_pi(pi_id, raw).model_dump())
    return list_response(items, total=total, limit=limit, offset=offset)


# ── Re-exports used by invoices.py ───────────────────────────────────────
__all__ = [
    "router",
    "create_payment_intent",
    "PaymentIntentCreateRequest",
    "PaymentIntentResponse",
    "_k_pi",
    "_load_pi",
    "_hydrate_pi",
    "_audit_pi",
    "GATEWAY_FEE_BPS",
    "GATEWAY_FEE_FIXED_CENTS",
]
