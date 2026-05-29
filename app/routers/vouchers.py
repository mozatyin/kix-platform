"""Vouchers — cross-store voucher issue / redeem / transfer module.

This file plays two roles:

1.  **Legacy CSV pool** — the original brand-portal "upload a CSV of codes"
    workflow.  Routes are exposed under ``/api/v1/brands/{brand_id}/vouchers``
    via ``brand_pool_router`` (mounted by main.py as ``vouchers.router``).
2.  **Cross-store voucher instances** — the new user-held voucher
    lifecycle (issue → claim → redeem → transfer → void → expire).  These
    routes are exposed under ``/api/v1/vouchers`` via the main
    ``router`` and also accept brand-scoped convenience issue requests
    under ``/api/v1/brands/{brand_id}/vouchers/issue``.

The cross-store module is the heart of the "老王's 10 bubble-tea stores"
use case: a voucher minted in store A can be redeemed at store B if both
stores are in the same master account and the master's
``voucher_network`` policy allows it.

Redis schema (cross-store)::

    voucher:{vid}                    HASH    full voucher state
    voucher:{vid}:redemption_history LIST    JSON events
    voucher:{vid}:transfer_history   LIST    JSON events
    user:{uid}:vouchers              ZSET    score=issued_at, member=vid
    brand:{bid}:issued_vouchers      ZSET    score=issued_at, member=vid
    brand:{bid}:redeemed_vouchers    ZSET    score=redeemed_at, member=vid
    brand:{bid}:voucher_stats        HASH    {issued,redeemed,expired,transferred}
    master:{mid}:voucher_network     HASH    {policy, custom_rules, configured_at}
    user:{uid}:notifications         LIST    JSON notifications

Atomicity is enforced via WATCH/MULTI on the voucher hash so that a
redeem cannot race with a transfer or a void.
"""

from __future__ import annotations

import io  # noqa: F401 — kept for backward-compat with legacy upload code path
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from app.database import get_db
from app.models import VoucherPool
from app.redis_client import get_redis
from app.schemas import (
    VoucherListItem,
    VoucherListResponse,
    VoucherSummary,
    VoucherUploadResponse,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
# Routers
# ══════════════════════════════════════════════════════════════════════════
#
# ``brand_pool_router`` is mounted by main.py under the prefix
# ``/api/v1/brands`` and exposes both the legacy CSV pool endpoints and the
# brand-scoped issue endpoint (``/{brand_id}/vouchers/issue``).
#
# ``router`` is mounted by main.py under the prefix ``/api/v1/vouchers`` and
# exposes the voucher-instance lifecycle (redeem/transfer/get/void/cleanup)
# and the master-level network configuration.
#
# Both routers live in the same module so they can share helpers without
# circular imports.  main.py keeps using ``vouchers.router`` for the existing
# brand prefix; the new global prefix uses ``vouchers.cross_store_router``.

brand_pool_router = APIRouter()
cross_store_router = APIRouter()

# Backwards-compatible alias — main.py imports ``vouchers.router`` for the
# brand-scoped routes. New main.py registrations also import
# ``cross_store_router``.
router = brand_pool_router


# ══════════════════════════════════════════════════════════════════════════
# Legacy CSV voucher-pool endpoints  (kept verbatim from previous version)
# ══════════════════════════════════════════════════════════════════════════

_CODE_PATTERN = re.compile(r"^[A-Z0-9\-]{4,50}$")


@brand_pool_router.get(
    "/{brand_id}/vouchers",
    response_model=VoucherListResponse,
    summary="List vouchers (CSV pool) for a brand",
)
async def list_vouchers(
    brand_id: str,
    db: AsyncSession = Depends(get_db),
) -> VoucherListResponse:
    result = await db.execute(
        select(VoucherPool).where(VoucherPool.brand_id == brand_id)
    )
    vouchers = result.scalars().all()
    items: list[VoucherListItem] = []
    summary_counts: dict[str, int] = {
        "available": 0, "assigned": 0, "redeemed": 0, "expired": 0,
    }
    for v in vouchers:
        items.append(VoucherListItem(
            code=v.code, tier=v.tier, status=v.status, description=v.description,
        ))
        if v.status in summary_counts:
            summary_counts[v.status] += 1
    return VoucherListResponse(
        vouchers=items, summary=VoucherSummary(**summary_counts),
    )


@brand_pool_router.post(
    "/{brand_id}/vouchers/upload",
    response_model=VoucherUploadResponse,
    summary="Upload voucher codes (CSV) into the brand pool",
    status_code=status.HTTP_201_CREATED,
)
async def upload_vouchers(
    brand_id: str,
    file: UploadFile = File(..., description="CSV file with one voucher code per line"),
    tier: str = Query(..., description="Voucher tier: bronze, silver, or gold"),
    description: str = Query("", description="Human-readable voucher description"),
    valid_days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> VoucherUploadResponse:
    valid_tiers = {"bronze", "silver", "gold"}
    if tier not in valid_tiers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid tier '{tier}'. Must be one of: {', '.join(sorted(valid_tiers))}",
        )
    content = await file.read()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File must be UTF-8 encoded",
        )
    imported = 0
    skipped_duplicates = 0
    errors: list[dict] = []
    lines = text.strip().splitlines()
    if not lines:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File is empty",
        )
    for line_num, raw_line in enumerate(lines, start=1):
        code = raw_line.strip().upper()
        if not code or code.startswith("#"):
            continue
        if not _CODE_PATTERN.match(code):
            errors.append(
                {"line": line_num, "code": code[:50], "error": "invalid_format"}
            )
            continue
        voucher = VoucherPool(
            brand_id=brand_id, code=code, tier=tier,
            description=description or None, status="available",
        )
        db.add(voucher)
        try:
            await db.flush()
            imported += 1
        except IntegrityError:
            await db.rollback()
            skipped_duplicates += 1
            logger.debug("Duplicate voucher code skipped: %s", code)
    logger.info(
        "Voucher upload: brand=%s tier=%s imported=%d skipped=%d errors=%d",
        brand_id, tier, imported, skipped_duplicates, len(errors),
    )
    return VoucherUploadResponse(
        imported=imported,
        skipped_duplicates=skipped_duplicates,
        errors=errors,
    )


# ══════════════════════════════════════════════════════════════════════════
# Cross-store voucher module — helpers
# ══════════════════════════════════════════════════════════════════════════

# ── Constants ─────────────────────────────────────────────────────────────

VOUCHER_TTL_GRACE_SECONDS = 30 * 86400  # keep voucher hash 30d after expiry
NETWORK_POLICIES = {"all_to_all", "hub_and_spoke", "custom", "none"}
SOURCE_KINDS = {"campaign", "gift", "promo", "purchase", "support"}
REDEEMABLE_PRESETS = {"issuer_only", "any_in_master"}
DEFAULT_CROSS_BRAND_COMMISSION_BPS = 0  # 0% intra-master by default
DEFAULT_CROSS_MASTER_COMMISSION_BPS = 500  # 5% across masters


# ── Redis key helpers ─────────────────────────────────────────────────────

def _k_voucher(vid: str) -> str:
    return f"voucher:{vid}"


def _k_voucher_redemption_history(vid: str) -> str:
    return f"voucher:{vid}:redemption_history"


def _k_voucher_transfer_history(vid: str) -> str:
    return f"voucher:{vid}:transfer_history"


def _k_user_vouchers(uid: str) -> str:
    return f"user:{uid}:vouchers"


def _k_brand_issued(bid: str) -> str:
    return f"brand:{bid}:issued_vouchers"


def _k_brand_redeemed(bid: str) -> str:
    return f"brand:{bid}:redeemed_vouchers"


def _k_brand_stats(bid: str) -> str:
    return f"brand:{bid}:voucher_stats"


def _k_master_network(mid: str) -> str:
    return f"master:{mid}:voucher_network"


def _k_master_brands(mid: str) -> str:
    return f"master:{mid}:brands"


def _k_brand_master(bid: str) -> str:
    return f"brand:{bid}:master"


def _k_user_notifications(uid: str) -> str:
    return f"user:{uid}:notifications"


def _k_voucher_template(brand_id: str, tid: str) -> str:
    # Mirror voucher_builder.py
    return f"brand:{brand_id}:voucher_templates:{tid}"


# ── Small utilities ───────────────────────────────────────────────────────

def _now() -> int:
    return int(time.time())


def _iso(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _new_voucher_id() -> str:
    return uuid4().hex[:24]


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def _safe_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


async def _load_voucher(r: aioredis.Redis, vid: str) -> dict[str, str]:
    data = await r.hgetall(_k_voucher(vid))
    if not data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"voucher_id={vid} not found",
        )
    return data


async def _master_of_brand(r: aioredis.Redis, brand_id: str) -> str | None:
    mid = await r.get(_k_brand_master(brand_id))
    return mid if mid else None


async def _master_brands(r: aioredis.Redis, master_id: str) -> set[str]:
    members = await r.smembers(_k_master_brands(master_id))
    return set(members or [])


async def _load_template_transferable(
    r: aioredis.Redis, brand_id: str, template_id: str | None
) -> bool:
    """Return whether the template allows transfer.

    If no template is found (template-less voucher), default to True so
    that the new module does not break grand-fathered records.  Brand-side
    code should set ``transferable`` directly on the voucher hash when
    minting without a template.
    """
    if not template_id:
        return True
    raw = await r.get(_k_voucher_template(brand_id, template_id))
    if not raw:
        return True
    try:
        tpl = json.loads(raw)
    except json.JSONDecodeError:
        return True
    return bool(tpl.get("transferable", True))


# ── Policy / network resolution ───────────────────────────────────────────

async def _load_network_policy(
    r: aioredis.Redis, master_id: str
) -> dict[str, Any]:
    raw = await r.hgetall(_k_master_network(master_id))
    if not raw:
        # Default policy: all brands within the master accept each others'
        # vouchers (most natural for a 10-store chain like 老王).
        return {
            "policy": "all_to_all",
            "custom_rules": {},
            "configured_at": None,
            "commission_bps": DEFAULT_CROSS_BRAND_COMMISSION_BPS,
        }
    out = dict(raw)
    out["custom_rules"] = _safe_loads(out.get("custom_rules"), {})
    out["commission_bps"] = int(
        out.get("commission_bps", DEFAULT_CROSS_BRAND_COMMISSION_BPS)
    )
    return out


def _network_allows(
    policy: dict[str, Any],
    issuer_brand: str,
    at_brand: str,
    master_brands: set[str],
) -> tuple[bool, str | None]:
    """Pure check: given a policy doc, does it allow issuer → at_brand?"""
    if issuer_brand not in master_brands or at_brand not in master_brands:
        return False, "brand_not_in_master"
    name = policy.get("policy", "all_to_all")
    if name == "none":
        return False, "policy_none"
    if name == "all_to_all":
        return True, None
    if name == "hub_and_spoke":
        hub = (policy.get("custom_rules") or {}).get("hub")
        if not hub:
            return False, "hub_not_configured"
        # Spokes redeem at hub or hub redeems at spokes.
        if issuer_brand == hub or at_brand == hub:
            return True, None
        return False, "hub_and_spoke_violation"
    if name == "custom":
        rules = policy.get("custom_rules") or {}
        # rules shape: {"<issuer>": ["<at_brand>", ...]}
        allowed = rules.get(issuer_brand) or []
        if at_brand in allowed:
            return True, None
        return False, "custom_rules_deny"
    return False, f"unknown_policy:{name}"


# ── Pydantic models ───────────────────────────────────────────────────────

class IssueVoucherRequest(BaseModel):
    template_id: str | None = Field(None, max_length=64)
    user_id: str = Field(..., min_length=1, max_length=128)
    # "issuer_only" | "any_in_master" | list[brand_id]
    redeemable_at: Any = "issuer_only"
    value_cents: int | None = Field(None, ge=0)
    expires_at: int | None = Field(None, ge=0)
    conditions: dict[str, Any] = Field(default_factory=dict)
    source: Literal["campaign", "gift", "promo", "purchase", "support"] = "campaign"
    transferable: bool = True
    max_uses: int = Field(1, ge=1, le=100)

    @field_validator("redeemable_at")
    @classmethod
    def _validate_redeemable_at(cls, v: Any) -> Any:
        if isinstance(v, str):
            if v not in REDEEMABLE_PRESETS:
                raise ValueError(
                    f"redeemable_at string must be one of {REDEEMABLE_PRESETS}"
                )
            return v
        if isinstance(v, list):
            if not v:
                raise ValueError("redeemable_at list must not be empty")
            for b in v:
                if not isinstance(b, str) or not b:
                    raise ValueError("redeemable_at list entries must be brand_ids")
            return v
        raise ValueError("redeemable_at must be a preset string or list of brand_ids")


class RedeemRequest(BaseModel):
    at_brand_id: str = Field(..., min_length=1)
    redeemer_user_id: str = Field(..., min_length=1)
    order_id: str | None = Field(None, max_length=128)
    order_amount_cents: int | None = Field(None, ge=0)


class TransferRequest(BaseModel):
    from_user_id: str = Field(..., min_length=1)
    to_user_id: str = Field(..., min_length=1)
    message: str = Field("", max_length=500)


class VoidRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    reason: str = Field("", max_length=500)


class CleanupRequest(BaseModel):
    admin_token: str = Field(..., min_length=1)
    dry_run: bool = True
    limit: int = Field(1000, ge=1, le=100000)


class NetworkConfigRequest(BaseModel):
    policy: Literal["all_to_all", "hub_and_spoke", "custom", "none"]
    custom_rules: dict[str, Any] = Field(default_factory=dict)
    commission_bps: int = Field(
        DEFAULT_CROSS_BRAND_COMMISSION_BPS, ge=0, le=10000
    )


# ══════════════════════════════════════════════════════════════════════════
# Optional integration hooks  (fail-soft — never block redemption)
# ══════════════════════════════════════════════════════════════════════════

async def _fire_pixel(
    r: aioredis.Redis, *, brand_id: str, user_id: str, event: str, meta: dict
) -> None:
    try:
        payload = {
            "brand_id": brand_id, "user_id": user_id, "event": event,
            "meta": meta, "ts": _now(),
        }
        await r.rpush("pixel:events", _dumps(payload))
        await r.ltrim("pixel:events", -10000, -1)
    except Exception as exc:  # pragma: no cover — analytics must not break flow
        logger.debug("pixel fire failed: %s", exc)


async def _enqueue_notification(
    r: aioredis.Redis, user_id: str, kind: str, payload: dict
) -> None:
    try:
        msg = {"kind": kind, "ts": _now(), **payload}
        await r.lpush(_k_user_notifications(user_id), _dumps(msg))
        await r.ltrim(_k_user_notifications(user_id), 0, 199)
    except Exception as exc:  # pragma: no cover
        logger.debug("notification enqueue failed: %s", exc)


async def _attribution_cross_brand(
    r: aioredis.Redis,
    *,
    user_id: str,
    source_brand: str,
    target_brand: str,
    amount_cents: int,
    order_id: str | None,
) -> None:
    """Record an attribution event for the cross-brand redemption.

    We deliberately do not import the attribution router (would create a
    cycle).  Instead we write the canonical hash directly — the attribution
    module's read paths see the same key.
    """
    try:
        event_id = uuid4().hex[:16]
        key = f"attr:{event_id}"
        ts = _now()
        meta = {
            "order_id": order_id or "",
            "kind": "voucher_redemption",
            "source_brand": source_brand,
            "target_brand": target_brand,
        }
        await r.hset(
            key,
            mapping={
                "event_id": event_id,
                "stage": "conversion",
                "user_id": user_id,
                "source_brand": source_brand,
                "target_brand": target_brand,
                "value_cents": str(amount_cents),
                "timestamp": str(ts),
                "meta": _dumps(meta),
            },
        )
        await r.expire(key, 90 * 86400)
    except Exception as exc:  # pragma: no cover
        logger.debug("cross-brand attribution failed: %s", exc)


# ══════════════════════════════════════════════════════════════════════════
# Cross-store endpoints  —  /api/v1/vouchers/...
# ══════════════════════════════════════════════════════════════════════════

# ── Issue ────────────────────────────────────────────────────────────────

async def _do_issue(
    r: aioredis.Redis, *, issuer_brand_id: str, body: IssueVoucherRequest
) -> dict[str, Any]:
    """Shared issue helper used by both the brand-scoped and global routes."""
    # Validate redeemable_at against the master if applicable.
    redeemable_at = body.redeemable_at
    master_id = await _master_of_brand(r, issuer_brand_id)
    resolved_redeemable: Any = redeemable_at

    if isinstance(redeemable_at, str) and redeemable_at == "any_in_master":
        if not master_id:
            raise HTTPException(
                status_code=400,
                detail="redeemable_at=any_in_master requires brand to belong to a master",
            )
        resolved_redeemable = f"any_in_master:{master_id}"
    elif isinstance(redeemable_at, list):
        # Ensure issuer is included implicitly (a voucher issued in store A
        # should also be redeemable at store A unless explicitly excluded).
        if issuer_brand_id not in redeemable_at:
            resolved_redeemable = [issuer_brand_id, *redeemable_at]

    vid = _new_voucher_id()
    issued_at = _now()
    expires_at = body.expires_at
    if expires_at and expires_at <= issued_at:
        raise HTTPException(status_code=400, detail="expires_at must be in the future")

    voucher: dict[str, str] = {
        "voucher_id": vid,
        "template_id": body.template_id or "",
        "issuer_brand_id": issuer_brand_id,
        "issuer_master_id": master_id or "",
        "holder_user_id": body.user_id,
        "original_holder_user_id": body.user_id,
        "redeemable_at": _dumps(resolved_redeemable),
        "value_cents": str(body.value_cents if body.value_cents is not None else 0),
        "residual_cents": str(body.value_cents if body.value_cents is not None else 0),
        "conditions": _dumps(body.conditions or {}),
        "source": body.source,
        "status": "issued",
        "transferable": "1" if body.transferable else "0",
        "max_uses": str(body.max_uses),
        "uses": "0",
        "issued_at": str(issued_at),
        "expires_at": str(expires_at) if expires_at else "",
    }

    pipe = r.pipeline()
    pipe.hset(_k_voucher(vid), mapping=voucher)
    if expires_at:
        pipe.expireat(_k_voucher(vid), expires_at + VOUCHER_TTL_GRACE_SECONDS)
    pipe.zadd(_k_user_vouchers(body.user_id), {vid: issued_at})
    pipe.zadd(_k_brand_issued(issuer_brand_id), {vid: issued_at})
    pipe.hincrby(_k_brand_stats(issuer_brand_id), "issued", 1)
    await pipe.execute()

    logger.info(
        "Voucher issued: vid=%s issuer=%s holder=%s source=%s redeemable_at=%s",
        vid, issuer_brand_id, body.user_id, body.source, resolved_redeemable,
    )

    # Fire-and-forget hooks
    await _fire_pixel(
        r,
        brand_id=issuer_brand_id, user_id=body.user_id, event="voucher_issued",
        meta={"voucher_id": vid, "source": body.source, "value_cents": body.value_cents},
    )
    if body.source in ("gift", "promo", "campaign"):
        await _enqueue_notification(
            r, body.user_id, kind="voucher_received",
            payload={
                "voucher_id": vid, "issuer_brand_id": issuer_brand_id,
                "value_cents": body.value_cents, "source": body.source,
            },
        )

    return {
        "voucher_id": vid,
        "status": "issued",
        "issuer_brand_id": issuer_brand_id,
        "holder_user_id": body.user_id,
        "redeemable_at": resolved_redeemable,
        "value_cents": body.value_cents,
        "expires_at": expires_at,
        "expires_at_iso": _iso(expires_at),
    }


@brand_pool_router.post(
    "/{brand_id}/vouchers/issue",
    summary="Issue a voucher (brand-scoped convenience route)",
    status_code=status.HTTP_201_CREATED,
)
async def issue_voucher_brand_scoped(
    brand_id: str,
    body: IssueVoucherRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    return await _do_issue(r, issuer_brand_id=brand_id, body=body)


@cross_store_router.post(
    "/issue",
    summary="Issue a voucher (issuer brand in body or query)",
    status_code=status.HTTP_201_CREATED,
)
async def issue_voucher_global(
    body: IssueVoucherRequest,
    issuer_brand_id: str = Query(..., min_length=1),
    r: aioredis.Redis = Depends(get_redis),
):
    return await _do_issue(r, issuer_brand_id=issuer_brand_id, body=body)


# ── Redeem ───────────────────────────────────────────────────────────────

def _parse_redeemable_at(raw: str | Any) -> Any:
    if isinstance(raw, str):
        if raw.startswith("any_in_master:") or raw in REDEEMABLE_PRESETS:
            return raw
        # JSON-encoded list or plain string
        decoded = _safe_loads(raw, raw)
        return decoded
    return raw


async def _validate_redeemable_at(
    r: aioredis.Redis,
    *,
    voucher: dict[str, str],
    at_brand_id: str,
) -> tuple[bool, str | None, dict[str, Any]]:
    """Return (allowed, reject_reason, network_meta)."""
    issuer = voucher.get("issuer_brand_id", "")
    redeemable_at = _parse_redeemable_at(voucher.get("redeemable_at", "issuer_only"))
    is_cross_brand = at_brand_id != issuer

    if not is_cross_brand:
        return True, None, {"is_cross_brand": False}

    # All cross-brand paths below
    if redeemable_at == "issuer_only":
        return False, "voucher_is_issuer_only", {"is_cross_brand": True}

    # any_in_master:<mid>
    if isinstance(redeemable_at, str) and redeemable_at.startswith("any_in_master:"):
        master_id = redeemable_at.split(":", 1)[1]
        master_brands = await _master_brands(r, master_id)
        if at_brand_id not in master_brands:
            return False, "at_brand_not_in_master", {
                "is_cross_brand": True, "master_id": master_id,
            }
        # Cross-check policy
        policy = await _load_network_policy(r, master_id)
        allowed, why = _network_allows(policy, issuer, at_brand_id, master_brands)
        return allowed, (None if allowed else why), {
            "is_cross_brand": True, "master_id": master_id,
            "policy": policy.get("policy"), "commission_bps": policy.get("commission_bps", 0),
        }

    # Explicit list
    if isinstance(redeemable_at, list):
        if at_brand_id in redeemable_at:
            # Still check policy if both brands share a master
            at_master = await _master_of_brand(r, at_brand_id)
            issuer_master = await _master_of_brand(r, issuer)
            if at_master and at_master == issuer_master:
                policy = await _load_network_policy(r, at_master)
                master_brands = await _master_brands(r, at_master)
                allowed, why = _network_allows(policy, issuer, at_brand_id, master_brands)
                return allowed, (None if allowed else why), {
                    "is_cross_brand": True, "master_id": at_master,
                    "policy": policy.get("policy"),
                    "commission_bps": policy.get("commission_bps", 0),
                }
            # Cross-master explicit allow-list: charge default x-master commission.
            return True, None, {
                "is_cross_brand": True, "cross_master": True,
                "commission_bps": DEFAULT_CROSS_MASTER_COMMISSION_BPS,
            }
        return False, "at_brand_not_in_allow_list", {"is_cross_brand": True}

    return False, "unknown_redeemable_at", {"is_cross_brand": True}


def _check_conditions(
    voucher: dict[str, str], order_amount_cents: int | None
) -> tuple[bool, str | None]:
    cond = _safe_loads(voucher.get("conditions"), {})
    min_order = cond.get("min_order") or cond.get("min_purchase_cents")
    if min_order is not None:
        if order_amount_cents is None:
            return False, "order_amount_required"
        if order_amount_cents < int(min_order):
            return False, "order_below_minimum"
    expires_at = voucher.get("expires_at", "")
    if expires_at:
        try:
            if int(expires_at) <= _now():
                return False, "voucher_expired"
        except ValueError:
            pass
    max_uses = int(voucher.get("max_uses", "1"))
    uses = int(voucher.get("uses", "0"))
    if uses >= max_uses:
        return False, "max_uses_exceeded"
    return True, None


@cross_store_router.post(
    "/{voucher_id}/redeem",
    summary="Redeem a voucher (cross-brand aware)",
)
async def redeem_voucher(
    voucher_id: str,
    body: RedeemRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Atomically redeem a voucher at ``at_brand_id``.

    Uses WATCH/MULTI on the voucher hash to prevent double-redeem races.
    """
    key = _k_voucher(voucher_id)

    # Acquire current state with a WATCH-style optimistic loop.
    for _attempt in range(5):
        async with r.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                voucher = await pipe.hgetall(key)
                if not voucher:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=404, detail=f"voucher_id={voucher_id} not found"
                    )

                # Status check
                status_now = voucher.get("status", "")
                if status_now not in ("issued", "claimed"):
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "ok": False, "reason": "invalid_status",
                            "current_status": status_now,
                        },
                    )

                # Holder check (only voucher holder can redeem unless cross-user
                # POS — we allow any redeemer to operate on the holder's behalf
                # for the POS use case; record the redeemer in the event).

                # Redeemable_at + policy
                allowed, why, net_meta = await _validate_redeemable_at(
                    r, voucher=voucher, at_brand_id=body.at_brand_id,
                )
                if not allowed:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=403,
                        detail={"ok": False, "reason": why, "network": net_meta},
                    )

                # Conditions
                cond_ok, cond_reason = _check_conditions(
                    voucher, body.order_amount_cents
                )
                if not cond_ok:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=422,
                        detail={"ok": False, "reason": cond_reason},
                    )

                # Compute applied value + residual (partial use for multi-use)
                face_value = int(voucher.get("value_cents", "0") or 0)
                residual = int(voucher.get("residual_cents", str(face_value)) or 0)
                value_applied = residual
                if body.order_amount_cents is not None and residual > body.order_amount_cents:
                    value_applied = body.order_amount_cents
                new_residual = max(0, residual - value_applied)
                new_uses = int(voucher.get("uses", "0")) + 1
                max_uses = int(voucher.get("max_uses", "1"))
                will_be_fully_consumed = (
                    new_uses >= max_uses or new_residual == 0
                )
                new_status = "redeemed" if will_be_fully_consumed else "issued"

                # Commission
                commission_bps = int(net_meta.get("commission_bps", 0) or 0)
                commission_cents = (value_applied * commission_bps) // 10000

                # Build event
                event = {
                    "type": "redeem",
                    "voucher_id": voucher_id,
                    "at_brand_id": body.at_brand_id,
                    "issuer_brand_id": voucher.get("issuer_brand_id"),
                    "redeemer_user_id": body.redeemer_user_id,
                    "holder_user_id": voucher.get("holder_user_id"),
                    "order_id": body.order_id,
                    "order_amount_cents": body.order_amount_cents,
                    "value_applied_cents": value_applied,
                    "residual_cents_after": new_residual,
                    "is_cross_brand": net_meta.get("is_cross_brand", False),
                    "commission_cents": commission_cents,
                    "ts": _now(),
                }

                # Begin transaction — all writes atomic
                pipe.multi()
                hash_update = {
                    "status": new_status,
                    "uses": str(new_uses),
                    "residual_cents": str(new_residual),
                    "redeemed_at": str(_now()),
                    "last_redeemed_at_brand": body.at_brand_id,
                }
                pipe.hset(key, mapping=hash_update)
                pipe.rpush(
                    _k_voucher_redemption_history(voucher_id),
                    _dumps(event),
                )
                pipe.zadd(
                    _k_brand_redeemed(body.at_brand_id),
                    {voucher_id: _now()},
                )
                pipe.hincrby(
                    _k_brand_stats(body.at_brand_id), "redeemed", 1
                )
                await pipe.execute()
                break  # success — exit retry loop
            except aioredis.WatchError:
                # Concurrent modification — retry
                continue
            except HTTPException:
                raise
    else:
        raise HTTPException(
            status_code=503, detail="redeem_contention_exceeded_retries"
        )

    # Side-effects after the atomic step  ─────────────────────────────────
    if event["is_cross_brand"]:
        await _attribution_cross_brand(
            r,
            user_id=voucher.get("holder_user_id", ""),
            source_brand=voucher.get("issuer_brand_id", ""),
            target_brand=body.at_brand_id,
            amount_cents=event["value_applied_cents"],
            order_id=body.order_id,
        )

    await _fire_pixel(
        r,
        brand_id=body.at_brand_id, user_id=body.redeemer_user_id,
        event="voucher_redeemed",
        meta={
            "voucher_id": voucher_id,
            "value_applied_cents": event["value_applied_cents"],
            "is_cross_brand": event["is_cross_brand"],
            "commission_cents": event["commission_cents"],
        },
    )

    return {
        "ok": True,
        "voucher_id": voucher_id,
        "status": new_status,
        "value_applied_cents": event["value_applied_cents"],
        "residual_cents": event["residual_cents_after"],
        "is_cross_brand": event["is_cross_brand"],
        "kix_commission_cents": event["commission_cents"],
    }


# ── Transfer ─────────────────────────────────────────────────────────────

@cross_store_router.post(
    "/{voucher_id}/transfer",
    summary="Transfer a voucher to another user",
)
async def transfer_voucher(
    voucher_id: str,
    body: TransferRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    if body.from_user_id == body.to_user_id:
        raise HTTPException(status_code=400, detail="from and to are identical")

    key = _k_voucher(voucher_id)

    for _attempt in range(5):
        async with r.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                voucher = await pipe.hgetall(key)
                if not voucher:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=404, detail=f"voucher_id={voucher_id} not found"
                    )
                if voucher.get("holder_user_id") != body.from_user_id:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=403, detail="from_user_id is not current holder"
                    )
                if voucher.get("status") not in ("issued", "claimed"):
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=409,
                        detail=f"cannot transfer voucher in status={voucher.get('status')}",
                    )
                if voucher.get("transferable", "1") != "1":
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=403, detail="voucher is not transferable"
                    )
                template_ok = await _load_template_transferable(
                    r,
                    voucher.get("issuer_brand_id", ""),
                    voucher.get("template_id") or None,
                )
                if not template_ok:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=403,
                        detail="template does not allow transfer",
                    )

                # Expiry check
                expires_at = voucher.get("expires_at") or ""
                if expires_at:
                    try:
                        if int(expires_at) <= _now():
                            await pipe.unwatch()
                            raise HTTPException(
                                status_code=410,
                                detail="voucher_expired",
                            )
                    except ValueError:
                        pass

                event = {
                    "type": "transfer",
                    "voucher_id": voucher_id,
                    "from_user_id": body.from_user_id,
                    "to_user_id": body.to_user_id,
                    "message": body.message,
                    "ts": _now(),
                }
                issued_at_score = int(voucher.get("issued_at", _now()))

                pipe.multi()
                pipe.hset(
                    key,
                    mapping={
                        "holder_user_id": body.to_user_id,
                        "last_transfer_at": str(_now()),
                    },
                )
                pipe.zrem(_k_user_vouchers(body.from_user_id), voucher_id)
                pipe.zadd(
                    _k_user_vouchers(body.to_user_id),
                    {voucher_id: issued_at_score},
                )
                pipe.rpush(
                    _k_voucher_transfer_history(voucher_id), _dumps(event)
                )
                pipe.hincrby(
                    _k_brand_stats(voucher.get("issuer_brand_id", "")),
                    "transferred", 1,
                )
                await pipe.execute()
                break
            except aioredis.WatchError:
                continue
            except HTTPException:
                raise
    else:
        raise HTTPException(
            status_code=503, detail="transfer_contention_exceeded_retries"
        )

    await _enqueue_notification(
        r, body.to_user_id, kind="voucher_gift_received",
        payload={
            "voucher_id": voucher_id,
            "from_user_id": body.from_user_id,
            "message": body.message,
        },
    )

    return {
        "ok": True,
        "voucher_id": voucher_id,
        "new_holder": body.to_user_id,
        "previous_holder": body.from_user_id,
    }


# ── Get / list ───────────────────────────────────────────────────────────

def _voucher_to_dict(state: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = dict(state)
    out["redeemable_at"] = _parse_redeemable_at(state.get("redeemable_at", ""))
    out["conditions"] = _safe_loads(state.get("conditions"), {})
    for int_field in ("value_cents", "residual_cents", "uses", "max_uses",
                      "issued_at", "expires_at"):
        if int_field in out and out[int_field] not in ("", None):
            try:
                out[int_field] = int(out[int_field])
            except (TypeError, ValueError):
                pass
    out["transferable"] = state.get("transferable", "1") == "1"
    return out


@cross_store_router.get(
    "/{voucher_id}",
    summary="Get a voucher by id",
)
async def get_voucher(
    voucher_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    state = await _load_voucher(r, voucher_id)
    voucher = _voucher_to_dict(state)
    history = await r.lrange(_k_voucher_redemption_history(voucher_id), 0, -1)
    transfers = await r.lrange(_k_voucher_transfer_history(voucher_id), 0, -1)
    voucher["redemption_history"] = [
        _safe_loads(h, {}) for h in (history or [])
    ]
    voucher["transfer_history"] = [
        _safe_loads(h, {}) for h in (transfers or [])
    ]
    return voucher


@cross_store_router.get(
    "/user/{user_id}",
    summary="List vouchers held by a user",
)
async def list_user_vouchers(
    user_id: str,
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=500),
    r: aioredis.Redis = Depends(get_redis),
):
    vids = await r.zrevrange(_k_user_vouchers(user_id), 0, limit - 1)
    out: list[dict[str, Any]] = []
    for vid in vids or []:
        state = await r.hgetall(_k_voucher(vid))
        if not state:
            continue
        if status_filter and state.get("status") != status_filter:
            continue
        out.append(_voucher_to_dict(state))
    return {"user_id": user_id, "count": len(out), "vouchers": out}


@cross_store_router.get(
    "/brand/{brand_id}/issued",
    summary="List vouchers issued by a brand",
)
async def list_brand_issued(
    brand_id: str,
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
    template_id: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    r: aioredis.Redis = Depends(get_redis),
):
    lo = from_ts if from_ts is not None else "-inf"
    hi = to_ts if to_ts is not None else "+inf"
    vids = await r.zrangebyscore(
        _k_brand_issued(brand_id), lo, hi, start=0, num=limit,
    )
    out: list[dict[str, Any]] = []
    for vid in vids or []:
        state = await r.hgetall(_k_voucher(vid))
        if not state:
            continue
        if template_id and state.get("template_id") != template_id:
            continue
        out.append(_voucher_to_dict(state))
    return {"brand_id": brand_id, "count": len(out), "vouchers": out}


@cross_store_router.get(
    "/brand/{brand_id}/redeemed",
    summary="List vouchers redeemed at a brand (any issuer)",
)
async def list_brand_redeemed(
    brand_id: str,
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
    limit: int = Query(100, ge=1, le=1000),
    r: aioredis.Redis = Depends(get_redis),
):
    lo = from_ts if from_ts is not None else "-inf"
    hi = to_ts if to_ts is not None else "+inf"
    vids = await r.zrangebyscore(
        _k_brand_redeemed(brand_id), lo, hi, start=0, num=limit,
    )
    out: list[dict[str, Any]] = []
    cross_brand_count = 0
    for vid in vids or []:
        state = await r.hgetall(_k_voucher(vid))
        if not state:
            continue
        v = _voucher_to_dict(state)
        if v.get("issuer_brand_id") != brand_id:
            cross_brand_count += 1
            v["__cross_brand_inbound"] = True
        out.append(v)
    return {
        "brand_id": brand_id, "count": len(out),
        "cross_brand_inbound": cross_brand_count, "vouchers": out,
    }


# ── Void ─────────────────────────────────────────────────────────────────

@cross_store_router.post(
    "/{voucher_id}/void",
    summary="Void a voucher (issuer or admin)",
)
async def void_voucher(
    voucher_id: str,
    body: VoidRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    key = _k_voucher(voucher_id)
    voucher = await r.hgetall(key)
    if not voucher:
        raise HTTPException(status_code=404, detail=f"voucher_id={voucher_id} not found")
    issuer = voucher.get("issuer_brand_id", "")
    if body.brand_id != issuer:
        # Allow same-master administrators to void.
        issuer_master = await _master_of_brand(r, issuer)
        requester_master = await _master_of_brand(r, body.brand_id)
        if not (issuer_master and issuer_master == requester_master):
            raise HTTPException(
                status_code=403,
                detail="only issuer brand (or same-master admin) may void",
            )
    if voucher.get("status") in ("redeemed", "void", "expired"):
        raise HTTPException(
            status_code=409,
            detail=f"cannot void voucher in status={voucher.get('status')}",
        )
    event = {
        "type": "void",
        "voucher_id": voucher_id,
        "brand_id": body.brand_id,
        "reason": body.reason,
        "previous_status": voucher.get("status"),
        "ts": _now(),
    }
    pipe = r.pipeline()
    pipe.hset(key, mapping={"status": "void", "voided_at": str(_now())})
    pipe.rpush(_k_voucher_redemption_history(voucher_id), _dumps(event))
    pipe.hincrby(_k_brand_stats(issuer), "voided", 1)
    await pipe.execute()

    await _enqueue_notification(
        r, voucher.get("holder_user_id", ""), kind="voucher_voided",
        payload={"voucher_id": voucher_id, "reason": body.reason},
    )

    return {
        "ok": True, "voucher_id": voucher_id, "status": "void",
        "previous_status": event["previous_status"],
    }


# ── Cleanup expired ──────────────────────────────────────────────────────

@cross_store_router.post(
    "/cleanup-expired",
    summary="Mark expired vouchers; optional notifications (admin)",
)
async def cleanup_expired(
    body: CleanupRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    from app.config import settings  # local to avoid top-level coupling
    expected = getattr(settings, "admin_token", None)
    if expected and body.admin_token != expected:
        raise HTTPException(status_code=403, detail="invalid admin_token")

    # Scan voucher hashes — bounded by ``limit``.
    expired: list[str] = []
    notified: list[str] = []
    now = _now()
    cursor = 0
    seen = 0
    while True:
        cursor, keys = await r.scan(cursor=cursor, match="voucher:*", count=200)
        for k in keys:
            # Skip history/sub-keys
            if k.count(":") != 1:
                continue
            seen += 1
            if seen > body.limit:
                cursor = 0
                break
            state = await r.hgetall(k)
            if not state:
                continue
            if state.get("status") not in ("issued", "claimed"):
                continue
            exp = state.get("expires_at") or ""
            if not exp:
                continue
            try:
                if int(exp) > now:
                    continue
            except ValueError:
                continue
            vid = state.get("voucher_id") or k.split(":", 1)[1]
            expired.append(vid)
            if not body.dry_run:
                await r.hset(k, mapping={"status": "expired", "expired_at": str(now)})
                issuer = state.get("issuer_brand_id", "")
                if issuer:
                    await r.hincrby(_k_brand_stats(issuer), "expired", 1)
                holder = state.get("holder_user_id", "")
                if holder:
                    await _enqueue_notification(
                        r, holder, kind="voucher_expired",
                        payload={"voucher_id": vid},
                    )
                    notified.append(holder)
        if cursor == 0:
            break
    return {
        "ok": True, "dry_run": body.dry_run,
        "expired_count": len(expired), "expired_voucher_ids": expired[:200],
        "notified_count": len(notified),
    }


# ══════════════════════════════════════════════════════════════════════════
# Master-level network configuration
# ══════════════════════════════════════════════════════════════════════════

@cross_store_router.get(
    "/master/{master_id}/redeemable-network",
    summary="Inspect intra-master voucher redemption network",
)
async def get_redeemable_network(
    master_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    master_brands = await _master_brands(r, master_id)
    if not master_brands:
        raise HTTPException(
            status_code=404, detail=f"master_id={master_id} has no brands"
        )
    policy = await _load_network_policy(r, master_id)
    brands = sorted(master_brands)
    network: list[dict[str, Any]] = []
    for bid in brands:
        accepts_from: list[str] = []
        issues_to: list[str] = []
        for other in brands:
            if other == bid:
                continue
            # bid accepts from `other` ?
            ok_in, _ = _network_allows(policy, other, bid, master_brands)
            if ok_in:
                accepts_from.append(other)
            # bid issues redeemable at `other` ?
            ok_out, _ = _network_allows(policy, bid, other, master_brands)
            if ok_out:
                issues_to.append(other)
        network.append({
            "brand_id": bid,
            "accepts_from": accepts_from,
            "issues_to": issues_to,
        })
    return {
        "master_id": master_id,
        "policy": policy.get("policy"),
        "commission_bps": policy.get("commission_bps", 0),
        "custom_rules": policy.get("custom_rules", {}),
        "configured_at": policy.get("configured_at"),
        "network": network,
    }


@cross_store_router.post(
    "/master/{master_id}/configure-network",
    summary="Configure intra-master voucher redemption policy",
)
async def configure_network(
    master_id: str,
    body: NetworkConfigRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    master_brands = await _master_brands(r, master_id)
    if not master_brands:
        raise HTTPException(
            status_code=404, detail=f"master_id={master_id} has no brands"
        )
    if body.policy == "hub_and_spoke":
        hub = (body.custom_rules or {}).get("hub")
        if not hub:
            raise HTTPException(
                status_code=400,
                detail="hub_and_spoke requires custom_rules.hub = '<brand_id>'",
            )
        if hub not in master_brands:
            raise HTTPException(
                status_code=400,
                detail=f"hub brand {hub!r} is not part of master {master_id!r}",
            )
    if body.policy == "custom":
        # Validate every key/value brand is in the master
        for issuer, allowed_list in (body.custom_rules or {}).items():
            if issuer not in master_brands:
                raise HTTPException(
                    status_code=400,
                    detail=f"custom_rules issuer {issuer!r} not in master",
                )
            if not isinstance(allowed_list, list):
                raise HTTPException(
                    status_code=400,
                    detail=f"custom_rules[{issuer!r}] must be a list of brand_ids",
                )
            for b in allowed_list:
                if b not in master_brands:
                    raise HTTPException(
                        status_code=400,
                        detail=f"custom_rules target {b!r} not in master",
                    )

    payload = {
        "policy": body.policy,
        "custom_rules": _dumps(body.custom_rules or {}),
        "configured_at": str(_now()),
        "commission_bps": str(body.commission_bps),
    }
    await r.hset(_k_master_network(master_id), mapping=payload)
    logger.info(
        "Master %s voucher_network configured: policy=%s commission_bps=%d",
        master_id, body.policy, body.commission_bps,
    )
    return {
        "ok": True, "master_id": master_id, "policy": body.policy,
        "commission_bps": body.commission_bps,
        "custom_rules": body.custom_rules or {},
    }
