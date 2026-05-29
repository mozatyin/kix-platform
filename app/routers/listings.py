"""C2C Marketplace Listing primitive — 闲鱼 / 淘宝 / eBay style.

A seller (user) publishes a listing scoped to a marketplace brand. Buyers can
browse, search, make offers, and trigger the post-sale flow. Promotions
("推一下") boost a listing's search ranking and bill the seller via the
marketplace wallet's take-rate flow.

Pipeline:
    POST /create                          → listing:{lid} HASH
                                            brand:{bid}:listings:active ZSET (score=created_at)
                                            brand:{bid}:listings:by_category:{cat} ZSET
                                            user:{uid}:listings SET
                                            emit listing.created (best-effort)
    GET  /{lid}                           → single listing
    GET  /seller/{uid}                    → seller's listings (filterable)
    GET  /brand/{bid}/search              → faceted search across active listings
    POST /{lid}/update                    → field-level partial update
    POST /{lid}/promote                   → 推一下: bump search ranking, bill seller
    POST /{lid}/mark-sold                 → terminal: sold, triggers post-sale flow
    POST /{lid}/remove                    → terminal: removed
    POST /{lid}/offer                     → buyer creates an offer
    POST /offers/{oid}/accept             → seller accepts → marks sold at offer price
    POST /offers/{oid}/counter            → seller counter-offer (creates a new offer)
    POST /offers/{oid}/reject
    GET  /{lid}/offers                    → all offers on a listing

Redis schema:
    listing:{lid}                          HASH
    brand:{bid}:listings:active            ZSET score=created_at (or promotion boost)
    brand:{bid}:listings:by_category:{cat} ZSET score=created_at (or promotion boost)
    user:{uid}:listings                    SET
    listing:{lid}:offers                   LIST of offer_id (chronological)
    offer:{oid}                            HASH

Integration:
    * Promotion calls into the marketplace-charge endpoint on wallet.py
      using the seller's user-wallet as the source of funds.
    * Post-sale (mark-sold or offer-accept) emits an event on the
      ``events:listing`` stream so downstream attribution / reservations
      can pick it up.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ─────────────────────────────────────────────────────────────

_EVENT_STREAM = "events:listing"
_EVENT_STREAM_MAXLEN = 50_000
_MAX_PHOTOS = 9
_DEFAULT_PROMOTE_BOOST_SECONDS = 7 * 24 * 3600  # 7-day default ranking boost
_STATUSES = ("active", "sold", "expired", "removed")
_CONDITIONS = ("new", "like_new", "good", "fair", "poor")
_OFFER_STATUSES = ("open", "accepted", "rejected", "countered", "expired")


# ── Redis key helpers ─────────────────────────────────────────────────────


def _k_listing(lid: str) -> str:
    return f"listing:{lid}"


def _k_brand_active(bid: str) -> str:
    return f"brand:{bid}:listings:active"


def _k_brand_by_category(bid: str, category: str) -> str:
    return f"brand:{bid}:listings:by_category:{category}"


def _k_user_listings(uid: str) -> str:
    return f"user:{uid}:listings"


def _k_listing_offers(lid: str) -> str:
    return f"listing:{lid}:offers"


def _k_offer(oid: str) -> str:
    return f"offer:{oid}"


# ── Utils ─────────────────────────────────────────────────────────────────


def _now() -> int:
    return int(time.time())


def _new_lid() -> str:
    return f"lst_{uuid4().hex[:16]}"


def _new_oid() -> str:
    return f"ofr_{uuid4().hex[:16]}"


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)


def _safe_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def _hash_to_listing(state: dict[str, str]) -> dict[str, Any]:
    if not state:
        return {}
    return {
        "listing_id": state.get("listing_id", ""),
        "brand_id": state.get("brand_id", ""),
        "seller_user_id": state.get("seller_user_id", ""),
        "title": state.get("title", ""),
        "description": state.get("description", ""),
        "price_cents": int(state.get("price_cents") or 0),
        "currency": state.get("currency", "CNY"),
        "category": state.get("category", ""),
        "subcategory": state.get("subcategory") or None,
        "photos": _safe_loads(state.get("photos"), []),
        "condition": state.get("condition", "good"),
        "shipping_method": state.get("shipping_method") or None,
        "location": state.get("location") or None,
        "quantity": int(state.get("quantity") or 1),
        "status": state.get("status", "active"),
        "created_at": int(state.get("created_at") or 0),
        "updated_at": int(state.get("updated_at") or 0),
        "expires_at": int(state["expires_at"]) if state.get("expires_at") else None,
        "promoted_until": int(state["promoted_until"]) if state.get("promoted_until") else None,
        "promotion_boost": int(state.get("promotion_boost") or 0),
        "sold_at": int(state["sold_at"]) if state.get("sold_at") else None,
        "buyer_user_id": state.get("buyer_user_id") or None,
        "sale_price_cents": int(state["sale_price_cents"]) if state.get("sale_price_cents") else None,
        "transaction_id": state.get("transaction_id") or None,
        "removed_at": int(state["removed_at"]) if state.get("removed_at") else None,
        "removed_by": state.get("removed_by") or None,
        "removed_reason": state.get("removed_reason") or None,
        "metadata": _safe_loads(state.get("metadata"), {}),
    }


def _hash_to_offer(state: dict[str, str]) -> dict[str, Any]:
    if not state:
        return {}
    return {
        "offer_id": state.get("offer_id", ""),
        "listing_id": state.get("listing_id", ""),
        "brand_id": state.get("brand_id", ""),
        "buyer_user_id": state.get("buyer_user_id", ""),
        "seller_user_id": state.get("seller_user_id", ""),
        "offer_price_cents": int(state.get("offer_price_cents") or 0),
        "currency": state.get("currency", "CNY"),
        "message": state.get("message") or None,
        "status": state.get("status", "open"),
        "created_at": int(state.get("created_at") or 0),
        "updated_at": int(state.get("updated_at") or 0),
        "counter_of": state.get("counter_of") or None,
        "countered_by": state.get("countered_by") or None,
    }


async def _emit_event(
    r: aioredis.Redis,
    *,
    event_type: str,
    listing_id: str,
    brand_id: str,
    user_id: str,
    extra: dict[str, Any] | None = None,
) -> None:
    payload = {
        "event_type": event_type,
        "listing_id": listing_id,
        "brand_id": brand_id,
        "user_id": user_id,
        "at": str(_now()),
    }
    if extra:
        payload["extra"] = _dumps(extra)
    try:
        await r.xadd(
            _EVENT_STREAM,
            payload,
            maxlen=_EVENT_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("listing event xadd failed: %s", exc)


# ── Pydantic models ───────────────────────────────────────────────────────


class CreateListingRequest(BaseModel):
    brand_id: str = Field(..., min_length=1, max_length=128)
    seller_user_id: str = Field(..., min_length=1, max_length=128)
    title: str = Field(..., min_length=1, max_length=256)
    description: str = Field("", max_length=8192)
    price_cents: int = Field(..., ge=0, le=10_000_000_000)
    currency: str = Field("CNY", min_length=3, max_length=8)
    category: str = Field(..., min_length=1, max_length=64)
    subcategory: str | None = Field(None, max_length=64)
    photos: list[str] = Field(default_factory=list, max_length=_MAX_PHOTOS)
    condition: Literal["new", "like_new", "good", "fair", "poor"] = "good"
    shipping_method: str | None = Field(None, max_length=64)
    location: str | None = Field(None, max_length=128)
    quantity: int = Field(1, ge=1, le=100_000)
    expires_at: int | None = Field(None, gt=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateListingResponse(BaseModel):
    listing_id: str
    status: str


class UpdateListingRequest(BaseModel):
    seller_user_id: str = Field(..., min_length=1)
    title: str | None = Field(None, min_length=1, max_length=256)
    description: str | None = Field(None, max_length=8192)
    price_cents: int | None = Field(None, ge=0, le=10_000_000_000)
    photos: list[str] | None = Field(None, max_length=_MAX_PHOTOS)
    condition: Literal["new", "like_new", "good", "fair", "poor"] | None = None
    shipping_method: str | None = Field(None, max_length=64)
    location: str | None = Field(None, max_length=128)
    quantity: int | None = Field(None, ge=1, le=100_000)
    expires_at: int | None = Field(None, gt=0)
    metadata: dict[str, Any] | None = None


class PromoteRequest(BaseModel):
    seller_user_id: str = Field(..., min_length=1)
    duration_hours: int = Field(..., ge=1, le=24 * 90)
    payment_method: str = Field("user_wallet", max_length=32)
    boost_amount_cents: int = Field(500, ge=1, le=10_000_000)


class MarkSoldRequest(BaseModel):
    buyer_user_id: str = Field(..., min_length=1)
    sale_price_cents: int = Field(..., ge=0, le=10_000_000_000)
    transaction_id: str | None = Field(None, max_length=128)


class RemoveRequest(BaseModel):
    by: Literal["seller", "admin"] = "seller"
    reason: str = Field("", max_length=500)


class OfferCreateRequest(BaseModel):
    buyer_user_id: str = Field(..., min_length=1, max_length=128)
    offer_price_cents: int = Field(..., ge=0, le=10_000_000_000)
    message: str | None = Field(None, max_length=1024)


class OfferAcceptRequest(BaseModel):
    seller_user_id: str = Field(..., min_length=1)


class OfferCounterRequest(BaseModel):
    seller_user_id: str = Field(..., min_length=1)
    counter_price_cents: int = Field(..., ge=0, le=10_000_000_000)
    message: str | None = Field(None, max_length=1024)


class OfferRejectRequest(BaseModel):
    seller_user_id: str = Field(..., min_length=1)
    reason: str = Field("", max_length=500)


# ── Internal helpers ──────────────────────────────────────────────────────


async def _load_listing(r: aioredis.Redis, lid: str) -> dict[str, str]:
    state = await r.hgetall(_k_listing(lid))
    if not state:
        raise HTTPException(status_code=404, detail="listing not found")
    return state


async def _load_offer(r: aioredis.Redis, oid: str) -> dict[str, str]:
    state = await r.hgetall(_k_offer(oid))
    if not state:
        raise HTTPException(status_code=404, detail="offer not found")
    return state


def _ranking_score(created_at: int, promotion_boost: int) -> float:
    """Search ranking score = created_at + promotion_boost.

    Promoted listings sort above older organic listings; among promoted
    items, more recent or more aggressive promotions win.
    """
    return float(created_at + promotion_boost)


async def _remove_from_indices(
    r: aioredis.Redis,
    pipe: Any,
    *,
    lid: str,
    brand_id: str,
    category: str,
) -> None:
    pipe.zrem(_k_brand_active(brand_id), lid)
    if category:
        pipe.zrem(_k_brand_by_category(brand_id, category), lid)


# ── Endpoints: lifecycle ──────────────────────────────────────────────────


@router.post(
    "/create",
    response_model=CreateListingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new C2C listing",
)
async def create_listing(
    body: CreateListingRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> CreateListingResponse:
    if body.expires_at is not None and body.expires_at <= _now():
        raise HTTPException(
            status_code=400, detail="expires_at must be in the future"
        )
    if len(body.photos) > _MAX_PHOTOS:
        raise HTTPException(
            status_code=400, detail=f"photos exceeds max {_MAX_PHOTOS}"
        )

    lid = _new_lid()
    now = _now()
    score = _ranking_score(now, 0)

    state: dict[str, str] = {
        "listing_id": lid,
        "brand_id": body.brand_id,
        "seller_user_id": body.seller_user_id,
        "title": body.title,
        "description": body.description,
        "price_cents": str(body.price_cents),
        "currency": body.currency.upper(),
        "category": body.category,
        "subcategory": body.subcategory or "",
        "photos": _dumps(body.photos),
        "condition": body.condition,
        "shipping_method": body.shipping_method or "",
        "location": body.location or "",
        "quantity": str(body.quantity),
        "status": "active",
        "created_at": str(now),
        "updated_at": str(now),
        "promotion_boost": "0",
        "metadata": _dumps(body.metadata),
    }
    if body.expires_at is not None:
        state["expires_at"] = str(body.expires_at)

    pipe = r.pipeline()
    pipe.hset(_k_listing(lid), mapping=state)
    pipe.zadd(_k_brand_active(body.brand_id), {lid: score})
    pipe.zadd(_k_brand_by_category(body.brand_id, body.category), {lid: score})
    pipe.sadd(_k_user_listings(body.seller_user_id), lid)
    await pipe.execute()

    await _emit_event(
        r,
        event_type="listing.created",
        listing_id=lid,
        brand_id=body.brand_id,
        user_id=body.seller_user_id,
        extra={
            "price_cents": body.price_cents,
            "category": body.category,
            "currency": body.currency.upper(),
        },
    )
    logger.info(
        "listing created: lid=%s brand=%s seller=%s price=%s",
        lid, body.brand_id, body.seller_user_id, body.price_cents,
    )
    return CreateListingResponse(listing_id=lid, status="active")


@router.get("/{listing_id}", summary="Get a single listing")
async def get_listing(
    listing_id: str, r: aioredis.Redis = Depends(get_redis)
) -> dict[str, Any]:
    state = await _load_listing(r, listing_id)
    return _hash_to_listing(state)


@router.get("/seller/{seller_user_id}", summary="List a seller's listings")
async def list_seller_listings(
    seller_user_id: str,
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    if status and status not in _STATUSES:
        raise HTTPException(status_code=400, detail=f"unknown status: {status}")
    lids = await r.smembers(_k_user_listings(seller_user_id))
    items: list[dict[str, Any]] = []
    if lids:
        pipe = r.pipeline()
        for lid in lids:
            pipe.hgetall(_k_listing(lid))
        rows = await pipe.execute()
        for state in rows:
            if not state:
                continue
            if status and state.get("status") != status:
                continue
            items.append(_hash_to_listing(state))
    # Newest first.
    items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    if len(items) > limit:
        items = items[:limit]
    return {
        "seller_user_id": seller_user_id,
        "count": len(items),
        "listings": items,
    }


@router.get(
    "/brand/{brand_id}/search",
    summary="Faceted search of a marketplace's active listings",
)
async def search_brand_listings(
    brand_id: str,
    category: str | None = Query(None),
    price_min: int | None = Query(None, ge=0),
    price_max: int | None = Query(None, ge=0),
    condition: str | None = Query(None),
    location: str | None = Query(None),
    sort: Literal["newest", "price_asc", "price_desc"] = Query("newest"),
    cursor: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    if condition and condition not in _CONDITIONS:
        raise HTTPException(
            status_code=400, detail=f"unknown condition: {condition}"
        )
    # Pull a wide slab from the right index (highest score first = newest /
    # promoted on top), then filter in memory.
    index_key = (
        _k_brand_by_category(brand_id, category)
        if category
        else _k_brand_active(brand_id)
    )
    # Heuristic: overfetch 5x to absorb filter losses.
    slab = min(limit * 5 + cursor, 5000)
    lids = await r.zrevrange(index_key, 0, slab - 1)

    items: list[dict[str, Any]] = []
    if lids:
        pipe = r.pipeline()
        for lid in lids:
            pipe.hgetall(_k_listing(lid))
        rows = await pipe.execute()
        for state in rows:
            if not state:
                continue
            if state.get("status") != "active":
                continue
            price = int(state.get("price_cents") or 0)
            if price_min is not None and price < price_min:
                continue
            if price_max is not None and price > price_max:
                continue
            if condition and state.get("condition") != condition:
                continue
            if location and state.get("location") != location:
                continue
            items.append(_hash_to_listing(state))

    # Sort.
    if sort == "price_asc":
        items.sort(key=lambda x: x.get("price_cents", 0))
    elif sort == "price_desc":
        items.sort(key=lambda x: x.get("price_cents", 0), reverse=True)
    # newest = already ordered by index desc; no-op.

    # Cursor paginate in-memory after filter.
    page = items[cursor : cursor + limit]
    next_cursor = cursor + len(page) if (cursor + len(page)) < len(items) else None

    return {
        "brand_id": brand_id,
        "count": len(page),
        "total_matched": len(items),
        "next_cursor": next_cursor,
        "listings": page,
    }


@router.post("/{listing_id}/update", summary="Update a listing's fields")
async def update_listing(
    listing_id: str,
    body: UpdateListingRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    state = await _load_listing(r, listing_id)
    if state.get("seller_user_id") != body.seller_user_id:
        raise HTTPException(
            status_code=403, detail="only the seller can update this listing"
        )
    if state.get("status") != "active":
        raise HTTPException(
            status_code=409,
            detail=f"cannot update listing in status={state.get('status')}",
        )

    now = _now()
    mapping: dict[str, str] = {"updated_at": str(now)}
    if body.title is not None:
        mapping["title"] = body.title
    if body.description is not None:
        mapping["description"] = body.description
    if body.price_cents is not None:
        mapping["price_cents"] = str(body.price_cents)
    if body.photos is not None:
        if len(body.photos) > _MAX_PHOTOS:
            raise HTTPException(
                status_code=400, detail=f"photos exceeds max {_MAX_PHOTOS}"
            )
        mapping["photos"] = _dumps(body.photos)
    if body.condition is not None:
        mapping["condition"] = body.condition
    if body.shipping_method is not None:
        mapping["shipping_method"] = body.shipping_method
    if body.location is not None:
        mapping["location"] = body.location
    if body.quantity is not None:
        mapping["quantity"] = str(body.quantity)
    if body.expires_at is not None:
        if body.expires_at <= now:
            raise HTTPException(
                status_code=400, detail="expires_at must be in the future"
            )
        mapping["expires_at"] = str(body.expires_at)
    if body.metadata is not None:
        mapping["metadata"] = _dumps(body.metadata)

    if len(mapping) == 1:  # only updated_at
        raise HTTPException(status_code=400, detail="no fields supplied")

    await r.hset(_k_listing(listing_id), mapping=mapping)
    new_state = await r.hgetall(_k_listing(listing_id))
    await _emit_event(
        r,
        event_type="listing.updated",
        listing_id=listing_id,
        brand_id=new_state.get("brand_id", ""),
        user_id=body.seller_user_id,
        extra={"fields": [k for k in mapping if k != "updated_at"]},
    )
    return _hash_to_listing(new_state)


@router.post(
    "/{listing_id}/promote",
    summary="推一下: pay to boost search ranking, bills the seller",
)
async def promote_listing(
    listing_id: str,
    body: PromoteRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    state = await _load_listing(r, listing_id)
    if state.get("seller_user_id") != body.seller_user_id:
        raise HTTPException(
            status_code=403, detail="only the seller can promote this listing"
        )
    if state.get("status") != "active":
        raise HTTPException(
            status_code=409,
            detail=f"cannot promote listing in status={state.get('status')}",
        )

    brand_id = state.get("brand_id", "")
    category = state.get("category", "")
    now = _now()
    promoted_until = now + body.duration_hours * 3600

    # Charge the seller's user-wallet → marketplace's brand wallet.
    # Lightweight inline implementation that mirrors the take-rate ledger;
    # the dedicated marketplace-charge endpoint in wallet.py handles the
    # transactional path during actual sales. Promotion is a flat fee.
    user_wallet_key = f"wallet:user:{body.seller_user_id}:balance"
    user_balance = int(await r.get(user_wallet_key) or 0)
    if user_balance < body.boost_amount_cents:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "insufficient_user_wallet",
                "balance_cents": user_balance,
                "required_cents": body.boost_amount_cents,
            },
        )

    charge_id = f"prm_{uuid4().hex[:16]}"
    boost_seconds = body.duration_hours * 3600
    # Promotion bumps ranking score by remaining boost seconds — strongest
    # boost first, decays as time passes.
    promotion_boost = int(state.get("promotion_boost") or 0) + boost_seconds
    created_at = int(state.get("created_at") or now)
    new_score = _ranking_score(created_at, promotion_boost)

    pipe = r.pipeline()
    pipe.decrby(user_wallet_key, body.boost_amount_cents)
    pipe.incrby(f"wallet:{brand_id}:balance", body.boost_amount_cents)
    pipe.incrby(f"wallet:{brand_id}:total_spent", 0)  # no-op, audit anchor
    pipe.hset(
        _k_listing(listing_id),
        mapping={
            "promotion_boost": str(promotion_boost),
            "promoted_until": str(promoted_until),
            "updated_at": str(now),
        },
    )
    pipe.zadd(_k_brand_active(brand_id), {listing_id: new_score})
    if category:
        pipe.zadd(
            _k_brand_by_category(brand_id, category),
            {listing_id: new_score},
        )
    pipe.hset(
        f"wallet:charge:{charge_id}",
        mapping={
            "charge_id": charge_id,
            "brand_id": brand_id,
            "amount": body.boost_amount_cents,
            "reason": "listing_promotion",
            "reference_id": listing_id,
            "ts": str(now),
            "status": "completed",
            "seller_user_id": body.seller_user_id,
        },
    )
    pipe.rpush(f"wallet:{brand_id}:transactions", charge_id)
    pipe.ltrim(f"wallet:{brand_id}:transactions", -10_000, -1)
    await pipe.execute()

    await _emit_event(
        r,
        event_type="listing.promoted",
        listing_id=listing_id,
        brand_id=brand_id,
        user_id=body.seller_user_id,
        extra={
            "duration_hours": body.duration_hours,
            "boost_amount_cents": body.boost_amount_cents,
            "promoted_until": promoted_until,
        },
    )

    return {
        "listing_id": listing_id,
        "charge_id": charge_id,
        "promoted_until": promoted_until,
        "promotion_boost": promotion_boost,
        "new_ranking_score": new_score,
        "charged_cents": body.boost_amount_cents,
    }


@router.post("/{listing_id}/mark-sold", summary="Mark listing sold")
async def mark_sold(
    listing_id: str,
    body: MarkSoldRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    state = await _load_listing(r, listing_id)
    if state.get("status") not in ("active",):
        raise HTTPException(
            status_code=409,
            detail=f"cannot mark sold from status={state.get('status')}",
        )

    brand_id = state.get("brand_id", "")
    seller_user_id = state.get("seller_user_id", "")
    category = state.get("category", "")
    now = _now()

    pipe = r.pipeline()
    pipe.hset(
        _k_listing(listing_id),
        mapping={
            "status": "sold",
            "sold_at": str(now),
            "buyer_user_id": body.buyer_user_id,
            "sale_price_cents": str(body.sale_price_cents),
            "transaction_id": body.transaction_id or "",
            "updated_at": str(now),
        },
    )
    await _remove_from_indices(
        r, pipe, lid=listing_id, brand_id=brand_id, category=category
    )
    await pipe.execute()

    await _emit_event(
        r,
        event_type="listing.sold",
        listing_id=listing_id,
        brand_id=brand_id,
        user_id=seller_user_id,
        extra={
            "buyer_user_id": body.buyer_user_id,
            "sale_price_cents": body.sale_price_cents,
            "transaction_id": body.transaction_id,
        },
    )

    return {
        "listing_id": listing_id,
        "status": "sold",
        "sold_at": now,
        "sale_price_cents": body.sale_price_cents,
        "buyer_user_id": body.buyer_user_id,
    }


@router.post("/{listing_id}/remove", summary="Remove a listing")
async def remove_listing(
    listing_id: str,
    body: RemoveRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    state = await _load_listing(r, listing_id)
    if state.get("status") not in ("active",):
        raise HTTPException(
            status_code=409,
            detail=f"cannot remove listing in status={state.get('status')}",
        )

    brand_id = state.get("brand_id", "")
    seller_user_id = state.get("seller_user_id", "")
    category = state.get("category", "")
    now = _now()

    pipe = r.pipeline()
    pipe.hset(
        _k_listing(listing_id),
        mapping={
            "status": "removed",
            "removed_at": str(now),
            "removed_by": body.by,
            "removed_reason": body.reason,
            "updated_at": str(now),
        },
    )
    await _remove_from_indices(
        r, pipe, lid=listing_id, brand_id=brand_id, category=category
    )
    await pipe.execute()

    await _emit_event(
        r,
        event_type="listing.removed",
        listing_id=listing_id,
        brand_id=brand_id,
        user_id=seller_user_id,
        extra={"by": body.by, "reason": body.reason},
    )
    return {"listing_id": listing_id, "status": "removed", "removed_at": now}


# ── Endpoints: offers ─────────────────────────────────────────────────────


@router.post(
    "/{listing_id}/offer",
    status_code=status.HTTP_201_CREATED,
    summary="Buyer creates an offer on a listing",
)
async def create_offer(
    listing_id: str,
    body: OfferCreateRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    state = await _load_listing(r, listing_id)
    if state.get("status") != "active":
        raise HTTPException(
            status_code=409,
            detail=f"cannot offer on listing in status={state.get('status')}",
        )

    seller_user_id = state.get("seller_user_id", "")
    if body.buyer_user_id == seller_user_id:
        raise HTTPException(
            status_code=400, detail="seller cannot offer on own listing"
        )

    brand_id = state.get("brand_id", "")
    currency = state.get("currency", "CNY")
    oid = _new_oid()
    now = _now()

    pipe = r.pipeline()
    pipe.hset(
        _k_offer(oid),
        mapping={
            "offer_id": oid,
            "listing_id": listing_id,
            "brand_id": brand_id,
            "buyer_user_id": body.buyer_user_id,
            "seller_user_id": seller_user_id,
            "offer_price_cents": str(body.offer_price_cents),
            "currency": currency,
            "message": body.message or "",
            "status": "open",
            "created_at": str(now),
            "updated_at": str(now),
        },
    )
    pipe.rpush(_k_listing_offers(listing_id), oid)
    pipe.ltrim(_k_listing_offers(listing_id), -1000, -1)
    # Notify seller (best-effort).
    pipe.lpush(
        f"user:{seller_user_id}:notifications",
        _dumps(
            {
                "kind": "listing_offer",
                "listing_id": listing_id,
                "offer_id": oid,
                "buyer_user_id": body.buyer_user_id,
                "offer_price_cents": body.offer_price_cents,
                "currency": currency,
                "ts": now,
            }
        ),
    )
    pipe.ltrim(f"user:{seller_user_id}:notifications", 0, 199)
    await pipe.execute()

    await _emit_event(
        r,
        event_type="listing.offer_created",
        listing_id=listing_id,
        brand_id=brand_id,
        user_id=body.buyer_user_id,
        extra={"offer_id": oid, "offer_price_cents": body.offer_price_cents},
    )

    return {"offer_id": oid, "status": "open"}


@router.post(
    "/offers/{offer_id}/accept",
    summary="Seller accepts an offer → marks listing sold at offer price",
)
async def accept_offer(
    offer_id: str,
    body: OfferAcceptRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    offer = await _load_offer(r, offer_id)
    if offer.get("seller_user_id") != body.seller_user_id:
        raise HTTPException(
            status_code=403, detail="only the seller can accept this offer"
        )
    if offer.get("status") != "open":
        raise HTTPException(
            status_code=409,
            detail=f"cannot accept offer in status={offer.get('status')}",
        )

    lid = offer.get("listing_id", "")
    state = await _load_listing(r, lid)
    if state.get("status") != "active":
        raise HTTPException(
            status_code=409,
            detail=f"listing not active (status={state.get('status')})",
        )

    brand_id = state.get("brand_id", "")
    category = state.get("category", "")
    buyer_user_id = offer.get("buyer_user_id", "")
    sale_price = int(offer.get("offer_price_cents") or 0)
    now = _now()

    pipe = r.pipeline()
    pipe.hset(
        _k_offer(offer_id),
        mapping={"status": "accepted", "updated_at": str(now)},
    )
    pipe.hset(
        _k_listing(lid),
        mapping={
            "status": "sold",
            "sold_at": str(now),
            "buyer_user_id": buyer_user_id,
            "sale_price_cents": str(sale_price),
            "updated_at": str(now),
        },
    )
    await _remove_from_indices(
        r, pipe, lid=lid, brand_id=brand_id, category=category
    )
    await pipe.execute()

    await _emit_event(
        r,
        event_type="listing.offer_accepted",
        listing_id=lid,
        brand_id=brand_id,
        user_id=body.seller_user_id,
        extra={
            "offer_id": offer_id,
            "buyer_user_id": buyer_user_id,
            "sale_price_cents": sale_price,
        },
    )

    return {
        "offer_id": offer_id,
        "listing_id": lid,
        "status": "accepted",
        "sale_price_cents": sale_price,
        "buyer_user_id": buyer_user_id,
    }


@router.post(
    "/offers/{offer_id}/counter",
    summary="Seller counters an open offer with a new price",
)
async def counter_offer(
    offer_id: str,
    body: OfferCounterRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    offer = await _load_offer(r, offer_id)
    if offer.get("seller_user_id") != body.seller_user_id:
        raise HTTPException(
            status_code=403, detail="only the seller can counter this offer"
        )
    if offer.get("status") != "open":
        raise HTTPException(
            status_code=409,
            detail=f"cannot counter offer in status={offer.get('status')}",
        )

    lid = offer.get("listing_id", "")
    brand_id = offer.get("brand_id", "")
    buyer_user_id = offer.get("buyer_user_id", "")
    currency = offer.get("currency", "CNY")
    now = _now()
    new_oid = _new_oid()

    pipe = r.pipeline()
    pipe.hset(
        _k_offer(offer_id),
        mapping={
            "status": "countered",
            "countered_by": new_oid,
            "updated_at": str(now),
        },
    )
    pipe.hset(
        _k_offer(new_oid),
        mapping={
            "offer_id": new_oid,
            "listing_id": lid,
            "brand_id": brand_id,
            "buyer_user_id": buyer_user_id,
            "seller_user_id": body.seller_user_id,
            "offer_price_cents": str(body.counter_price_cents),
            "currency": currency,
            "message": body.message or "",
            "status": "open",
            "created_at": str(now),
            "updated_at": str(now),
            "counter_of": offer_id,
        },
    )
    pipe.rpush(_k_listing_offers(lid), new_oid)
    pipe.ltrim(_k_listing_offers(lid), -1000, -1)
    pipe.lpush(
        f"user:{buyer_user_id}:notifications",
        _dumps(
            {
                "kind": "listing_counter",
                "listing_id": lid,
                "offer_id": new_oid,
                "counter_of": offer_id,
                "counter_price_cents": body.counter_price_cents,
                "currency": currency,
                "ts": now,
            }
        ),
    )
    pipe.ltrim(f"user:{buyer_user_id}:notifications", 0, 199)
    await pipe.execute()

    await _emit_event(
        r,
        event_type="listing.offer_countered",
        listing_id=lid,
        brand_id=brand_id,
        user_id=body.seller_user_id,
        extra={
            "original_offer_id": offer_id,
            "counter_offer_id": new_oid,
            "counter_price_cents": body.counter_price_cents,
        },
    )

    return {
        "original_offer_id": offer_id,
        "counter_offer_id": new_oid,
        "status": "countered",
        "counter_price_cents": body.counter_price_cents,
    }


@router.post(
    "/offers/{offer_id}/reject", summary="Seller rejects an open offer"
)
async def reject_offer(
    offer_id: str,
    body: OfferRejectRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    offer = await _load_offer(r, offer_id)
    if offer.get("seller_user_id") != body.seller_user_id:
        raise HTTPException(
            status_code=403, detail="only the seller can reject this offer"
        )
    if offer.get("status") != "open":
        raise HTTPException(
            status_code=409,
            detail=f"cannot reject offer in status={offer.get('status')}",
        )

    lid = offer.get("listing_id", "")
    brand_id = offer.get("brand_id", "")
    now = _now()

    await r.hset(
        _k_offer(offer_id),
        mapping={
            "status": "rejected",
            "updated_at": str(now),
            "reject_reason": body.reason,
        },
    )

    await _emit_event(
        r,
        event_type="listing.offer_rejected",
        listing_id=lid,
        brand_id=brand_id,
        user_id=body.seller_user_id,
        extra={"offer_id": offer_id, "reason": body.reason},
    )
    return {"offer_id": offer_id, "status": "rejected"}


@router.get("/{listing_id}/offers", summary="List offers on a listing")
async def list_offers(
    listing_id: str,
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    if status and status not in _OFFER_STATUSES:
        raise HTTPException(status_code=400, detail=f"unknown status: {status}")
    oids = await r.lrange(_k_listing_offers(listing_id), -limit, -1)
    oids.reverse()  # newest first
    items: list[dict[str, Any]] = []
    if oids:
        pipe = r.pipeline()
        for oid in oids:
            pipe.hgetall(_k_offer(oid))
        rows = await pipe.execute()
        for st in rows:
            if not st:
                continue
            if status and st.get("status") != status:
                continue
            items.append(_hash_to_offer(st))
    return {
        "listing_id": listing_id,
        "count": len(items),
        "offers": items,
    }
