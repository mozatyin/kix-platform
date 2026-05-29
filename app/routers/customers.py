"""Customer abstraction (Stripe-style merchant Customer object).

A ``Customer`` is the persistent merchant identity in our billing stack. It
collects billing email, billing address, tax id, an arbitrary set of saved
``PaymentMethod`` objects and a designated default. Subsequent ``SetupIntent``
``PaymentIntent`` and ``Invoice`` operations attach to the customer rather
than to the (legacy, monolithic) wallet.

Why a dedicated Customer (post Trinity-A audit)
-----------------------------------------------
Until now KiX merchants existed only as a ``brand_id`` hash. There was no
place to:

  * store multiple cards per brand (one-card-per-brand was a Redis schema
    limitation, not a product rule);
  * disable / rotate cards without losing card history;
  * record tax ids and structured billing addresses (needed for invoicing);
  * issue formal invoices with line items + VAT.

The Customer object solves all four by becoming the canonical billing root.

Compat with legacy wallet
-------------------------
Existing ``wallet.topup`` / ``wallet.charge`` keep working unchanged. New
code SHOULD prefer ``customers`` + ``payment_intents`` + ``invoices``; the
legacy wallet is treated as a back-compat shim.

Stripe integration
------------------
If ``STRIPE_SECRET_KEY`` is configured (i.e. a real key, not the sentinel
``sk_test_stub``) we mint a real Stripe Customer and persist the
``stripe_customer_id`` (``cus_...``) alongside the KiX customer hash. In
dev / CI we simulate.

Redis schema
------------
    customer:{cus_id}                  HASH (brand_id, billing_email,
                                            billing_address_json, tax_id,
                                            default_pm, stripe_customer_id,
                                            created_at, updated_at)
    brand:{bid}:customer               STRING → cus_id (reverse lookup)
    customer:{cus_id}:payment_methods  SET of pm_ids
    customer:{cus_id}:invoices         ZSET (score = created_at)
    customer:{cus_id}:audit            LIST (recent lifecycle events)
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any
from uuid import uuid4

import redis.asyncio as aioredis
import stripe
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, EmailStr, Field

from app.api_standards import (
    error_response,
    list_response,
    mint_id,
    not_found,
    now_ts,
    validation_failed,
)
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Stripe SDK init ───────────────────────────────────────────────────────
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "sk_test_stub")


def _stripe_is_live() -> bool:
    return bool(stripe.api_key) and stripe.api_key != "sk_test_stub"


# ── Constants / keys ──────────────────────────────────────────────────────
AUDIT_MAX = 500
CUSTOMER_ID_PREFIX = "cus"  # not in ID_PREFIXES — Stripe-style namespace


def _k_cust(cus_id: str) -> str:
    return f"customer:{cus_id}"


def _k_brand_cust(brand_id: str) -> str:
    return f"brand:{brand_id}:customer"


def _k_cust_pms(cus_id: str) -> str:
    return f"customer:{cus_id}:payment_methods"


def _k_cust_invoices(cus_id: str) -> str:
    return f"customer:{cus_id}:invoices"


def _k_audit(cus_id: str) -> str:
    return f"customer:{cus_id}:audit"


async def _audit(
    r: aioredis.Redis,
    cus_id: str,
    event: str,
    detail: dict | None = None,
) -> None:
    entry = {"event": event, "ts": now_ts(), "detail": detail or {}}
    try:
        await r.rpush(_k_audit(cus_id), json.dumps(entry, ensure_ascii=False))
        await r.ltrim(_k_audit(cus_id), -AUDIT_MAX, -1)
    except Exception as exc:  # never break the request path
        logger.warning("customer audit failed cus_id=%s event=%s: %s", cus_id, event, exc)


def _mint_customer_id() -> str:
    """Stripe-style cus_<24-hex> id (longer than KiX 22-hex default)."""
    return f"{CUSTOMER_ID_PREFIX}_{uuid4().hex[:24]}"


# ── Pydantic models ──────────────────────────────────────────────────────
class BillingAddress(BaseModel):
    line1: str = Field(..., max_length=256)
    line2: str | None = Field(default=None, max_length=256)
    city: str = Field(..., max_length=128)
    state: str | None = Field(default=None, max_length=128)
    postal_code: str = Field(..., max_length=32)
    country: str = Field(..., min_length=2, max_length=2)  # ISO-3166-1 alpha-2


class CreateCustomerRequest(BaseModel):
    brand_id: str = Field(..., min_length=1, max_length=128)
    billing_email: EmailStr | None = None
    billing_address: BillingAddress | None = None
    tax_id: str | None = Field(default=None, max_length=64)
    default_payment_method: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=256)
    metadata: dict[str, Any] | None = None


class UpdateCustomerRequest(BaseModel):
    billing_email: EmailStr | None = None
    billing_address: BillingAddress | None = None
    tax_id: str | None = Field(default=None, max_length=64)
    default_payment_method_id: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=256)
    metadata: dict[str, Any] | None = None


class CustomerResponse(BaseModel):
    customer_id: str
    brand_id: str
    billing_email: str | None = None
    billing_address: dict[str, Any] | None = None
    tax_id: str | None = None
    default_payment_method_id: str | None = None
    name: str | None = None
    stripe_customer_id: str | None = None
    payment_method_ids: list[str] = Field(default_factory=list)
    invoice_count: int = 0
    metadata: dict[str, Any] | None = None
    created_at: int
    updated_at: int


# ── Storage helpers ──────────────────────────────────────────────────────
async def _load_customer(cus_id: str, r: aioredis.Redis) -> dict[str, Any] | None:
    raw = await r.hgetall(_k_cust(cus_id))
    return raw or None


async def _hydrate(cus_id: str, raw: dict[str, Any], r: aioredis.Redis) -> CustomerResponse:
    pm_ids = sorted(await r.smembers(_k_cust_pms(cus_id)))
    inv_count = int(await r.zcard(_k_cust_invoices(cus_id)))
    addr_json = raw.get("billing_address_json")
    md_json = raw.get("metadata_json")
    return CustomerResponse(
        customer_id=cus_id,
        brand_id=raw["brand_id"],
        billing_email=raw.get("billing_email") or None,
        billing_address=json.loads(addr_json) if addr_json else None,
        tax_id=raw.get("tax_id") or None,
        default_payment_method_id=raw.get("default_pm") or None,
        name=raw.get("name") or None,
        stripe_customer_id=raw.get("stripe_customer_id") or None,
        payment_method_ids=pm_ids,
        invoice_count=inv_count,
        metadata=json.loads(md_json) if md_json else None,
        created_at=int(raw.get("created_at", now_ts())),
        updated_at=int(raw.get("updated_at", raw.get("created_at", now_ts()))),
    )


async def _stripe_create_customer(req: CreateCustomerRequest) -> str | None:
    """Mint a real Stripe Customer or return ``None`` in simulated mode."""
    if not _stripe_is_live():
        return None
    try:
        kwargs: dict[str, Any] = {
            "metadata": {"brand_id": req.brand_id, **(req.metadata or {})},
        }
        if req.billing_email:
            kwargs["email"] = str(req.billing_email)
        if req.name:
            kwargs["name"] = req.name
        if req.billing_address:
            addr = req.billing_address.model_dump()
            kwargs["address"] = {
                "line1": addr["line1"],
                "line2": addr.get("line2"),
                "city": addr["city"],
                "state": addr.get("state"),
                "postal_code": addr["postal_code"],
                "country": addr["country"],
            }
        c = stripe.Customer.create(**kwargs)
        return c["id"]
    except Exception as exc:
        logger.warning("stripe customer create failed: %s", exc)
        return None


async def _stripe_update_customer(
    stripe_customer_id: str, req: UpdateCustomerRequest
) -> None:
    if not _stripe_is_live() or not stripe_customer_id:
        return
    try:
        kwargs: dict[str, Any] = {}
        if req.billing_email is not None:
            kwargs["email"] = str(req.billing_email)
        if req.name is not None:
            kwargs["name"] = req.name
        if req.billing_address is not None:
            addr = req.billing_address.model_dump()
            kwargs["address"] = {
                "line1": addr["line1"],
                "line2": addr.get("line2"),
                "city": addr["city"],
                "state": addr.get("state"),
                "postal_code": addr["postal_code"],
                "country": addr["country"],
            }
        if req.default_payment_method_id is not None:
            kwargs["invoice_settings"] = {
                "default_payment_method": req.default_payment_method_id,
            }
        if req.metadata is not None:
            kwargs["metadata"] = req.metadata
        if kwargs:
            stripe.Customer.modify(stripe_customer_id, **kwargs)
    except Exception as exc:
        logger.warning(
            "stripe customer modify failed cus=%s: %s", stripe_customer_id, exc
        )


# ── Public lookups used by other routers ─────────────────────────────────
async def get_customer_id_for_brand(
    brand_id: str, r: aioredis.Redis
) -> str | None:
    cus_id = await r.get(_k_brand_cust(brand_id))
    return cus_id or None


async def ensure_customer_exists(
    cus_id: str, r: aioredis.Redis
) -> dict[str, Any]:
    raw = await _load_customer(cus_id, r)
    if not raw:
        raise not_found("customer", cus_id)
    return raw


# ── Endpoints ────────────────────────────────────────────────────────────
@router.post("/create", status_code=status.HTTP_201_CREATED, response_model=CustomerResponse)
async def create_customer(
    req: CreateCustomerRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> CustomerResponse:
    """Mint a new Customer for a brand. One Customer per brand_id (idempotent).

    If a customer already exists for the brand_id the existing record is
    returned untouched — this lets clients retry without race-creating
    duplicates. To overwrite, use ``/update``.
    """
    existing_cus_id = await r.get(_k_brand_cust(req.brand_id))
    if existing_cus_id:
        raw = await _load_customer(existing_cus_id, r)
        if raw:
            return await _hydrate(existing_cus_id, raw, r)

    cus_id = _mint_customer_id()
    stripe_id = await _stripe_create_customer(req)
    ts = now_ts()

    addr_json = (
        json.dumps(req.billing_address.model_dump(), ensure_ascii=False)
        if req.billing_address
        else ""
    )
    md_json = json.dumps(req.metadata, ensure_ascii=False) if req.metadata else ""
    hash_data: dict[str, Any] = {
        "brand_id": req.brand_id,
        "billing_email": str(req.billing_email) if req.billing_email else "",
        "billing_address_json": addr_json,
        "tax_id": req.tax_id or "",
        "default_pm": req.default_payment_method or "",
        "name": req.name or "",
        "stripe_customer_id": stripe_id or "",
        "metadata_json": md_json,
        "created_at": ts,
        "updated_at": ts,
    }

    pipe = r.pipeline()
    pipe.hset(_k_cust(cus_id), mapping=hash_data)
    pipe.set(_k_brand_cust(req.brand_id), cus_id)
    if req.default_payment_method:
        pipe.sadd(_k_cust_pms(cus_id), req.default_payment_method)
    await pipe.execute()

    await _audit(
        r,
        cus_id,
        "customer.created",
        {"brand_id": req.brand_id, "stripe_customer_id": stripe_id},
    )
    return await _hydrate(cus_id, hash_data, r)


@router.get("/{customer_id}", response_model=CustomerResponse)
async def get_customer(
    customer_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> CustomerResponse:
    raw = await _load_customer(customer_id, r)
    if not raw:
        raise not_found("customer", customer_id)
    return await _hydrate(customer_id, raw, r)


@router.post("/{customer_id}/update", response_model=CustomerResponse)
async def update_customer(
    customer_id: str,
    req: UpdateCustomerRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> CustomerResponse:
    raw = await _load_customer(customer_id, r)
    if not raw:
        raise not_found("customer", customer_id)

    # Validate the default_payment_method_id is a known pm under this customer
    if req.default_payment_method_id is not None and req.default_payment_method_id != "":
        is_member = await r.sismember(
            _k_cust_pms(customer_id), req.default_payment_method_id
        )
        if not is_member:
            raise validation_failed(
                "default_payment_method_id",
                f"{req.default_payment_method_id} not attached to {customer_id}",
            )

    updates: dict[str, Any] = {"updated_at": now_ts()}
    if req.billing_email is not None:
        updates["billing_email"] = str(req.billing_email)
    if req.billing_address is not None:
        updates["billing_address_json"] = json.dumps(
            req.billing_address.model_dump(), ensure_ascii=False
        )
    if req.tax_id is not None:
        updates["tax_id"] = req.tax_id
    if req.default_payment_method_id is not None:
        updates["default_pm"] = req.default_payment_method_id
    if req.name is not None:
        updates["name"] = req.name
    if req.metadata is not None:
        updates["metadata_json"] = json.dumps(req.metadata, ensure_ascii=False)

    await r.hset(_k_cust(customer_id), mapping=updates)
    await _stripe_update_customer(raw.get("stripe_customer_id", ""), req)
    await _audit(r, customer_id, "customer.updated", {"fields": sorted(updates.keys())})

    raw2 = await _load_customer(customer_id, r)
    return await _hydrate(customer_id, raw2 or raw, r)


@router.get("/by-brand/{brand_id}", response_model=CustomerResponse)
async def get_customer_by_brand(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> CustomerResponse:
    cus_id = await r.get(_k_brand_cust(brand_id))
    if not cus_id:
        raise not_found("customer", f"brand:{brand_id}")
    raw = await _load_customer(cus_id, r)
    if not raw:
        # data drift — reverse-lookup pointed at a gone hash
        raise not_found("customer", cus_id)
    return await _hydrate(cus_id, raw, r)


@router.post(
    "/{customer_id}/payment-methods/attach",
    status_code=status.HTTP_200_OK,
)
async def attach_payment_method(
    customer_id: str,
    payment_method_id: str,
    set_as_default: bool = False,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Link an existing PaymentMethod to this customer.

    The actual ``payment_method:{pm_id}`` hash is owned by
    ``payment_methods.py``; we just maintain the customer-side index so that
    SetupIntent / PaymentIntent flows can enumerate cards quickly.
    """
    raw = await _load_customer(customer_id, r)
    if not raw:
        raise not_found("customer", customer_id)
    pipe = r.pipeline()
    pipe.sadd(_k_cust_pms(customer_id), payment_method_id)
    updates: dict[str, Any] = {"updated_at": now_ts()}
    if set_as_default:
        updates["default_pm"] = payment_method_id
    pipe.hset(_k_cust(customer_id), mapping=updates)
    await pipe.execute()
    await _audit(
        r,
        customer_id,
        "payment_method.attached",
        {"payment_method_id": payment_method_id, "set_as_default": set_as_default},
    )
    return {"ok": True, "customer_id": customer_id, "payment_method_id": payment_method_id}


@router.post(
    "/{customer_id}/payment-methods/detach",
    status_code=status.HTTP_200_OK,
)
async def detach_payment_method(
    customer_id: str,
    payment_method_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    raw = await _load_customer(customer_id, r)
    if not raw:
        raise not_found("customer", customer_id)
    pipe = r.pipeline()
    pipe.srem(_k_cust_pms(customer_id), payment_method_id)
    updates: dict[str, Any] = {"updated_at": now_ts()}
    # If this was the default, clear it. Caller can pick a new default via
    # /update.
    if raw.get("default_pm") == payment_method_id:
        updates["default_pm"] = ""
    pipe.hset(_k_cust(customer_id), mapping=updates)
    await pipe.execute()
    await _audit(
        r,
        customer_id,
        "payment_method.detached",
        {"payment_method_id": payment_method_id},
    )
    return {"ok": True, "customer_id": customer_id, "payment_method_id": payment_method_id}


@router.get("/{customer_id}/payment-methods")
async def list_payment_methods(
    customer_id: str,
    limit: int = 50,
    offset: int = 0,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    raw = await _load_customer(customer_id, r)
    if not raw:
        raise not_found("customer", customer_id)
    pm_ids = sorted(await r.smembers(_k_cust_pms(customer_id)))
    total = len(pm_ids)
    page = pm_ids[offset : offset + limit]
    items = []
    for pm in page:
        method = await r.hgetall(f"payment_method:{pm}")
        items.append(
            {
                "payment_method_id": pm,
                "method_type": method.get("method_type"),
                "last4": method.get("last4") or None,
                "expiry_month": int(method["expiry_month"]) if method.get("expiry_month") else None,
                "expiry_year": int(method["expiry_year"]) if method.get("expiry_year") else None,
                "status": method.get("status", "unknown"),
                "verified": method.get("verified") == "1",
                "is_default": raw.get("default_pm") == pm,
            }
        )
    return list_response(items, total=total, limit=limit, offset=offset)


@router.get("/{customer_id}/audit")
async def get_audit_log(
    customer_id: str,
    limit: int = 100,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    raw = await _load_customer(customer_id, r)
    if not raw:
        raise not_found("customer", customer_id)
    rows = await r.lrange(_k_audit(customer_id), -limit, -1)
    events = [json.loads(row) for row in rows]
    return {"customer_id": customer_id, "events": events, "count": len(events)}
