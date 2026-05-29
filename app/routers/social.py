"""Social graph + feed + like (kudos) for KiX.

Three concerns share a single router:

  1. SocialGraph   — friendships (mutual) and follows (one-way).
  2. SocialFeed    — activity stream with fan-out-on-write.
  3. Kudos         — likes + comments on feed posts.

All keys are brand-isolated where it matters (per-brand feed), since a
user can be active in multiple branded apps with separate timelines.

Storage layout (Redis):

    user:{uid}:friends                       SET of user_ids
    user:{uid}:friend_requests:incoming      SET of "from_user:request_id"
    friend_request:{request_id}              HASH (from, to, brand, status)
    user:{uid}:following                     SET of user_ids
    user:{uid}:followers                     SET of user_ids
    user:{uid}:feed:{bid}                    ZSET (score=ts, member=post_id)
    post:{pid}                               HASH (author, brand, event, payload, ts, like_count)
    post:{pid}:likes                         SET of user_ids
    post:{pid}:comments                      LIST of JSON comment objects
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()

# Max followers to fan out to in a single pipeline. Larger sets are
# chunked to avoid pipeline blow-up.
FANOUT_CHUNK_SIZE = 500

# Maximum feed entries kept per (user, brand) — older entries get trimmed.
FEED_MAX_ENTRIES = 500


# ── Pydantic models ────────────────────────────────────────────────────────


class FriendRequest(BaseModel):
    from_user: str = Field(..., min_length=1)
    to_user: str = Field(..., min_length=1)
    brand_id: str = Field(..., min_length=1)
    idempotency_key: str | None = Field(default=None, max_length=128)


class FriendAccept(BaseModel):
    request_id: str = Field(..., min_length=1)
    idempotency_key: str | None = Field(default=None, max_length=128)


class FriendRemove(BaseModel):
    user_id: str = Field(..., min_length=1)
    friend_id: str = Field(..., min_length=1)
    idempotency_key: str | None = Field(default=None, max_length=128)


class FollowRequest(BaseModel):
    follower_id: str = Field(..., min_length=1)
    followed_id: str = Field(..., min_length=1)
    idempotency_key: str | None = Field(default=None, max_length=128)


class FeedPostRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    brand_id: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)  # high_score, level_up, etc.
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=128)


class LikeRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    idempotency_key: str | None = Field(default=None, max_length=128)


class CommentRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1, max_length=1000)
    idempotency_key: str | None = Field(default=None, max_length=128)


# ── Bug 8 fix: lightweight idempotency primitive ─────────────────────────
# Each POST endpoint accepts an optional ``idempotency_key`` in its body.
# On first use we SET NX a short-lived marker under ``social:idem:{key}``
# with the JSON response; subsequent calls observe the cached response
# and short-circuit, avoiding duplicate mutations (double-follows, double
# kudos, duplicate comments) when clients retry on network flakiness.

SOCIAL_IDEM_TTL_SECONDS = 24 * 3600


def _k_idem(key: str) -> str:
    return f"social:idem:{key}"


async def _idem_get(r: aioredis.Redis, key: str | None) -> dict[str, Any] | None:
    """Return a previously-cached response for this idempotency key, or None."""
    if not key:
        return None
    raw = await r.get(_k_idem(key))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


async def _idem_put(
    r: aioredis.Redis, key: str | None, payload: dict[str, Any]
) -> None:
    """Record this response under the idempotency key (TTL 24h)."""
    if not key:
        return
    try:
        await r.set(
            _k_idem(key),
            json.dumps(payload, default=str),
            nx=True,
            ex=SOCIAL_IDEM_TTL_SECONDS,
        )
    except (TypeError, ValueError):
        # Don't fail the request if we can't serialise the payload.
        logger.warning("social idem put failed for key=%s", key)


# ── Redis key helpers ─────────────────────────────────────────────────────


def _k_friends(uid: str) -> str:
    return f"user:{uid}:friends"


def _k_incoming(uid: str) -> str:
    return f"user:{uid}:friend_requests:incoming"


def _k_outgoing(uid: str) -> str:
    return f"user:{uid}:friend_requests:outgoing"


def _k_request(rid: str) -> str:
    return f"friend_request:{rid}"


def _k_following(uid: str) -> str:
    return f"user:{uid}:following"


def _k_followers(uid: str) -> str:
    return f"user:{uid}:followers"


def _k_feed(uid: str, bid: str) -> str:
    return f"user:{uid}:feed:{bid}"


def _k_post(pid: str) -> str:
    return f"post:{pid}"


def _k_post_likes(pid: str) -> str:
    return f"post:{pid}:likes"


def _k_post_comments(pid: str) -> str:
    return f"post:{pid}:comments"


# ── Helpers ────────────────────────────────────────────────────────────────


def _now() -> int:
    return int(time.time())


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:14]}"


async def _post_to_view(
    r: aioredis.Redis, pid: str, viewer_id: str | None = None
) -> dict[str, Any] | None:
    state = await r.hgetall(_k_post(pid))
    if not state:
        return None
    like_count = await r.scard(_k_post_likes(pid))
    has_liked = False
    if viewer_id:
        has_liked = bool(await r.sismember(_k_post_likes(pid), viewer_id))
    comment_count = await r.llen(_k_post_comments(pid))
    return {
        "post_id": pid,
        "user_id": state.get("user_id"),
        "brand_id": state.get("brand_id"),
        "event_type": state.get("event_type"),
        "payload": json.loads(state.get("payload") or "{}"),
        "created_at": int(state.get("created_at") or 0),
        "like_count": int(like_count or 0),
        "comment_count": int(comment_count or 0),
        "viewer_has_liked": has_liked,
    }


# ── Friends ───────────────────────────────────────────────────────────────


@router.post(
    "/friends/request",
    summary="Send a friend request",
    status_code=status.HTTP_201_CREATED,
)
async def friend_request(
    body: FriendRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    cached = await _idem_get(r, body.idempotency_key)
    if cached is not None:
        return cached

    if body.from_user == body.to_user:
        raise HTTPException(400, "Cannot friend yourself")

    # Already friends?
    if await r.sismember(_k_friends(body.from_user), body.to_user):
        out = {
            "ok": True,
            "request_id": None,
            "status": "already_friends",
        }
        await _idem_put(r, body.idempotency_key, out)
        return out

    rid = _new_id("freq")
    record = {
        "request_id": rid,
        "from_user": body.from_user,
        "to_user": body.to_user,
        "brand_id": body.brand_id,
        "status": "pending",
        "created_at": str(_now()),
    }
    pipe = r.pipeline()
    pipe.hset(_k_request(rid), mapping=record)
    pipe.sadd(_k_incoming(body.to_user), rid)
    pipe.sadd(_k_outgoing(body.from_user), rid)
    await pipe.execute()
    out = {"ok": True, "request_id": rid, "status": "pending"}
    await _idem_put(r, body.idempotency_key, out)
    return out


@router.post(
    "/friends/accept",
    summary="Accept a pending friend request",
)
async def friend_accept(
    body: FriendAccept,
    r: aioredis.Redis = Depends(get_redis),
):
    cached = await _idem_get(r, body.idempotency_key)
    if cached is not None:
        return cached

    record = await r.hgetall(_k_request(body.request_id))
    if not record:
        raise HTTPException(404, "Friend request not found")
    if record.get("status") != "pending":
        raise HTTPException(
            409, f"Request status is {record.get('status')}"
        )
    a = record["from_user"]
    b = record["to_user"]
    pipe = r.pipeline()
    pipe.hset(_k_request(body.request_id), "status", "accepted")
    pipe.hset(_k_request(body.request_id), "accepted_at", str(_now()))
    pipe.sadd(_k_friends(a), b)
    pipe.sadd(_k_friends(b), a)
    pipe.srem(_k_incoming(b), body.request_id)
    pipe.srem(_k_outgoing(a), body.request_id)
    await pipe.execute()
    out = {"ok": True, "request_id": body.request_id, "status": "accepted"}
    await _idem_put(r, body.idempotency_key, out)
    return out


@router.post(
    "/friends/remove",
    summary="Remove a friend (symmetric)",
)
async def friend_remove(
    body: FriendRemove,
    r: aioredis.Redis = Depends(get_redis),
):
    cached = await _idem_get(r, body.idempotency_key)
    if cached is not None:
        return cached
    pipe = r.pipeline()
    pipe.srem(_k_friends(body.user_id), body.friend_id)
    pipe.srem(_k_friends(body.friend_id), body.user_id)
    removed = await pipe.execute()
    out = {"ok": True, "removed": any(removed)}
    await _idem_put(r, body.idempotency_key, out)
    return out


@router.get(
    "/{user_id}/friends",
    summary="List a user's friends",
)
async def list_friends(
    user_id: str,
    brand_id: str | None = Query(None),
    r: aioredis.Redis = Depends(get_redis),
):
    # Friends are global; brand_id is accepted but only affects metadata.
    members = await r.smembers(_k_friends(user_id))
    return {
        "user_id": user_id,
        "brand_id": brand_id,
        "friend_count": len(members),
        "friends": sorted(members),
    }


@router.get(
    "/{user_id}/friend-requests",
    summary="List incoming friend requests for a user",
)
async def list_incoming_requests(
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    rids = await r.smembers(_k_incoming(user_id))
    out: list[dict[str, str]] = []
    for rid in rids:
        rec = await r.hgetall(_k_request(rid))
        if rec:
            out.append(rec)
    return {"user_id": user_id, "requests": out}


# ── Following ─────────────────────────────────────────────────────────────


@router.post(
    "/follow",
    summary="Follow another user (one-way)",
)
async def follow(
    body: FollowRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    cached = await _idem_get(r, body.idempotency_key)
    if cached is not None:
        return cached
    if body.follower_id == body.followed_id:
        raise HTTPException(400, "Cannot follow yourself")
    pipe = r.pipeline()
    pipe.sadd(_k_following(body.follower_id), body.followed_id)
    pipe.sadd(_k_followers(body.followed_id), body.follower_id)
    await pipe.execute()
    out = {"ok": True}
    await _idem_put(r, body.idempotency_key, out)
    return out


@router.post(
    "/unfollow",
    summary="Unfollow another user",
)
async def unfollow(
    body: FollowRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    cached = await _idem_get(r, body.idempotency_key)
    if cached is not None:
        return cached
    pipe = r.pipeline()
    pipe.srem(_k_following(body.follower_id), body.followed_id)
    pipe.srem(_k_followers(body.followed_id), body.follower_id)
    await pipe.execute()
    out = {"ok": True}
    await _idem_put(r, body.idempotency_key, out)
    return out


@router.get(
    "/{user_id}/followers",
    summary="List followers (people following this user)",
)
async def list_followers(
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    members = await r.smembers(_k_followers(user_id))
    return {
        "user_id": user_id,
        "follower_count": len(members),
        "followers": sorted(members),
    }


@router.get(
    "/{user_id}/following",
    summary="List users this user follows",
)
async def list_following(
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    members = await r.smembers(_k_following(user_id))
    return {
        "user_id": user_id,
        "following_count": len(members),
        "following": sorted(members),
    }


# ── Feed (activity stream, fan-out-on-write) ───────────────────────────────


@router.post(
    "/feed/post",
    summary="Publish an activity to the user's followers' feeds",
    status_code=status.HTTP_201_CREATED,
)
async def feed_post(
    body: FeedPostRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    cached = await _idem_get(r, body.idempotency_key)
    if cached is not None:
        return cached
    pid = _new_id("post")
    now = _now()

    post_state = {
        "post_id": pid,
        "user_id": body.user_id,
        "brand_id": body.brand_id,
        "event_type": body.event_type,
        "payload": json.dumps(body.payload),
        "created_at": str(now),
        "like_count": "0",
    }

    # Always include author themselves in fan-out target list.
    followers = await r.smembers(_k_followers(body.user_id))
    targets = set(followers) | {body.user_id}

    # Pipeline: write post then ZADD into each follower's feed.
    pipe = r.pipeline()
    pipe.hset(_k_post(pid), mapping=post_state)
    count = 0
    for uid in targets:
        pipe.zadd(_k_feed(uid, body.brand_id), {pid: float(now)})
        # Cap the feed length to FEED_MAX_ENTRIES (drop oldest).
        pipe.zremrangebyrank(
            _k_feed(uid, body.brand_id), 0, -(FEED_MAX_ENTRIES + 1)
        )
        count += 1
        if count % FANOUT_CHUNK_SIZE == 0:
            await pipe.execute()
            pipe = r.pipeline()
    if count % FANOUT_CHUNK_SIZE != 0:
        await pipe.execute()

    logger.info(
        "Feed fan-out: post=%s user=%s brand=%s targets=%d",
        pid,
        body.user_id,
        body.brand_id,
        len(targets),
    )
    out = {
        "ok": True,
        "post_id": pid,
        "fanout_count": len(targets),
        "created_at": now,
    }
    await _idem_put(r, body.idempotency_key, out)
    return out


@router.get(
    "/{user_id}/feed",
    summary="Fetch reverse-chronological feed for a user (per brand)",
)
async def get_feed(
    user_id: str,
    brand_id: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    r: aioredis.Redis = Depends(get_redis),
):
    start = offset
    stop = offset + limit - 1
    pids = await r.zrevrange(_k_feed(user_id, brand_id), start, stop)
    items: list[dict[str, Any]] = []
    for pid in pids:
        view = await _post_to_view(r, pid, viewer_id=user_id)
        if view is not None:
            items.append(view)
    return {
        "user_id": user_id,
        "brand_id": brand_id,
        "count": len(items),
        "items": items,
    }


@router.post(
    "/feed/{post_id}/like",
    summary="Like a feed post (Kudos +1) — idempotent",
)
async def like_post(
    post_id: str,
    body: LikeRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    cached = await _idem_get(r, body.idempotency_key)
    if cached is not None:
        return cached
    if not await r.exists(_k_post(post_id)):
        raise HTTPException(404, "Post not found")
    added = await r.sadd(_k_post_likes(post_id), body.user_id)
    if added:
        await r.hincrby(_k_post(post_id), "like_count", 1)
    new_count = await r.scard(_k_post_likes(post_id))
    out = {
        "ok": True,
        "post_id": post_id,
        "kudos": int(new_count or 0),
        "newly_liked": bool(added),
    }
    await _idem_put(r, body.idempotency_key, out)
    return out


@router.post(
    "/feed/{post_id}/unlike",
    summary="Remove a like from a post",
)
async def unlike_post(
    post_id: str,
    body: LikeRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    cached = await _idem_get(r, body.idempotency_key)
    if cached is not None:
        return cached
    if not await r.exists(_k_post(post_id)):
        raise HTTPException(404, "Post not found")
    removed = await r.srem(_k_post_likes(post_id), body.user_id)
    if removed:
        await r.hincrby(_k_post(post_id), "like_count", -1)
    new_count = await r.scard(_k_post_likes(post_id))
    out = {
        "ok": True,
        "post_id": post_id,
        "kudos": int(new_count or 0),
        "newly_unliked": bool(removed),
    }
    await _idem_put(r, body.idempotency_key, out)
    return out


@router.post(
    "/feed/{post_id}/comment",
    summary="Append a comment to a feed post",
    status_code=status.HTTP_201_CREATED,
)
async def comment_post(
    post_id: str,
    body: CommentRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    cached = await _idem_get(r, body.idempotency_key)
    if cached is not None:
        return cached
    if not await r.exists(_k_post(post_id)):
        raise HTTPException(404, "Post not found")
    cid = _new_id("c")
    comment = {
        "comment_id": cid,
        "post_id": post_id,
        "user_id": body.user_id,
        "text": body.text,
        "created_at": _now(),
    }
    await r.rpush(_k_post_comments(post_id), json.dumps(comment))
    out = {"ok": True, "comment": comment}
    await _idem_put(r, body.idempotency_key, out)
    return out


@router.get(
    "/feed/{post_id}",
    summary="Get a single post (with kudos + comment count)",
)
async def get_post(
    post_id: str,
    viewer_id: str | None = Query(None),
    r: aioredis.Redis = Depends(get_redis),
):
    view = await _post_to_view(r, post_id, viewer_id=viewer_id)
    if view is None:
        raise HTTPException(404, "Post not found")
    return view


@router.get(
    "/feed/{post_id}/comments",
    summary="List comments for a post",
)
async def list_comments(
    post_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    r: aioredis.Redis = Depends(get_redis),
):
    if not await r.exists(_k_post(post_id)):
        raise HTTPException(404, "Post not found")
    raws = await r.lrange(
        _k_post_comments(post_id), offset, offset + limit - 1
    )
    items: list[dict[str, Any]] = []
    for raw in raws:
        try:
            items.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return {"post_id": post_id, "count": len(items), "comments": items}
