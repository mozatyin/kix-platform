"""Payment Method on-file storage + anti-fraud check.

Per MERCHANT_FLOW_TRUTH.md, every brand MUST have a payment method on file
(even FREE tier) to:
  1. Prevent abuse (one card = one account)
  2. Enable Year-2 auto-charge for subscriptions
  3. Background billing for ad campaigns

PCI compliance: we NEVER store the full PAN. Only a gateway ``payment_token``
(reference returned by Stripe/Adyen/etc.), the masked ``last4`` for display,
expiry, and holder metadata. The raw card never touches our infrastructure.

Anti-fraud: each payment-token hash is indexed back to the set of brand_ids
it has been linked to. At registration we reject tokens already linked to a
*different* brand, preventing "one card = N free accounts" abuse.

Redis schema
------------
    payment_method:{pm_id}                    HASH (state, masked info, no PAN)
    brand:{bid}:payment_methods               SET  of pm_ids (active+removed)
    brand:{bid}:payment_method:default        STRING (pm_id)
    payment_token_hash:{hash}                 SET  of brand_ids (fraud index)
    payment_method:{pm_id}:audit              LIST (events: created/verified/...)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Literal
from uuid import uuid4

import redis.asyncio as aioredis
import stripe
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Stripe SDK init ───────────────────────────────────────────────────────
# In production set STRIPE_SECRET_KEY (sk_live_… or sk_test_…). With the
# default "sk_test_stub" sentinel the gateway calls fall back to a local
# simulation so dev/CI can run without network credentials.
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "sk_test_stub")


def _stripe_is_live() -> bool:
    """True when a real Stripe API key is configured."""
    return bool(stripe.api_key) and stripe.api_key != "sk_test_stub"


# ── Constants ────────────────────────────────────────────────────────────
SUPPORTED_METHOD_TYPES = {
    "credit_card",
    "debit_card",
    "wechat_pay",
    "alipay",
    "corporate_account",
    "bank_transfer",
}

AUDIT_MAX = 500
DEFAULT_VERIFY_AMOUNT_CENTS = 100  # ¥1 hold + reverse
GATEWAY_FEE_BPS = 290              # 2.9% Stripe-style
GATEWAY_FEE_FIXED_CENTS = 30


# ── Key helpers ──────────────────────────────────────────────────────────
def _k_pm(pm_id: str) -> str:
    return f"payment_method:{pm_id}"


def _k_brand_pms(brand_id: str) -> str:
    return f"brand:{brand_id}:payment_methods"


def _k_brand_default(brand_id: str) -> str:
    return f"brand:{brand_id}:payment_method:default"


def _k_token_hash(token_hash: str) -> str:
    return f"payment_token_hash:{token_hash}"


def _k_audit(pm_id: str) -> str:
    return f"payment_method:{pm_id}:audit"


def _k_charge_idem(pm_id: str, reference_id: str) -> str:
    return f"payment_method:{pm_id}:charge_idem:{reference_id}"


def _hash_token(payment_token: str) -> str:
    """SHA-256 of the gateway token. Stable for fraud-index lookups while
    never persisting the raw token alongside the index value."""
    return hashlib.sha256(payment_token.encode("utf-8")).hexdigest()


async def _audit(
    r: aioredis.Redis,
    pm_id: str,
    event: str,
    detail: dict | None = None,
) -> None:
    entry = {
        "event": event,
        "ts": time.time(),
        "detail": detail or {},
    }
    try:
        await r.rpush(_k_audit(pm_id), json.dumps(entry, ensure_ascii=False))
        await r.ltrim(_k_audit(pm_id), -AUDIT_MAX, -1)
    except Exception as exc:  # never break the request path
        logger.warning("audit failed pm_id=%s event=%s: %s", pm_id, event, exc)


# ── Pydantic models ──────────────────────────────────────────────────────
class AddPaymentMethodRequest(BaseModel):
    method_type: Literal[
        "credit_card",
        "debit_card",
        "wechat_pay",
        "alipay",
        "corporate_account",
        "bank_transfer",
    ]
    payment_token: str = Field(..., min_length=1, max_length=512)
    last4: str | None = Field(default=None, max_length=4)
    expiry_month: int | None = Field(default=None, ge=1, le=12)
    expiry_year: int | None = Field(default=None, ge=2024, le=2099)
    holder_name: str = Field(..., min_length=1, max_length=128)
    holder_email: str | None = Field(default=None, max_length=320)
    billing_address: dict[str, Any] | None = None
    is_default: bool = True
    # Anti-fraud context (optional but recommended at registration)
    ip_address: str | None = Field(default=None, max_length=64)
    device_fingerprint: str | None = Field(default=None, max_length=128)

    @field_validator("last4")
    @classmethod
    def _last4(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.isdigit() or len(v) != 4:
            raise ValueError("last4 must be exactly 4 digits")
        return v


class PaymentMethodResponse(BaseModel):
    payment_method_id: str
    brand_id: str
    method_type: str
    last4: str | None = None
    expiry_month: int | None = None
    expiry_year: int | None = None
    holder_name: str | None = None
    is_default: bool
    status: Literal["active", "removed", "inactive"]
    verified: bool
    created_at: float
    updated_at: float | None = None


class AddPaymentMethodResponse(BaseModel):
    payment_method_id: str
    status: Literal["active", "inactive"]
    verified: bool
    is_default: bool


class VerifyRequest(BaseModel):
    micro_charge_amount_cents: int = Field(
        default=DEFAULT_VERIFY_AMOUNT_CENTS, ge=1, le=10_000
    )


class VerifyResponse(BaseModel):
    payment_method_id: str
    verified: bool
    gateway_auth_id: str | None = None
    reversed: bool


class RemoveRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=512)


class SetDefaultResponse(BaseModel):
    payment_method_id: str
    is_default: bool


class ChargeRequest(BaseModel):
    amount_cents: int = Field(..., gt=0, le=1_000_000_00)  # ≤ $1M
    reason: str = Field(..., min_length=1, max_length=256)
    reference_id: str = Field(..., min_length=1, max_length=128)
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    credit_wallet: bool = True  # on success, also credit brand wallet
    payment_method_id: str | None = None  # override default


class ChargeResponse(BaseModel):
    ok: bool
    charge_id: str | None = None
    gateway_tx_id: str | None = None
    amount_cents: int
    gateway_fee_cents: int = 0
    reason: str | None = None  # error code on failure
    idempotent: bool = False
    new_wallet_balance_cents: int | None = None


class AntiFraudRequest(BaseModel):
    brand_id: str = Field(..., min_length=1, max_length=128)
    payment_token_hash: str | None = Field(default=None, min_length=1, max_length=128)
    payment_token: str | None = Field(default=None, min_length=1, max_length=512)
    ip_address: str | None = Field(default=None, max_length=64)
    device_fingerprint: str | None = Field(default=None, max_length=128)


class AntiFraudResponse(BaseModel):
    is_unique: bool
    conflicting_brand_ids: list[str]
    token_hash: str


# ── Internal helpers ─────────────────────────────────────────────────────
async def check_payment_uniqueness(
    payment_token_hash: str, brand_id: str, r: aioredis.Redis
) -> tuple[bool, list[str]]:
    """Return (is_unique, conflicting_brand_ids).

    "Unique" means: no *other* brand has linked this card. The caller's own
    brand_id is excluded — relinking the same card to the same brand is fine.
    """
    existing = await r.smembers(_k_token_hash(payment_token_hash))
    others = sorted([b for b in existing if b != brand_id])
    return (len(others) == 0, others)


async def link_payment_to_brand(
    brand_id: str, payment_token_hash: str, r: aioredis.Redis
) -> None:
    """Record card-to-brand linkage for fraud check (idempotent)."""
    await r.sadd(_k_token_hash(payment_token_hash), brand_id)


async def _get_default_pm_id(brand_id: str, r: aioredis.Redis) -> str | None:
    pm_id = await r.get(_k_brand_default(brand_id))
    return pm_id or None


async def _load_pm(pm_id: str, r: aioredis.Redis) -> dict[str, Any] | None:
    raw = await r.hgetall(_k_pm(pm_id))
    return raw or None


async def _gateway_charge(
    pm_id: str,
    amount_cents: int,
    currency: str,
    reference_id: str,
    r: aioredis.Redis,
) -> dict[str, Any]:
    """Real Stripe PaymentIntent integration.

    Behaviour:
      - No real STRIPE_SECRET_KEY → simulated success (preserves dev/CI flow).
      - Real key  → off-session, auto-confirmed PaymentIntent against the
        stored Stripe customer + payment-method ids; idempotent on
        ``reference_id`` so retries collapse to one charge upstream.
    """
    method = await r.hgetall(_k_pm(pm_id))
    if not method:
        return {"success": False, "error": "method_not_found"}
    if method.get("status") != "active":
        return {"success": False, "error": "method_inactive"}
    if method.get("verified") != "1":
        return {"success": False, "error": "method_unverified"}

    # Simulated path — no real Stripe key configured.
    if not _stripe_is_live():
        logger.warning("No STRIPE_SECRET_KEY; using simulated charge")
        fee = (amount_cents * GATEWAY_FEE_BPS) // 10_000 + GATEWAY_FEE_FIXED_CENTS
        return {
            "success": True,
            "gateway_tx_id": f"sim_tx_{uuid4().hex}",
            "gateway_fee_cents": fee,
            "currency": currency,
            "reference_id": reference_id,
        }

    # Real Stripe path.
    stripe_pm_id = method.get("payment_token", "")
    stripe_customer_id = method.get("stripe_customer_id", "")

    if not stripe_customer_id:
        return {"success": False, "error": "no_stripe_customer"}
    if not stripe_pm_id:
        return {"success": False, "error": "no_stripe_payment_method"}

    try:
        intent = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency=currency.lower(),
            customer=stripe_customer_id,
            payment_method=stripe_pm_id,
            off_session=True,
            confirm=True,
            metadata={
                "reference_id": reference_id,
                "brand_id": method.get("brand_id", ""),
                "payment_method_id": pm_id,
            },
            idempotency_key=reference_id,
        )
    except stripe.error.CardError as e:
        return {
            "success": False,
            "error": f"card_declined:{e.code}",
            "decline_code": getattr(e, "decline_code", None),
        }
    except stripe.error.RateLimitError:
        return {"success": False, "error": "rate_limited"}
    except stripe.error.StripeError as e:
        logger.exception("Stripe error: %s", e)
        return {"success": False, "error": str(e)[:200]}

    if intent.status != "succeeded":
        return {"success": False, "error": f"intent_status_{intent.status}"}

    # Best-effort fee extraction — depends on Stripe expand options.
    gateway_fee = 0
    try:
        charges = getattr(intent, "charges", None)
        if charges and charges.data:
            bt = charges.data[0].balance_transaction
            if bt is not None:
                gateway_fee = int(getattr(bt, "fee", 0) or 0)
    except Exception:  # noqa: BLE001 — fee is informational
        gateway_fee = 0

    if gateway_fee == 0:
        # Fallback estimate so callers always see a fee figure.
        gateway_fee = (amount_cents * GATEWAY_FEE_BPS) // 10_000 + GATEWAY_FEE_FIXED_CENTS

    return {
        "success": True,
        "gateway_tx_id": intent.id,
        "gateway_fee_cents": gateway_fee,
        "currency": currency,
        "reference_id": reference_id,
    }


async def _create_stripe_customer(
    brand_id: str, holder_name: str, email: str | None = None
) -> str:
    """Create a Stripe Customer for the brand (or return a simulated id in dev)."""
    if not _stripe_is_live():
        return f"cus_sim_{brand_id}"
    customer = stripe.Customer.create(
        name=holder_name,
        email=email,
        metadata={"brand_id": brand_id},
    )
    return customer.id


async def _attach_stripe_payment_method(
    stripe_pm_id: str, stripe_customer_id: str
) -> None:
    """Attach a payment method to a customer (no-op in simulated mode)."""
    if not _stripe_is_live():
        return
    try:
        stripe.PaymentMethod.attach(stripe_pm_id, customer=stripe_customer_id)
    except stripe.error.InvalidRequestError as e:
        # Already attached / re-attach is fine — surface anything else.
        msg = str(e).lower()
        if "already" in msg or "attached" in msg:
            return
        raise


async def _get_or_create_brand_stripe_customer(
    brand_id: str,
    holder_name: str,
    email: str | None,
    r: aioredis.Redis,
) -> str:
    """Look up cached brand → Stripe customer id; create on miss.

    Cache key: ``brand:{brand_id}:stripe_customer_id``. We persist it on the
    brand so subsequent payment methods reuse the same Stripe customer (one
    customer per brand is the right granularity for billing).
    """
    cache_key = f"brand:{brand_id}:stripe_customer_id"
    cached = await r.get(cache_key)
    if cached:
        return cached
    customer_id = await _create_stripe_customer(brand_id, holder_name, email)
    await r.set(cache_key, customer_id)
    return customer_id


async def _gateway_auth_reverse(
    pm_id: str, amount_cents: int, r: aioredis.Redis
) -> dict[str, Any]:
    """Stub: place a small auth hold + immediately reverse it to validate
    the payment instrument without actually billing the customer.

    Production: gateway.PaymentIntents.create(amount, capture_method="manual")
    then .cancel(). MVP: simulated success.
    """
    method = await r.hgetall(_k_pm(pm_id))
    if not method:
        return {"verified": False, "error": "method_not_found"}
    if method.get("status") != "active":
        return {"verified": False, "error": "method_inactive"}

    return {
        "verified": True,
        "gateway_auth_id": f"sim_auth_{uuid4().hex}",
        "reversed": True,
        "amount_cents": amount_cents,
    }


def _project_response(pm_id: str, raw: dict[str, Any]) -> PaymentMethodResponse:
    return PaymentMethodResponse(
        payment_method_id=pm_id,
        brand_id=raw.get("brand_id", ""),
        method_type=raw.get("method_type", ""),
        last4=raw.get("last4") or None,
        expiry_month=int(raw["expiry_month"]) if raw.get("expiry_month") else None,
        expiry_year=int(raw["expiry_year"]) if raw.get("expiry_year") else None,
        holder_name=raw.get("holder_name") or None,
        is_default=raw.get("is_default") == "1",
        status=raw.get("status", "inactive"),  # type: ignore[arg-type]
        verified=raw.get("verified") == "1",
        created_at=float(raw.get("created_at") or 0.0),
        updated_at=float(raw["updated_at"]) if raw.get("updated_at") else None,
    )


# ── POST /{brand_id}/add ─────────────────────────────────────────────────
@router.post("/{brand_id}/add", response_model=AddPaymentMethodResponse)
async def add_payment_method(
    brand_id: str,
    body: AddPaymentMethodRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> AddPaymentMethodResponse:
    """Register a payment method on file for the brand.

    Validates the gateway token against the anti-fraud index — same token
    already linked to *another* brand triggers 409 ``card_already_in_use``.
    The raw PAN never enters our system; we store the gateway token reference
    plus PCI-safe display fields (last4, expiry).
    """
    token_hash = _hash_token(body.payment_token)

    is_unique, conflicts = await check_payment_uniqueness(token_hash, brand_id, r)
    if not is_unique:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "card_already_in_use",
                "conflicting_brand_ids": conflicts,
                "hint": "this payment instrument is already on file for another brand",
            },
        )

    pm_id = f"pm_{uuid4().hex}"
    now = time.time()

    # ── Stripe customer + attach ────────────────────────────────────────
    # For card/debit methods we expect ``payment_token`` to be a Stripe
    # PaymentMethod id (``pm_…``) coming from Stripe.js / Elements. We tie
    # it to a Stripe Customer scoped to the brand so future off-session
    # charges work. For non-card method types (wechat/alipay/bank) we still
    # create a customer record so the brand has a uniform billing entity.
    stripe_customer_id = ""
    stripe_attach_error: str | None = None
    try:
        stripe_customer_id = await _get_or_create_brand_stripe_customer(
            brand_id, body.holder_name, body.holder_email, r
        )
        if body.method_type in ("credit_card", "debit_card"):
            await _attach_stripe_payment_method(
                body.payment_token, stripe_customer_id
            )
    except stripe.error.StripeError as exc:  # type: ignore[attr-defined]
        # Don't hard-fail registration in dev; capture for audit and continue.
        stripe_attach_error = str(exc)[:200]
        logger.warning(
            "Stripe customer/attach failed brand=%s pm=%s err=%s",
            brand_id, pm_id, stripe_attach_error,
        )
    except Exception as exc:  # noqa: BLE001
        stripe_attach_error = str(exc)[:200]
        logger.warning(
            "Stripe customer setup non-stripe failure brand=%s pm=%s: %s",
            brand_id, pm_id, exc,
        )

    mapping: dict[str, Any] = {
        "payment_method_id": pm_id,
        "brand_id": brand_id,
        "method_type": body.method_type,
        "payment_token": body.payment_token,  # Stripe pm_… or other gateway ref
        "payment_token_hash": token_hash,
        "stripe_customer_id": stripe_customer_id,
        "last4": body.last4 or "",
        "expiry_month": str(body.expiry_month) if body.expiry_month else "",
        "expiry_year": str(body.expiry_year) if body.expiry_year else "",
        "holder_name": body.holder_name,
        "holder_email": body.holder_email or "",
        "billing_address": json.dumps(body.billing_address or {}, ensure_ascii=False),
        "status": "active",
        "verified": "0",
        "is_default": "0",
        "created_at": now,
        "updated_at": now,
        "ip_address": body.ip_address or "",
        "device_fingerprint": body.device_fingerprint or "",
    }
    if stripe_attach_error:
        mapping["stripe_attach_error"] = stripe_attach_error
    await r.hset(_k_pm(pm_id), mapping=mapping)
    await r.sadd(_k_brand_pms(brand_id), pm_id)
    await link_payment_to_brand(brand_id, token_hash, r)

    # Auto-default logic: first method becomes default; subsequent methods
    # default only if caller asks.
    existing_default = await _get_default_pm_id(brand_id, r)
    becomes_default = body.is_default or existing_default is None
    if becomes_default:
        await _set_default_internal(brand_id, pm_id, r)

    await _audit(
        r,
        pm_id,
        "created",
        {
            "brand_id": brand_id,
            "method_type": body.method_type,
            "is_default": becomes_default,
        },
    )

    logger.info(
        "payment_method created brand=%s pm_id=%s type=%s default=%s",
        brand_id,
        pm_id,
        body.method_type,
        becomes_default,
    )

    return AddPaymentMethodResponse(
        payment_method_id=pm_id,
        status="active",
        verified=False,
        is_default=becomes_default,
    )


# ── GET /brand/{brand_id} ────────────────────────────────────────────────
@router.get("/brand/{brand_id}", response_model=list[PaymentMethodResponse])
async def list_payment_methods(
    brand_id: str,
    include_removed: bool = False,
    r: aioredis.Redis = Depends(get_redis),
) -> list[PaymentMethodResponse]:
    """List payment methods on file for a brand (PCI-safe masked view)."""
    pm_ids = await r.smembers(_k_brand_pms(brand_id))
    out: list[PaymentMethodResponse] = []
    for pm_id in sorted(pm_ids):
        raw = await _load_pm(pm_id, r)
        if not raw:
            continue
        if not include_removed and raw.get("status") == "removed":
            continue
        out.append(_project_response(pm_id, raw))
    # Default first, then newest first.
    out.sort(key=lambda p: (not p.is_default, -p.created_at))
    return out


# ── GET /{pm_id} ─────────────────────────────────────────────────────────
@router.get("/{pm_id}", response_model=PaymentMethodResponse)
async def get_payment_method(
    pm_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> PaymentMethodResponse:
    raw = await _load_pm(pm_id, r)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "payment_method_not_found", "payment_method_id": pm_id},
        )
    return _project_response(pm_id, raw)


# ── POST /{pm_id}/verify ─────────────────────────────────────────────────
@router.post("/{pm_id}/verify", response_model=VerifyResponse)
async def verify_payment_method(
    pm_id: str,
    body: VerifyRequest | None = None,
    r: aioredis.Redis = Depends(get_redis),
) -> VerifyResponse:
    """Trigger a small auth + reverse to confirm the payment instrument."""
    raw = await _load_pm(pm_id, r)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "payment_method_not_found"},
        )
    if raw.get("status") != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "method_not_active", "status": raw.get("status")},
        )

    amount = (body or VerifyRequest()).micro_charge_amount_cents
    result = await _gateway_auth_reverse(pm_id, amount, r)
    verified = bool(result.get("verified"))

    if verified:
        await r.hset(
            _k_pm(pm_id),
            mapping={
                "verified": "1",
                "verified_at": time.time(),
                "updated_at": time.time(),
            },
        )

    await _audit(
        r,
        pm_id,
        "verified" if verified else "verify_failed",
        {
            "amount_cents": amount,
            "gateway_auth_id": result.get("gateway_auth_id"),
            "error": result.get("error"),
        },
    )

    return VerifyResponse(
        payment_method_id=pm_id,
        verified=verified,
        gateway_auth_id=result.get("gateway_auth_id"),
        reversed=bool(result.get("reversed")),
    )


# ── POST /{pm_id}/set-default ────────────────────────────────────────────
async def _set_default_internal(
    brand_id: str, pm_id: str, r: aioredis.Redis
) -> None:
    """Mark `pm_id` as default for the brand; clear flag on the previous."""
    prev = await _get_default_pm_id(brand_id, r)
    if prev and prev != pm_id:
        await r.hset(
            _k_pm(prev), mapping={"is_default": "0", "updated_at": time.time()}
        )
    await r.set(_k_brand_default(brand_id), pm_id)
    await r.hset(
        _k_pm(pm_id), mapping={"is_default": "1", "updated_at": time.time()}
    )


@router.post("/{pm_id}/set-default", response_model=SetDefaultResponse)
async def set_default(
    pm_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> SetDefaultResponse:
    raw = await _load_pm(pm_id, r)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "payment_method_not_found"},
        )
    if raw.get("status") != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "method_not_active", "status": raw.get("status")},
        )
    brand_id = raw["brand_id"]
    await _set_default_internal(brand_id, pm_id, r)
    await _audit(r, pm_id, "set_default", {"brand_id": brand_id})
    return SetDefaultResponse(payment_method_id=pm_id, is_default=True)


# ── POST /{pm_id}/remove ─────────────────────────────────────────────────
async def _brand_has_active_subscription(
    brand_id: str, r: aioredis.Redis
) -> bool:
    """Best-effort check: does the brand have any active subscription?

    Looks at common subscription-state shapes used by adjacent routers
    (brand_subscriptions / subscriptions / commerce_loop). All probes are
    optional — a missing key just means "no active sub on that path".
    """
    candidates = [
        f"brand_subscription:{brand_id}:status",
        f"subscription:brand:{brand_id}:status",
        f"brand:{brand_id}:subscription:status",
    ]
    for k in candidates:
        v = await r.get(k)
        if v and v in ("active", "trialing", "past_due"):
            return True
    return False


@router.post("/{pm_id}/remove")
async def remove_payment_method(
    pm_id: str,
    body: RemoveRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Soft-delete a payment method.

    Refuses to remove when:
      - it's the brand's *only* active method, OR
      - the brand has an active subscription that depends on a default method
        (and this is the default).
    """
    raw = await _load_pm(pm_id, r)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "payment_method_not_found"},
        )
    if raw.get("status") == "removed":
        return {"ok": True, "payment_method_id": pm_id, "already_removed": True}

    brand_id = raw["brand_id"]

    # Count active methods for this brand.
    pm_ids = await r.smembers(_k_brand_pms(brand_id))
    active_count = 0
    for other_pm in pm_ids:
        if other_pm == pm_id:
            continue
        other = await _load_pm(other_pm, r)
        if other and other.get("status") == "active":
            active_count += 1

    if active_count == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "cannot_remove_only_method",
                "hint": "add another payment method before removing this one",
            },
        )

    is_default = raw.get("is_default") == "1"
    if is_default and await _brand_has_active_subscription(brand_id, r):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "cannot_remove_default_with_active_subscription",
                "hint": "set another method as default first",
            },
        )

    now = time.time()
    await r.hset(
        _k_pm(pm_id),
        mapping={
            "status": "removed",
            "removed_at": now,
            "removal_reason": body.reason,
            "is_default": "0",
            "updated_at": now,
        },
    )

    # If we removed the default, promote another active method.
    if is_default:
        await r.delete(_k_brand_default(brand_id))
        for other_pm in pm_ids:
            if other_pm == pm_id:
                continue
            other = await _load_pm(other_pm, r)
            if other and other.get("status") == "active":
                await _set_default_internal(brand_id, other_pm, r)
                break

    await _audit(r, pm_id, "removed", {"reason": body.reason})
    logger.info("payment_method removed brand=%s pm_id=%s", brand_id, pm_id)
    return {"ok": True, "payment_method_id": pm_id, "status": "removed"}


# ── POST /{brand_id}/charge ──────────────────────────────────────────────
@router.post("/{brand_id}/charge", response_model=ChargeResponse)
async def charge_payment_method(
    brand_id: str,
    body: ChargeRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> ChargeResponse:
    """Background-charge the brand's default (or specified) payment method.

    Used for:
      - Year-2 subscription renewals (auto-charge)
      - Ad-campaign top-ups when wallet balance is low
      - Any merchant-initiated transaction with on-file consent

    On success, optionally credits the brand wallet (``credit_wallet=true``).
    Idempotent on ``reference_id``.
    """
    # Idempotency check first — replays must short-circuit.
    pm_id_hint = body.payment_method_id or await _get_default_pm_id(brand_id, r)
    if not pm_id_hint:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "no_payment_method_on_file",
                "hint": "add a payment method before charging",
            },
        )

    idem_key = _k_charge_idem(pm_id_hint, body.reference_id)
    existing = await r.get(idem_key)
    if existing:
        cached = await r.hgetall(f"payment_method_charge:{existing}")
        if cached:
            return ChargeResponse(
                ok=cached.get("ok") == "1",
                charge_id=existing,
                gateway_tx_id=cached.get("gateway_tx_id") or None,
                amount_cents=int(cached.get("amount_cents") or 0),
                gateway_fee_cents=int(cached.get("gateway_fee_cents") or 0),
                reason=cached.get("reason") or None,
                idempotent=True,
                new_wallet_balance_cents=(
                    int(cached["new_wallet_balance_cents"])
                    if cached.get("new_wallet_balance_cents")
                    else None
                ),
            )

    raw = await _load_pm(pm_id_hint, r)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "payment_method_not_found"},
        )
    if raw.get("brand_id") != brand_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "brand_mismatch"},
        )
    if raw.get("status") != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "method_not_active", "status": raw.get("status")},
        )
    if raw.get("verified") != "1":
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "method_unverified",
                "hint": "call /verify before background charges",
            },
        )

    result = await _gateway_charge(
        pm_id_hint, body.amount_cents, body.currency, body.reference_id, r
    )

    charge_id = f"pmc_{uuid4().hex}"
    now = time.time()

    if not result.get("success"):
        await r.hset(
            f"payment_method_charge:{charge_id}",
            mapping={
                "charge_id": charge_id,
                "ok": "0",
                "brand_id": brand_id,
                "payment_method_id": pm_id_hint,
                "amount_cents": body.amount_cents,
                "currency": body.currency,
                "reason": result.get("error", "gateway_declined"),
                "reference_id": body.reference_id,
                "ts": now,
            },
        )
        await r.set(idem_key, charge_id, ex=86400)
        await _audit(
            r,
            pm_id_hint,
            "charge_failed",
            {
                "charge_id": charge_id,
                "amount_cents": body.amount_cents,
                "reason": result.get("error"),
                "reference_id": body.reference_id,
            },
        )
        return ChargeResponse(
            ok=False,
            charge_id=charge_id,
            amount_cents=body.amount_cents,
            reason=result.get("error", "gateway_declined"),
        )

    gateway_tx_id = result["gateway_tx_id"]
    gateway_fee = int(result.get("gateway_fee_cents", 0))

    # Optionally credit the brand wallet.
    new_wallet_balance: int | None = None
    if body.credit_wallet:
        try:
            from app.routers.wallet import _k_balance  # local import to avoid cycles
            new_wallet_balance = int(
                await r.incrby(_k_balance(brand_id), body.amount_cents)
            )
            await r.rpush(
                f"wallet:{brand_id}:transactions", charge_id
            )
            await r.ltrim(f"wallet:{brand_id}:transactions", -10_000, -1)
        except Exception as exc:
            logger.warning(
                "wallet credit failed brand=%s charge=%s: %s",
                brand_id,
                charge_id,
                exc,
            )

    await r.hset(
        f"payment_method_charge:{charge_id}",
        mapping={
            "charge_id": charge_id,
            "ok": "1",
            "brand_id": brand_id,
            "payment_method_id": pm_id_hint,
            "amount_cents": body.amount_cents,
            "gateway_fee_cents": gateway_fee,
            "currency": body.currency,
            "reason_label": body.reason,
            "gateway_tx_id": gateway_tx_id,
            "reference_id": body.reference_id,
            "ts": now,
            "new_wallet_balance_cents": (
                str(new_wallet_balance) if new_wallet_balance is not None else ""
            ),
        },
    )
    await r.set(idem_key, charge_id, ex=86400)
    await _audit(
        r,
        pm_id_hint,
        "charged",
        {
            "charge_id": charge_id,
            "amount_cents": body.amount_cents,
            "gateway_tx_id": gateway_tx_id,
            "reference_id": body.reference_id,
            "credited_wallet": body.credit_wallet,
        },
    )

    logger.info(
        "payment_method charged brand=%s pm_id=%s amount=%s gateway_tx=%s",
        brand_id,
        pm_id_hint,
        body.amount_cents,
        gateway_tx_id,
    )

    return ChargeResponse(
        ok=True,
        charge_id=charge_id,
        gateway_tx_id=gateway_tx_id,
        amount_cents=body.amount_cents,
        gateway_fee_cents=gateway_fee,
        new_wallet_balance_cents=new_wallet_balance,
    )


# ── POST /anti-fraud-check ───────────────────────────────────────────────
@router.post("/anti-fraud-check", response_model=AntiFraudResponse)
async def anti_fraud_check(
    body: AntiFraudRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> AntiFraudResponse:
    """Check whether a payment token is already linked to other brands.

    Called at brand registration to prevent "one card = N free accounts".
    Either pass ``payment_token_hash`` (recommended — never sends raw token
    over the wire) or ``payment_token`` (server hashes it).
    """
    if not body.payment_token_hash and not body.payment_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_token",
                "hint": "supply either payment_token_hash or payment_token",
            },
        )

    token_hash = body.payment_token_hash or _hash_token(body.payment_token or "")
    is_unique, conflicts = await check_payment_uniqueness(token_hash, body.brand_id, r)

    # Audit fraud-check attempts (anonymous — keyed by token hash, not brand).
    try:
        await r.rpush(
            f"anti_fraud_check:{token_hash}",
            json.dumps(
                {
                    "brand_id": body.brand_id,
                    "ip_address": body.ip_address,
                    "device_fingerprint": body.device_fingerprint,
                    "is_unique": is_unique,
                    "ts": time.time(),
                },
                ensure_ascii=False,
            ),
        )
        await r.ltrim(f"anti_fraud_check:{token_hash}", -100, -1)
    except Exception as exc:
        logger.warning("anti_fraud_check audit failed: %s", exc)

    return AntiFraudResponse(
        is_unique=is_unique,
        conflicting_brand_ids=conflicts,
        token_hash=token_hash,
    )


# ── Stripe live flow: SetupIntent + REST aliases ─────────────────────────
# Replaces the legacy "POST payment_token directly" path for new clients.
# The mobile/web client:
#   1. POST /{brand_id}/add-setup-intent          → {client_secret}
#   2. Stripe Elements collects card with the client_secret
#   3. On success, Stripe fires setup_intent.succeeded → webhook attaches PM
#
# The existing /{brand_id}/add endpoint stays for callers that already hold
# a Stripe PaymentMethod id (server-to-server flow).

class SetupIntentResponse(BaseModel):
    brand_id: str
    setup_intent_id: str
    client_secret: str
    customer_id: str | None = None
    mode: str  # live/test/mock


@router.post(
    "/{brand_id}/add-setup-intent",
    response_model=SetupIntentResponse,
)
async def create_payment_method_setup_intent(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> SetupIntentResponse:
    """Issue a SetupIntent so the client can collect card details safely.

    Use this for the modern Stripe Elements flow. The legacy ``/add``
    endpoint (which accepts a pre-tokenised payment_token) is preserved
    for server-side integrations.
    """
    from app.services import stripe_live

    # Reuse / create the brand's Stripe Customer so future off-session
    # charges work. In mock mode this returns a sentinel string.
    customer_id: str | None = None
    try:
        customer_id = await _get_or_create_brand_stripe_customer(
            brand_id, brand_id, None, r
        )
    except Exception as exc:  # noqa: BLE001 — best effort
        logger.warning(
            "setup_intent: customer ensure failed brand=%s: %s", brand_id, exc
        )

    try:
        si = stripe_live.create_payment_method_setup_intent(
            brand_id, customer_id=customer_id
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("setup_intent create failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "setup_intent_failed", "hint": str(exc)[:200]},
        ) from exc

    return SetupIntentResponse(
        brand_id=brand_id,
        setup_intent_id=si["setup_intent_id"],
        client_secret=si["client_secret"],
        customer_id=customer_id,
        mode=si.get("mode") or stripe_live.get_mode(),
    )


@router.delete("/{pm_id}")
async def delete_payment_method(
    pm_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """DELETE alias for ``/{pm_id}/remove`` — also detaches at Stripe."""
    from app.services import stripe_live

    raw = await _load_pm(pm_id, r)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "payment_method_not_found"},
        )

    # Soft-delete locally via the existing path (handles defaults +
    # subscription guards). We synthesise a minimal RemoveRequest.
    body = RemoveRequest(reason="deleted_via_rest")
    result = await remove_payment_method(pm_id, body, r)  # type: ignore[arg-type]

    # Best-effort: detach at Stripe. Failure here doesn't undo the local
    # remove — the audit log captures both legs.
    stripe_pm_id = raw.get("payment_token") or ""
    detach_info: dict[str, Any] = {"attempted": False}
    if stripe_pm_id.startswith("pm_"):
        try:
            detach_info = stripe_live.detach_payment_method(stripe_pm_id)
            detach_info["attempted"] = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "stripe detach failed pm=%s: %s", stripe_pm_id, exc
            )
            detach_info = {"attempted": True, "error": str(exc)[:200]}
        await _audit(r, pm_id, "stripe_detached", detach_info)

    result["stripe_detach"] = detach_info
    return result


class SetDefaultPutRequest(BaseModel):
    brand_id: str | None = None  # informational; resolved from pm_id


@router.put("/{pm_id}/set-default", response_model=SetDefaultResponse)
async def set_default_put(
    pm_id: str,
    body: SetDefaultPutRequest | None = None,
    r: aioredis.Redis = Depends(get_redis),
) -> SetDefaultResponse:
    """PUT form of set-default — also pushes the default to Stripe Customer."""
    from app.services import stripe_live

    raw = await _load_pm(pm_id, r)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "payment_method_not_found"},
        )
    if raw.get("status") != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "method_not_active", "status": raw.get("status")},
        )
    brand_id = raw["brand_id"]
    await _set_default_internal(brand_id, pm_id, r)
    await _audit(r, pm_id, "set_default", {"brand_id": brand_id, "via": "PUT"})

    stripe_customer_id = raw.get("stripe_customer_id") or ""
    stripe_pm_id = raw.get("payment_token") or ""
    if stripe_customer_id and stripe_pm_id.startswith("pm_"):
        try:
            stripe_live.set_default_payment_method(stripe_customer_id, stripe_pm_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "stripe set_default failed pm=%s cust=%s: %s",
                stripe_pm_id, stripe_customer_id, exc,
            )

    return SetDefaultResponse(payment_method_id=pm_id, is_default=True)


# ── GET /{pm_id}/audit ───────────────────────────────────────────────────
@router.get("/{pm_id}/audit")
async def get_audit_trail(
    pm_id: str,
    limit: int = 100,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Audit trail for a payment method (created/verified/charged/removed/...)."""
    raw = await _load_pm(pm_id, r)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "payment_method_not_found"},
        )
    limit = max(1, min(limit, AUDIT_MAX))
    entries = await r.lrange(_k_audit(pm_id), -limit, -1)
    parsed: list[dict[str, Any]] = []
    for raw_entry in entries:
        try:
            parsed.append(json.loads(raw_entry))
        except (ValueError, TypeError):
            continue
    parsed.reverse()  # newest first
    return {"payment_method_id": pm_id, "events": parsed}
