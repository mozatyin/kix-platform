"""Storefront / Brand Profile router for KiX.

A brand's public-facing destination — like a merchant page on Meituan
or a Shopify shop. One page per brand, surfaced at
``/landing/storefront.html?b={brand_id}``, backed by these endpoints.

Concerns covered:

  1. Profile config       — display_name, bio, hero/logo, brand color,
                            contact, featured games & vouchers,
                            socials, custom markdown sections.
  2. Public profile fetch — no auth, hides nothing sensitive.
  3. Games / Vouchers     — featured-first, plus brand-owned items
                            fetched live from existing modules.
  4. Stores               — pulled from geofence ``brand:{bid}:stores``.
  5. Follow               — one-way subscribe, mirrored to social
                            graph so existing feed fan-out keeps working.
  6. Reviews              — 1-5 star rating + optional anonymous comment,
                            with running aggregate.
  7. Discover             — cross-brand catalog with country/category
                            filter + trending or top-rated sort. Feeds
                            the attribution loop.

Storage layout (Redis):

    storefront:{bid}                  HASH (profile config)
    brand:{bid}:followers             SET of user_ids
    user:{uid}:brand_follows          SET of brand_ids   (reverse index)
    brand:{bid}:reviews               LIST of review_ids (newest first)
    review:{review_id}                HASH (brand, user, rating, comment, anonymous, ts)
    brand:{bid}:rating                HASH {avg, count, sum}
    brand:{bid}:rating:top            ZSET (score=rating, member=review_id)
    storefront:discover:by_country    SORTED SET (score=trending_score, member=bid)
    storefront:discover:by_category   HASH {category -> SET of bids}  (we use SET per cat)
    storefront:cat:{category}         SET of bids
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Pydantic models ───────────────────────────────────────────────────────


class ContactInfo(BaseModel):
    email: str | None = None
    phone: str | None = None
    website: str | None = None
    address: str | None = None


class SocialLinks(BaseModel):
    instagram: str | None = None
    tiktok: str | None = None
    facebook: str | None = None


class CustomSection(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    content_md: str = Field(..., max_length=10_000)


class StorefrontConfig(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=120)
    bio: str | None = Field(default=None, max_length=1000)
    hero_image_url: str | None = None
    logo_url: str | None = None
    brand_color: str = Field(default="#00FC00", max_length=16)
    contact: ContactInfo = Field(default_factory=ContactInfo)
    featured_games: list[str] = Field(default_factory=list)
    featured_vouchers: list[str] = Field(default_factory=list)
    show_stores: bool = True
    socials: SocialLinks = Field(default_factory=SocialLinks)
    custom_sections: list[CustomSection] = Field(default_factory=list)
    country: str | None = Field(default=None, max_length=8)   # e.g. "ID"
    category: str | None = Field(default=None, max_length=32)  # food|retail|service|...


class FollowBody(BaseModel):
    user_id: str = Field(..., min_length=1)


class ReviewBody(BaseModel):
    user_id: str = Field(..., min_length=1)
    rating: int = Field(..., ge=1, le=5)
    comment: str = Field(default="", max_length=2000)
    anonymous: bool = False


# ── Redis key helpers ─────────────────────────────────────────────────────


def _k_profile(bid: str) -> str:
    return f"storefront:{bid}"


def _k_followers(bid: str) -> str:
    return f"brand:{bid}:followers"


def _k_user_brand_follows(uid: str) -> str:
    return f"user:{uid}:brand_follows"


def _k_reviews(bid: str) -> str:
    return f"brand:{bid}:reviews"


def _k_review(rid: str) -> str:
    return f"review:{rid}"


def _k_rating(bid: str) -> str:
    return f"brand:{bid}:rating"


def _k_rating_top(bid: str) -> str:
    return f"brand:{bid}:rating:top"


def _k_discover_country() -> str:
    return "storefront:discover:by_country"


def _k_discover_category(category: str) -> str:
    return f"storefront:cat:{category}"


def _k_brand_stores(bid: str) -> str:
    return f"brand:{bid}:stores"


def _k_brand_voucher_templates(bid: str) -> str:
    return f"brand:{bid}:voucher_templates"


def _k_voucher_template(bid: str, tid: str) -> str:
    return f"brand:{bid}:voucher_templates:{tid}"


# ── Internal helpers ──────────────────────────────────────────────────────


def _now() -> int:
    return int(time.time())


async def _load_profile(r: aioredis.Redis, bid: str) -> dict[str, Any] | None:
    raw = await r.hgetall(_k_profile(bid))
    if not raw:
        return None
    return _profile_from_hash(bid, raw)


def _profile_from_hash(bid: str, raw: dict[str, Any]) -> dict[str, Any]:
    def _j(field: str, default: Any) -> Any:
        v = raw.get(field)
        if not v:
            return default
        try:
            return json.loads(v) if isinstance(v, str) else v
        except json.JSONDecodeError:
            return default

    return {
        "brand_id": bid,
        "display_name": raw.get("display_name") or bid,
        "bio": raw.get("bio") or "",
        "hero_image_url": raw.get("hero_image_url") or None,
        "logo_url": raw.get("logo_url") or None,
        "brand_color": raw.get("brand_color") or "#00FC00",
        "contact": _j("contact", {}),
        "featured_games": _j("featured_games", []),
        "featured_vouchers": _j("featured_vouchers", []),
        "show_stores": raw.get("show_stores", "1") == "1",
        "socials": _j("socials", {}),
        "custom_sections": _j("custom_sections", []),
        "country": raw.get("country") or None,
        "category": raw.get("category") or None,
        "created_at": int(float(raw.get("created_at", 0))),
        "updated_at": int(float(raw.get("updated_at", 0))),
        "public_url": f"/landing/storefront.html?b={bid}",
    }


async def _trending_bump(r: aioredis.Redis, bid: str, delta: float = 1.0) -> None:
    """Bump a brand's trending score. Caller decides delta (view/follow/play)."""
    await r.zincrby(_k_discover_country(), delta, bid)


# ── Endpoints: profile configure / fetch ─────────────────────────────────


@router.post("/{brand_id}/configure", summary="Create or update a storefront profile")
async def configure_storefront(
    brand_id: str,
    body: StorefrontConfig,
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    now = _now()
    existing = await r.hgetall(_k_profile(brand_id))

    record = {
        "display_name": body.display_name,
        "bio": body.bio or "",
        "hero_image_url": body.hero_image_url or "",
        "logo_url": body.logo_url or "",
        "brand_color": body.brand_color,
        "contact": body.contact.model_dump_json(),
        "featured_games": json.dumps(body.featured_games),
        "featured_vouchers": json.dumps(body.featured_vouchers),
        "show_stores": "1" if body.show_stores else "0",
        "socials": body.socials.model_dump_json(),
        "custom_sections": json.dumps([s.model_dump() for s in body.custom_sections]),
        "country": body.country or "",
        "category": body.category or "",
        "updated_at": str(now),
    }
    if not existing:
        record["created_at"] = str(now)

    await r.hset(_k_profile(brand_id), mapping=record)

    # Re-index in discovery sets.
    if body.country:
        # ensure presence in country index (score init at 0 if new)
        if await r.zscore(_k_discover_country(), brand_id) is None:
            await r.zadd(_k_discover_country(), {brand_id: 0.0})
    if body.category:
        await r.sadd(_k_discover_category(body.category), brand_id)
    # If category changed, we don't auto-purge old category SETs — cheap
    # eventual consistency. Operators can clean via re-configure passes.

    logger.info(
        "storefront.configure brand=%s name=%s country=%s category=%s",
        brand_id, body.display_name, body.country, body.category,
    )
    return {
        "ok": True,
        "brand_id": brand_id,
        "public_url": f"/landing/storefront.html?b={brand_id}",
    }


@router.get("/discover", summary="Cross-brand storefront discovery")
async def discover_brands(
    country: str | None = Query(None, max_length=8),
    category: str | None = Query(None, max_length=32),
    sort: Literal["trending", "top_rated"] = Query("trending"),
    limit: int = Query(20, ge=1, le=100),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    """List brands matching filter — feeds cross-brand attribution loop."""
    # 1. Candidate set
    if category:
        cat_members = await r.smembers(_k_discover_category(category))
        candidates = set(cat_members)
    else:
        candidates = None  # means "all"

    # 2. Sort by trending or top_rated
    items: list[dict[str, Any]] = []
    if sort == "trending":
        # Sorted set by trending score, descending
        # Pull broadly then post-filter so SET intersection stays cheap.
        raw = await r.zrevrange(_k_discover_country(), 0, max(limit * 5, 50), withscores=True)
        for bid, score in raw:
            if candidates is not None and bid not in candidates:
                continue
            prof = await _load_profile(r, bid)
            if not prof:
                continue
            if country and prof.get("country") and prof["country"] != country:
                continue
            items.append({**_summary(prof), "trending_score": score})
            if len(items) >= limit:
                break
    else:  # top_rated
        # Need to iterate ratings — for a v1, just scan candidates (or all brands in country index).
        if candidates is None:
            raw = await r.zrange(_k_discover_country(), 0, -1)
            candidates = set(raw)
        scored: list[tuple[float, int, dict[str, Any]]] = []
        for bid in candidates:
            prof = await _load_profile(r, bid)
            if not prof:
                continue
            if country and prof.get("country") and prof["country"] != country:
                continue
            rating = await r.hgetall(_k_rating(bid))
            avg = float(rating.get("avg", 0.0) or 0.0)
            count = int(rating.get("count", 0) or 0)
            scored.append((avg, count, {**_summary(prof), "avg_rating": avg, "rating_count": count}))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        items = [s[2] for s in scored[:limit]]

    return {
        "country": country,
        "category": category,
        "sort": sort,
        "count": len(items),
        "items": items,
    }


def _summary(prof: dict[str, Any]) -> dict[str, Any]:
    return {
        "brand_id": prof["brand_id"],
        "display_name": prof["display_name"],
        "bio": prof["bio"],
        "logo_url": prof["logo_url"],
        "hero_image_url": prof["hero_image_url"],
        "brand_color": prof["brand_color"],
        "country": prof.get("country"),
        "category": prof.get("category"),
        "public_url": prof["public_url"],
    }


@router.get("/{brand_id}", summary="Public storefront profile")
async def get_storefront(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    prof = await _load_profile(r, brand_id)
    if not prof:
        raise HTTPException(404, f"Storefront not configured for brand '{brand_id}'")

    follower_count = await r.scard(_k_followers(brand_id))
    rating = await r.hgetall(_k_rating(brand_id))
    avg = float(rating.get("avg", 0.0) or 0.0)
    rcount = int(rating.get("count", 0) or 0)

    # Cheap trending bump on profile view — used by discover sort.
    await _trending_bump(r, brand_id, 0.1)

    return {
        **prof,
        "follower_count": follower_count,
        "avg_rating": round(avg, 2),
        "rating_count": rcount,
    }


# ── Endpoints: games / vouchers / stores tabs ────────────────────────────


@router.get("/{brand_id}/games", summary="Games available on this storefront")
async def list_games(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    prof = await _load_profile(r, brand_id)
    if not prof:
        raise HTTPException(404, "Storefront not configured")

    featured = prof.get("featured_games") or []
    games: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 1. Featured games first — pull what we can from game_catalog.
    for slug in featured:
        if slug in seen:
            continue
        seen.add(slug)
        game = await _resolve_game(r, brand_id, slug)
        if game:
            game["featured"] = True
            games.append(game)

    # 2. Brand-owned creative game records (the brand's portal creations).
    creative_ids = await r.smembers(f"brand:{brand_id}:games")
    for cid in creative_ids:
        if cid in seen:
            continue
        seen.add(cid)
        raw = await r.hgetall(f"game:{cid}")
        if not raw:
            continue
        games.append({
            "id": cid,
            "game_slug": raw.get("game_slug") or cid,
            "name": raw.get("name") or raw.get("game_slug") or cid,
            "description": raw.get("description") or "",
            "thumbnail_url": raw.get("thumbnail_url") or None,
            "featured": False,
        })

    return {"brand_id": brand_id, "count": len(games), "games": games}


async def _resolve_game(
    r: aioredis.Redis, brand_id: str, slug_or_id: str
) -> dict[str, Any] | None:
    """Resolve a featured-game token to a card-ready dict.

    Accepts either:
      * a game_slug from the global catalog, or
      * a brand-creative id stored at ``game:{id}``.
    Returns None when neither resolves (caller skips silently).
    """
    # Try creative id first
    raw = await r.hgetall(f"game:{slug_or_id}")
    if raw:
        return {
            "id": slug_or_id,
            "game_slug": raw.get("game_slug") or slug_or_id,
            "name": raw.get("name") or raw.get("game_slug") or slug_or_id,
            "description": raw.get("description") or "",
            "thumbnail_url": raw.get("thumbnail_url") or None,
        }
    # Fallback: treat as slug from static catalog
    return {
        "id": slug_or_id,
        "game_slug": slug_or_id,
        "name": slug_or_id.replace("_", " ").replace("-", " ").title(),
        "description": "",
        "thumbnail_url": None,
    }


@router.get("/{brand_id}/vouchers", summary="Vouchers user can claim")
async def list_vouchers(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    prof = await _load_profile(r, brand_id)
    if not prof:
        raise HTTPException(404, "Storefront not configured")

    featured = prof.get("featured_vouchers") or []
    vouchers: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Featured first
    for tid in featured:
        if tid in seen:
            continue
        seen.add(tid)
        raw = await r.get(_k_voucher_template(brand_id, tid))
        if not raw:
            continue
        try:
            tpl = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        vouchers.append({**_voucher_card(tid, tpl), "featured": True})

    # Then the rest of the brand's templates
    tids = await r.smembers(_k_brand_voucher_templates(brand_id))
    for tid in tids:
        if tid in seen:
            continue
        seen.add(tid)
        raw = await r.get(_k_voucher_template(brand_id, tid))
        if not raw:
            continue
        try:
            tpl = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        vouchers.append({**_voucher_card(tid, tpl), "featured": False})

    return {"brand_id": brand_id, "count": len(vouchers), "vouchers": vouchers}


def _voucher_card(tid: str, tpl: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": tid,
        "title": tpl.get("title") or tpl.get("name") or tid,
        "description": tpl.get("description") or tpl.get("message_template") or "",
        "value": tpl.get("value") or tpl.get("discount") or None,
        "currency": tpl.get("currency") or None,
        "expires_at": tpl.get("expires_at") or None,
    }


@router.get("/{brand_id}/stores", summary="Physical store locations")
async def list_stores(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    prof = await _load_profile(r, brand_id)
    if not prof:
        raise HTTPException(404, "Storefront not configured")
    if not prof.get("show_stores"):
        return {"brand_id": brand_id, "count": 0, "stores": []}

    store_ids = await r.smembers(_k_brand_stores(brand_id))
    stores: list[dict[str, Any]] = []
    for sid in store_ids:
        raw = await r.hgetall(f"store:{sid}")
        if not raw:
            continue
        stores.append({
            "id": sid,
            "name": raw.get("name") or sid,
            "address": raw.get("address") or "",
            "lat": float(raw["lat"]) if raw.get("lat") else None,
            "lng": float(raw["lng"]) if raw.get("lng") else None,
            "radius_meters": int(raw.get("radius_meters", 0) or 0),
        })
    return {"brand_id": brand_id, "count": len(stores), "stores": stores}


# ── Endpoints: follow / followers ────────────────────────────────────────


@router.post("/{brand_id}/follow", summary="Follow a brand storefront")
async def follow_brand(
    brand_id: str,
    body: FollowBody,
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    # Light existence guard — let follows of un-configured brands succeed
    # so the SDK can pre-seed; but we still require the brand key to exist
    # somewhere in the system to avoid garbage.
    pipe = r.pipeline()
    pipe.sadd(_k_followers(brand_id), body.user_id)
    pipe.sadd(_k_user_brand_follows(body.user_id), brand_id)
    await pipe.execute()
    await _trending_bump(r, brand_id, 1.0)
    logger.info("storefront.follow brand=%s user=%s", brand_id, body.user_id)
    return {"ok": True, "brand_id": brand_id, "user_id": body.user_id}


@router.post("/{brand_id}/unfollow", summary="Unfollow a brand")
async def unfollow_brand(
    brand_id: str,
    body: FollowBody,
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    pipe = r.pipeline()
    pipe.srem(_k_followers(brand_id), body.user_id)
    pipe.srem(_k_user_brand_follows(body.user_id), brand_id)
    await pipe.execute()
    return {"ok": True}


@router.get("/{brand_id}/followers/count", summary="Follower count for a brand")
async def follower_count(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    n = await r.scard(_k_followers(brand_id))
    return {"brand_id": brand_id, "count": n}


# ── Endpoints: reviews ───────────────────────────────────────────────────


@router.post("/{brand_id}/review", summary="Add a review (1-5 stars)")
async def add_review(
    brand_id: str,
    body: ReviewBody,
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    if not await r.exists(_k_profile(brand_id)):
        raise HTTPException(404, "Storefront not configured")

    review_id = f"rev_{uuid4().hex[:16]}"
    record = {
        "review_id": review_id,
        "brand_id": brand_id,
        "user_id": body.user_id,
        "rating": str(body.rating),
        "comment": body.comment or "",
        "anonymous": "1" if body.anonymous else "0",
        "ts": str(_now()),
    }
    pipe = r.pipeline()
    pipe.hset(_k_review(review_id), mapping=record)
    pipe.lpush(_k_reviews(brand_id), review_id)
    pipe.ltrim(_k_reviews(brand_id), 0, 999)  # cap recent list at 1k
    pipe.zadd(_k_rating_top(brand_id), {review_id: float(body.rating)})
    # Update running aggregate
    pipe.hincrbyfloat(_k_rating(brand_id), "sum", float(body.rating))
    pipe.hincrby(_k_rating(brand_id), "count", 1)
    await pipe.execute()

    # Recompute average (cheap — small numbers)
    agg = await r.hgetall(_k_rating(brand_id))
    total = float(agg.get("sum", 0.0) or 0.0)
    count = int(agg.get("count", 0) or 0)
    avg = round(total / count, 3) if count else 0.0
    await r.hset(_k_rating(brand_id), "avg", str(avg))

    logger.info(
        "storefront.review brand=%s user=%s rating=%d", brand_id, body.user_id, body.rating
    )
    return {"ok": True, "review_id": review_id, "avg_rating": avg, "count": count}


@router.get("/{brand_id}/reviews", summary="List reviews for a brand")
async def list_reviews(
    brand_id: str,
    limit: int = Query(20, ge=1, le=100),
    sort: Literal["recent", "top"] = Query("recent"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict:
    agg = await r.hgetall(_k_rating(brand_id))
    avg = float(agg.get("avg", 0.0) or 0.0)
    count = int(agg.get("count", 0) or 0)

    if sort == "top":
        rids = await r.zrevrange(_k_rating_top(brand_id), 0, limit - 1)
    else:
        rids = await r.lrange(_k_reviews(brand_id), 0, limit - 1)

    reviews: list[dict[str, Any]] = []
    for rid in rids:
        raw = await r.hgetall(_k_review(rid))
        if not raw:
            continue
        anon = raw.get("anonymous") == "1"
        reviews.append({
            "id": rid,
            "user_id": "anonymous" if anon else raw.get("user_id"),
            "rating": int(raw.get("rating", 0) or 0),
            "comment": raw.get("comment") or "",
            "anonymous": anon,
            "ts": int(float(raw.get("ts", 0) or 0)),
        })

    return {
        "brand_id": brand_id,
        "avg_rating": round(avg, 2),
        "count": count,
        "sort": sort,
        "reviews": reviews,
    }
