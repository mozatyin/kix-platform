"""Push Topics — FCM topic subscribe / unsubscribe / broadcast.

Topics let us fan out a single push to N subscribers without keeping the
membership list on our side. Firebase maintains it server-side. This is
the only sane way to ship "all subscribers of brand X get this offer"
because individual sends would burn our hourly quota.

Also exposes a thin token-register endpoint (alias of the kix_id one,
mounted under ``/api/v1/push`` so the consumer-app SDK has a stable
namespace for both registration and topic ops).

Endpoints:

  POST /api/v1/push/register-token         — register an FCM/APNS token
  POST /api/v1/push/topic/subscribe        — subscribe tokens to a topic
  POST /api/v1/push/topic/unsubscribe      — unsubscribe tokens
  POST /api/v1/push/topic/{topic}/broadcast — fan-out a push to subscribers
  GET  /api/v1/push/topic/list              — list subscribed topics for kid
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.redis_client import get_redis
from app.services import fcm_client


logger = logging.getLogger(__name__)
router = APIRouter()


# Subscription tracker so the consumer app can show "you are subscribed
# to: foo, bar". We keep this on our side because Firebase doesn't
# expose per-token topic membership queries.
KID_TOPICS_KEY = "kid:{kid}:push_topics"
TOPIC_SUBSCRIBERS_KEY = "push:topic:{topic}:subscribers"


# ── Models ────────────────────────────────────────────────────────────────


class TokenRegisterRequest(BaseModel):
    kid: str = Field(..., min_length=1, max_length=128)
    platform: Literal["ios", "android", "web", "wechat"]
    token: str = Field(..., min_length=8, max_length=4096)
    device_id: str | None = None


class TokenRegisterResponse(BaseModel):
    device_id: str
    status: str
    mode: str


class TopicSubscribeRequest(BaseModel):
    kid: str = Field(..., min_length=1, max_length=128)
    topic: str = Field(..., min_length=1, max_length=128)
    # Optional explicit tokens; default: all kid's registered tokens.
    tokens: list[str] | None = None


class TopicSubscribeResponse(BaseModel):
    ok: bool
    topic: str
    success_count: int
    failure_count: int
    mode: str
    detail: str | None = None


class TopicBroadcastRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=1000)
    deep_link: str | None = None
    data: dict[str, str] | None = None


# ── Helpers ───────────────────────────────────────────────────────────────


async def _kid_tokens(r: aioredis.Redis, kid: str) -> list[str]:
    """Return all live FCM-compatible tokens for a kid (ios/android/web)."""
    devices = await r.smembers(f"kid:{kid}:push_devices")
    tokens: list[str] = []
    for did in devices or []:
        info = await r.hgetall(f"push_device:{did}")
        if not info:
            continue
        if info.get("active") == "0":
            continue
        plat = (info.get("platform") or "").lower()
        if plat in ("ios", "android", "web"):
            tok = info.get("token")
            if tok:
                tokens.append(tok)
    return tokens


# ── Endpoint: register-token (alias of kix_id endpoint) ──────────────────


@router.post("/register-token", response_model=TokenRegisterResponse)
async def register_token(
    body: TokenRegisterRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> TokenRegisterResponse:
    """Register an FCM/APNS/Web Push token for a kid.

    Stable namespace mirror of ``POST /api/v1/kix-id/{kid}/push-device/register``
    so consumer-app SDKs can use a single ``/api/v1/push/*`` prefix.

    Token format is validated structurally (length + no whitespace) before
    registration. WeChat openids skip the validation.
    """
    from app.workers.push_worker import device_register

    try:
        device_id = await device_register(
            r,
            kid=body.kid,
            platform=body.platform,
            token=body.token,
            device_id=body.device_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return TokenRegisterResponse(
        device_id=device_id,
        status="registered",
        mode=fcm_client.get_mode(),
    )


# ── Endpoint: subscribe ──────────────────────────────────────────────────


@router.post("/topic/subscribe", response_model=TopicSubscribeResponse)
async def topic_subscribe(
    body: TopicSubscribeRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> TopicSubscribeResponse:
    """Subscribe a kid's tokens to a topic.

    If ``tokens`` is omitted we look up the kid's registered FCM tokens
    and subscribe all of them. We also record the membership on our side
    (in ``kid:{kid}:push_topics`` + ``push:topic:{topic}:subscribers``)
    so the consumer app can list topics and the admin can count subs.
    """
    tokens = body.tokens or await _kid_tokens(r, body.kid)
    if not tokens:
        return TopicSubscribeResponse(
            ok=False, topic=body.topic, success_count=0, failure_count=0,
            mode=fcm_client.get_mode(),
            detail="no_registered_tokens",
        )

    result = await fcm_client.subscribe_to_topic(tokens, body.topic)
    if not result.get("success"):
        return TopicSubscribeResponse(
            ok=False, topic=body.topic,
            success_count=int(result.get("success_count", 0)),
            failure_count=int(result.get("failure_count", len(tokens))),
            mode=fcm_client.get_mode(),
            detail=result.get("error"),
        )

    # Track membership on our side (best-effort).
    pipe = r.pipeline()
    pipe.sadd(KID_TOPICS_KEY.format(kid=body.kid), body.topic)
    pipe.sadd(TOPIC_SUBSCRIBERS_KEY.format(topic=body.topic), body.kid)
    pipe.hset(
        f"push:topic:{body.topic}:meta",
        mapping={"last_subscribed_at": str(time.time())},
    )
    await pipe.execute()

    return TopicSubscribeResponse(
        ok=True, topic=body.topic,
        success_count=int(result.get("success_count", len(tokens))),
        failure_count=int(result.get("failure_count", 0)),
        mode=fcm_client.get_mode(),
    )


# ── Endpoint: unsubscribe ────────────────────────────────────────────────


@router.post("/topic/unsubscribe", response_model=TopicSubscribeResponse)
async def topic_unsubscribe(
    body: TopicSubscribeRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> TopicSubscribeResponse:
    """Remove a kid's tokens from a topic."""
    tokens = body.tokens or await _kid_tokens(r, body.kid)
    if not tokens:
        return TopicSubscribeResponse(
            ok=False, topic=body.topic, success_count=0, failure_count=0,
            mode=fcm_client.get_mode(),
            detail="no_registered_tokens",
        )

    result = await fcm_client.unsubscribe_from_topic(tokens, body.topic)
    pipe = r.pipeline()
    pipe.srem(KID_TOPICS_KEY.format(kid=body.kid), body.topic)
    pipe.srem(TOPIC_SUBSCRIBERS_KEY.format(topic=body.topic), body.kid)
    await pipe.execute()
    return TopicSubscribeResponse(
        ok=bool(result.get("success")),
        topic=body.topic,
        success_count=int(result.get("success_count", len(tokens))),
        failure_count=int(result.get("failure_count", 0)),
        mode=fcm_client.get_mode(),
        detail=result.get("error"),
    )


# ── Endpoint: list topics for kid ────────────────────────────────────────


@router.get("/topic/list")
async def topic_list(
    kid: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """List topics the kid is subscribed to (from our tracker)."""
    topics = await r.smembers(KID_TOPICS_KEY.format(kid=kid))
    return {"kid": kid, "topics": sorted(list(topics or []))}


# ── Endpoint: broadcast ──────────────────────────────────────────────────


@router.post("/topic/{topic}/broadcast")
async def topic_broadcast(
    topic: str,
    body: TopicBroadcastRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Fan-out a push to every subscriber of ``topic``.

    Uses FCM's server-side fan-out (single API call) rather than
    iterating tokens — fastest path for "brand broadcast" use cases.
    """
    if not topic:
        raise HTTPException(status_code=400, detail="topic required")

    data = dict(body.data or {})
    if body.deep_link:
        data["deep_link"] = body.deep_link

    result = await fcm_client.send_to_topic(
        topic=topic, title=body.title, body=body.body, data=data or None,
    )
    if not result.get("success"):
        return {
            "ok": False,
            "topic": topic,
            "error": result.get("error", "unknown"),
            "mode": result.get("mode"),
        }

    # Bump a counter so admins can see broadcast volume.
    try:
        await r.hincrby(f"push:topic:{topic}:meta", "broadcasts_total", 1)
    except Exception:  # pragma: no cover
        pass

    return {
        "ok": True,
        "topic": topic,
        "message_id": result.get("message_id"),
        "mode": result.get("mode"),
    }


# ── Endpoint: health ─────────────────────────────────────────────────────


@router.get("/health")
async def push_health() -> dict[str, Any]:
    """Health probe for the push subsystem.

    Returns the FCM client mode, configuration status, and recent
    delivery counters so ops can verify the push pipeline is live and
    pushing real notifications (not silently falling back to mock).
    """
    last_ts = await fcm_client.last_sent_ts()
    failures = await fcm_client.failures_last_24h()
    return {
        "mode": fcm_client.get_mode(),
        "configured": fcm_client.is_configured(),
        "last_sent_ts": last_ts,
        "last_sent_age_seconds": (
            int(time.time() - last_ts) if last_ts else None
        ),
        "failures_last_24h": failures,
        "rate_limit_per_hour": fcm_client.MAX_PUSHES_PER_HOUR,
    }


__all__ = ["router"]
