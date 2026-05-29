"""Invoice (Stripe-style formal invoice with line items + tax).

An ``Invoice`` is a structured bill issued against a ``Customer``. Each
invoice has one or more line items (description, unit amount, quantity,
optional tax rate), a status state-machine, and — once finalized — a stable
PDF/URL artifact for accounting.

This is the replacement for the wallet's monolithic "topup receipt": the
wallet had no concept of line items, VAT, or formal documents that
accountants and tax authorities can audit against.

State machine
-------------
    draft   →  open           →  paid
            →  void
                ↘  uncollectible

  * ``draft``         — being assembled, line items mutable
  * ``open``          — finalized, immutable, awaiting payment
  * ``paid``          — PaymentIntent succeeded
  * ``void``          — manually voided (no longer collectible)
  * ``uncollectible`` — written off after dunning failed

Redis schema
------------
    invoice:{inv_id}              HASH (status, customer_id, currency,
                                        subtotal_cents, tax_cents,
                                        total_cents, amount_due_cents,
                                        amount_paid_cents, due_date_ts,
                                        finalized_at, paid_at, voided_at,
                                        payment_intent_id, hosted_invoice_url,
                                        invoice_pdf_url, number,
                                        auto_finalize, ...)
    invoice:{inv_id}:line_items   LIST (JSON entries — append-only until
                                        finalize, then frozen)
    customer:{cus_id}:invoices    ZSET (score=created_at)
    invoice:{inv_id}:audit         LIST (lifecycle events)
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
from pydantic import BaseModel, Field, field_validator

from app.api_standards import (
    error_response,
    list_response,
    not_found,
    now_ts,
    validation_failed,
)
from app.redis_client import get_redis
from app.routers.customers import _k_cust, _k_cust_invoices, ensure_customer_exists
from app.routers.payment_intents import (
    GATEWAY_FEE_BPS,
    GATEWAY_FEE_FIXED_CENTS,
    PaymentIntentCreateRequest,
    _hydrate_pi,
    _k_pi,
    _load_pi,
    create_payment_intent,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Stripe SDK init ───────────────────────────────────────────────────────
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "sk_test_stub")


def _stripe_is_live() -> bool:
    return bool(stripe.api_key) and stripe.api_key != "sk_test_stub"


# ── Constants / state ────────────────────────────────────────────────────
AUDIT_MAX = 500
MAX_LINE_ITEMS = 200
MAX_AMOUNT_CENTS = 1_000_000_00  # $1M per line

INVOICE_STATES = {"draft", "open", "paid", "void", "uncollectible"}
TERMINAL_STATES = {"paid", "void", "uncollectible"}


# ── Key helpers ──────────────────────────────────────────────────────────
def _k_inv(inv_id: str) -> str:
    return f"invoice:{inv_id}"


def _k_inv_items(inv_id: str) -> str:
    return f"invoice:{inv_id}:line_items"


def _k_inv_audit(inv_id: str) -> str:
    return f"invoice:{inv_id}:audit"


def _k_inv_number(cus_id: str) -> str:
    return f"customer:{cus_id}:invoice_number_seq"


def _mint_inv() -> str:
    return f"in_{uuid4().hex[:24]}"


async def _audit(
    r: aioredis.Redis, inv_id: str, event: str, detail: dict | None = None
) -> None:
    entry = {"event": event, "ts": now_ts(), "detail": detail or {}}
    try:
        await r.rpush(_k_inv_audit(inv_id), json.dumps(entry, ensure_ascii=False))
        await r.ltrim(_k_inv_audit(inv_id), -AUDIT_MAX, -1)
    except Exception as exc:
        logger.warning("invoice audit failed inv=%s: %s", inv_id, exc)


# ── Pydantic models ──────────────────────────────────────────────────────
class LineItem(BaseModel):
    description: str = Field(..., min_length=1, max_length=512)
    amount_cents: int = Field(..., gt=0, le=MAX_AMOUNT_CENTS)
    quantity: int = Field(default=1, ge=1, le=10_000)
    tax_rate_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    metadata: dict[str, Any] | None = None

    @field_validator("tax_rate_pct")
    @classmethod
    def _round_tax(cls, v: float) -> float:
        # Two decimal places — e.g. 19.0 (DE), 8.875 rounds to 8.88 (NYC).
        return round(v, 2)


class CreateInvoiceRequest(BaseModel):
    customer_id: str = Field(..., min_length=1, max_length=128)
    line_items: list[LineItem] = Field(..., min_length=1, max_length=MAX_LINE_ITEMS)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    description: str | None = Field(default=None, max_length=1024)
    due_date_ts: int | None = None
    auto_finalize: bool = True
    auto_charge: bool = False  # when True + auto_finalize, also attempt charge
    footer: str | None = Field(default=None, max_length=1024)
    metadata: dict[str, Any] | None = None


class LineItemResponse(BaseModel):
    description: str
    amount_cents: int
    quantity: int
    line_subtotal_cents: int
    tax_rate_pct: float
    line_tax_cents: int
    line_total_cents: int
    metadata: dict[str, Any] | None = None


class InvoiceResponse(BaseModel):
    invoice_id: str
    number: str | None = None
    customer_id: str
    status: str
    currency: str
    line_items: list[LineItemResponse]
    subtotal_cents: int
    tax_cents: int
    total_cents: int
    amount_due_cents: int
    amount_paid_cents: int
    description: str | None = None
    footer: str | None = None
    due_date_ts: int | None = None
    finalized_at: int | None = None
    paid_at: int | None = None
    voided_at: int | None = None
    payment_intent_id: str | None = None
    hosted_invoice_url: str | None = None
    invoice_pdf_url: str | None = None
    stripe_invoice_id: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: int
    updated_at: int


# ── Computation helpers ──────────────────────────────────────────────────
def _compute_line(item: LineItem) -> dict[str, Any]:
    subtotal = item.amount_cents * item.quantity
    tax = int(round(subtotal * (item.tax_rate_pct / 100.0)))
    return {
        "description": item.description,
        "amount_cents": item.amount_cents,
        "quantity": item.quantity,
        "line_subtotal_cents": subtotal,
        "tax_rate_pct": item.tax_rate_pct,
        "line_tax_cents": tax,
        "line_total_cents": subtotal + tax,
        "metadata": item.metadata,
    }


def _compute_totals(items: list[dict[str, Any]]) -> tuple[int, int, int]:
    subtotal = sum(i["line_subtotal_cents"] for i in items)
    tax = sum(i["line_tax_cents"] for i in items)
    return subtotal, tax, subtotal + tax


async def _load_invoice(inv_id: str, r: aioredis.Redis) -> dict[str, Any] | None:
    raw = await r.hgetall(_k_inv(inv_id))
    return raw or None


async def _load_items(inv_id: str, r: aioredis.Redis) -> list[dict[str, Any]]:
    rows = await r.lrange(_k_inv_items(inv_id), 0, -1)
    return [json.loads(r_) for r_ in rows]


async def _next_invoice_number(cus_id: str, r: aioredis.Redis) -> str:
    seq = await r.incr(_k_inv_number(cus_id))
    # KIX-{6-char cus suffix}-{6-digit seq}
    suffix = cus_id.split("_", 1)[1][:6].upper() if "_" in cus_id else cus_id[:6].upper()
    return f"KIX-{suffix}-{int(seq):06d}"


def _hydrate(
    inv_id: str, raw: dict[str, Any], items: list[dict[str, Any]]
) -> InvoiceResponse:
    md_json = raw.get("metadata_json")
    return InvoiceResponse(
        invoice_id=inv_id,
        number=raw.get("number") or None,
        customer_id=raw["customer_id"],
        status=raw.get("status", "draft"),
        currency=raw.get("currency", "USD"),
        line_items=[LineItemResponse(**i) for i in items],
        subtotal_cents=int(raw.get("subtotal_cents", 0)),
        tax_cents=int(raw.get("tax_cents", 0)),
        total_cents=int(raw.get("total_cents", 0)),
        amount_due_cents=int(raw.get("amount_due_cents", 0)),
        amount_paid_cents=int(raw.get("amount_paid_cents", 0)),
        description=raw.get("description") or None,
        footer=raw.get("footer") or None,
        due_date_ts=int(raw["due_date_ts"]) if raw.get("due_date_ts") else None,
        finalized_at=int(raw["finalized_at"]) if raw.get("finalized_at") else None,
        paid_at=int(raw["paid_at"]) if raw.get("paid_at") else None,
        voided_at=int(raw["voided_at"]) if raw.get("voided_at") else None,
        payment_intent_id=raw.get("payment_intent_id") or None,
        hosted_invoice_url=raw.get("hosted_invoice_url") or None,
        invoice_pdf_url=raw.get("invoice_pdf_url") or None,
        stripe_invoice_id=raw.get("stripe_invoice_id") or None,
        metadata=json.loads(md_json) if md_json else None,
        created_at=int(raw.get("created_at", now_ts())),
        updated_at=int(raw.get("updated_at", now_ts())),
    )


def _stripe_invoice_create(
    customer: dict[str, Any],
    items: list[dict[str, Any]],
    req: CreateInvoiceRequest,
) -> dict[str, Any]:
    """Mint a real Stripe Invoice in live mode, else return ``None``-ish."""
    if not _stripe_is_live() or not customer.get("stripe_customer_id"):
        return {"simulated": True}
    try:
        # Add invoice items first
        for it in items:
            stripe.InvoiceItem.create(
                customer=customer["stripe_customer_id"],
                amount=it["line_total_cents"],
                currency=req.currency.lower(),
                description=it["description"],
                quantity=it["quantity"],
            )
        inv = stripe.Invoice.create(
            customer=customer["stripe_customer_id"],
            description=req.description,
            footer=req.footer,
            auto_advance=req.auto_finalize,
            metadata=req.metadata or {},
            due_date=req.due_date_ts,
        )
        return {
            "id": inv["id"],
            "hosted_invoice_url": inv.get("hosted_invoice_url"),
            "invoice_pdf": inv.get("invoice_pdf"),
            "number": inv.get("number"),
            "simulated": False,
        }
    except Exception as exc:
        logger.warning("stripe invoice create failed: %s", exc)
        return {"simulated": True, "error": str(exc)}


# ── Endpoints ────────────────────────────────────────────────────────────
@router.post(
    "/create",
    status_code=status.HTTP_201_CREATED,
    response_model=InvoiceResponse,
)
async def create_invoice(
    req: CreateInvoiceRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> InvoiceResponse:
    customer = await ensure_customer_exists(req.customer_id, r)

    items = [_compute_line(li) for li in req.line_items]
    subtotal, tax, total = _compute_totals(items)
    inv_id = _mint_inv()
    ts = now_ts()
    gw = _stripe_invoice_create(customer, items, req)

    md_json = json.dumps(req.metadata, ensure_ascii=False) if req.metadata else ""
    record: dict[str, Any] = {
        "customer_id": req.customer_id,
        "status": "draft",
        "currency": req.currency.upper(),
        "subtotal_cents": subtotal,
        "tax_cents": tax,
        "total_cents": total,
        "amount_due_cents": total,
        "amount_paid_cents": 0,
        "description": req.description or "",
        "footer": req.footer or "",
        "due_date_ts": req.due_date_ts or "",
        "auto_finalize": "1" if req.auto_finalize else "0",
        "stripe_invoice_id": gw["id"] if not gw.get("simulated") else "",
        "hosted_invoice_url": gw.get("hosted_invoice_url") or "",
        "invoice_pdf_url": gw.get("invoice_pdf") or "",
        "number": gw.get("number") or "",
        "metadata_json": md_json,
        "created_at": ts,
        "updated_at": ts,
    }

    pipe = r.pipeline()
    pipe.hset(_k_inv(inv_id), mapping=record)
    for it in items:
        pipe.rpush(_k_inv_items(inv_id), json.dumps(it, ensure_ascii=False))
    pipe.zadd(_k_cust_invoices(req.customer_id), {inv_id: ts})
    await pipe.execute()

    await _audit(
        r,
        inv_id,
        "invoice.created",
        {
            "customer_id": req.customer_id,
            "subtotal_cents": subtotal,
            "tax_cents": tax,
            "total_cents": total,
            "line_item_count": len(items),
        },
    )

    # Optional auto-finalize → optional auto-charge flow
    if req.auto_finalize:
        await _finalize(inv_id, r)
        if req.auto_charge:
            await _charge(inv_id, r)

    raw2 = await _load_invoice(inv_id, r)
    items2 = await _load_items(inv_id, r)
    return _hydrate(inv_id, raw2 or record, items2 or items)


async def _finalize(inv_id: str, r: aioredis.Redis) -> dict[str, Any]:
    raw = await _load_invoice(inv_id, r)
    if not raw:
        raise not_found("invoice", inv_id)
    if raw["status"] != "draft":
        # already finalized — idempotent no-op
        return raw

    cus_id = raw["customer_id"]
    number = raw.get("number") or await _next_invoice_number(cus_id, r)
    finalized_at = now_ts()

    # Hosted URL stub (real value comes from Stripe via webhook in live mode)
    hosted = raw.get("hosted_invoice_url") or f"https://invoice.kix.app/{inv_id}"
    pdf = raw.get("invoice_pdf_url") or f"https://invoice.kix.app/{inv_id}.pdf"

    if _stripe_is_live() and raw.get("stripe_invoice_id"):
        try:
            si = stripe.Invoice.finalize_invoice(raw["stripe_invoice_id"])
            hosted = si.get("hosted_invoice_url") or hosted
            pdf = si.get("invoice_pdf") or pdf
            number = si.get("number") or number
        except Exception as exc:
            logger.warning("stripe Invoice.finalize failed: %s", exc)

    updates = {
        "status": "open",
        "number": number,
        "finalized_at": finalized_at,
        "hosted_invoice_url": hosted,
        "invoice_pdf_url": pdf,
        "updated_at": finalized_at,
    }
    await r.hset(_k_inv(inv_id), mapping=updates)
    await _audit(r, inv_id, "invoice.finalized", {"number": number})
    return {**raw, **updates}


async def _charge(inv_id: str, r: aioredis.Redis) -> dict[str, Any]:
    raw = await _load_invoice(inv_id, r)
    if not raw:
        raise not_found("invoice", inv_id)
    if raw["status"] not in {"open"}:
        raise error_response(
            409,
            "invalid_state",
            f"invoice in state {raw['status']} cannot be charged",
            state=raw["status"],
        )

    cus_id = raw["customer_id"]
    customer_hash = await r.hgetall(_k_cust(cus_id))
    if not customer_hash:
        raise not_found("customer", cus_id)
    default_pm = customer_hash.get("default_pm") or ""
    if not default_pm:
        raise validation_failed(
            "default_payment_method",
            f"customer {cus_id} has no default payment method on file",
        )

    pi_req = PaymentIntentCreateRequest(
        customer_id=cus_id,
        amount_cents=int(raw["amount_due_cents"]),
        currency=raw.get("currency", "USD"),
        payment_method_id=default_pm,
        description=f"Invoice {raw.get('number') or inv_id}",
        metadata={"invoice_id": inv_id, **(json.loads(raw["metadata_json"]) if raw.get("metadata_json") else {})},
        off_session=True,
        confirm=True,
        invoice_id=inv_id,
    )
    pi = await create_payment_intent(pi_req, r)

    paid_now = pi.status == "succeeded"
    updates: dict[str, Any] = {
        "payment_intent_id": pi.payment_intent_id,
        "updated_at": now_ts(),
    }
    if paid_now:
        updates.update(
            {
                "status": "paid",
                "amount_paid_cents": int(raw["amount_due_cents"]),
                "amount_due_cents": 0,
                "paid_at": now_ts(),
            }
        )
    await r.hset(_k_inv(inv_id), mapping=updates)

    await _audit(
        r,
        inv_id,
        "invoice.charged",
        {
            "payment_intent_id": pi.payment_intent_id,
            "status": pi.status,
            "paid": paid_now,
        },
    )
    return {**raw, **updates}


@router.post("/{invoice_id}/finalize", response_model=InvoiceResponse)
async def finalize_invoice(
    invoice_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> InvoiceResponse:
    await _finalize(invoice_id, r)
    raw = await _load_invoice(invoice_id, r)
    items = await _load_items(invoice_id, r)
    if not raw:
        raise not_found("invoice", invoice_id)
    return _hydrate(invoice_id, raw, items)


@router.post("/{invoice_id}/charge", response_model=InvoiceResponse)
async def charge_invoice(
    invoice_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> InvoiceResponse:
    await _charge(invoice_id, r)
    raw = await _load_invoice(invoice_id, r)
    items = await _load_items(invoice_id, r)
    if not raw:
        raise not_found("invoice", invoice_id)
    return _hydrate(invoice_id, raw, items)


@router.post("/{invoice_id}/void", response_model=InvoiceResponse)
async def void_invoice(
    invoice_id: str,
    reason: str | None = None,
    r: aioredis.Redis = Depends(get_redis),
) -> InvoiceResponse:
    raw = await _load_invoice(invoice_id, r)
    if not raw:
        raise not_found("invoice", invoice_id)
    if raw["status"] in {"paid", "void", "uncollectible"}:
        raise error_response(
            409,
            "invalid_state",
            f"invoice in state {raw['status']} cannot be voided",
            state=raw["status"],
        )

    if _stripe_is_live() and raw.get("stripe_invoice_id"):
        try:
            stripe.Invoice.void_invoice(raw["stripe_invoice_id"])
        except Exception as exc:
            logger.warning("stripe Invoice.void failed: %s", exc)

    updates = {
        "status": "void",
        "voided_at": now_ts(),
        "amount_due_cents": 0,
        "updated_at": now_ts(),
    }
    await r.hset(_k_inv(invoice_id), mapping=updates)
    await _audit(r, invoice_id, "invoice.voided", {"reason": reason})

    items = await _load_items(invoice_id, r)
    return _hydrate(invoice_id, {**raw, **updates}, items)


@router.post(
    "/{invoice_id}/mark-uncollectible",
    response_model=InvoiceResponse,
)
async def mark_uncollectible(
    invoice_id: str,
    reason: str | None = None,
    r: aioredis.Redis = Depends(get_redis),
) -> InvoiceResponse:
    raw = await _load_invoice(invoice_id, r)
    if not raw:
        raise not_found("invoice", invoice_id)
    if raw["status"] not in {"open"}:
        raise error_response(
            409,
            "invalid_state",
            f"invoice in state {raw['status']} cannot be marked uncollectible",
            state=raw["status"],
        )
    if _stripe_is_live() and raw.get("stripe_invoice_id"):
        try:
            stripe.Invoice.mark_uncollectible(raw["stripe_invoice_id"])
        except Exception as exc:
            logger.warning("stripe Invoice.mark_uncollectible failed: %s", exc)

    updates = {
        "status": "uncollectible",
        "updated_at": now_ts(),
    }
    await r.hset(_k_inv(invoice_id), mapping=updates)
    await _audit(r, invoice_id, "invoice.uncollectible", {"reason": reason})

    items = await _load_items(invoice_id, r)
    return _hydrate(invoice_id, {**raw, **updates}, items)


@router.post(
    "/{invoice_id}/line-items/add",
    response_model=InvoiceResponse,
)
async def add_line_item(
    invoice_id: str,
    item: LineItem,
    r: aioredis.Redis = Depends(get_redis),
) -> InvoiceResponse:
    """Add a line item to a DRAFT invoice. Idempotent only via caller-side
    deduplication — body has no idempotency key.
    """
    raw = await _load_invoice(invoice_id, r)
    if not raw:
        raise not_found("invoice", invoice_id)
    if raw["status"] != "draft":
        raise error_response(
            409,
            "invalid_state",
            "line items can only be added while invoice is in draft",
            state=raw["status"],
        )
    current_count = int(await r.llen(_k_inv_items(invoice_id)))
    if current_count >= MAX_LINE_ITEMS:
        raise validation_failed(
            "line_items", f"invoice already at limit ({MAX_LINE_ITEMS})"
        )

    line = _compute_line(item)
    await r.rpush(_k_inv_items(invoice_id), json.dumps(line, ensure_ascii=False))

    items = await _load_items(invoice_id, r)
    subtotal, tax, total = _compute_totals(items)
    updates = {
        "subtotal_cents": subtotal,
        "tax_cents": tax,
        "total_cents": total,
        "amount_due_cents": total,
        "updated_at": now_ts(),
    }
    await r.hset(_k_inv(invoice_id), mapping=updates)
    await _audit(
        r,
        invoice_id,
        "invoice.line_item_added",
        {"description": item.description, "line_total_cents": line["line_total_cents"]},
    )
    return _hydrate(invoice_id, {**raw, **updates}, items)


@router.get("/{invoice_id}", response_model=InvoiceResponse)
async def get_invoice(
    invoice_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> InvoiceResponse:
    raw = await _load_invoice(invoice_id, r)
    if not raw:
        raise not_found("invoice", invoice_id)
    items = await _load_items(invoice_id, r)
    return _hydrate(invoice_id, raw, items)


@router.get("/customer/{customer_id}")
async def list_invoices_for_customer(
    customer_id: str,
    status_filter: Literal["draft", "open", "paid", "void", "uncollectible", "any"] = "any",
    limit: int = 50,
    offset: int = 0,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    await ensure_customer_exists(customer_id, r)
    total = int(await r.zcard(_k_cust_invoices(customer_id)))
    ids = await r.zrevrange(_k_cust_invoices(customer_id), 0, -1)
    items: list[dict[str, Any]] = []
    skipped = 0
    for inv_id in ids:
        raw = await _load_invoice(inv_id, r)
        if not raw:
            continue
        if status_filter != "any" and raw.get("status") != status_filter:
            continue
        if skipped < offset:
            skipped += 1
            continue
        if len(items) >= limit:
            break
        line_items = await _load_items(inv_id, r)
        items.append(_hydrate(inv_id, raw, line_items).model_dump())
    # When filter is "any" the true total comes from the zset cardinality.
    # When filtered, the displayed total is len(items) (best-effort, paginated).
    effective_total = total if status_filter == "any" else None
    return list_response(items, total=effective_total, limit=limit, offset=offset)


@router.get("/{invoice_id}/audit")
async def get_invoice_audit(
    invoice_id: str,
    limit: int = 100,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    raw = await _load_invoice(invoice_id, r)
    if not raw:
        raise not_found("invoice", invoice_id)
    rows = await r.lrange(_k_inv_audit(invoice_id), -limit, -1)
    events = [json.loads(row) for row in rows]
    return {"invoice_id": invoice_id, "events": events, "count": len(events)}
