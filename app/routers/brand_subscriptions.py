"""Brand subscription tiers — FREE / STARTER / GROWTH / ENTERPRISE.

KiX's SECOND revenue line (after ads). Per MERCHANT_FLOW_TRUTH.md
merchants start on the FREE tier (1 game / 5 recipes / 2 active campaigns
/ 3 custom audiences) and upgrade to STARTER / GROWTH / ENTERPRISE for
higher quotas + premium features.

This module owns:

* Tier configuration (``TIERS``) — single source of truth.
* Tier lifecycle endpoints (current / upgrade / downgrade / cancel /
  billing-history / auto-renew-config).
* Usage + quota endpoints (``GET /usage``, ``POST /quota/check``).
* The ``check_quota()`` helper that OTHER routers import before creating
  a resource (campaigns / audiences / recipes / games).

Redis schema
------------
    brand:{bid}:subscription                HASH   — tier, started_at, ...
    brand:{bid}:subscription:history        LIST   — JSON audit events
    brand:{bid}:games_count                 INT
    brand:{bid}:campaigns_count             INT    (active campaigns)
    brand:{bid}:audiences_count             INT
    brand:{bid}:recipes_count               INT
    brand_subscription:tiers                HASH   — cached tier config
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, get_read_db
from app.i18n.currency import (
    get_primary_currency,
    get_subscription_price_cents,
)
from app.models.subscription import BrandSubscription, SubscriptionHistory
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Tier definitions ───────────────────────────────────────────────────────


_STARTER_FEATURES = [
    "basic_modules",
    "sdk",
    "portal",
    "analytics_basic",
    "consent",
    "ab_testing",
    "custom_branding",
]

_GROWTH_FEATURES = _STARTER_FEATURES + [
    "analytics_advanced",
    "api_access",
    "priority_support",
]

_ENTERPRISE_FEATURES = _GROWTH_FEATURES + [
    "white_label",
    "sla_99_9",
    "dedicated_account_manager",
    "custom_integration",
    "data_export_unlimited",
]

TIERS: dict[str, dict[str, Any]] = {
    "free": {
        "monthly_cents": 0,
        "annual_cents": 0,
        "max_games": 1,
        "max_recipes": 5,
        "max_campaigns_active": 2,
        "max_audiences": 3,
        "features": ["basic_modules", "sdk", "portal", "analytics_basic", "consent"],
        "label_cn": "免费版",
        "label_en": "Free",
    },
    "starter": {
        "monthly_cents": 19900,    # ¥199 / month
        "annual_cents": 199000,    # ¥1990 / year (save ¥398)
        "max_games": 3,
        "max_recipes": 20,
        "max_campaigns_active": 5,
        "max_audiences": 10,
        "features": _STARTER_FEATURES,
        "label_cn": "入门版",
        "label_en": "Starter",
    },
    "growth": {
        "monthly_cents": 99900,    # ¥999 / month
        "annual_cents": 999000,    # ¥9990 / year
        "max_games": 10,
        "max_recipes": 50,
        "max_campaigns_active": 20,
        "max_audiences": 30,
        "features": _GROWTH_FEATURES,
        "label_cn": "成长版",
        "label_en": "Growth",
    },
    "enterprise": {
        "monthly_cents": 500000,   # ¥5000 / month
        "annual_cents": 5000000,   # ¥50000 / year
        "max_games": -1,           # unlimited
        "max_recipes": -1,
        "max_campaigns_active": -1,
        "max_audiences": -1,
        "features": _ENTERPRISE_FEATURES,
        "label_cn": "企业版",
        "label_en": "Enterprise",
    },
}

# Ordered, low → high. Used by ``_next_tier_with_resource``.
TIER_ORDER: list[str] = ["free", "starter", "growth", "enterprise"]

VALID_TIERS = set(TIERS.keys())
VALID_RESOURCES = {"games", "recipes", "campaigns_active", "audiences"}
VALID_BILLING = {"monthly", "annual"}
VALID_EFFECTIVE = {"immediate", "end_of_period"}

DAY_SECONDS = 86400
YEAR_SECONDS = 365 * DAY_SECONDS

# Trial: 3 months free (Apple Music strategy — long enough for merchant to
# see value, short enough that switching cost > continuing cost). See
# MERCHANT_FLOW_TRUTH.md for rationale.
TRIAL_DAYS = 90
TRIAL_SECONDS = TRIAL_DAYS * DAY_SECONDS

HISTORY_MAX_LEN = 500


# ── Pydantic models ────────────────────────────────────────────────────────


class UpgradeRequest(BaseModel):
    to_tier: Literal["starter", "growth", "enterprise"]
    billing: Literal["monthly", "annual"] = "monthly"
    first_year_free: bool = False
    payment_method_id: str | None = None


class DowngradeRequest(BaseModel):
    to_tier: Literal["free", "starter", "growth"]
    effective: Literal["immediate", "end_of_period"] = "end_of_period"


class CancelRequest(BaseModel):
    reason: str = Field(default="", max_length=500)
    effective: Literal["immediate", "end_of_period"] = "end_of_period"


class QuotaCheckRequest(BaseModel):
    brand_id: str = Field(min_length=1)
    resource: Literal["games", "recipes", "campaigns_active", "audiences"]


class AutoRenewConfig(BaseModel):
    enabled: bool
    payment_method_id: str | None = None
    renew_to_tier: str | None = None  # ``None`` / ``"same"`` → keep current tier


# ── Redis key helpers ──────────────────────────────────────────────────────


def _sub_key(bid: str) -> str:
    return f"brand:{bid}:subscription"


def _history_key(bid: str) -> str:
    return f"brand:{bid}:subscription:history"


def _count_key(bid: str, resource: str) -> str:
    # ``campaigns_active`` stores under the canonical ``campaigns_count`` slot
    # so creator routers don't need to know the quota name.
    slot = "campaigns" if resource == "campaigns_active" else resource
    return f"brand:{bid}:{slot}_count"


# ── Internal helpers ───────────────────────────────────────────────────────


async def _get_brand_tier(r: aioredis.Redis, brand_id: str) -> str:
    """Return the brand's current tier — defaults to ``"free"``."""
    tier = await r.hget(_sub_key(brand_id), "tier")
    if not tier:
        return "free"
    tier = tier.decode() if isinstance(tier, bytes) else tier
    return tier if tier in VALID_TIERS else "free"


async def _count_resource(
    r: aioredis.Redis, brand_id: str, resource_name: str
) -> int:
    raw = await r.get(_count_key(brand_id, resource_name))
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _next_tier_with_resource(
    resource_name: str, current_limit: int
) -> str | None:
    """Return the cheapest tier whose ``max_<resource>`` > ``current_limit``.

    ``-1`` (unlimited) is treated as the maximum.
    """
    quota_key = f"max_{resource_name}"
    for tier in TIER_ORDER:
        limit = TIERS[tier].get(quota_key, 0)
        if limit == -1 or limit > current_limit:
            return tier
    return None


async def _append_history(
    r: aioredis.Redis, brand_id: str, event: dict[str, Any]
) -> None:
    """Push an audit event onto the brand's subscription history list."""
    event.setdefault("ts", time.time())
    key = _history_key(brand_id)
    pipe = r.pipeline()
    pipe.lpush(key, json.dumps(event, default=str))
    pipe.ltrim(key, 0, HISTORY_MAX_LEN - 1)
    await pipe.execute()


async def _load_sub_record(
    r: aioredis.Redis, brand_id: str
) -> dict[str, Any]:
    """Hydrate the subscription HASH into a plain dict (decoded values)."""
    raw = await r.hgetall(_sub_key(brand_id))
    if not raw:
        # Implicit FREE tier — no record yet.
        return {
            "brand_id": brand_id,
            "tier": "free",
            "billing": "monthly",
            "started_at": None,
            "expires_at": None,
            "auto_renew": False,
            "payment_method_id": None,
            "first_year_free": False,
            "next_charge_at": None,
            "cancel_pending": False,
        }
    out: dict[str, Any] = {"brand_id": brand_id}
    for k, v in raw.items():
        key = k.decode() if isinstance(k, bytes) else k
        val = v.decode() if isinstance(v, bytes) else v
        out[key] = val
    # Light type coercion for known numeric / boolean fields.
    for f in ("started_at", "expires_at", "next_charge_at"):
        if out.get(f) in (None, "", "None"):
            out[f] = None
        else:
            try:
                out[f] = float(out[f])
            except (TypeError, ValueError):
                out[f] = None
    for f in ("auto_renew", "first_year_free", "cancel_pending"):
        out[f] = str(out.get(f, "false")).lower() == "true"
    return out


# ── PostgreSQL helpers (durable source of truth) ──────────────────────────


async def _pg_get_sub(
    db: AsyncSession | None, brand_id: str
) -> BrandSubscription | None:
    """Fetch the PG row for ``brand_id``; tolerant of a missing session.

    During the dual-write migration window the ``brand_subscriptions``
    table may not yet exist (e.g. test envs that haven't run migration
    0002 yet). We swallow that specific error and return ``None`` so
    callers fall back to Redis.
    """
    if db is None:
        return None
    try:
        return await db.get(BrandSubscription, brand_id)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "does not exist" in msg or "undefinedtable" in msg:
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return None
        raise


async def _pg_upsert_sub(
    db: AsyncSession | None,
    brand_id: str,
    fields: dict[str, Any],
) -> None:
    """Insert-or-update a brand's subscription row.

    ``fields`` uses native Python types (ints / bools / strings), not
    the Redis-encoded strings — convert before calling.

    Silently no-ops if the PG table does not yet exist (dual-write
    migration safety).
    """
    if db is None:
        return
    try:
        row = await db.get(BrandSubscription, brand_id)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "does not exist" in msg or "undefinedtable" in msg:
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return
        raise
    if row is None:
        # When creating from a partial update we still need NOT NULL
        # columns to be present — fall back to sensible defaults.
        now_ts = int(fields.get("started_at") or time.time())
        defaults: dict[str, Any] = {
            "brand_id": brand_id,
            "tier": "free",
            "billing": "monthly",
            "started_at": now_ts,
            "expires_at": now_ts,
            "next_charge_at": now_ts,
            "auto_renew": False,
            "first_year_free": False,
            "cancel_pending": False,
            "dunning_state": "none",
            "dunning_attempts": 0,
            "metadata_json": {},
        }
        defaults.update(fields)
        row = BrandSubscription(**defaults)
        db.add(row)
        return
    for k, v in fields.items():
        if hasattr(row, k):
            setattr(row, k, v)


async def _pg_append_history(
    db: AsyncSession | None,
    brand_id: str,
    event: str,
    *,
    from_tier: str | None = None,
    to_tier: str | None = None,
    charge_amount_cents: int | None = None,
    metadata: dict[str, Any] | None = None,
    ts: float | None = None,
) -> None:
    if db is None:
        return
    # Best-effort: only add to session if the subscriptions row already
    # exists (FK to brand_subscriptions). If the brand has no PG row yet
    # the upsert helper already swallowed an UndefinedTable error so
    # skip the history too.
    existing = await _pg_get_sub(db, brand_id)
    if existing is None:
        return
    db.add(
        SubscriptionHistory(
            brand_id=brand_id,
            event=event,
            from_tier=from_tier,
            to_tier=to_tier,
            charge_amount_cents=charge_amount_cents,
            metadata_json=metadata or {},
            ts=int(ts if ts is not None else time.time()),
        )
    )


async def _resolve_sub_record(
    r: aioredis.Redis,
    brand_id: str,
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    """PG-first read with Redis fallback during the migration window.

    Returns the same dict shape ``_load_sub_record`` produced from Redis
    so all downstream code is unchanged.
    """
    if db is not None:
        row = await _pg_get_sub(db, brand_id)
        if row is not None:
            return row.to_dict()
    return await _load_sub_record(r, brand_id)


async def _resolve_brand_tier(
    r: aioredis.Redis,
    brand_id: str,
    db: AsyncSession | None = None,
) -> str:
    if db is not None:
        row = await _pg_get_sub(db, brand_id)
        if row is not None:
            return row.tier if row.tier in VALID_TIERS else "free"
    return await _get_brand_tier(r, brand_id)


def _tier_price_cents(tier: str, billing: str, region: str | None = None) -> int:
    """Return the active region's MSRP for ``(tier, billing)``.

    Falls back to the legacy CN-only ``TIERS`` table when the price book
    doesn't have an entry — this preserves backwards-compatibility with
    older tests that pre-date the per-region price book.
    """
    price = get_subscription_price_cents(tier, billing, region)
    if price:
        return price
    if billing == "annual":
        return TIERS[tier]["annual_cents"]
    return TIERS[tier]["monthly_cents"]


def _tier_price_currency(region: str | None = None) -> str:
    """Currency in which a subscription is billed for the given region."""
    return get_primary_currency(region)


def _cycle_seconds(billing: str) -> int:
    return YEAR_SECONDS if billing == "annual" else 30 * DAY_SECONDS


async def _usage_snapshot(
    r: aioredis.Redis, brand_id: str, tier: str
) -> dict[str, Any]:
    """Build the usage-vs-limits snapshot used by ``/usage`` and ``/current``."""
    config = TIERS[tier]
    usage: dict[str, int] = {}
    limits: dict[str, int] = {}
    over_limit: dict[str, dict[str, int]] = {}
    progress_pct: dict[str, float] = {}
    for resource in ("games", "recipes", "campaigns_active", "audiences"):
        current = await _count_resource(r, brand_id, resource)
        limit = config[f"max_{resource}"]
        usage[resource] = current
        limits[resource] = limit
        if limit == -1:
            progress_pct[resource] = 0.0
        else:
            progress_pct[resource] = (
                round(current / limit, 4) if limit > 0 else 0.0
            )
            if current > limit:
                over_limit[resource] = {"current": current, "limit": limit}
    return {
        "tier": tier,
        "usage": usage,
        "limits": limits,
        "over_limit": over_limit,
        "progress_pct": progress_pct,
    }


# ── Public quota helper (imported by other routers) ───────────────────────


async def check_quota(
    brand_id: str,
    resource_name: str,
    r: aioredis.Redis,
    db: AsyncSession | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Check whether ``brand_id`` may create one more ``resource_name``.

    ``resource_name`` ∈ ``{"games", "campaigns_active", "audiences",
    "recipes"}``. Returns ``(allowed, info)``. Callers in other routers
    should raise ``HTTPException(402, ...)`` when ``allowed is False``.

    ``db`` is optional for backwards compatibility — when supplied the
    tier is sourced from PostgreSQL (source of truth) with Redis as
    fallback for legacy brands not yet migrated.
    """
    if resource_name not in VALID_RESOURCES:
        raise ValueError(f"unknown_resource:{resource_name}")
    tier = await _resolve_brand_tier(r, brand_id, db)
    config = TIERS[tier]
    limit = config[f"max_{resource_name}"]
    if limit == -1:
        return True, {"unlimited": True, "tier": tier}
    current = await _count_resource(r, brand_id, resource_name)
    if current >= limit:
        return False, {
            "current": current,
            "limit": limit,
            "tier": tier,
            "upgrade_required_to": _next_tier_with_resource(
                resource_name, limit
            ),
        }
    return True, {
        "current": current,
        "limit": limit,
        "remaining": limit - current,
        "tier": tier,
    }


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.get("/tiers")
async def list_tiers() -> dict[str, Any]:
    """Return the full ``TIERS`` config — used by the portal pricing page."""
    return {"tiers": TIERS, "order": TIER_ORDER}


@router.post("/{brand_id}/current")
async def get_current(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_read_db),
) -> dict[str, Any]:
    """Return the brand's current tier, billing state, and usage snapshot.

    Read-only — routed through the read replica when configured.
    """
    record = await _resolve_sub_record(r, brand_id, db)
    tier = record["tier"] if record["tier"] in VALID_TIERS else "free"
    usage = await _usage_snapshot(r, brand_id, tier)
    return {
        "subscription": record,
        "config": TIERS[tier],
        "usage": usage,
    }


@router.post("/{brand_id}/upgrade")
async def upgrade(
    brand_id: str,
    body: UpgradeRequest,
    r: aioredis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Upgrade the brand to a paid tier.

    With ``first_year_free=True`` (the trial flag — kept for backwards-compat
    but now means 3 months, not a year), the upgrade is immediate but no
    charge is taken now — ``next_charge_at`` is pushed out 90 days and a
    ``FREE_TRIAL_3MO`` history event is recorded for the cron auto-renewer.

    Apple Music strategy: 3 months free → merchant sees real value (data
    accumulates) → switching cost > continuing cost. See MERCHANT_FLOW_TRUTH.md.
    """
    current_tier = await _resolve_brand_tier(r, brand_id, db)
    new_tier = body.to_tier
    if TIER_ORDER.index(new_tier) <= TIER_ORDER.index(current_tier):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "not_an_upgrade",
                "current": current_tier,
                "requested": new_tier,
            },
        )

    now = time.time()
    cycle = _cycle_seconds(body.billing)
    expires_at = now + cycle
    charge_amount = _tier_price_cents(new_tier, body.billing)

    if body.first_year_free:
        # 3-month free trial (Apple Music strategy) — no charge now,
        # auto-charge at day 91. Long enough to see value, short enough
        # that switching cost exceeds continuing cost.
        next_charge_at = now + TRIAL_SECONDS
        charge_now = 0
        first_year_free_flag = True
    else:
        next_charge_at = expires_at
        charge_now = charge_amount
        first_year_free_flag = False

    record = {
        "tier": new_tier,
        "billing": body.billing,
        "started_at": now,
        "expires_at": expires_at,
        "next_charge_at": next_charge_at,
        "auto_renew": "true",
        "payment_method_id": body.payment_method_id or "",
        "first_year_free": "true" if first_year_free_flag else "false",
        "cancel_pending": "false",
    }
    await r.hset(_sub_key(brand_id), mapping={k: str(v) for k, v in record.items()})
    await _append_history(
        r,
        brand_id,
        {
            "event": "FREE_TRIAL_3MO" if first_year_free_flag else "UPGRADE",
            "from_tier": current_tier,
            "to_tier": new_tier,
            "billing": body.billing,
            "charge_amount_cents": charge_now,
            "next_charge_at": next_charge_at,
        },
    )

    # Dual-write to PostgreSQL (source of truth)
    event_name = "FREE_TRIAL_3MO" if first_year_free_flag else "UPGRADE"
    await _pg_upsert_sub(
        db,
        brand_id,
        {
            "tier": new_tier,
            "billing": body.billing,
            "started_at": int(now),
            "expires_at": int(expires_at),
            "next_charge_at": int(next_charge_at),
            "auto_renew": True,
            "payment_method_id": body.payment_method_id or None,
            "first_year_free": first_year_free_flag,
            "cancel_pending": False,
            "dunning_state": "none",
            "dunning_attempts": 0,
            "dunning_reason": None,
        },
    )
    await _pg_append_history(
        db,
        brand_id,
        event_name,
        from_tier=current_tier,
        to_tier=new_tier,
        charge_amount_cents=charge_now,
        metadata={
            "billing": body.billing,
            "next_charge_at": next_charge_at,
        },
        ts=now,
    )

    # Default-on auto-recharge: STARTER+ tiers with a verified payment method
    # get auto-recharge enabled at 20% threshold (unless previously opted out).
    autorecharge_v2: dict[str, Any] | None = None
    try:
        from app.routers.wallet import enable_autorecharge_v2_for_upgrade
        autorecharge_v2 = await enable_autorecharge_v2_for_upgrade(brand_id, r)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "autorecharge_v2 default-enable failed brand=%s: %s", brand_id, exc
        )

    return {
        "tier": new_tier,
        "effective_at": now,
        "next_charge_at": next_charge_at,
        "charge_amount": charge_now,
        "billing": body.billing,
        "first_year_free": first_year_free_flag,
        "autorecharge_v2": autorecharge_v2,
    }


@router.post("/{brand_id}/downgrade")
async def downgrade(
    brand_id: str,
    body: DowngradeRequest,
    r: aioredis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Move the brand to a lower tier.

    If current usage exceeds the new tier's quota the API returns a
    structured ``over_limit`` payload — the portal must walk the user
    through disabling extras before retrying.
    """
    current_tier = await _resolve_brand_tier(r, brand_id, db)
    new_tier = body.to_tier
    if TIER_ORDER.index(new_tier) >= TIER_ORDER.index(current_tier):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "not_a_downgrade",
                "current": current_tier,
                "requested": new_tier,
            },
        )

    usage = await _usage_snapshot(r, brand_id, new_tier)
    if usage["over_limit"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "over_new_tier_quota",
                "message": "Disable extras before downgrading.",
                "over_limit": usage["over_limit"],
                "target_tier": new_tier,
            },
        )

    record = await _resolve_sub_record(r, brand_id, db)
    now = time.time()
    if body.effective == "immediate":
        effective_at = now
        await r.hset(
            _sub_key(brand_id),
            mapping={
                "tier": new_tier,
                "started_at": str(now),
                "cancel_pending": "false",
            },
        )
        await _pg_upsert_sub(
            db,
            brand_id,
            {
                "tier": new_tier,
                "started_at": int(now),
                "cancel_pending": False,
            },
        )
    else:
        # Schedule at end of period — keep current tier active until then.
        effective_at = record.get("expires_at") or now
        await r.hset(
            _sub_key(brand_id),
            mapping={
                "pending_tier": new_tier,
                "pending_effective_at": str(effective_at),
            },
        )
        await _pg_upsert_sub(
            db,
            brand_id,
            {
                "pending_tier": new_tier,
                "pending_effective_at": int(effective_at),
            },
        )

    await _append_history(
        r,
        brand_id,
        {
            "event": "DOWNGRADE",
            "from_tier": current_tier,
            "to_tier": new_tier,
            "effective": body.effective,
            "effective_at": effective_at,
        },
    )
    await _pg_append_history(
        db,
        brand_id,
        "DOWNGRADE",
        from_tier=current_tier,
        to_tier=new_tier,
        metadata={
            "effective": body.effective,
            "effective_at": effective_at,
        },
        ts=now,
    )
    return {"tier": new_tier, "effective_at": effective_at, "effective": body.effective}


@router.post("/{brand_id}/cancel")
async def cancel(
    brand_id: str,
    body: CancelRequest,
    r: aioredis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Cancel the brand's paid subscription — drops to FREE.

    Data is retained — only quotas are enforced again at the FREE level.
    """
    current_tier = await _resolve_brand_tier(r, brand_id, db)
    if current_tier == "free":
        return {
            "tier": "free",
            "effective_at": time.time(),
            "note": "already_free",
        }

    record = await _resolve_sub_record(r, brand_id, db)
    now = time.time()
    if body.effective == "immediate":
        effective_at = now
        await r.hset(
            _sub_key(brand_id),
            mapping={
                "tier": "free",
                "billing": "monthly",
                "auto_renew": "false",
                "cancel_pending": "false",
                "started_at": str(now),
                "expires_at": str(now),
                "next_charge_at": str(now),
                "first_year_free": "false",
            },
        )
        await _pg_upsert_sub(
            db,
            brand_id,
            {
                "tier": "free",
                "billing": "monthly",
                "auto_renew": False,
                "cancel_pending": False,
                "started_at": int(now),
                "expires_at": int(now),
                "next_charge_at": int(now),
                "first_year_free": False,
            },
        )
    else:
        effective_at = record.get("expires_at") or now
        await r.hset(
            _sub_key(brand_id),
            mapping={
                "cancel_pending": "true",
                "auto_renew": "false",
                "pending_tier": "free",
                "pending_effective_at": str(effective_at),
            },
        )
        await _pg_upsert_sub(
            db,
            brand_id,
            {
                "cancel_pending": True,
                "auto_renew": False,
                "pending_tier": "free",
                "pending_effective_at": int(effective_at),
            },
        )

    await _append_history(
        r,
        brand_id,
        {
            "event": "CANCEL",
            "from_tier": current_tier,
            "to_tier": "free",
            "reason": body.reason,
            "effective": body.effective,
            "effective_at": effective_at,
        },
    )
    await _pg_append_history(
        db,
        brand_id,
        "CANCEL",
        from_tier=current_tier,
        to_tier="free",
        metadata={
            "reason": body.reason,
            "effective": body.effective,
            "effective_at": effective_at,
        },
        ts=now,
    )
    return {
        "tier": "free" if body.effective == "immediate" else current_tier,
        "pending_tier": "free" if body.effective == "end_of_period" else None,
        "effective_at": effective_at,
        "effective": body.effective,
    }


@router.get("/{brand_id}/usage")
async def get_usage(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_read_db),
) -> dict[str, Any]:
    """Return per-resource usage / limits / over-limit deltas.

    Read-only — routed through the read replica when configured.
    """
    tier = await _resolve_brand_tier(r, brand_id, db)
    return await _usage_snapshot(r, brand_id, tier)


@router.post("/quota/check")
async def quota_check(
    body: QuotaCheckRequest,
    r: aioredis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Public quota-check endpoint — mirrors the in-process ``check_quota``."""
    allowed, info = await check_quota(body.brand_id, body.resource, r, db)
    return {"allowed": allowed, **info}


@router.post("/{brand_id}/billing-history")
async def billing_history(
    brand_id: str,
    limit: int = Query(default=100, ge=1, le=HISTORY_MAX_LEN),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return the brand's subscription audit log (newest first)."""
    raw = await r.lrange(_history_key(brand_id), 0, limit - 1)
    events: list[dict[str, Any]] = []
    for blob in raw:
        try:
            text = blob.decode() if isinstance(blob, bytes) else blob
            events.append(json.loads(text))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return {"brand_id": brand_id, "count": len(events), "events": events}


@router.post("/{brand_id}/auto-renew-config")
async def auto_renew_config(
    brand_id: str,
    body: AutoRenewConfig,
    r: aioredis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Toggle auto-renewal + (optionally) bind a renewal target tier."""
    current_tier = await _resolve_brand_tier(r, brand_id, db)
    if current_tier == "free" and body.enabled:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "free_tier_has_no_renewal"},
        )

    renew_to = body.renew_to_tier
    if renew_to in (None, "", "same"):
        renew_to = current_tier
    elif renew_to not in VALID_TIERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "invalid_renew_to_tier", "value": renew_to},
        )

    await r.hset(
        _sub_key(brand_id),
        mapping={
            "auto_renew": "true" if body.enabled else "false",
            "payment_method_id": body.payment_method_id or "",
            "renew_to_tier": renew_to,
        },
    )
    await _append_history(
        r,
        brand_id,
        {
            "event": "AUTO_RENEW_CONFIG",
            "enabled": body.enabled,
            "renew_to_tier": renew_to,
        },
    )
    await _pg_upsert_sub(
        db,
        brand_id,
        {
            "auto_renew": bool(body.enabled),
            "payment_method_id": body.payment_method_id or None,
            "renew_to_tier": renew_to,
        },
    )
    await _pg_append_history(
        db,
        brand_id,
        "AUTO_RENEW_CONFIG",
        metadata={
            "enabled": body.enabled,
            "renew_to_tier": renew_to,
        },
    )
    return {
        "brand_id": brand_id,
        "auto_renew": body.enabled,
        "renew_to_tier": renew_to,
        "payment_method_id": body.payment_method_id,
    }


# ── Admin: trigger the billing cron manually ───────────────────────────────


class RunBillingCronRequest(BaseModel):
    admin_token: str = Field(..., min_length=8, max_length=512)


class RunBillingCronResponse(BaseModel):
    scanned: int
    charged: int
    failed: int
    downgraded: int


@router.post(
    "/admin/run-billing-cron",
    response_model=RunBillingCronResponse,
)
async def admin_run_billing_cron(
    body: RunBillingCronRequest,
) -> RunBillingCronResponse:
    """Manually fire one billing-cron sweep.

    For staging/dev verification + ops break-glass — production should rely
    on the worker process (``python -m app.workers.billing_cron``). The
    auth model mirrors ``payouts._check_admin``: shared pre-shared key
    against ``settings.jwt_secret`` until JWT roles land.
    """
    from app.config import settings
    from app.security import constant_time_eq

    if not constant_time_eq(body.admin_token, settings.jwt_secret):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "admin_token_invalid"},
        )

    from app.workers.billing_cron import run_once

    result = await run_once()
    return RunBillingCronResponse(**result)
