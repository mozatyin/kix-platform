"""Commerce Loop Engine — converts game play into merchant revenue.

Implements the 5 modules of the KiX commerce loop:
  1. ScoreToCoupon       — game score → tiered discount coupon
  2. EnergyToPurchase    — sell energy refills (entry-ticket model)
  3. RewardChain         — coupons expire, must be redeemed
  4. UpsellMoment        — at redemption, suggest upgrade
  5. RedemptionStore     — long-term points → physical/digital rewards

All state lives in Redis, namespaced by brand_id. Operations are idempotent
via session_id (coupon claim) or transaction_id (purchases). No LLM calls —
pure deterministic state machines.
"""

from __future__ import annotations

import json
import logging
import secrets
import string
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Redis key helpers ────────────────────────────────────────────────────


def _k_coupon_tiers(brand_id: str) -> str:
    return f"brand:{brand_id}:coupon_tiers"


def _k_energy_packs(brand_id: str) -> str:
    return f"brand:{brand_id}:energy_packs"


def _k_upsell_rules(brand_id: str) -> str:
    return f"brand:{brand_id}:upsell_rules"


def _k_store_items(brand_id: str) -> str:
    return f"brand:{brand_id}:store_items"


def _k_user_coupons(user_id: str, brand_id: str) -> str:
    return f"user:{user_id}:coupons:{brand_id}"


def _k_coupon(coupon_id: str) -> str:
    return f"coupon:{coupon_id}"


def _k_purchases(brand_id: str) -> str:
    return f"brand:{brand_id}:purchases"


def _k_tx(tx_id: str) -> str:
    return f"transaction:{tx_id}"


def _k_session_claim(brand_id: str, session_id: str) -> str:
    """Idempotency key: maps session_id → coupon_id for a brand."""
    return f"brand:{brand_id}:session_claim:{session_id}"


def _k_brand_codes(brand_id: str) -> str:
    """Set of all coupon codes ever issued for this brand (uniqueness)."""
    return f"brand:{brand_id}:coupon_codes"


def _k_user_points(user_id: str, brand_id: str) -> str:
    return f"user:{user_id}:points:{brand_id}"


def _k_user_stars(user_id: str, brand_id: str) -> str:
    return f"user:{user_id}:stars:{brand_id}"


def _k_user_energy(user_id: str, brand_id: str) -> str:
    return f"user:{user_id}:energy:{brand_id}"


def _k_analytics_counter(brand_id: str, metric: str) -> str:
    return f"brand:{brand_id}:analytics:{metric}"


# ── Code & ID generators ─────────────────────────────────────────────────

_CODE_ALPHABET = string.ascii_uppercase + string.digits


def _gen_coupon_code() -> str:
    """6-char uppercase alphanumeric code."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _now_ts() -> int:
    return int(time.time())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Pydantic models ──────────────────────────────────────────────────────


class CouponTier(BaseModel):
    name: str
    min_score: int = Field(..., ge=0)
    discount_type: Literal["percent", "fixed", "free"]
    discount_value: float = Field(..., ge=0)
    max_redeems_per_user: int = Field(1, ge=1)
    expires_in_days: int = Field(30, ge=1, le=365)


class ConfigureCouponsRequest(BaseModel):
    brand_id: str
    tiers: list[CouponTier]


class ClaimCouponRequest(BaseModel):
    user_id: str
    brand_id: str
    score: int = Field(..., ge=0)
    game_slug: str
    session_id: str


class RedeemCouponRequest(BaseModel):
    merchant_pos_id: str
    actual_purchase_amount: float = Field(..., ge=0)


class EnergyPack(BaseModel):
    id: str
    energy: int = Field(..., ge=1)
    price_cents: int = Field(..., ge=0)
    currency: str = "CNY"


class ConfigureEnergyPacksRequest(BaseModel):
    brand_id: str
    packs: list[EnergyPack]


class BuyEnergyRequest(BaseModel):
    user_id: str
    brand_id: str
    pack: str  # pack id
    payment_method: str
    payment_token: str


class ExtendRewardRequest(BaseModel):
    coupon_id: str
    user_id: str
    cost_energy: int = Field(5, ge=1)


class UpsellRule(BaseModel):
    when_amount_below: float = Field(..., ge=0)
    upgrade_text: str
    upgrade_price: float = Field(..., ge=0)
    upgrade_discount: float = Field(0, ge=0)


class ConfigureUpsellRequest(BaseModel):
    brand_id: str
    rules: list[UpsellRule]


class UpsellSuggestRequest(BaseModel):
    brand_id: str
    coupon_id: str
    current_cart_amount: float = Field(..., ge=0)


class StoreItem(BaseModel):
    id: str
    name: str
    image_url: str = ""
    point_cost: int = Field(..., ge=0)
    stock: int = Field(..., ge=0)
    category: str = "general"


class ConfigureStoreRequest(BaseModel):
    brand_id: str
    items: list[StoreItem]


class StorePurchaseRequest(BaseModel):
    user_id: str
    brand_id: str
    item_id: str
    currency: Literal["points", "stars"] = "points"


# ── Internal helpers ─────────────────────────────────────────────────────


async def _load_tiers(r: aioredis.Redis, brand_id: str) -> list[dict]:
    raw = await r.get(_k_coupon_tiers(brand_id))
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


async def _load_packs(r: aioredis.Redis, brand_id: str) -> list[dict]:
    raw = await r.get(_k_energy_packs(brand_id))
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


async def _load_upsell_rules(r: aioredis.Redis, brand_id: str) -> list[dict]:
    raw = await r.get(_k_upsell_rules(brand_id))
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


async def _generate_unique_code(r: aioredis.Redis, brand_id: str) -> str:
    """Generate a 6-char code, ensure unique per brand. Retry up to 10x."""
    for _ in range(10):
        code = _gen_coupon_code()
        added = await r.sadd(_k_brand_codes(brand_id), code)
        if added == 1:
            return code
    # Extremely unlikely; fall back to 8-char
    for _ in range(10):
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
        added = await r.sadd(_k_brand_codes(brand_id), code)
        if added == 1:
            return code
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to generate unique coupon code",
    )


async def _load_coupon(r: aioredis.Redis, coupon_id: str) -> dict:
    data = await r.hgetall(_k_coupon(coupon_id))
    if not data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Coupon {coupon_id} not found",
        )
    return data


async def _log_transaction(
    r: aioredis.Redis,
    brand_id: str,
    tx_type: str,
    payload: dict[str, Any],
) -> str:
    """Append a transaction record for audit/analytics."""
    tx_id = _gen_id("tx")
    ts = _now_ts()
    record = {
        "tx_id": tx_id,
        "brand_id": brand_id,
        "type": tx_type,
        "timestamp": ts,
        "created_at": _now_iso(),
        "payload": json.dumps(payload, default=str),
    }
    # Store as HASH with stringified values
    flat = {k: (v if isinstance(v, str) else str(v)) for k, v in record.items()}
    await r.hset(_k_tx(tx_id), mapping=flat)
    await r.zadd(_k_purchases(brand_id), {tx_id: ts})
    return tx_id


async def _bump(r: aioredis.Redis, brand_id: str, metric: str, by: float = 1) -> None:
    """Increment an analytics counter (integer or float)."""
    key = _k_analytics_counter(brand_id, metric)
    if isinstance(by, float) and not by.is_integer():
        await r.incrbyfloat(key, by)
    else:
        await r.incrby(key, int(by))


def _pick_highest_tier(tiers: list[dict], score: int) -> dict | None:
    """Return the tier with the highest min_score that the score satisfies."""
    matching = [t for t in tiers if score >= int(t.get("min_score", 0))]
    if not matching:
        return None
    return max(matching, key=lambda t: int(t.get("min_score", 0)))


# ── 1. ScoreToCoupon ─────────────────────────────────────────────────────


@router.post("/coupons/configure")
async def configure_coupons(
    body: ConfigureCouponsRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Merchant defines coupon tiers for a brand."""
    payload = [t.model_dump() for t in body.tiers]
    await r.set(_k_coupon_tiers(body.brand_id), json.dumps(payload))
    logger.info(
        "Configured %d coupon tiers for brand=%s", len(payload), body.brand_id
    )
    return {"ok": True, "tiers_count": len(payload)}


@router.post("/coupons/claim")
async def claim_coupon(
    body: ClaimCouponRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """User submits a game score to claim a tiered coupon.

    Idempotent on (brand_id, session_id): replaying the same session
    returns the same coupon.
    """
    # Idempotency check
    session_key = _k_session_claim(body.brand_id, body.session_id)
    existing_id = await r.get(session_key)
    if existing_id:
        coupon = await _load_coupon(r, existing_id)
        return {
            "coupon_id": existing_id,
            "tier_name": coupon.get("tier_name"),
            "discount_type": coupon.get("discount_type"),
            "discount_value": float(coupon.get("discount_value", 0)),
            "expires_at": coupon.get("expires_at"),
            "code": coupon.get("code"),
            "idempotent_replay": True,
        }

    tiers = await _load_tiers(r, body.brand_id)
    if not tiers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No coupon tiers configured for this brand",
        )

    matched = _pick_highest_tier(tiers, body.score)
    if matched is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Score {body.score} does not meet any tier threshold",
        )

    coupon_id = _gen_id("cpn")
    code = await _generate_unique_code(r, body.brand_id)
    expires_in_days = int(matched.get("expires_in_days", 30))
    expires_at_dt = datetime.now(timezone.utc) + timedelta(days=expires_in_days)
    expires_at_ts = int(expires_at_dt.timestamp())
    created_ts = _now_ts()

    coupon_hash = {
        "coupon_id": coupon_id,
        "user_id": body.user_id,
        "brand_id": body.brand_id,
        "tier_name": str(matched.get("name", "")),
        "discount_type": str(matched.get("discount_type", "percent")),
        "discount_value": str(matched.get("discount_value", 0)),
        "max_redeems_per_user": str(matched.get("max_redeems_per_user", 1)),
        "expires_at": expires_at_dt.isoformat(),
        "expires_at_ts": str(expires_at_ts),
        "status": "active",
        "code": code,
        "created_at": _now_iso(),
        "created_ts": str(created_ts),
        "game_slug": body.game_slug,
        "session_id": body.session_id,
        "score": str(body.score),
    }

    # Atomic-ish: pipeline the writes
    pipe = r.pipeline()
    pipe.hset(_k_coupon(coupon_id), mapping=coupon_hash)
    pipe.expireat(_k_coupon(coupon_id), expires_at_ts + 86400 * 90)  # keep 90d post-expiry
    pipe.zadd(_k_user_coupons(body.user_id, body.brand_id), {coupon_id: expires_at_ts})
    pipe.set(session_key, coupon_id, ex=86400 * 30)
    await pipe.execute()

    # Analytics
    await _bump(r, body.brand_id, "coupons_claimed")
    await _log_transaction(
        r,
        body.brand_id,
        "coupon_claimed",
        {
            "coupon_id": coupon_id,
            "user_id": body.user_id,
            "tier": matched.get("name"),
            "score": body.score,
            "game_slug": body.game_slug,
        },
    )

    return {
        "coupon_id": coupon_id,
        "tier_name": matched.get("name"),
        "discount_type": matched.get("discount_type"),
        "discount_value": float(matched.get("discount_value", 0)),
        "expires_at": expires_at_dt.isoformat(),
        "code": code,
    }


@router.get("/coupons/{user_id}")
async def list_coupons(
    user_id: str,
    brand_id: str = Query(...),
    status_filter: Literal["active", "expired", "redeemed", "all"] = Query(
        "active", alias="status"
    ),
    r: aioredis.Redis = Depends(get_redis),
):
    """List a user's coupons for a brand, optionally filtered by status."""
    coupon_ids: list[str] = await r.zrange(
        _k_user_coupons(user_id, brand_id), 0, -1
    )

    items: list[dict] = []
    now_ts = _now_ts()
    for cid in coupon_ids:
        data = await r.hgetall(_k_coupon(cid))
        if not data:
            continue

        stored_status = data.get("status", "active")
        exp_ts = int(data.get("expires_at_ts", 0))
        # Lazy-expire
        if stored_status == "active" and exp_ts and now_ts > exp_ts:
            effective_status = "expired"
        else:
            effective_status = stored_status

        if status_filter != "all" and effective_status != status_filter:
            continue

        items.append(
            {
                "coupon_id": cid,
                "tier_name": data.get("tier_name"),
                "discount_type": data.get("discount_type"),
                "discount_value": float(data.get("discount_value", 0)),
                "status": effective_status,
                "code": data.get("code"),
                "expires_at": data.get("expires_at"),
                "created_at": data.get("created_at"),
                "game_slug": data.get("game_slug"),
            }
        )

    return {"user_id": user_id, "brand_id": brand_id, "count": len(items), "coupons": items}


@router.post("/coupons/{coupon_id}/redeem")
async def redeem_coupon(
    coupon_id: str,
    body: RedeemCouponRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Merchant POS redeems a coupon. Validates state, computes savings,
    fires an upsell suggestion."""
    data = await _load_coupon(r, coupon_id)
    brand_id = data.get("brand_id", "")

    if data.get("status") == "redeemed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Coupon already redeemed",
        )

    exp_ts = int(data.get("expires_at_ts", 0))
    if exp_ts and _now_ts() > exp_ts:
        # Mark as expired lazily
        await r.hset(_k_coupon(coupon_id), "status", "expired")
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Coupon expired",
        )

    discount_type = data.get("discount_type", "percent")
    discount_value = float(data.get("discount_value", 0))
    purchase = float(body.actual_purchase_amount)

    if discount_type == "percent":
        savings = round(purchase * (discount_value / 100.0), 2)
    elif discount_type == "fixed":
        savings = round(min(discount_value, purchase), 2)
    elif discount_type == "free":
        savings = round(purchase, 2)
    else:
        savings = 0.0

    redeemed_ts = _now_ts()
    await r.hset(
        _k_coupon(coupon_id),
        mapping={
            "status": "redeemed",
            "redeemed_at": _now_iso(),
            "redeemed_at_ts": str(redeemed_ts),
            "actual_purchase_amount": str(purchase),
            "savings_amount": str(savings),
            "merchant_pos_id": body.merchant_pos_id,
        },
    )

    # Analytics
    await _bump(r, brand_id, "coupons_redeemed")
    await _bump(r, brand_id, "total_savings_value_cents", int(round(savings * 100)))
    await _bump(r, brand_id, "total_purchase_amount_cents", int(round(purchase * 100)))

    await _log_transaction(
        r,
        brand_id,
        "coupon_redeemed",
        {
            "coupon_id": coupon_id,
            "user_id": data.get("user_id"),
            "merchant_pos_id": body.merchant_pos_id,
            "purchase_amount": purchase,
            "savings_amount": savings,
        },
    )

    # Compute upsell suggestion inline
    upsell: dict | None = None
    rules = await _load_upsell_rules(r, brand_id)
    for rule in rules:
        threshold = float(rule.get("when_amount_below", 0))
        if purchase < threshold:
            upsell = {
                "upgrade_text": rule.get("upgrade_text", ""),
                "upgrade_price": float(rule.get("upgrade_price", 0)),
                "upgrade_discount": float(rule.get("upgrade_discount", 0)),
            }
            break

    return {
        "ok": True,
        "coupon_id": coupon_id,
        "savings_amount": savings,
        "actual_purchase_amount": purchase,
        "upsell_suggestion": upsell,
    }


# ── 2. EnergyToPurchase ──────────────────────────────────────────────────


@router.post("/energy/configure")
async def configure_energy_packs(
    body: ConfigureEnergyPacksRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Merchant defines purchasable energy packs."""
    payload = [p.model_dump() for p in body.packs]
    await r.set(_k_energy_packs(body.brand_id), json.dumps(payload))
    return {"ok": True, "packs_count": len(payload)}


@router.post("/energy/buy")
async def buy_energy(
    body: BuyEnergyRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """User buys an energy pack. Payment is mocked (token is treated as
    authoritative — in production this would call a payment gateway)."""
    packs = await _load_packs(r, body.brand_id)
    pack = next((p for p in packs if p.get("id") == body.pack), None)
    if pack is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Energy pack '{body.pack}' not configured for brand",
        )

    if not body.payment_token or len(body.payment_token) < 4:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Invalid payment_token",
        )

    energy_amount = int(pack.get("energy", 0))
    price_cents = int(pack.get("price_cents", 0))
    currency = str(pack.get("currency", "CNY"))

    # Grant energy (separate from QR-grant Lua path; pure increment)
    new_balance = await r.incrby(
        _k_user_energy(body.user_id, body.brand_id), energy_amount
    )

    tx_id = await _log_transaction(
        r,
        body.brand_id,
        "energy_purchase",
        {
            "user_id": body.user_id,
            "pack_id": body.pack,
            "energy_granted": energy_amount,
            "price_cents": price_cents,
            "currency": currency,
            "payment_method": body.payment_method,
            "payment_token_hash": body.payment_token[:4] + "***",
        },
    )

    await _bump(r, body.brand_id, "energy_packs_sold")
    await _bump(r, body.brand_id, "energy_revenue_cents", price_cents)

    return {
        "ok": True,
        "energy_granted": energy_amount,
        "energy_balance": new_balance,
        "transaction_id": tx_id,
        "price_cents": price_cents,
        "currency": currency,
    }


# ── 3. RewardChain ───────────────────────────────────────────────────────


@router.get("/rewards/{user_id}")
async def get_rewards(
    user_id: str,
    brand_id: str = Query(...),
    expiring_window_days: int = Query(3, ge=1, le=30),
    r: aioredis.Redis = Depends(get_redis),
):
    """Return active vouchers + which ones are expiring soon."""
    coupon_ids: list[str] = await r.zrange(
        _k_user_coupons(user_id, brand_id), 0, -1
    )

    now_ts = _now_ts()
    soon_ts = now_ts + expiring_window_days * 86400

    active: list[dict] = []
    expiring_soon: list[dict] = []

    for cid in coupon_ids:
        data = await r.hgetall(_k_coupon(cid))
        if not data:
            continue
        if data.get("status") != "active":
            continue
        exp_ts = int(data.get("expires_at_ts", 0))
        if exp_ts and exp_ts < now_ts:
            # Lazy-expire
            await r.hset(_k_coupon(cid), "status", "expired")
            continue

        record = {
            "coupon_id": cid,
            "tier_name": data.get("tier_name"),
            "code": data.get("code"),
            "discount_type": data.get("discount_type"),
            "discount_value": float(data.get("discount_value", 0)),
            "expires_at": data.get("expires_at"),
            "days_left": max(0, (exp_ts - now_ts) // 86400) if exp_ts else None,
        }
        active.append(record)
        if exp_ts and exp_ts <= soon_ts:
            expiring_soon.append(record)

    return {
        "user_id": user_id,
        "brand_id": brand_id,
        "active_count": len(active),
        "active": active,
        "expiring_soon": expiring_soon,
    }


@router.post("/rewards/extend")
async def extend_reward(
    body: ExtendRewardRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Spend energy to extend a coupon's expiry by 7 days."""
    data = await _load_coupon(r, body.coupon_id)

    if data.get("user_id") != body.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Coupon does not belong to this user",
        )
    if data.get("status") != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot extend a {data.get('status')} coupon",
        )

    brand_id = data.get("brand_id", "")
    energy_key = _k_user_energy(body.user_id, brand_id)

    # Decrement energy atomically (negative balance disallowed)
    new_balance = await r.decrby(energy_key, body.cost_energy)
    if new_balance < 0:
        # Roll back
        await r.incrby(energy_key, body.cost_energy)
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Insufficient energy",
        )

    extend_seconds = 7 * 86400
    old_exp_ts = int(data.get("expires_at_ts", _now_ts()))
    new_exp_ts = max(old_exp_ts, _now_ts()) + extend_seconds
    new_exp_iso = datetime.fromtimestamp(new_exp_ts, tz=timezone.utc).isoformat()

    await r.hset(
        _k_coupon(body.coupon_id),
        mapping={
            "expires_at": new_exp_iso,
            "expires_at_ts": str(new_exp_ts),
        },
    )
    # Update the ZSET score so list ordering is fresh
    await r.zadd(
        _k_user_coupons(body.user_id, brand_id),
        {body.coupon_id: new_exp_ts},
    )

    await _log_transaction(
        r,
        brand_id,
        "reward_extended",
        {
            "coupon_id": body.coupon_id,
            "user_id": body.user_id,
            "cost_energy": body.cost_energy,
            "new_expires_at": new_exp_iso,
        },
    )

    return {
        "ok": True,
        "coupon_id": body.coupon_id,
        "new_expires_at": new_exp_iso,
        "energy_balance": new_balance,
    }


# ── 4. UpsellMoment ──────────────────────────────────────────────────────


@router.post("/upsell/configure")
async def configure_upsell(
    body: ConfigureUpsellRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Merchant defines upsell rules."""
    payload = [rule.model_dump() for rule in body.rules]
    await r.set(_k_upsell_rules(body.brand_id), json.dumps(payload))
    return {"ok": True, "rules_count": len(payload)}


@router.post("/upsell/suggest")
async def upsell_suggest(
    body: UpsellSuggestRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Given a cart amount + coupon, return the best matching upsell rule.

    The 'best match' is the rule with the LOWEST when_amount_below that is
    still above current_cart_amount — i.e. the closest threshold the user
    can reach with the smallest extra spend.
    """
    rules = await _load_upsell_rules(r, body.brand_id)
    if not rules:
        return {"suggestion": None}

    candidates = [
        rule
        for rule in rules
        if body.current_cart_amount < float(rule.get("when_amount_below", 0))
    ]
    if not candidates:
        return {"suggestion": None}

    best = min(candidates, key=lambda r_: float(r_.get("when_amount_below", 0)))

    # Compute savings_if_accept: discount applied to upgrade_price
    upgrade_price = float(best.get("upgrade_price", 0))
    upgrade_discount = float(best.get("upgrade_discount", 0))
    savings_if_accept = round(upgrade_price * (upgrade_discount / 100.0), 2)

    return {
        "suggestion": best.get("upgrade_text", ""),
        "upgrade_price": upgrade_price,
        "upgrade_discount": upgrade_discount,
        "savings_if_accept": savings_if_accept,
        "threshold": float(best.get("when_amount_below", 0)),
    }


# ── 5. RedemptionStore ───────────────────────────────────────────────────


@router.post("/store/configure")
async def configure_store(
    body: ConfigureStoreRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Merchant defines redeemable store items."""
    key = _k_store_items(body.brand_id)
    # Clear & repopulate
    await r.delete(key)
    if body.items:
        mapping = {item.id: json.dumps(item.model_dump()) for item in body.items}
        await r.hset(key, mapping=mapping)
    return {"ok": True, "items_count": len(body.items)}


@router.get("/store/{brand_id}")
async def list_store(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Return all configured store items for a brand."""
    raw = await r.hgetall(_k_store_items(brand_id))
    items = []
    for _, item_json in raw.items():
        try:
            items.append(json.loads(item_json))
        except json.JSONDecodeError:
            continue
    items.sort(key=lambda x: x.get("point_cost", 0))
    return {"brand_id": brand_id, "count": len(items), "items": items}


@router.post("/store/purchase")
async def store_purchase(
    body: StorePurchaseRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """User spends points or stars to redeem a store item."""
    item_json = await r.hget(_k_store_items(body.brand_id), body.item_id)
    if not item_json:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Item '{body.item_id}' not found",
        )
    try:
        item = json.loads(item_json)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Corrupted item record",
        )

    stock = int(item.get("stock", 0))
    cost = int(item.get("point_cost", 0))
    if stock <= 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Item out of stock",
        )

    currency_key = (
        _k_user_points(body.user_id, body.brand_id)
        if body.currency == "points"
        else _k_user_stars(body.user_id, body.brand_id)
    )

    # Deduct currency atomically
    new_balance = await r.decrby(currency_key, cost)
    if new_balance < 0:
        await r.incrby(currency_key, cost)  # rollback
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"Insufficient {body.currency}",
        )

    # Decrement stock
    item["stock"] = stock - 1
    await r.hset(_k_store_items(body.brand_id), body.item_id, json.dumps(item))

    # Determine fulfillment kind
    category = item.get("category", "general").lower()
    if category in {"voucher", "coupon", "digital"}:
        fulfillment = "voucher_code"
        item_granted = {"voucher_code": _gen_coupon_code()}
    else:
        fulfillment = "physical_ship"
        item_granted = {"shipping_required": True, "item_id": body.item_id}

    tx_id = await _log_transaction(
        r,
        body.brand_id,
        "store_purchase",
        {
            "user_id": body.user_id,
            "item_id": body.item_id,
            "currency": body.currency,
            "currency_spent": cost,
            "fulfillment": fulfillment,
            "item_granted": item_granted,
        },
    )

    await _bump(r, body.brand_id, "store_purchases")
    await _bump(
        r,
        body.brand_id,
        f"store_{body.currency}_spent",
        cost,
    )

    return {
        "ok": True,
        "item_id": body.item_id,
        "item_granted": item_granted,
        "currency": body.currency,
        "currency_spent": cost,
        "currency_balance": new_balance,
        "fulfillment": fulfillment,
        "transaction_id": tx_id,
    }


# ── Analytics ────────────────────────────────────────────────────────────


@router.get("/analytics/{brand_id}")
async def analytics(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Aggregate counters for the brand. Cheap O(metrics) reads."""
    metrics = [
        "coupons_claimed",
        "coupons_redeemed",
        "total_savings_value_cents",
        "total_purchase_amount_cents",
        "energy_packs_sold",
        "energy_revenue_cents",
        "store_purchases",
        "store_points_spent",
        "store_stars_spent",
    ]
    pipe = r.pipeline()
    for m in metrics:
        pipe.get(_k_analytics_counter(brand_id, m))
    values = await pipe.execute()

    raw_counts = {
        m: (float(v) if v is not None else 0.0) for m, v in zip(metrics, values)
    }

    claimed = int(raw_counts["coupons_claimed"])
    redeemed = int(raw_counts["coupons_redeemed"])
    redemption_rate = (redeemed / claimed) if claimed > 0 else 0.0

    total_savings_value = round(raw_counts["total_savings_value_cents"] / 100.0, 2)
    total_purchase_amount = round(
        raw_counts["total_purchase_amount_cents"] / 100.0, 2
    )
    avg_purchase_amount = (
        round(total_purchase_amount / redeemed, 2) if redeemed > 0 else 0.0
    )
    energy_revenue = round(raw_counts["energy_revenue_cents"] / 100.0, 2)

    return {
        "brand_id": brand_id,
        "coupons_claimed": claimed,
        "coupons_redeemed": redeemed,
        "redemption_rate": round(redemption_rate, 4),
        "total_savings_value": total_savings_value,
        "total_purchase_amount": total_purchase_amount,
        "avg_purchase_amount": avg_purchase_amount,
        "energy_packs_sold": int(raw_counts["energy_packs_sold"]),
        "energy_revenue": energy_revenue,
        "store_purchases": int(raw_counts["store_purchases"]),
        "store_points_spent": int(raw_counts["store_points_spent"]),
        "store_stars_spent": int(raw_counts["store_stars_spent"]),
    }
