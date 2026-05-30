"""Enterprise Portal — Settings & Account Switcher endpoints.

This router serves the **Settings view** of the new portal UI and the
multi-brand account switcher. Like ``portal_api.py``, it is a
read-mostly composition layer on top of existing routers (wallet,
payment_methods, invoices, portal_auth, brands).

Endpoints
---------
- ``GET  /api/v1/portal/settings/profile/{bid}`` — brand profile
- ``PUT  /api/v1/portal/settings/profile/{bid}`` — update profile
- ``GET  /api/v1/portal/settings/billing/{bid}`` — billing summary + invoices
- ``GET  /api/v1/portal/settings/payment-methods/{bid}`` — saved PMs + default
- ``PUT  /api/v1/portal/settings/payment-methods/{bid}/auto-recharge`` —
  update auto-recharge threshold + topup amount
- ``GET  /api/v1/portal/settings/team/{bid}`` — staff roster + roles
- ``POST /api/v1/portal/settings/team/{bid}/invite`` — invite staff
- ``GET  /api/v1/portal/settings/notifications/{bid}`` — notification prefs
- ``PUT  /api/v1/portal/settings/notifications/{bid}`` — update prefs
- ``GET  /api/v1/portal/settings/integrations/{bid}`` — connected integrations
- ``GET  /api/v1/portal/settings/security/{bid}`` — 2FA / sessions / last-login
- ``GET  /api/v1/portal/settings/demo-mode/{bid}`` — demo data toggle
- ``PUT  /api/v1/portal/settings/demo-mode/{bid}`` — flip demo-mode flag
- ``GET  /api/v1/portal/accounts/me`` — brands the current user owns

Auth model
----------
Per spec we mirror the brand-translations router pattern:
* ``Authorization: Bearer <JWT>`` issued by ``/api/v1/portal/auth/login``
  (decoded payload's ``brand_id`` claim must match the path's ``bid``).
* OR ``X-Owner-Id: <bid>`` for service-to-service / local dev parity.

This router never mutates other routers' data; it reads their canonical
keys (``wallet:{bid}:*``, ``brand:{bid}:payment_methods``, etc.) and
writes only into namespaces it owns (``brand:{bid}:profile``,
``brand:{bid}:team``, ``brand:{bid}:notif_prefs``, ``brand:{bid}:demo``).

Locale-aware shape
------------------
- Money: ``{value_cents, currency, formatted_display}``
- Time:  ``{epoch_seconds, iso8601, formatted_display}``
- Status: ``{value, display_label_i18n_key, display_label}``
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt, JWTError
from pydantic import BaseModel, Field, field_validator

from app.api_standards import error_response, list_response, not_found
from app.config import settings
from app.i18n import t as i18n_t
from app.i18n.context import get_current_locale
from app.i18n.formatting import format_currency, format_date, format_datetime
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()

# Bearer scheme is optional so endpoints can also accept ``X-Owner-Id``.
_bearer = HTTPBearer(auto_error=False)


# ── Constants ────────────────────────────────────────────────────────────

PROFILE_KEY = "brand:{bid}:profile"
TEAM_KEY = "brand:{bid}:team"  # HASH: member_id -> json
TEAM_INVITES_KEY = "brand:{bid}:team_invites"  # HASH: invite_id -> json
NOTIF_PREFS_KEY = "brand:{bid}:notif_prefs"
INTEGRATIONS_KEY = "brand:{bid}:integrations"
DEMO_FLAG_KEY = "brand:{bid}:demo_enabled"
SECURITY_KEY = "brand:{bid}:security"
LAST_LOGIN_KEY = "brand:{bid}:last_login"
SESSION_LIST_KEY = "brand:{bid}:sessions"  # LIST of JSON
OWNED_BRANDS_KEY = "user:{email}:owned_brands"  # SET of brand_ids
DEFAULT_CURRENCY_FALLBACK = "CNY"

# Notification categories used in the UI toggles.
_NOTIF_CATEGORIES: tuple[str, ...] = (
    "billing",
    "campaigns",
    "audiences",
    "wallet_low",
    "disputes",
    "security",
    "product_updates",
)
_NOTIF_CHANNELS: tuple[str, ...] = ("email", "sms", "push")

_ROLES = frozenset({"Admin", "Editor", "Viewer"})

import re as _re

_EMAIL_RE = _re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email(v: str | None) -> str | None:
    if v is None:
        return None
    if not _EMAIL_RE.match(v):
        raise ValueError("invalid email")
    return v

# Integrations we currently surface in the Settings UI. The "connected"
# flag is read from a per-brand hash so connections survive deploys.
_INTEGRATION_REGISTRY: tuple[dict[str, str], ...] = (
    {"key": "stripe", "name": "Stripe", "category": "payment"},
    {"key": "square", "name": "Square", "category": "payment"},
    {"key": "shopify", "name": "Shopify", "category": "ecommerce"},
    {"key": "grabpay", "name": "GrabPay", "category": "payment"},
    {"key": "ovo", "name": "OVO", "category": "payment"},
    {"key": "meta", "name": "Meta Ads", "category": "ads"},
    {"key": "google", "name": "Google Ads", "category": "ads"},
    {"key": "tiktok", "name": "TikTok Ads", "category": "ads"},
)


# ── Auth helpers ─────────────────────────────────────────────────────────


def _authorise(
    brand_id: str,
    credentials: HTTPAuthorizationCredentials | None,
    x_owner_id: str | None,
) -> str:
    """Resolve the actor for a brand-scoped request.

    Accepts either an Authorization: Bearer JWT (must encode the same
    ``brand_id`` we're targeting OR have ``brand_id="all"`` for admins)
    or an ``X-Owner-Id`` header matching the brand id.

    Returns the actor identity string for audit logging. Raises 401 on
    missing/invalid auth, 403 when the auth doesn't match the brand.
    """
    if x_owner_id and x_owner_id == brand_id:
        return f"owner:{brand_id}"
    if credentials and credentials.credentials:
        try:
            payload = jwt.decode(
                credentials.credentials,
                settings.jwt_secret,
                algorithms=[settings.jwt_algorithm],
            )
        except JWTError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "invalid_token", "reason": str(exc)},
            ) from exc
        token_bid = payload.get("brand_id")
        if token_bid in (brand_id, "all"):
            return f"jwt:{payload.get('sub', 'unknown')}"
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "forbidden", "reason": "brand_id_mismatch"},
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "auth_required",
                "reason": "Authorization Bearer or X-Owner-Id required"},
    )


def _resolve_actor_email(
    credentials: HTTPAuthorizationCredentials | None,
    x_owner_id: str | None,
) -> str | None:
    """Best-effort lookup of the actor email for /accounts/me."""
    if credentials and credentials.credentials:
        try:
            payload = jwt.decode(
                credentials.credentials,
                settings.jwt_secret,
                algorithms=[settings.jwt_algorithm],
            )
            return payload.get("sub")
        except JWTError:
            return None
    return x_owner_id  # caller passes their email-shaped id


# ── Locale-formatting helpers (mirror portal_api.py) ─────────────────────


def _now() -> float:
    return time.time()


def _money(amount_cents: int, currency: str) -> dict[str, Any]:
    locale = get_current_locale()
    try:
        formatted = format_currency(int(amount_cents or 0), currency, locale)
    except Exception:
        formatted = f"{currency} {(amount_cents or 0) / 100:.2f}"
    return {
        "value_cents": int(amount_cents or 0),
        "currency": currency,
        "formatted_display": formatted,
    }


def _ts(epoch: float | int | None) -> dict[str, Any] | None:
    if not epoch:
        return None
    try:
        dt = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None
    locale = get_current_locale()
    return {
        "epoch_seconds": int(float(epoch)),
        "iso8601": dt.isoformat(),
        "formatted_display": format_datetime(dt, locale=locale),
    }


def _date_label(day: str) -> dict[str, Any]:
    try:
        dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return {"iso8601": day, "formatted_display": day}
    return {
        "iso8601": day,
        "formatted_display": format_date(dt, locale=get_current_locale()),
    }


def _status_badge(state: str, table: dict[str, str]) -> dict[str, str]:
    key = table.get(state, f"status.{state}")
    return {
        "value": state,
        "display_label_i18n_key": key,
        "display_label": i18n_t(key, locale=get_current_locale()),
    }


async def _read_currency(r: aioredis.Redis, brand_id: str) -> str:
    cur = await r.get(f"wallet:{brand_id}:currency")
    return (cur or DEFAULT_CURRENCY_FALLBACK).upper()


# ── Pydantic request bodies ──────────────────────────────────────────────


class ProfileUpdate(BaseModel):
    brand_name: str | None = Field(default=None, max_length=200)
    logo_url: str | None = Field(default=None, max_length=2048)
    contact_email: str | None = Field(default=None, max_length=320)
    contact_phone: str | None = Field(default=None, max_length=64)

    @field_validator("contact_email")
    @classmethod
    def _check_email(cls, v: str | None) -> str | None:
        return _validate_email(v)

    tax_id: str | None = Field(default=None, max_length=64)
    business_type: str | None = Field(
        default=None,
        description="One of: individual, company, non_profit, government",
        max_length=64,
    )
    address_line1: str | None = Field(default=None, max_length=255)
    address_line2: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=128)
    state: str | None = Field(default=None, max_length=128)
    postal_code: str | None = Field(default=None, max_length=32)
    country: str | None = Field(default=None, max_length=4)
    website_url: str | None = Field(default=None, max_length=2048)


class NotificationPrefsUpdate(BaseModel):
    """Map of ``{category: {channel: bool}}``.

    Categories: billing, campaigns, audiences, wallet_low, disputes,
    security, product_updates. Channels: email, sms, push.
    Unknown keys are silently dropped (forward-compatible).
    """

    preferences: dict[str, dict[str, bool]] = Field(default_factory=dict)


class TeamInviteRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    role: str = Field(..., description="Admin | Editor | Viewer")
    message: str | None = Field(default=None, max_length=1024)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        if not _EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v


class AutoRechargeUpdate(BaseModel):
    enabled: bool = True
    threshold_cents: int = Field(50_000, ge=0)
    recharge_amount_cents: int = Field(500_000, gt=0)
    payment_method_id: str | None = None


class DemoModeUpdate(BaseModel):
    demo_enabled: bool


# ── 1. Profile ───────────────────────────────────────────────────────────


_DEFAULT_PROFILE = {
    "brand_name": "",
    "logo_url": "",
    "contact_email": "",
    "contact_phone": "",
    "tax_id": "",
    "business_type": "company",
    "address_line1": "",
    "address_line2": "",
    "city": "",
    "state": "",
    "postal_code": "",
    "country": "SG",
    "website_url": "",
}


@router.get(
    "/settings/profile/{bid}",
    tags=["portal-settings"],
    summary="Brand profile (logo / contact / tax ID / address)",
    responses={
        200: {
            "description": "Brand profile fields",
            "content": {
                "application/json": {
                    "example": {
                        "brand_id": "b_demo",
                        "profile": {
                            "brand_name": "Demo Cafe",
                            "logo_url": "https://cdn.kix.app/b_demo/logo.png",
                            "contact_email": "ops@demo.example",
                            "tax_id": "201912345R",
                            "business_type": "company",
                            "country": "SG",
                        },
                        "updated_at": None,
                    }
                }
            },
        }
    },
)
async def get_profile(
    bid: str,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Read the merchant's profile fields. Returns defaults on first read."""
    _authorise(bid, credentials, x_owner_id)
    raw = await r.hgetall(PROFILE_KEY.format(bid=bid))
    profile = dict(_DEFAULT_PROFILE)
    profile.update({k: v for k, v in raw.items() if k != "updated_at"})
    updated_at = raw.get("updated_at")
    return {
        "brand_id": bid,
        "profile": profile,
        "updated_at": _ts(float(updated_at)) if updated_at else None,
    }


@router.put(
    "/settings/profile/{bid}",
    tags=["portal-settings"],
    summary="Update brand profile fields",
)
async def put_profile(
    bid: str,
    body: ProfileUpdate,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Patch the profile. Empty / unset fields are left unchanged."""
    actor = _authorise(bid, credentials, x_owner_id)
    update: dict[str, str] = {}
    for k, v in body.model_dump(exclude_none=True).items():
        update[k] = str(v)
    if not update:
        raise error_response(422, "empty_update", "no profile fields supplied")
    update["updated_at"] = str(_now())
    update["updated_by"] = actor
    await r.hset(PROFILE_KEY.format(bid=bid), mapping=update)
    return await get_profile(bid, credentials, x_owner_id, r)


# ── 2. Billing summary + invoices list ──────────────────────────────────


_INVOICE_STATUS_I18N: dict[str, str] = {
    "draft": "status.invoice_draft",
    "open": "status.invoice_open",
    "paid": "status.invoice_paid",
    "void": "status.invoice_void",
    "uncollectible": "status.invoice_uncollectible",
}


def _ladder_12mo_cutoff() -> float:
    return (datetime.now(timezone.utc) - timedelta(days=365)).timestamp()


def _serialise_invoice_row(
    inv_id: str, raw: dict[str, str], currency: str
) -> dict[str, Any]:
    return {
        "invoice_id": inv_id,
        "number": raw.get("number"),
        "status": _status_badge(raw.get("status", "draft"), _INVOICE_STATUS_I18N),
        "total": _money(int(raw.get("total_cents", 0) or 0), currency),
        "amount_due": _money(int(raw.get("amount_due_cents", 0) or 0), currency),
        "amount_paid": _money(int(raw.get("amount_paid_cents", 0) or 0), currency),
        "due_date": _ts(float(raw.get("due_date_ts", 0) or 0)),
        "finalized_at": _ts(float(raw.get("finalized_at", 0) or 0)),
        "paid_at": _ts(float(raw.get("paid_at", 0) or 0)),
        "created_at": _ts(float(raw.get("created_at", 0) or 0)),
        "hosted_invoice_url": raw.get("hosted_invoice_url") or None,
        "invoice_pdf_url": raw.get("invoice_pdf_url") or None,
    }


@router.get(
    "/settings/billing/{bid}",
    tags=["portal-settings"],
    summary="Billing summary + last 12 months of invoices",
    responses={
        200: {
            "description": "Wallet balance + paginated invoice list",
            "content": {
                "application/json": {
                    "example": {
                        "brand_id": "b_demo",
                        "currency": "SGD",
                        "current_balance": {
                            "value_cents": 250_000,
                            "currency": "SGD",
                            "formatted_display": "S$2,500.00",
                        },
                        "next_invoice_date": None,
                        "last_invoice": None,
                        "invoices": [],
                        "count": 0,
                    }
                }
            },
        }
    },
)
async def get_billing(
    bid: str,
    limit: int = 50,
    offset: int = 0,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Billing summary: balance + next invoice + last 12 mo invoice list."""
    _authorise(bid, credentials, x_owner_id)
    if limit < 1 or limit > 200:
        raise error_response(422, "invalid_limit", "limit must be 1..200")

    currency = await _read_currency(r, bid)
    balance = int(await r.get(f"wallet:{bid}:balance") or 0)

    # Demo-mode short-circuit: render an empty billing block.
    if await _is_demo_disabled(r, bid):
        return {
            "brand_id": bid,
            "currency": currency,
            "current_balance": _money(balance, currency),
            "next_invoice_date": None,
            "last_invoice": None,
            "invoices": list_response(items=[], total=0, limit=limit, offset=offset),
            "demo_mode_disabled": True,
        }

    # Invoices: brand-id is treated as customer_id in the existing
    # invoices router, so we reuse ``customer:{cus_id}:invoices`` ZSET.
    inv_zset_key = f"customer:{bid}:invoices"
    cutoff = _ladder_12mo_cutoff()
    # Bounded fetch — we filter by 12mo in memory.
    all_ids = await r.zrevrangebyscore(
        inv_zset_key, "+inf", cutoff, start=0, num=500
    )
    rows: list[dict[str, Any]] = []
    for inv_id in all_ids:
        raw = await r.hgetall(f"invoice:{inv_id}")
        if not raw:
            continue
        rows.append(_serialise_invoice_row(inv_id, raw, currency))

    next_inv_ts = await r.get(f"brand:{bid}:next_invoice_at")
    last_invoice = rows[0] if rows else None

    return {
        "brand_id": bid,
        "currency": currency,
        "current_balance": _money(balance, currency),
        "next_invoice_date": _ts(float(next_inv_ts)) if next_inv_ts else None,
        "last_invoice": last_invoice,
        "invoices": list_response(
            items=rows[offset:offset + limit],
            total=len(rows),
            limit=limit,
            offset=offset,
        ),
    }


# ── 3. Payment methods (read-only listing) + auto-recharge update ────────


_PM_STATUS_I18N: dict[str, str] = {
    "active": "status.pm_active",
    "pending_verification": "status.pm_pending",
    "removed": "status.pm_removed",
    "expired": "status.pm_expired",
}


@router.get(
    "/settings/payment-methods/{bid}",
    tags=["portal-settings"],
    summary="List saved payment methods + default + auto-recharge config",
    responses={
        200: {
            "description": "Saved PMs and auto-recharge",
            "content": {
                "application/json": {
                    "example": {
                        "brand_id": "b_demo",
                        "payment_methods": [],
                        "default_payment_method_id": None,
                        "auto_recharge": {
                            "enabled": False,
                            "threshold": {"value_cents": 0, "currency": "SGD",
                                          "formatted_display": "S$0.00"},
                            "topup_amount": {"value_cents": 0, "currency": "SGD",
                                             "formatted_display": "S$0.00"},
                            "payment_method_id": None,
                        },
                    }
                }
            },
        }
    },
)
async def get_payment_methods(
    bid: str,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """List saved PMs (masked) + the default + auto-recharge config.

    Mirrors the on-disk layout used by the payment_methods router.
    """
    _authorise(bid, credentials, x_owner_id)
    currency = await _read_currency(r, bid)

    pm_ids = await r.smembers(f"brand:{bid}:payment_methods")
    default_id = await r.get(f"brand:{bid}:payment_method:default")

    items: list[dict[str, Any]] = []
    for pm_id in pm_ids:
        raw = await r.hgetall(f"payment_method:{pm_id}")
        if not raw:
            continue
        items.append({
            "payment_method_id": pm_id,
            "type": raw.get("type", "card"),
            "brand": raw.get("brand"),
            "last4": raw.get("last4"),
            "exp_month": raw.get("exp_month"),
            "exp_year": raw.get("exp_year"),
            "holder_name": raw.get("holder_name"),
            "status": _status_badge(
                raw.get("state", "active"), _PM_STATUS_I18N
            ),
            "is_default": pm_id == default_id,
            "created_at": _ts(float(raw.get("created_at", 0) or 0)),
        })

    # Auto-recharge config
    ar = await r.hgetall(f"wallet:{bid}:auto_recharge")
    auto_recharge = {
        "enabled": ar.get("enabled", "0") == "1",
        "threshold": _money(int(ar.get("threshold_cents", 0) or 0), currency),
        "topup_amount": _money(
            int(ar.get("recharge_amount_cents", 0) or 0), currency
        ),
        "payment_method_id": ar.get("payment_method_id") or ar.get("payment_token") or None,
    }

    return {
        "brand_id": bid,
        "currency": currency,
        "payment_methods": items,
        "default_payment_method_id": default_id,
        "auto_recharge": auto_recharge,
    }


@router.put(
    "/settings/payment-methods/{bid}/auto-recharge",
    tags=["portal-settings"],
    summary="Update wallet auto-recharge (threshold + topup amount)",
)
async def put_auto_recharge(
    bid: str,
    body: AutoRechargeUpdate,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Persist the auto-recharge config under ``wallet:{bid}:auto_recharge``.

    Mirrors the on-disk schema the wallet router reads via
    :func:`_get_auto_recharge_config`.
    """
    _authorise(bid, credentials, x_owner_id)
    mapping = {
        "enabled": "1" if body.enabled else "0",
        "threshold_cents": str(body.threshold_cents),
        "recharge_amount_cents": str(body.recharge_amount_cents),
    }
    if body.payment_method_id:
        mapping["payment_method_id"] = body.payment_method_id
    await r.hset(f"wallet:{bid}:auto_recharge", mapping=mapping)
    return await get_payment_methods(bid, credentials, x_owner_id, r)


# ── 4. Team & invites ────────────────────────────────────────────────────


def _team_owner_seed(bid: str) -> dict[str, Any]:
    """Synthesise the implicit owner entry if no team has been set up."""
    return {
        "member_id": f"owner:{bid}",
        "email": "",
        "role": "Admin",
        "status": "active",
        "joined_at": None,
        "is_implicit_owner": True,
    }


@router.get(
    "/settings/team/{bid}",
    tags=["portal-settings"],
    summary="List staff members + roles",
)
async def get_team(
    bid: str,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Roster of staff for a brand. Includes pending invites."""
    _authorise(bid, credentials, x_owner_id)
    members_raw = await r.hgetall(TEAM_KEY.format(bid=bid))
    members: list[dict[str, Any]] = []
    for mid, payload in members_raw.items():
        try:
            entry = json.loads(payload)
        except (TypeError, ValueError):
            continue
        entry["member_id"] = mid
        entry["joined_at"] = _ts(entry.get("joined_at"))
        members.append(entry)

    invites_raw = await r.hgetall(TEAM_INVITES_KEY.format(bid=bid))
    invites: list[dict[str, Any]] = []
    for inv_id, payload in invites_raw.items():
        try:
            entry = json.loads(payload)
        except (TypeError, ValueError):
            continue
        entry["invite_id"] = inv_id
        entry["created_at"] = _ts(entry.get("created_at"))
        invites.append(entry)

    if not members:
        members.append(_team_owner_seed(bid))

    return {
        "brand_id": bid,
        "members": members,
        "pending_invites": invites,
        "roles": sorted(_ROLES),
    }


@router.post(
    "/settings/team/{bid}/invite",
    tags=["portal-settings"],
    status_code=status.HTTP_201_CREATED,
    summary="Invite a new staff member",
)
async def post_invite(
    bid: str,
    body: TeamInviteRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Create a pending invite. A real email send is out of scope here."""
    actor = _authorise(bid, credentials, x_owner_id)
    if body.role not in _ROLES:
        raise error_response(
            422, "invalid_role",
            f"role must be one of {sorted(_ROLES)}", requested=body.role,
        )
    invite_id = f"inv_{secrets.token_urlsafe(12)}"
    entry = {
        "invite_id": invite_id,
        "email": str(body.email),
        "role": body.role,
        "message": body.message or "",
        "status": "pending",
        "created_at": _now(),
        "invited_by": actor,
    }
    await r.hset(
        TEAM_INVITES_KEY.format(bid=bid), invite_id, json.dumps(entry)
    )
    return {"ok": True, "invite": {
        **entry, "created_at": _ts(entry["created_at"])
    }}


# ── 5. Notification preferences ──────────────────────────────────────────


def _default_notif_prefs() -> dict[str, dict[str, bool]]:
    """All categories on for email, off for sms/push (conservative)."""
    return {
        cat: {ch: (ch == "email") for ch in _NOTIF_CHANNELS}
        for cat in _NOTIF_CATEGORIES
    }


@router.get(
    "/settings/notifications/{bid}",
    tags=["portal-settings"],
    summary="Notification preferences (email/sms/push per category)",
)
async def get_notification_prefs(
    bid: str,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _authorise(bid, credentials, x_owner_id)
    raw = await r.get(NOTIF_PREFS_KEY.format(bid=bid))
    prefs: dict[str, dict[str, bool]] = _default_notif_prefs()
    if raw:
        try:
            stored = json.loads(raw)
            for cat in _NOTIF_CATEGORIES:
                if cat in stored and isinstance(stored[cat], dict):
                    for ch in _NOTIF_CHANNELS:
                        if ch in stored[cat]:
                            prefs[cat][ch] = bool(stored[cat][ch])
        except (TypeError, ValueError):
            pass
    return {
        "brand_id": bid,
        "categories": list(_NOTIF_CATEGORIES),
        "channels": list(_NOTIF_CHANNELS),
        "preferences": prefs,
    }


@router.put(
    "/settings/notifications/{bid}",
    tags=["portal-settings"],
    summary="Update notification preferences",
)
async def put_notification_prefs(
    bid: str,
    body: NotificationPrefsUpdate,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _authorise(bid, credentials, x_owner_id)
    # Filter to known categories/channels — forward-compatible drop-unknown.
    cleaned: dict[str, dict[str, bool]] = {}
    for cat, chmap in (body.preferences or {}).items():
        if cat not in _NOTIF_CATEGORIES:
            continue
        if not isinstance(chmap, dict):
            continue
        cleaned[cat] = {
            ch: bool(chmap[ch]) for ch in _NOTIF_CHANNELS if ch in chmap
        }
    await r.set(NOTIF_PREFS_KEY.format(bid=bid), json.dumps(cleaned))
    return await get_notification_prefs(bid, credentials, x_owner_id, r)


# ── 6. Integrations ──────────────────────────────────────────────────────


@router.get(
    "/settings/integrations/{bid}",
    tags=["portal-settings"],
    summary="Connected integrations (Stripe / Shopify / Meta / etc.)",
)
async def get_integrations(
    bid: str,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return registry of supported integrations + per-brand connection flags."""
    _authorise(bid, credentials, x_owner_id)
    state = await r.hgetall(INTEGRATIONS_KEY.format(bid=bid))
    items: list[dict[str, Any]] = []
    for entry in _INTEGRATION_REGISTRY:
        connected = state.get(entry["key"]) == "1"
        connected_at = state.get(f"{entry['key']}:at")
        items.append({
            "key": entry["key"],
            "name": entry["name"],
            "category": entry["category"],
            "connected": connected,
            "connected_at": _ts(float(connected_at)) if connected_at else None,
        })
    return {
        "brand_id": bid,
        "integrations": items,
        "count": len(items),
    }


# ── 7. Security overview ─────────────────────────────────────────────────


@router.get(
    "/settings/security/{bid}",
    tags=["portal-settings"],
    summary="Security: 2FA / last login / active sessions",
)
async def get_security(
    bid: str,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _authorise(bid, credentials, x_owner_id)
    sec = await r.hgetall(SECURITY_KEY.format(bid=bid))
    last_login = await r.get(LAST_LOGIN_KEY.format(bid=bid))
    raw_sessions = await r.lrange(SESSION_LIST_KEY.format(bid=bid), 0, 49)
    sessions: list[dict[str, Any]] = []
    for blob in raw_sessions:
        try:
            entry = json.loads(blob)
        except (TypeError, ValueError):
            continue
        entry["last_seen"] = _ts(entry.get("last_seen"))
        sessions.append(entry)
    return {
        "brand_id": bid,
        "two_factor_enabled": sec.get("2fa_enabled", "0") == "1",
        "two_factor_method": sec.get("2fa_method") or None,
        "last_login": _ts(float(last_login)) if last_login else None,
        "active_sessions": sessions,
        "session_count": len(sessions),
        "password_last_changed": _ts(
            float(sec["password_last_changed"])
        ) if sec.get("password_last_changed") else None,
    }


# ── 8. Demo-mode toggle (P0-10) ──────────────────────────────────────────


async def _is_demo_disabled(r: aioredis.Redis, bid: str) -> bool:
    """``True`` when sample data should be hidden for a brand.

    A brand is treated as "demo enabled" by default for new merchants
    so they see sample dashboards. Once the operator flips the toggle
    off, downstream endpoints (e.g. billing/invoices) should return
    empty arrays.
    """
    val = await r.get(DEMO_FLAG_KEY.format(bid=bid))
    if val is None:
        return False  # default: demo data visible
    return val == "0"


@router.get(
    "/settings/demo-mode/{bid}",
    tags=["portal-settings"],
    summary="Read the demo-data toggle for a brand",
)
async def get_demo_mode(
    bid: str,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _authorise(bid, credentials, x_owner_id)
    val = await r.get(DEMO_FLAG_KEY.format(bid=bid))
    enabled = (val is None) or (val == "1")
    return {"brand_id": bid, "demo_enabled": enabled}


@router.put(
    "/settings/demo-mode/{bid}",
    tags=["portal-settings"],
    summary="Toggle demo data visibility for a brand",
)
async def put_demo_mode(
    bid: str,
    body: DemoModeUpdate,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _authorise(bid, credentials, x_owner_id)
    await r.set(DEMO_FLAG_KEY.format(bid=bid), "1" if body.demo_enabled else "0")
    return {"brand_id": bid, "demo_enabled": body.demo_enabled}


# ── 9. Account switcher: brands the current user owns ───────────────────


@router.get(
    "/accounts/me",
    tags=["portal-settings"],
    summary="Brands the authenticated user can switch to",
    responses={
        200: {
            "description": "Owned brands + active brand_id",
            "content": {
                "application/json": {
                    "example": {
                        "email": "ops@demo.example",
                        "active_brand_id": "b_demo",
                        "brands": [
                            {"brand_id": "b_demo", "brand_name": "Demo Cafe",
                             "logo_url": "", "role": "Admin"},
                        ],
                        "count": 1,
                    }
                }
            },
        }
    },
)
async def get_accounts_me(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_owner_id: str | None = Header(None, alias="X-Owner-Id"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return all brands the caller owns + their active brand."""
    email = _resolve_actor_email(credentials, x_owner_id)
    active_bid: str | None = None
    if credentials and credentials.credentials:
        try:
            payload = jwt.decode(
                credentials.credentials,
                settings.jwt_secret,
                algorithms=[settings.jwt_algorithm],
            )
            active_bid = payload.get("brand_id")
        except JWTError:
            active_bid = None
    elif x_owner_id:
        active_bid = x_owner_id

    if not email and not active_bid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "auth_required"},
        )

    bids: set[str] = set()

    # Path 1: explicit user→brands set (multi-brand operators).
    if email:
        bids.update(
            await r.smembers(OWNED_BRANDS_KEY.format(email=email))
        )

    # Path 2: portal_operator hash records a single brand_id per email.
    if email:
        op_data = await r.hgetall(f"portal_operator:{email}")
        single = op_data.get("brand_id")
        if single and single != "all":
            bids.add(single)

    # Path 3: token-derived brand_id ensures at least one entry.
    if active_bid and active_bid != "all":
        bids.add(active_bid)

    brands: list[dict[str, Any]] = []
    for bid in sorted(bids):
        profile = await r.hgetall(PROFILE_KEY.format(bid=bid))
        brands.append({
            "brand_id": bid,
            "brand_name": profile.get("brand_name") or bid,
            "logo_url": profile.get("logo_url") or "",
            "role": "Admin",  # current user is the owner of bids we surface
            "is_active": bid == active_bid,
        })

    return {
        "email": email,
        "active_brand_id": active_bid,
        "brands": brands,
        "count": len(brands),
    }


__all__ = ["router"]
