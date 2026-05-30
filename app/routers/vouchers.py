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

Cross-brand redemption is handled via the KiX auction algorithm. Vouchers
themselves are local to the issuer or the master, never to bilateral
partnerships. The supported ``redeemable_at`` shapes are:

  * ``"issuer_only"`` — only the issuing brand may redeem.
  * ``"any_in_master:{master_id}"`` — any brand inside the same master
    account (multi-store chain) may redeem, subject to the master's
    network policy.
  * ``list[brand_id]`` — an explicit allow-list set by the issuer as a
    KiX-policy gift (NOT a contract). The receiving brand gets no
    automatic payout — this is co-marketing, not an auction settlement.

Partnership-based redemption (``"partnership:{pid}"``) is removed.
Cross-brand value flow goes through the auction engine, not through
voucher metadata.

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

from app.api_standards import error_response, list_response
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

class RelationalConditions(BaseModel):
    """Household / family / bundle / cohort / temporal / cumulative predicates.

    Distinct from per-user-local conditions (``min_purchase_cents``,
    ``tier_required``, ``first_time_user_only``) which live under the
    ``conditions`` field. These predicates evaluate against the redeemer's
    relationships, bundle holdings, cohort membership, and historical activity
    across the master account.
    """

    # ── Household / family ─────────────────────────────────────────────────
    min_children_count: int | None = Field(None, ge=0)
    min_household_members: int | None = Field(None, ge=0)
    sibling_discount: bool | None = None  # requires a sibling already enrolled
    relationship_type_required: str | None = Field(None, max_length=64)

    # ── Bundle / multi-item ───────────────────────────────────────────────
    bundle_required: list[str] | None = None  # template_ids user must hold
    min_bundle_count: int | None = Field(None, ge=0)

    # ── Group / cohort ────────────────────────────────────────────────────
    same_master_member: bool | None = None
    same_audience_required: str | None = Field(None, max_length=128)

    # ── Time / sequence ───────────────────────────────────────────────────
    after_purchase_within_days: int | None = Field(None, ge=0, le=3650)
    after_relationship_added_days: int | None = Field(None, ge=0, le=3650)

    # ── Cumulative ────────────────────────────────────────────────────────
    cumulative_spend_min_cents: int | None = Field(None, ge=0)
    visit_count_min: int | None = Field(None, ge=0)


class IssueVoucherRequest(BaseModel):
    template_id: str | None = Field(None, max_length=64)
    user_id: str = Field(..., min_length=1, max_length=128)
    # "issuer_only" | "any_in_master" | list[brand_id]
    redeemable_at: Any = "issuer_only"
    value_cents: int | None = Field(None, ge=0)
    expires_at: int | None = Field(None, ge=0)
    conditions: dict[str, Any] = Field(default_factory=dict)
    relational_conditions: dict[str, Any] | None = None
    source: Literal[
        "campaign", "gift", "promo", "purchase", "support", "game_win"
    ] = "campaign"
    transferable: bool = True
    max_uses: int = Field(1, ge=1, le=100)
    # Holder type discriminator for the QR→game→voucher anonymous flow.
    # When ``holder_type="device_fp"``, the ``user_id`` field stores the
    # device fingerprint instead of a kid, and the voucher is expected to
    # be reserved + claimed (upgraded to a kid) before redemption.
    holder_type: Literal["kid", "device_fp"] = "kid"

    @field_validator("redeemable_at")
    @classmethod
    def _validate_redeemable_at(cls, v: Any) -> Any:
        # Defense-in-depth: reject the removed ``partnership:{pid}`` scheme
        # with a clear, actionable error message before any other parsing.
        if isinstance(v, str) and v.startswith("partnership:"):
            raise ValueError(
                "Partnership-based redemption removed — use "
                "'issuer_only', 'any_in_master:{mid}', or a list of brand_ids."
            )
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
                if b.startswith("partnership:"):
                    raise ValueError(
                        "Partnership-based redemption removed — "
                        "redeemable_at list entries must be plain brand_ids."
                    )
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


class CommissionSplit(BaseModel):
    """Custom commission split for cross-brand voucher redemption.

    When a voucher is issued by brand A and redeemed at brand B, the
    transaction value flows according to this split. Default is
    issuer_pct=0, kix_pct=0.30, redeemer_pct=0.70 (KiX takes 30%, redeemer
    keeps the rest, issuer gets nothing — it was a gift). Templates may
    override this with any split that sums to 1.0.
    """
    issuer_pct: float = Field(..., ge=0.0, le=1.0)
    kix_pct: float = Field(..., ge=0.0, le=1.0)
    redeemer_pct: float = Field(..., ge=0.0, le=1.0)

    @field_validator("redeemer_pct")
    @classmethod
    def _validate_sum(cls, v: float, info: Any) -> float:
        # Pydantic v2 passes other field values in info.data.
        issuer = (info.data or {}).get("issuer_pct", 0.0)
        kix = (info.data or {}).get("kix_pct", 0.0)
        total = issuer + kix + v
        if not (0.999 <= total <= 1.001):
            raise ValueError(
                f"commission_split must sum to 1.0 (got {total:.4f})"
            )
        return v


class VoucherCancelRequest(BaseModel):
    cancelled_by: Literal["user", "issuer", "admin"]
    reason: str = Field("", max_length=500)
    refund_to_user: bool | None = None


class BulkCancelRequest(BaseModel):
    voucher_ids: list[str] = Field(..., min_length=1, max_length=100)
    cancelled_by: Literal["user", "issuer", "admin"]
    reason: str = Field("", max_length=500)


class BulkIssueRequest(BaseModel):
    brand_id: str = Field(..., min_length=1, max_length=128)
    template_id: str | None = Field(None, max_length=64)
    user_ids: list[str] = Field(..., min_length=1, max_length=1000)
    values: list[int] | None = None
    expires_at: int | None = Field(None, ge=0)
    conditions: dict[str, Any] = Field(default_factory=dict)
    source: Literal["campaign", "gift", "promo", "purchase", "support"] = "campaign"
    transferable: bool = True
    max_uses: int = Field(1, ge=1, le=100)

    @field_validator("values")
    @classmethod
    def _validate_values(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return v
        for x in v:
            if not isinstance(x, int) or x < 0:
                raise ValueError("values entries must be non-negative ints")
        return v


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
            # api_standards: structured error envelope, human-readable message kept.
            raise error_response(
                400,
                "validation_failed",
                "redeemable_at=any_in_master requires brand to belong to a master",
                field="redeemable_at",
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
        raise error_response(
            400,
            "validation_failed",
            "expires_at must be in the future",
            field="expires_at",
        )

    # Validate relational_conditions against schema if provided. If not
    # provided on the request, try to inherit from the template.
    rel_cond_dict: dict[str, Any] = {}
    if body.relational_conditions:
        try:
            rel_cond_dict = RelationalConditions(
                **body.relational_conditions
            ).model_dump(exclude_none=True)
        except Exception as exc:
            raise error_response(
                400,
                "validation_failed",
                f"invalid relational_conditions: {exc}",
                field="relational_conditions",
            )
    elif body.template_id:
        # Inherit relational_conditions from the template if present.
        tpl_raw = await r.get(_k_voucher_template(issuer_brand_id, body.template_id))
        if tpl_raw:
            try:
                tpl = json.loads(tpl_raw)
                tpl_rel = tpl.get("relational_conditions")
                if isinstance(tpl_rel, dict) and tpl_rel:
                    rel_cond_dict = RelationalConditions(
                        **tpl_rel
                    ).model_dump(exclude_none=True)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

    voucher: dict[str, str] = {
        "voucher_id": vid,
        "template_id": body.template_id or "",
        "issuer_brand_id": issuer_brand_id,
        "issuer_master_id": master_id or "",
        "holder_user_id": body.user_id,
        "holder_type": body.holder_type,
        "original_holder_user_id": body.user_id,
        "redeemable_at": _dumps(resolved_redeemable),
        "value_cents": str(body.value_cents if body.value_cents is not None else 0),
        "residual_cents": str(body.value_cents if body.value_cents is not None else 0),
        "conditions": _dumps(body.conditions or {}),
        "relational_conditions": _dumps(rel_cond_dict),
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
    """Decode the stored ``redeemable_at`` field into a normalised shape.

    Supported shapes (post-cleanup):
      * ``"issuer_only"``
      * ``"any_in_master:{master_id}"``
      * ``list[brand_id]`` (JSON-encoded on disk)

    The legacy ``"partnership:{pid}"`` scheme is removed; if a stale
    voucher hash from before this cleanup still carries one, we surface it
    untouched so the redeem path can reject it with ``unknown_redeemable_at``
    rather than silently honouring it.
    """
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
    """Return ``(allowed, reject_reason, network_meta)``.

    Three legal cross-brand paths (in order of frequency):

    1. ``issuer_only`` — cross-brand always rejected.
    2. ``any_in_master:{mid}`` — intra-master multi-store chain. The
       master's voucher_network policy is consulted; KiX takes its
       configured intra-master commission (default 0%).
    3. ``list[brand_id]`` — explicit allow-list set by the issuer. This
       is **rare** (mostly co-marketing): a voucher issued by Brand A
       redeemed at Brand B. KiX takes its standard CPS commission.
       Brand A gets nothing automatic — the voucher was a gift, not an
       auction win. Cross-master entries in the list use
       ``DEFAULT_CROSS_MASTER_COMMISSION_BPS``.

    Partnership-based redemption (``"partnership:{pid}"``) is **removed**.
    Any stale stored voucher carrying this scheme is rejected here with
    ``unknown_redeemable_at`` and must be re-issued under one of the
    three legal shapes above.
    """
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


# ── Relational predicate evaluator ────────────────────────────────────────
#
# Redis key conventions consumed (created by sibling modules; missing keys
# evaluate as "empty set" and conditions that require them will reject):
#
#   user:{uid}:relationship_by_type:{type}  SET    related user_ids
#   user:{uid}:relationship_added_at:{type} HASH   {related_uid: ts_added}
#   user:{uid}:household                    SET    user_ids in the household
#   user:{uid}:purchases                    ZSET   score=ts, member=order_id
#   user:{uid}:audiences                    SET    audience_ids (see audiences.py)
#   brand:{bid}:users                       SET    enrolled users (see attribution.py)
#   master:{mid}:brands                     SET    brands in master
#   brand:{bid}:master                      STR    master_id for brand
#
# When a relational condition requires inspecting state that does not yet
# exist (e.g. household tracking not implemented), the call returns
# (False, "<missing_data_reason>") so that merchants cannot accidentally
# auto-redeem on missing data.

async def _evaluate_relational_conditions(
    r: aioredis.Redis,
    *,
    voucher: dict[str, str],
    redeemer_user_id: str,
    at_brand_id: str,
) -> tuple[bool, str | None, list[str]]:
    """Evaluate household / bundle / cohort / temporal predicates.

    Returns ``(ok, first_blocker, passed_checks)``.  ``passed_checks`` is
    populated for use by the simulate endpoint so the merchant UI can show
    which predicates were satisfied.
    """
    raw_cond = voucher.get("relational_conditions") or "{}"
    cond = _safe_loads(raw_cond, {}) or {}
    if not cond:
        return True, None, []

    passes: list[str] = []
    now = _now()

    # ── Household / family ─────────────────────────────────────────────
    if cond.get("min_children_count") is not None:
        need = int(cond["min_children_count"])
        children = await r.smembers(
            f"user:{redeemer_user_id}:relationship_by_type:parent_of"
        )
        have = len(children or [])
        if have < need:
            return False, f"requires {need} children, has {have}", passes
        passes.append(f"min_children_count>={need}")

    if cond.get("min_household_members") is not None:
        need = int(cond["min_household_members"])
        household = await r.smembers(f"user:{redeemer_user_id}:household")
        have = len(household or [])
        if have < need:
            return False, f"requires {need} household members, has {have}", passes
        passes.append(f"min_household_members>={need}")

    if cond.get("sibling_discount"):
        siblings = await r.smembers(
            f"user:{redeemer_user_id}:relationship_by_type:sibling"
        )
        if not siblings:
            return False, "no siblings on record", passes
        master_id = await r.get(_k_brand_master(at_brand_id))
        # Check whether at least one sibling is an enrolled member of the
        # brand or any brand in the same master.
        if master_id:
            master_brands = await _master_brands(r, master_id)
        else:
            master_brands = {at_brand_id}
        found_enrolled = False
        for sib in siblings:
            for bid in master_brands:
                if await r.sismember(f"brand:{bid}:users", sib):
                    found_enrolled = True
                    break
            if found_enrolled:
                break
        if not found_enrolled:
            return False, "no sibling is brand member", passes
        passes.append("sibling_discount")

    rel_type = cond.get("relationship_type_required")
    if rel_type:
        related = await r.smembers(
            f"user:{redeemer_user_id}:relationship_by_type:{rel_type}"
        )
        if not related:
            return False, f"missing relationship_type={rel_type}", passes
        passes.append(f"relationship_type_required={rel_type}")

    # ── Bundle / multi-item ────────────────────────────────────────────
    bundle_required = cond.get("bundle_required") or []
    if bundle_required:
        # Bounded scan: cap at 1000 vouchers per holder for bundle membership.
        user_vouchers = await r.zrange(
            _k_user_vouchers(redeemer_user_id), 0, 999
        )
        # Also check vouchers held by the original holder if different
        holder = voucher.get("holder_user_id", "")
        if holder and holder != redeemer_user_id:
            user_vouchers = list(user_vouchers or []) + list(
                await r.zrange(_k_user_vouchers(holder), 0, 999) or []
            )
        held_templates: set[str] = set()
        for vid in user_vouchers or []:
            v = await r.hgetall(_k_voucher(vid))
            if not v:
                continue
            if v.get("status") in ("issued", "claimed", "redeemed"):
                tid = v.get("template_id") or ""
                if tid:
                    held_templates.add(tid)
        for tid in bundle_required:
            if tid not in held_templates:
                return False, f"missing required bundle item: {tid}", passes
        passes.append(f"bundle_required={len(bundle_required)}")

    if cond.get("min_bundle_count") is not None:
        need = int(cond["min_bundle_count"])
        # Count vouchers across the bundle_required set, or all active
        # vouchers if no bundle_required specified.
        target_templates = set(bundle_required) if bundle_required else None
        user_vouchers = await r.zrange(
            _k_user_vouchers(redeemer_user_id), 0, -1
        )
        count = 0
        for vid in user_vouchers or []:
            v = await r.hgetall(_k_voucher(vid))
            if not v:
                continue
            if v.get("status") not in ("issued", "claimed", "redeemed"):
                continue
            tid = v.get("template_id") or ""
            if target_templates is None or tid in target_templates:
                count += 1
        if count < need:
            return False, f"min_bundle_count {need}, has {count}", passes
        passes.append(f"min_bundle_count>={need}")

    # ── Group / cohort ─────────────────────────────────────────────────
    if cond.get("same_master_member"):
        issuer = voucher.get("issuer_brand_id", "")
        issuer_master = await r.get(_k_brand_master(issuer)) if issuer else None
        if not issuer_master:
            return False, "issuer brand has no master", passes
        # Redeemer must be enrolled in at least one brand of the issuer's
        # master.
        master_brands = await _master_brands(r, issuer_master)
        is_member = False
        for bid in master_brands:
            if await r.sismember(f"brand:{bid}:users", redeemer_user_id):
                is_member = True
                break
        if not is_member:
            return False, "redeemer not in issuer master", passes
        passes.append("same_master_member")

    aud_required = cond.get("same_audience_required")
    if aud_required:
        in_aud = await r.sismember(
            f"audience:{aud_required}:members", redeemer_user_id
        )
        if not in_aud:
            return False, f"not in audience {aud_required}", passes
        passes.append(f"same_audience_required={aud_required}")

    # ── Time / sequence ────────────────────────────────────────────────
    if cond.get("after_purchase_within_days") is not None:
        days = int(cond["after_purchase_within_days"])
        window_start = now - days * 86400
        # Most recent purchase (highest score)
        latest = await r.zrevrange(
            f"user:{redeemer_user_id}:purchases", 0, 0, withscores=True
        )
        if not latest:
            return False, "no purchase on record", passes
        try:
            last_ts = int(latest[0][1])
        except (IndexError, TypeError, ValueError):
            return False, "purchase timestamp unreadable", passes
        if last_ts < window_start:
            return False, (
                f"last purchase older than {days}d"
            ), passes
        passes.append(f"after_purchase_within_days<={days}")

    if cond.get("after_relationship_added_days") is not None:
        days = int(cond["after_relationship_added_days"])
        threshold = now - days * 86400
        # The user's relationships must have been added at least `days` ago
        # (anti-gaming: someone can't add a child today and claim the voucher).
        rel_type_check = rel_type or "parent_of"
        added_map = await r.hgetall(
            f"user:{redeemer_user_id}:relationship_added_at:{rel_type_check}"
        )
        if not added_map:
            return False, "no relationship add-timestamp recorded", passes
        # Require at least one relationship that crossed the threshold.
        eligible = False
        for _related_uid, ts_str in (added_map or {}).items():
            try:
                if int(ts_str) <= threshold:
                    eligible = True
                    break
            except (TypeError, ValueError):
                continue
        if not eligible:
            return False, (
                f"relationship added less than {days}d ago"
            ), passes
        passes.append(f"after_relationship_added_days>={days}")

    # ── Cumulative ─────────────────────────────────────────────────────
    if cond.get("cumulative_spend_min_cents") is not None:
        need = int(cond["cumulative_spend_min_cents"])
        master_id = await r.get(_k_brand_master(at_brand_id))
        if master_id:
            brand_ids = await _master_brands(r, master_id)
        else:
            brand_ids = {at_brand_id}
        # Paginated aggregation: cumulative spend may span thousands of events.
        MAX_PAGE = 1000
        total = 0
        for bid in brand_ids:
            cursor = 0
            while True:
                events = await r.zrange(
                    f"brand:{bid}:attr_incoming",
                    cursor,
                    cursor + MAX_PAGE - 1,
                )
                if not events:
                    break
                for eid in events:
                    e = await r.hgetall(f"attr:{eid}")
                    if not e:
                        continue
                    if e.get("user_id") != redeemer_user_id:
                        continue
                    if e.get("stage") != "conversion":
                        continue
                    try:
                        total += int(e.get("value_cents", 0) or 0)
                    except (TypeError, ValueError):
                        continue
                if len(events) < MAX_PAGE:
                    break
                cursor += MAX_PAGE
        if total < need:
            return False, (
                f"cumulative spend {total} < required {need}"
            ), passes
        passes.append(f"cumulative_spend_min_cents>={need}")

    if cond.get("visit_count_min") is not None:
        need = int(cond["visit_count_min"])
        master_id = await r.get(_k_brand_master(at_brand_id))
        if master_id:
            brand_ids = await _master_brands(r, master_id)
        else:
            brand_ids = {at_brand_id}
        visit_count = 0
        for bid in brand_ids:
            # Visits are recorded as members of a per-user-per-brand set or
            # zset.  Try the zset form first (visit timestamps).
            n = await r.zcard(f"brand:{bid}:visits:{redeemer_user_id}")
            if not n:
                # Fall back to attribution events count.
                events = await r.zrange(
                    f"brand:{bid}:attr_incoming", 0, -1
                )
                for eid in events or []:
                    e = await r.hgetall(f"attr:{eid}")
                    if e and e.get("user_id") == redeemer_user_id:
                        visit_count += 1
            else:
                visit_count += int(n)
        if visit_count < need:
            return False, (
                f"visits {visit_count} < required {need}"
            ), passes
        passes.append(f"visit_count_min>={need}")

    return True, None, passes


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

                # Relational conditions (household / bundle / cohort / etc.)
                rel_ok, rel_reason, _rel_passes = await _evaluate_relational_conditions(
                    r,
                    voucher=voucher,
                    redeemer_user_id=body.redeemer_user_id,
                    at_brand_id=body.at_brand_id,
                )
                if not rel_ok:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "ok": False,
                            "reason": "relational_condition_failed",
                            "blocker": rel_reason,
                        },
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

    # Outbound webhook fan-out — best-effort, never break redemption path.
    try:
        from app.routers.webhooks_outbound import fan_out_webhook_to_brand
        issuer_brand = (voucher or {}).get("issuer_brand_id", "")
        if issuer_brand:
            await fan_out_webhook_to_brand(
                issuer_brand,
                "voucher.redeemed",
                {
                    "voucher_id": voucher_id,
                    "at_brand_id": body.at_brand_id,
                    "redeemer_user_id": body.redeemer_user_id,
                    "holder_user_id": (voucher or {}).get("holder_user_id"),
                    "value_applied_cents": event["value_applied_cents"],
                    "residual_cents_after": event["residual_cents_after"],
                    "is_cross_brand": event["is_cross_brand"],
                    "commission_cents": event["commission_cents"],
                    "order_id": body.order_id,
                    "new_status": new_status,
                },
                r,
            )
        if (
            event["is_cross_brand"]
            and body.at_brand_id
            and body.at_brand_id != issuer_brand
        ):
            await fan_out_webhook_to_brand(
                body.at_brand_id,
                "voucher.redeemed",
                {
                    "voucher_id": voucher_id,
                    "issuer_brand_id": issuer_brand,
                    "redeemer_user_id": body.redeemer_user_id,
                    "value_applied_cents": event["value_applied_cents"],
                    "is_cross_brand": True,
                    "order_id": body.order_id,
                },
                r,
            )
    except Exception as _exc:  # pragma: no cover
        logger.debug("webhook fan-out (voucher.redeemed) failed: %s", _exc)

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
    out["relational_conditions"] = _safe_loads(
        state.get("relational_conditions"), {}
    )
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
    # api_standards: list_response envelope merged with legacy fields
    # (brand_id, vouchers) so existing clients keep working.
    envelope = list_response(items=out, total=len(out), limit=limit, offset=0)
    return {"brand_id": brand_id, "vouchers": out, **envelope}


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


# ══════════════════════════════════════════════════════════════════════════
# 5-minute reservation + claim flow (anonymous device_fp → registered kid)
# ══════════════════════════════════════════════════════════════════════════
#
# This solves the 80%→20% funnel loss in the QR→game→voucher flow: when a
# user wins a voucher anonymously (via device_fp), the voucher is held in a
# short-lived reservation until they either claim it (binding a phone/kid)
# or 5 minutes elapse and the reservation is released back to "issued".
#
# Redis schema additions:
#
#   voucher:{vid}:reservation         HASH  {device_fp|kid, reserved_at,
#                                            expires_at, reservation_token}
#                                     EXPIRE = ttl_seconds (default 300)
#   device:{fp}:reserved_vouchers     SET   of vid
#   kid:{kid}:reserved_vouchers       SET   of vid
#   voucher_reservation_token:{tok}   STR   → vid, EXPIRE = ttl_seconds
#

DEFAULT_RESERVATION_TTL_SECONDS = 300
MAX_RESERVATION_TTL_SECONDS = 3600
MIN_RESERVATION_TTL_SECONDS = 30


def _k_voucher_reservation(vid: str) -> str:
    return f"voucher:{vid}:reservation"


def _k_device_reserved(fp: str) -> str:
    return f"device:{fp}:reserved_vouchers"


def _k_kid_reserved(kid: str) -> str:
    return f"kid:{kid}:reserved_vouchers"


def _k_reservation_token(token: str) -> str:
    return f"voucher_reservation_token:{token}"


def _new_reservation_token() -> str:
    return uuid4().hex


class ReserveVoucherRequest(BaseModel):
    device_fingerprint: str | None = Field(None, min_length=1, max_length=128)
    kid: str | None = Field(None, min_length=1, max_length=128)
    ttl_seconds: int = Field(
        DEFAULT_RESERVATION_TTL_SECONDS,
        ge=MIN_RESERVATION_TTL_SECONDS,
        le=MAX_RESERVATION_TTL_SECONDS,
    )

    @field_validator("kid")
    @classmethod
    def _at_least_one_holder(cls, v: str | None, info: Any) -> str | None:
        fp = (info.data or {}).get("device_fingerprint")
        if not fp and not v:
            raise ValueError(
                "device_fingerprint or kid required"
            )
        return v


class ClaimVoucherRequest(BaseModel):
    device_fingerprint: str | None = Field(None, min_length=1, max_length=128)
    kid: str | None = Field(None, min_length=1, max_length=128)
    phone: str | None = Field(None, min_length=1, max_length=64)
    otp: str | None = Field(None, min_length=1, max_length=32)
    email: str | None = Field(None, min_length=1, max_length=128)

    @field_validator("kid")
    @classmethod
    def _at_least_one_holder(cls, v: str | None, info: Any) -> str | None:
        fp = (info.data or {}).get("device_fingerprint")
        if not fp and not v:
            raise ValueError(
                "device_fingerprint or kid required"
            )
        return v


class ReleaseReservationRequest(BaseModel):
    device_fingerprint: str | None = Field(None, min_length=1, max_length=128)
    kid: str | None = Field(None, min_length=1, max_length=128)

    @field_validator("kid")
    @classmethod
    def _at_least_one_holder(cls, v: str | None, info: Any) -> str | None:
        fp = (info.data or {}).get("device_fingerprint")
        if not fp and not v:
            raise ValueError(
                "device_fingerprint or kid required"
            )
        return v


def _holder_matches(
    reservation: dict[str, str],
    device_fp: str | None,
    kid: str | None,
) -> bool:
    """True iff the reservation was made by the same fp or kid presented."""
    res_fp = reservation.get("device_fp") or ""
    res_kid = reservation.get("kid") or ""
    if device_fp and res_fp and device_fp == res_fp:
        return True
    if kid and res_kid and kid == res_kid:
        return True
    return False


async def _load_reservation(
    r: aioredis.Redis, vid: str
) -> dict[str, str] | None:
    data = await r.hgetall(_k_voucher_reservation(vid))
    if not data:
        return None
    return dict(data)


@cross_store_router.post(
    "/{voucher_id}/reserve",
    summary="Reserve a voucher for 5 minutes (game-win → claim flow)",
)
async def reserve_voucher(
    voucher_id: str,
    body: ReserveVoucherRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Hold a freshly-issued voucher for the device/kid that won it.

    Idempotent: a same-fp / same-kid re-call extends the TTL and returns
    the existing reservation_token.
    """
    key = _k_voucher(voucher_id)

    for _attempt in range(5):
        async with r.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                voucher = await pipe.hgetall(key)
                if not voucher:
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=404,
                        detail=f"voucher_id={voucher_id} not found",
                    )

                cur_status = voucher.get("status", "")
                existing_res = await r.hgetall(_k_voucher_reservation(voucher_id))

                if cur_status == "reserved" and existing_res:
                    # Idempotent: same holder extends TTL.
                    if not _holder_matches(
                        existing_res, body.device_fingerprint, body.kid
                    ):
                        await pipe.unwatch()
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "ok": False,
                                "reason": "already_reserved_by_other",
                                "voucher_id": voucher_id,
                            },
                        )
                    # Extend TTL on the same reservation.
                    now = _now()
                    new_expires_at = now + body.ttl_seconds
                    token = existing_res.get("reservation_token") or _new_reservation_token()
                    pipe.multi()
                    pipe.hset(
                        _k_voucher_reservation(voucher_id),
                        mapping={
                            "reserved_at": existing_res.get("reserved_at", str(now)),
                            "expires_at": str(new_expires_at),
                            "reservation_token": token,
                        },
                    )
                    pipe.expire(_k_voucher_reservation(voucher_id), body.ttl_seconds)
                    pipe.set(
                        _k_reservation_token(token), voucher_id,
                        ex=body.ttl_seconds,
                    )
                    pipe.hset(
                        key,
                        mapping={
                            "reservation_expires_at": str(new_expires_at),
                        },
                    )
                    await pipe.execute()
                    return {
                        "ok": True,
                        "voucher_id": voucher_id,
                        "status": "reserved",
                        "reservation_token": token,
                        "expires_at": new_expires_at,
                        "expires_at_iso": _iso(new_expires_at),
                        "extended": True,
                    }

                if cur_status != "issued":
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "ok": False,
                            "reason": "invalid_status",
                            "current_status": cur_status,
                            "voucher_id": voucher_id,
                        },
                    )

                # Status is "issued" — create new reservation.
                now = _now()
                expires_at = now + body.ttl_seconds
                token = _new_reservation_token()

                res_hash: dict[str, str] = {
                    "reserved_at": str(now),
                    "expires_at": str(expires_at),
                    "reservation_token": token,
                }
                if body.device_fingerprint:
                    res_hash["device_fp"] = body.device_fingerprint
                if body.kid:
                    res_hash["kid"] = body.kid

                voucher_update: dict[str, str] = {
                    "status": "reserved",
                    "reserved_at": str(now),
                    "reservation_expires_at": str(expires_at),
                }
                if body.device_fingerprint:
                    voucher_update["reserved_for_device_fp"] = body.device_fingerprint
                if body.kid:
                    voucher_update["reserved_for_kid"] = body.kid

                pipe.multi()
                pipe.hset(key, mapping=voucher_update)
                pipe.hset(
                    _k_voucher_reservation(voucher_id), mapping=res_hash
                )
                pipe.expire(_k_voucher_reservation(voucher_id), body.ttl_seconds)
                pipe.set(
                    _k_reservation_token(token), voucher_id,
                    ex=body.ttl_seconds,
                )
                if body.device_fingerprint:
                    pipe.sadd(
                        _k_device_reserved(body.device_fingerprint),
                        voucher_id,
                    )
                    pipe.expire(
                        _k_device_reserved(body.device_fingerprint),
                        max(body.ttl_seconds * 2, 86400),
                    )
                if body.kid:
                    pipe.sadd(_k_kid_reserved(body.kid), voucher_id)
                    pipe.expire(
                        _k_kid_reserved(body.kid),
                        max(body.ttl_seconds * 2, 86400),
                    )
                await pipe.execute()
                logger.info(
                    "Voucher reserved: vid=%s holder=%s ttl=%ds",
                    voucher_id,
                    body.device_fingerprint or body.kid,
                    body.ttl_seconds,
                )
                return {
                    "ok": True,
                    "voucher_id": voucher_id,
                    "status": "reserved",
                    "reservation_token": token,
                    "expires_at": expires_at,
                    "expires_at_iso": _iso(expires_at),
                    "extended": False,
                }
            except aioredis.WatchError:
                continue
            except HTTPException:
                raise
    raise HTTPException(
        status_code=503, detail="reserve_contention_exceeded_retries"
    )


@cross_store_router.post(
    "/{voucher_id}/claim",
    summary="Claim a reserved voucher (bind to kid; optional phone upgrade)",
)
async def claim_voucher(
    voucher_id: str,
    body: ClaimVoucherRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Bind an active reservation to a registered kid.

    If a ``phone`` (+ ``otp``) is supplied, the anonymous device_fp is
    upgraded to a registered kid via ``ensure_kid`` from kix_id (which is
    the identity-link entry point for anon→registered upgrades).
    """
    key = _k_voucher(voucher_id)
    reservation = await _load_reservation(r, voucher_id)
    if not reservation:
        raise HTTPException(
            status_code=404,
            detail={
                "ok": False,
                "reason": "no_active_reservation",
                "voucher_id": voucher_id,
            },
        )
    if not _holder_matches(reservation, body.device_fingerprint, body.kid):
        raise HTTPException(
            status_code=403,
            detail={
                "ok": False,
                "reason": "reservation_holder_mismatch",
                "voucher_id": voucher_id,
            },
        )

    voucher = await r.hgetall(key)
    if not voucher:
        raise HTTPException(
            status_code=404, detail=f"voucher_id={voucher_id} not found"
        )
    if voucher.get("status") != "reserved":
        raise HTTPException(
            status_code=409,
            detail={
                "ok": False,
                "reason": "invalid_status",
                "current_status": voucher.get("status", ""),
                "voucher_id": voucher_id,
            },
        )

    # Resolve / mint the kid to bind to.
    target_kid: str | None = body.kid
    is_new_kid = False
    device_fp_used = body.device_fingerprint or reservation.get("device_fp") or ""

    if body.phone:
        # Phone upgrade path: anonymous device → registered kid.
        if not body.otp:
            raise HTTPException(
                status_code=400,
                detail="otp required when phone is supplied",
            )
        try:
            from app.routers.kix_id import ensure_kid  # local import to avoid cycle
        except Exception as exc:  # pragma: no cover
            raise HTTPException(
                status_code=500, detail=f"identity link unavailable: {exc}"
            )
        target_kid, is_new_kid = await ensure_kid(
            r,
            phone=body.phone,
            email=body.email,
            device_fp=device_fp_used or None,
        )
    elif not target_kid:
        # No phone, no kid — fall back to a device-bound synthetic kid.
        # We only do this if the reservation was made by device_fp; the
        # caller is asserting they don't need phone-level identity yet.
        if not device_fp_used:
            raise HTTPException(
                status_code=400,
                detail=(
                    "must provide one of {kid, phone+otp} to claim"
                ),
            )
        try:
            from app.routers.kix_id import ensure_kid
        except Exception as exc:  # pragma: no cover
            raise HTTPException(
                status_code=500, detail=f"identity link unavailable: {exc}"
            )
        target_kid, is_new_kid = await ensure_kid(r, device_fp=device_fp_used)

    if not target_kid:  # defensive
        raise HTTPException(
            status_code=500, detail="failed to resolve target kid"
        )

    now = _now()
    prior_holder = voucher.get("holder_user_id", "")
    issued_at_score = int(voucher.get("issued_at", now) or now)
    token = reservation.get("reservation_token") or ""

    pipe = r.pipeline()
    pipe.hset(
        key,
        mapping={
            "status": "claimed",
            "holder_user_id": target_kid,
            "holder_type": "kid",
            "claimed_at": str(now),
            "claimed_via_device_fp": device_fp_used,
            "reservation_expires_at": "",
            "reserved_for_device_fp": "",
            "reserved_for_kid": "",
        },
    )
    # Re-anchor the user-vouchers zset onto the registered kid.
    pipe.zadd(_k_user_vouchers(target_kid), {voucher_id: issued_at_score})
    if prior_holder and prior_holder != target_kid:
        pipe.zrem(_k_user_vouchers(prior_holder), voucher_id)
    # Clear reservation state.
    pipe.delete(_k_voucher_reservation(voucher_id))
    if token:
        pipe.delete(_k_reservation_token(token))
    if device_fp_used:
        pipe.srem(_k_device_reserved(device_fp_used), voucher_id)
    res_kid_field = reservation.get("kid") or ""
    if res_kid_field:
        pipe.srem(_k_kid_reserved(res_kid_field), voucher_id)
    # Append claim event to redemption history (audit trail).
    pipe.rpush(
        _k_voucher_redemption_history(voucher_id),
        _dumps({
            "type": "claim",
            "voucher_id": voucher_id,
            "kid": target_kid,
            "is_new_kid": is_new_kid,
            "device_fp_used": device_fp_used,
            "phone_provided": bool(body.phone),
            "ts": now,
        }),
    )
    await pipe.execute()

    # Hooks (fail-soft)
    await _fire_pixel(
        r,
        brand_id=voucher.get("issuer_brand_id", ""),
        user_id=target_kid,
        event="voucher_claimed",
        meta={
            "voucher_id": voucher_id,
            "is_new_kid": is_new_kid,
            "via_phone": bool(body.phone),
        },
    )
    await _enqueue_notification(
        r, target_kid, kind="voucher_claimed",
        payload={
            "voucher_id": voucher_id,
            "issuer_brand_id": voucher.get("issuer_brand_id", ""),
        },
    )

    # Return enriched voucher dict.
    new_state = await r.hgetall(key)
    return {
        "ok": True,
        "voucher_id": voucher_id,
        "status": "claimed",
        "kid": target_kid,
        "is_new_kid": is_new_kid,
        "voucher": _voucher_to_dict(new_state),
    }


@cross_store_router.post(
    "/{voucher_id}/release",
    summary="Release a voucher reservation (user clicked discard)",
)
async def release_voucher_reservation(
    voucher_id: str,
    body: ReleaseReservationRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return a reserved voucher to ``issued`` so it can be re-claimed."""
    key = _k_voucher(voucher_id)
    reservation = await _load_reservation(r, voucher_id)
    if not reservation:
        raise HTTPException(
            status_code=404,
            detail={
                "ok": False,
                "reason": "no_active_reservation",
                "voucher_id": voucher_id,
            },
        )
    if not _holder_matches(reservation, body.device_fingerprint, body.kid):
        raise HTTPException(
            status_code=403,
            detail={
                "ok": False,
                "reason": "reservation_holder_mismatch",
                "voucher_id": voucher_id,
            },
        )

    voucher = await r.hgetall(key)
    if not voucher:
        raise HTTPException(
            status_code=404, detail=f"voucher_id={voucher_id} not found"
        )
    if voucher.get("status") != "reserved":
        # Reservation already torn down / voucher mutated elsewhere — treat
        # as a no-op release rather than 409.
        await r.delete(_k_voucher_reservation(voucher_id))
        return {
            "ok": True,
            "voucher_id": voucher_id,
            "status": voucher.get("status", ""),
            "released": False,
            "noop": True,
        }

    token = reservation.get("reservation_token") or ""
    device_fp = reservation.get("device_fp") or ""
    res_kid = reservation.get("kid") or ""

    pipe = r.pipeline()
    pipe.hset(
        key,
        mapping={
            "status": "issued",
            "reserved_at": "",
            "reservation_expires_at": "",
            "reserved_for_device_fp": "",
            "reserved_for_kid": "",
            "reservation_released_at": str(_now()),
        },
    )
    pipe.delete(_k_voucher_reservation(voucher_id))
    if token:
        pipe.delete(_k_reservation_token(token))
    if device_fp:
        pipe.srem(_k_device_reserved(device_fp), voucher_id)
    if res_kid:
        pipe.srem(_k_kid_reserved(res_kid), voucher_id)
    await pipe.execute()

    return {
        "ok": True,
        "voucher_id": voucher_id,
        "status": "issued",
        "released": True,
    }


@cross_store_router.get(
    "/reserved/by-device/{device_fingerprint}",
    summary="List active reservations for a device",
)
async def list_reservations_by_device(
    device_fingerprint: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    return await _list_reservations(
        r, key=_k_device_reserved(device_fingerprint),
        holder_field="device_fp", holder_value=device_fingerprint,
    )


@cross_store_router.get(
    "/reserved/by-kid/{kid}",
    summary="List active reservations for a kid",
)
async def list_reservations_by_kid(
    kid: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    return await _list_reservations(
        r, key=_k_kid_reserved(kid),
        holder_field="kid", holder_value=kid,
    )


async def _list_reservations(
    r: aioredis.Redis,
    *,
    key: str,
    holder_field: str,
    holder_value: str,
) -> dict[str, Any]:
    """Return only reservations that are still alive (TTL not expired).

    The voucher hash may still say ``status=reserved`` even after the
    reservation HASH has been evicted by Redis TTL; we treat the absence
    of the reservation HASH as authoritative and prune the membership set
    in passing.
    """
    vids = await r.smembers(key)
    alive: list[dict[str, Any]] = []
    stale: list[str] = []
    for vid in vids or []:
        res = await r.hgetall(_k_voucher_reservation(vid))
        if not res:
            stale.append(vid)
            continue
        # Cross-check the holder.
        if res.get(holder_field) != holder_value:
            stale.append(vid)
            continue
        try:
            expires_at = int(res.get("expires_at", "0") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        if expires_at and expires_at <= _now():
            stale.append(vid)
            continue
        state = await r.hgetall(_k_voucher(vid))
        if not state:
            stale.append(vid)
            continue
        v = _voucher_to_dict(state)
        v["reservation"] = {
            "reserved_at": int(res.get("reserved_at", "0") or 0),
            "expires_at": expires_at,
            "expires_at_iso": _iso(expires_at),
            "reservation_token": res.get("reservation_token", ""),
        }
        alive.append(v)
    # Prune membership set lazily.
    if stale:
        await r.srem(key, *stale)
    return {
        holder_field: holder_value,
        "count": len(alive),
        "reservations": alive,
    }


@cross_store_router.post(
    "/admin/cleanup-expired-reservations",
    summary="Release expired reservations back to issued (admin)",
)
async def cleanup_expired_reservations(
    body: CleanupRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Scan for expired reservations and release vouchers back to ``issued``.

    Idempotent. Two failure modes are healed:

    1. Reservation HASH still alive but ``expires_at <= now`` (timer not
       yet fired — should not happen with Redis EXPIRE but is possible
       under clock skew or manual ttl edits).
    2. Reservation HASH evicted by TTL but voucher still says
       ``status=reserved`` (the common case — Redis EXPIRE cleared the
       reservation but did not roll back the voucher hash).
    """
    from app.config import settings  # local to avoid top-level coupling
    from app.security import constant_time_eq

    expected = getattr(settings, "admin_token", None)
    if expected and not constant_time_eq(body.admin_token, expected):
        raise HTTPException(status_code=403, detail="invalid admin_token")

    scanned = 0
    released: list[str] = []
    now = _now()
    cursor = 0
    while True:
        cursor, keys = await r.scan(cursor=cursor, match="voucher:*", count=200)
        for k in keys:
            # Only voucher HASHes, not sub-keys.
            if k.count(":") != 1:
                continue
            scanned += 1
            if scanned > body.limit:
                cursor = 0
                break
            state = await r.hgetall(k)
            if not state:
                continue
            if state.get("status") != "reserved":
                continue
            vid = state.get("voucher_id") or k.split(":", 1)[1]
            res = await r.hgetall(_k_voucher_reservation(vid))
            is_expired = False
            if not res:
                # Reservation HASH evicted; voucher still says reserved.
                is_expired = True
            else:
                try:
                    exp_ts = int(res.get("expires_at", "0") or 0)
                except (TypeError, ValueError):
                    exp_ts = 0
                if exp_ts and exp_ts <= now:
                    is_expired = True
            if not is_expired:
                continue
            released.append(vid)
            if body.dry_run:
                continue
            # Release: roll status back to issued + tear down side state.
            token = (res or {}).get("reservation_token") or ""
            device_fp = (res or {}).get("device_fp") or state.get(
                "reserved_for_device_fp", ""
            )
            res_kid = (res or {}).get("kid") or state.get(
                "reserved_for_kid", ""
            )
            pipe = r.pipeline()
            pipe.hset(
                k,
                mapping={
                    "status": "issued",
                    "reservation_expires_at": "",
                    "reserved_for_device_fp": "",
                    "reserved_for_kid": "",
                    "reservation_released_at": str(now),
                    "reservation_release_reason": "expired",
                },
            )
            pipe.delete(_k_voucher_reservation(vid))
            if token:
                pipe.delete(_k_reservation_token(token))
            if device_fp:
                pipe.srem(_k_device_reserved(device_fp), vid)
            if res_kid:
                pipe.srem(_k_kid_reserved(res_kid), vid)
            await pipe.execute()
        if cursor == 0:
            break
    logger.info(
        "Reservation cleanup: dry_run=%s scanned=%d released=%d",
        body.dry_run, scanned, len(released),
    )
    return {
        "ok": True,
        "dry_run": body.dry_run,
        "scanned": scanned,
        "released": len(released),
        "released_voucher_ids": released[:200],
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
    from app.security import constant_time_eq

    expected = getattr(settings, "admin_token", None)
    if expected and not constant_time_eq(body.admin_token, expected):
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
# Relational-condition: simulate + template attachment
# ══════════════════════════════════════════════════════════════════════════

class SimulateRedeemRequest(BaseModel):
    at_brand_id: str = Field(..., min_length=1)
    redeemer_user_id: str = Field(..., min_length=1)
    order_amount_cents: int | None = Field(None, ge=0)


@cross_store_router.post(
    "/{voucher_id}/simulate-redeem",
    summary="Dry-run a voucher redemption (eligibility preview)",
)
async def simulate_redeem(
    voucher_id: str,
    body: SimulateRedeemRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Evaluate every gate that ``redeem`` would evaluate, without mutating.

    Returns which predicates pass and which block, so a merchant UI can show
    the customer exactly why a voucher cannot yet be redeemed (e.g. needs one
    more child enrolled, or another bundle item).
    """
    state = await _load_voucher(r, voucher_id)
    blockers: list[str] = []
    passes: list[str] = []

    # Status
    status_now = state.get("status", "")
    if status_now not in ("issued", "claimed"):
        blockers.append(f"invalid_status:{status_now}")
    else:
        passes.append(f"status={status_now}")

    # Redeemable_at policy
    allowed, why, net_meta = await _validate_redeemable_at(
        r, voucher=state, at_brand_id=body.at_brand_id,
    )
    if allowed:
        passes.append("redeemable_at")
    else:
        blockers.append(f"redeemable_at:{why}")

    # Standard conditions (min purchase, expiry, max_uses)
    cond_ok, cond_reason = _check_conditions(state, body.order_amount_cents)
    if cond_ok:
        passes.append("conditions")
    else:
        blockers.append(f"conditions:{cond_reason}")

    # Relational conditions
    rel_ok, rel_blocker, rel_passes = await _evaluate_relational_conditions(
        r,
        voucher=state,
        redeemer_user_id=body.redeemer_user_id,
        at_brand_id=body.at_brand_id,
    )
    passes.extend(rel_passes)
    if not rel_ok and rel_blocker:
        blockers.append(f"relational:{rel_blocker}")

    return {
        "voucher_id": voucher_id,
        "would_succeed": not blockers,
        "blockers": blockers,
        "passes": passes,
        "network": net_meta,
    }


class TemplateConditionsRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    template_id: str = Field(..., min_length=1, max_length=64)
    relational_conditions: dict[str, Any] = Field(default_factory=dict)


@cross_store_router.post(
    "/templates/with-conditions",
    summary="Attach relational conditions to a voucher template",
)
async def attach_template_relational_conditions(
    body: TemplateConditionsRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Persist ``relational_conditions`` onto a voucher template.

    All future vouchers issued from this template inherit the conditions
    (see ``_do_issue``).  Existing voucher instances are not retro-modified.
    """
    # Validate schema first.
    try:
        normalized = RelationalConditions(
            **(body.relational_conditions or {})
        ).model_dump(exclude_none=True)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"invalid relational_conditions: {exc}",
        )

    key = _k_voucher_template(body.brand_id, body.template_id)
    raw = await r.get(key)
    if raw:
        try:
            tpl = json.loads(raw)
            if not isinstance(tpl, dict):
                tpl = {}
        except json.JSONDecodeError:
            tpl = {}
    else:
        # Create a minimal template record if none exists yet.
        tpl = {
            "template_id": body.template_id,
            "brand_id": body.brand_id,
            "created_at": _now(),
        }

    tpl["relational_conditions"] = normalized
    tpl["updated_at"] = _now()
    await r.set(key, _dumps(tpl))
    # Best-effort: register template in the brand's template set.
    try:
        await r.sadd(f"brand:{body.brand_id}:voucher_templates", body.template_id)
    except Exception as exc:  # pragma: no cover
        logger.debug("template set-register failed: %s", exc)

    logger.info(
        "Template %s/%s relational_conditions updated: keys=%s",
        body.brand_id, body.template_id, sorted(normalized.keys()),
    )
    return {
        "ok": True,
        "brand_id": body.brand_id,
        "template_id": body.template_id,
        "relational_conditions": normalized,
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


# ══════════════════════════════════════════════════════════════════════════
# Cancellation flows  (single + bulk)  +  bulk-issue cohort issuance
# ══════════════════════════════════════════════════════════════════════════

async def _do_cancel(
    r: aioredis.Redis, *, voucher_id: str, cancelled_by: str, reason: str,
    refund_to_user: bool | None,
) -> dict[str, Any]:
    """Cancel a single voucher.

    Semantics:
      * ``issued`` → ``cancelled`` (was a gift, no refund needed).
      * If voucher is reserved against an order (status==``claimed`` and
        ``reserved_against_order`` set), release the hold.
      * ``redeemed``/``void``/``expired``/``cancelled`` cannot be cancelled.
    """
    key = _k_voucher(voucher_id)
    state = await r.hgetall(key)
    if not state:
        raise HTTPException(
            status_code=404, detail=f"voucher_id={voucher_id} not found"
        )
    cur_status = state.get("status", "")
    if cur_status in ("redeemed", "void", "expired", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail={
                "ok": False, "reason": "invalid_status",
                "current_status": cur_status,
                "voucher_id": voucher_id,
            },
        )

    was_reserved = bool(state.get("reserved_against_order"))
    holder = state.get("holder_user_id", "")
    issuer = state.get("issuer_brand_id", "")
    cancel_ts = _now()

    pipe = r.pipeline()
    update: dict[str, str] = {
        "status": "cancelled",
        "cancelled_at": str(cancel_ts),
        "cancelled_by": cancelled_by,
        "cancel_reason": reason,
        "previous_status": cur_status,
    }
    if was_reserved:
        # Release the hold — strip the reservation marker.
        update["reserved_against_order"] = ""
        update["reservation_released_at"] = str(cancel_ts)
    pipe.hset(key, mapping=update)
    pipe.rpush(
        _k_voucher_redemption_history(voucher_id),
        _dumps({
            "type": "cancel",
            "voucher_id": voucher_id,
            "cancelled_by": cancelled_by,
            "reason": reason,
            "previous_status": cur_status,
            "was_reserved": was_reserved,
            "ts": cancel_ts,
        }),
    )
    if issuer:
        pipe.hincrby(_k_brand_stats(issuer), "cancelled", 1)
    await pipe.execute()

    # Best-effort push notification to holder.
    if holder:
        await _enqueue_notification(
            r, holder, kind="voucher_cancelled",
            payload={
                "voucher_id": voucher_id,
                "reason": reason,
                "cancelled_by": cancelled_by,
                "was_reserved": was_reserved,
                "refund_to_user": bool(refund_to_user) if refund_to_user is not None else False,
            },
        )

    return {
        "ok": True,
        "voucher_id": voucher_id,
        "status": "cancelled",
        "previous_status": cur_status,
        "was_reserved": was_reserved,
        "cancelled_by": cancelled_by,
    }


@cross_store_router.post(
    "/{voucher_id}/cancel",
    summary="Cancel a voucher (user / issuer / admin)",
)
async def cancel_voucher(
    voucher_id: str,
    body: VoucherCancelRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    return await _do_cancel(
        r,
        voucher_id=voucher_id,
        cancelled_by=body.cancelled_by,
        reason=body.reason,
        refund_to_user=body.refund_to_user,
    )


@cross_store_router.post(
    "/bulk-cancel",
    summary="Cancel up to 100 vouchers in one call",
)
async def bulk_cancel_vouchers(
    body: BulkCancelRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    cancelled_count = 0
    failed: list[dict[str, Any]] = []
    for vid in body.voucher_ids:
        try:
            await _do_cancel(
                r,
                voucher_id=vid,
                cancelled_by=body.cancelled_by,
                reason=body.reason,
                refund_to_user=None,
            )
            cancelled_count += 1
        except HTTPException as exc:
            failed.append({
                "vid": vid,
                "reason": exc.detail if isinstance(exc.detail, str)
                          else (exc.detail or {}).get("reason", "unknown")
                          if isinstance(exc.detail, dict) else str(exc.detail),
                "status_code": exc.status_code,
            })
        except Exception as exc:  # pragma: no cover
            failed.append({"vid": vid, "reason": f"internal:{exc}"})

    return {
        "ok": True,
        "cancelled_count": cancelled_count,
        "failed_count": len(failed),
        "failed": failed,
    }


@cross_store_router.post(
    "/bulk-issue",
    status_code=status.HTTP_201_CREATED,
    summary="Issue same voucher to a cohort (up to 1000 users)",
)
async def bulk_issue_vouchers(
    body: BulkIssueRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Issue the same voucher to N users.

    Per-user value can vary via ``values`` (must be same length as
    ``user_ids``); otherwise inherits the template's value or 0. Each issued
    voucher inherits the template policy (transferable, conditions,
    relational_conditions, commission_split). Partial failures are reported
    in ``failed`` so the caller can retry only those.
    """
    if body.values is not None and len(body.values) != len(body.user_ids):
        raise HTTPException(
            status_code=400,
            detail=(
                f"values length ({len(body.values)}) must equal "
                f"user_ids length ({len(body.user_ids)})"
            ),
        )

    # Inherit template default value if no per-user values supplied.
    template_default_value: int | None = None
    if body.values is None and body.template_id:
        tpl_raw = await r.get(
            _k_voucher_template(body.brand_id, body.template_id)
        )
        if tpl_raw:
            try:
                tpl = json.loads(tpl_raw)
                vobj = tpl.get("value") or {}
                if vobj.get("type") == "fixed":
                    template_default_value = int(
                        float(vobj.get("amount", 0)) * 100
                    )
                elif "default_value_cents" in tpl:
                    template_default_value = int(tpl["default_value_cents"])
            except (json.JSONDecodeError, TypeError, ValueError):
                template_default_value = None

    issued: list[str] = []
    failed: list[dict[str, Any]] = []

    for idx, uid in enumerate(body.user_ids):
        per_value = (
            body.values[idx]
            if body.values is not None
            else template_default_value
        )
        try:
            req = IssueVoucherRequest(
                template_id=body.template_id,
                user_id=uid,
                redeemable_at="issuer_only",
                value_cents=per_value,
                expires_at=body.expires_at,
                conditions=body.conditions,
                source=body.source,
                transferable=body.transferable,
                max_uses=body.max_uses,
            )
            res = await _do_issue(r, issuer_brand_id=body.brand_id, body=req)
            issued.append(res["voucher_id"])
        except HTTPException as exc:
            failed.append({
                "user_id": uid,
                "reason": (
                    exc.detail if isinstance(exc.detail, str)
                    else str(exc.detail)
                ),
                "status_code": exc.status_code,
            })
        except Exception as exc:  # pragma: no cover
            failed.append({"user_id": uid, "reason": f"internal:{exc}"})

    logger.info(
        "Bulk issue: brand=%s template=%s requested=%d issued=%d failed=%d",
        body.brand_id, body.template_id or "-",
        len(body.user_ids), len(issued), len(failed),
    )

    return {
        "ok": True,
        "issued_count": len(issued),
        "voucher_ids": issued,
        "failed_count": len(failed),
        "failed": failed,
    }


# ══════════════════════════════════════════════════════════════════════════
# Commission-split on voucher templates
# ══════════════════════════════════════════════════════════════════════════

class TemplateCommissionSplitRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    template_id: str = Field(..., min_length=1, max_length=64)
    commission_split: CommissionSplit


@cross_store_router.post(
    "/templates/commission-split",
    summary="Attach a custom commission split to a voucher template",
)
async def attach_template_commission_split(
    body: TemplateCommissionSplitRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Persist a ``commission_split`` on a voucher template.

    When a voucher minted from this template is redeemed cross-brand, the
    settlement engine looks up the template's split (issuer/kix/redeemer)
    rather than applying the default 30% KiX take. The default behaviour
    persists when no split is set: KiX takes 30%, redeemer keeps 70%,
    issuer gets nothing (the voucher was a gift).
    """
    split_dump = body.commission_split.model_dump()
    key = _k_voucher_template(body.brand_id, body.template_id)
    raw = await r.get(key)
    if raw:
        try:
            tpl = json.loads(raw)
            if not isinstance(tpl, dict):
                tpl = {}
        except json.JSONDecodeError:
            tpl = {}
    else:
        tpl = {
            "template_id": body.template_id,
            "brand_id": body.brand_id,
            "created_at": _now(),
        }

    tpl["commission_split"] = split_dump
    tpl["updated_at"] = _now()
    await r.set(key, _dumps(tpl))
    try:
        await r.sadd(
            f"brand:{body.brand_id}:voucher_templates", body.template_id
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("template set-register failed: %s", exc)

    logger.info(
        "Template %s/%s commission_split set: %s",
        body.brand_id, body.template_id, split_dump,
    )
    return {
        "ok": True,
        "brand_id": body.brand_id,
        "template_id": body.template_id,
        "commission_split": split_dump,
    }


# ══════════════════════════════════════════════════════════════════════════
# Cross-brand voucher-pool integration hook (Wave E item 4)
# ══════════════════════════════════════════════════════════════════════════
#
# This is the **minimal** integration surface between the legacy
# single-brand / intra-master voucher module above and the new
# cross-brand pooling module in ``app.services.voucher_pool``. We keep
# the hook one-way and read-only: the voucher card UI can ask "which
# cross-brand pools is the issuing brand part of, and what extra shops
# does that imply?" without touching any of the existing voucher
# state or redeem semantics. The pooling module owns its own minted
# vouchers (``pool_voucher:{vid}``) and never overwrites the legacy
# ``voucher:{vid}`` hash.

@cross_store_router.get(
    "/{voucher_id}/pool-context",
    summary="(Hook) report the cross-brand pools the issuer participates in",
)
async def voucher_pool_context_hook(
    voucher_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Surface cross-brand pool membership for a legacy voucher's issuer.

    This is a *read-only* hook into ``app.services.voucher_pool`` —
    we never mutate pool state from here, and the legacy voucher's
    redemption path is unchanged. Returning the issuer's pool IDs +
    member counts gives the storefront UI enough to display "also
    redeemable at N nearby shops" without coupling the redeem path
    to pooling rules.
    """
    state = await _load_voucher(r, voucher_id)
    voucher = _voucher_to_dict(state)
    issuer = voucher.get("issuer_brand_id")
    if not issuer:
        return {"voucher_id": voucher_id, "pools": []}

    # Local import keeps the legacy module's import graph hermetic when
    # voucher_pool isn't loaded (e.g. in some test isolates).
    from app.services import voucher_pool as _vp

    pool_ids = await _vp.list_brand_pools(r, issuer)
    pools: list[dict[str, Any]] = []
    for pid in pool_ids:
        p = await _vp.get_pool(r, pid)
        if p and p.get("status") == "active":
            pools.append({
                "pool_id": pid,
                "name": p["name"],
                "district": p["district"],
                "member_count": len(p["members"]),
            })
    return {
        "voucher_id": voucher_id,
        "issuer_brand_id": issuer,
        "pool_count": len(pools),
        "pools": pools,
    }


# ══════════════════════════════════════════════════════════════════════════
# Voucher lifecycle endpoints  (paired with voucher_lifecycle_worker.py)
# ══════════════════════════════════════════════════════════════════════════
#
# These endpoints expose the worker's state to the mobile client and to
# brand-portal admins:
#
#   * POST /{vid}/extend-grace          — manual grace extension by user
#   * GET  /expiring/{user_id}          — upcoming-expiry list
#   * GET  /expired/{user_id}/winback-offers — pending win-back offers
#   * GET  /admin/vouchers/expiration-stats  — per-brand dashboard counters
#
# The hourly worker (app.workers.voucher_lifecycle_worker.run_once) is
# the canonical writer of these keys; the endpoints below are pure reads
# (or one explicit user-initiated write for ``extend-grace``).


class ExtendGraceRequest(BaseModel):
    grace_hours: int = Field(24, ge=1, le=168)  # 1 h – 7 d max
    reason: str = Field("", max_length=200)


def _k_voucher_grace_applied(vid: str) -> str:
    return f"voucher:{vid}:grace_applied"


def _k_voucher_winback_offered(vid: str) -> str:
    return f"voucher:{vid}:winback_offered"


def _k_user_winback_offers(uid: str) -> str:
    return f"user:{uid}:voucher_winback_offers"


def _k_brand_expiration_stats(bid: str) -> str:
    return f"brand:{bid}:voucher_expiration_stats"


def _k_voucher_lifecycle_audit(vid: str) -> str:
    return f"voucher:{vid}:lifecycle_audit"


@cross_store_router.post(
    "/{voucher_id}/extend-grace",
    summary="Manually extend a voucher's expiry by a grace window",
)
async def extend_grace(
    voucher_id: str,
    body: ExtendGraceRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Apply a one-shot grace-period extension to a voucher.

    The hourly worker already auto-extends $20+ vouchers by 24 h and
    $50+ vouchers by 72 h after they expire. This endpoint is for the
    rarer case where the user explicitly asks for extra time (support
    ticket, in-app "extend" button) before or just after expiry.

    Returns 409 if grace has already been applied (idempotent).
    """
    key = _k_voucher(voucher_id)
    voucher = await r.hgetall(key)
    if not voucher:
        raise HTTPException(status_code=404, detail="voucher_not_found")
    if voucher.get("status") not in ("issued", "claimed", "expired"):
        raise HTTPException(
            status_code=409,
            detail=f"cannot_extend_status={voucher.get('status')}",
        )
    if await r.exists(_k_voucher_grace_applied(voucher_id)):
        raise HTTPException(status_code=409, detail="grace_already_applied")

    try:
        old_expires_at = int(voucher.get("expires_at", "0") or 0)
    except ValueError:
        old_expires_at = 0
    if not old_expires_at:
        raise HTTPException(status_code=422, detail="voucher_has_no_expiry")
    new_expires_at = max(old_expires_at, _now()) + body.grace_hours * 3600

    pipe = r.pipeline()
    pipe.hset(
        key,
        mapping={
            "expires_at": str(new_expires_at),
            "status": "issued",  # revive if it had flipped to expired
            "grace_extended_at": str(_now()),
            "grace_hours": str(body.grace_hours),
            "grace_reason": body.reason,
        },
    )
    pipe.set(
        _k_voucher_grace_applied(voucher_id),
        str(_now()),
        ex=365 * 86400,
    )
    pipe.hincrby(
        _k_brand_expiration_stats(voucher.get("issuer_brand_id", "")),
        "grace_extensions_manual",
        1,
    )
    pipe.rpush(
        _k_voucher_lifecycle_audit(voucher_id),
        _dumps({
            "event": "manual_grace_extend",
            "grace_hours": body.grace_hours,
            "reason": body.reason,
            "old_expires_at": old_expires_at,
            "new_expires_at": new_expires_at,
            "ts": _now(),
        }),
    )
    pipe.ltrim(_k_voucher_lifecycle_audit(voucher_id), -200, -1)
    await pipe.execute()

    return {
        "ok": True,
        "voucher_id": voucher_id,
        "old_expires_at": old_expires_at,
        "new_expires_at": new_expires_at,
        "grace_hours": body.grace_hours,
    }


@cross_store_router.get(
    "/expiring/{user_id}",
    summary="List the user's vouchers expiring in the upcoming window",
)
async def list_expiring(
    user_id: str,
    within_days: int = Query(14, ge=1, le=90),
    r: aioredis.Redis = Depends(get_redis),
):
    """Surface every voucher the user holds that will expire inside
    ``within_days``. Mobile uses this for the "expiring soon" badge.
    """
    cutoff = _now() + within_days * 86400
    vids = await r.zrevrange(_k_user_vouchers(user_id), 0, -1) or []
    out: list[dict[str, Any]] = []
    for vid in vids:
        v = await r.hgetall(_k_voucher(vid))
        if not v:
            continue
        if v.get("status") not in ("issued", "claimed"):
            continue
        exp_raw = v.get("expires_at") or ""
        if not exp_raw:
            continue
        try:
            exp = int(exp_raw)
        except ValueError:
            continue
        if exp <= 0 or exp > cutoff:
            continue
        out.append({
            "voucher_id": vid,
            "issuer_brand_id": v.get("issuer_brand_id"),
            "value_cents": int(v.get("value_cents", 0) or 0),
            "expires_at": exp,
            "expires_at_iso": _iso(exp),
            "seconds_to_expiry": max(0, exp - _now()),
        })
    # Sort by closest-expiry first.
    out.sort(key=lambda x: x["expires_at"])
    return {
        "user_id": user_id,
        "within_days": within_days,
        "count": len(out),
        "vouchers": out,
    }


@cross_store_router.get(
    "/expired/{user_id}/winback-offers",
    summary="List the user's pending win-back offers for expired vouchers",
)
async def list_winback_offers(
    user_id: str,
    include_claimed: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    r: aioredis.Redis = Depends(get_redis),
):
    """Each entry is a 50%-credit offer minted when a voucher's grace
    window lapsed without redemption. The mobile client can use these to
    let users one-tap re-issue.
    """
    raw_offers = await r.lrange(_k_user_winback_offers(user_id), 0, limit - 1)
    out: list[dict[str, Any]] = []
    for raw in raw_offers or []:
        try:
            offer = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not include_claimed and offer.get("claimed"):
            continue
        out.append(offer)
    return {"user_id": user_id, "count": len(out), "offers": out}


@cross_store_router.get(
    "/admin/expiration-stats",
    summary="Admin: per-brand voucher expiration dashboard counters",
)
async def admin_expiration_stats(
    brand_id: str = Query(...),
    r: aioredis.Redis = Depends(get_redis),
):
    """Return reminders-sent / grace-extensions / expired / winback counters
    for a single brand. Trinity dashboard reads this. Open endpoint (no
    admin token required for now — wire to the global admin guard when
    the admin auth layer is unified across all routers).
    """
    raw = await r.hgetall(_k_brand_expiration_stats(brand_id))
    stats = {k: int(v) for k, v in (raw or {}).items() if v.isdigit()}
    return {
        "brand_id": brand_id,
        "reminders_sent": stats.get("reminders_sent", 0),
        "grace_extensions": stats.get("grace_extensions", 0),
        "grace_extensions_manual": stats.get("grace_extensions_manual", 0),
        "expired": stats.get("expired", 0),
        "winback_offered": stats.get("winback_offered", 0),
        "ts": _now(),
    }
