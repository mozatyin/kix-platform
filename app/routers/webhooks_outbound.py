"""Outbound Webhooks — merchant-facing event delivery.

Merchants register HTTPS endpoints to receive real-time KiX events
(``auction.won``, ``conversion.attributed``, ``wallet.charged`` ...). Each
registered webhook gets a unique signing secret; every delivery is signed
with HMAC-SHA256 so merchants can verify authenticity.

Delivery model
--------------
* Producers (auction, wallet, disputes, subscriptions, ...) call
  :func:`fan_out_webhook_to_brand` after key state changes.
* That helper enqueues one :func:`deliver_webhook` job per *active* webhook
  for the brand that has subscribed to the event type.
* The background worker (``app.workers.webhook_worker``) pops the queue,
  signs the payload, POSTs to the merchant URL, and on non-2xx responses
  re-enqueues with exponential backoff (1m → 5m → 30m → 2h → 24h).

HMAC signature header
---------------------
::

    X-KiX-Webhook-Signature: t=<unix_ts>,v1=<hex_hmac_sha256>
    X-KiX-Event-Type:       <event_type>
    X-KiX-Delivery-Id:      <whd_...>

The signed payload is ``f"{ts}.{raw_body}"``. Merchants reject
signatures older than 5 minutes to prevent replay.

Redis schema
------------
::

    webhook:{webhook_id}                  HASH   {brand_id, target_url, event_types(csv),
                                                  signing_secret, status, description,
                                                  created_at, last_delivery_at,
                                                  failed_streak}
    brand:{brand_id}:webhooks             SET    of webhook_id
    webhook:event:{event_type}            SET    of webhook_id (fast event→subscribers lookup)
    webhook:delivery:{delivery_id}        HASH   delivery state (payload, attempts, status, ...)
    webhook:delivery:queue                ZSET   score=next_attempt_at, member=delivery_id
    webhook:{webhook_id}:deliveries       ZSET   score=created_at, member=delivery_id
                                                  (capped to DELIVERY_INDEX_MAX)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any, Literal
from uuid import uuid4

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, HttpUrl

from app.api_standards import list_response, now_ts
from app.redis_client import get_redis

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Constants ────────────────────────────────────────────────────────────
SUPPORTED_EVENT_TYPES: set[str] = {
    "auction.won",
    "conversion.attributed",
    "wallet.charged",
    "wallet.balance_low",
    "subscription.renewed",
    "subscription.cancelled",
    "dispute.opened",
    "dispute.resolved",
    "voucher.redeemed",
    "payout.processed",
}

# Exponential backoff schedule (seconds). Length defines MAX_RETRIES.
RETRY_BACKOFF_SECONDS: list[int] = [60, 300, 1800, 7200, 86400]
MAX_RETRIES = len(RETRY_BACKOFF_SECONDS)

DELIVERY_TTL_SECONDS = 14 * 86400        # delivery records kept 14 days
DELIVERY_INDEX_MAX = 500                 # per-webhook recent delivery index size
SECRET_BYTES = 32                        # 256-bit signing secrets
SIGNATURE_TOLERANCE_SECONDS = 300        # ±5 min for verify
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 500
RESPONSE_BODY_PREVIEW = 1000             # bytes of response body to retain
HTTP_TIMEOUT_SECONDS = 10.0


# ── Key helpers ──────────────────────────────────────────────────────────
def _k_webhook(wid: str) -> str:
    return f"webhook:{wid}"


def _k_brand_webhooks(bid: str) -> str:
    return f"brand:{bid}:webhooks"


def _k_event_subscribers(event_type: str) -> str:
    return f"webhook:event:{event_type}"


def _k_delivery(did: str) -> str:
    return f"webhook:delivery:{did}"


_K_DELIVERY_QUEUE = "webhook:delivery:queue"


def _k_webhook_deliveries(wid: str) -> str:
    return f"webhook:{wid}:deliveries"


# ── ID minting (Stripe-style prefix; not in api_standards ID_PREFIXES) ───
def _mint_webhook_id() -> str:
    return f"whk_{uuid4().hex[:22]}"


def _mint_delivery_id() -> str:
    return f"whd_{uuid4().hex[:22]}"


def _mint_signing_secret() -> str:
    """256-bit URL-safe secret. Prefix lets merchants spot it in logs."""
    return "whsec_" + secrets.token_urlsafe(SECRET_BYTES)


# ── HMAC signing (also exposed for merchant SDK use) ─────────────────────


def sign_webhook(payload: bytes, secret: str, ts: int) -> str:
    """Generate ``t=<ts>,v1=<hex>`` signature for a webhook payload."""
    signed = f"{ts}.{payload.decode('utf-8')}".encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify_webhook(
    payload: bytes,
    signature: str,
    secret: str,
    tolerance_seconds: int = SIGNATURE_TOLERANCE_SECONDS,
) -> bool:
    """Merchant-side verification helper (we ship this in the SDK)."""
    try:
        parts = dict(p.split("=", 1) for p in signature.split(","))
        ts = int(parts["t"])
        mac = parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs(time.time() - ts) > tolerance_seconds:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.{payload.decode('utf-8')}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(mac, expected)


# ── Pydantic models ──────────────────────────────────────────────────────
class RegisterWebhookRequest(BaseModel):
    brand_id: str = Field(..., min_length=1, max_length=128)
    target_url: HttpUrl
    event_types: list[str] = Field(..., min_length=1, max_length=32)
    description: str | None = Field(default=None, max_length=512)


class RegisterWebhookResponse(BaseModel):
    webhook_id: str
    signing_secret: str
    status: Literal["active"]
    target_url: str
    event_types: list[str]
    created_at: int


class TestWebhookRequest(BaseModel):
    event_type: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any] | None = None


class WebhookSummary(BaseModel):
    webhook_id: str
    brand_id: str
    target_url: str
    event_types: list[str]
    status: str
    description: str | None
    created_at: int
    last_delivery_at: int | None
    failed_streak: int


class DeliveryRecord(BaseModel):
    delivery_id: str
    webhook_id: str
    event_type: str
    status: str
    attempts: int
    response_code: int | None
    last_error: str | None
    created_at: int
    delivered_at: int | None
    next_attempt_at: int | None


# ── Internal helpers ─────────────────────────────────────────────────────


def _decode_event_types(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [e for e in raw.split(",") if e]


def _validate_event_types(event_types: list[str]) -> list[str]:
    cleaned: list[str] = []
    bad: list[str] = []
    for et in event_types:
        et = et.strip()
        if not et:
            continue
        if et not in SUPPORTED_EVENT_TYPES:
            bad.append(et)
        else:
            cleaned.append(et)
    if bad:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_failed",
                "field": "event_types",
                "reason": "unsupported event types",
                "unsupported": bad,
                "supported": sorted(SUPPORTED_EVENT_TYPES),
            },
        )
    if not cleaned:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_failed",
                "field": "event_types",
                "reason": "must contain at least one supported event type",
                "supported": sorted(SUPPORTED_EVENT_TYPES),
            },
        )
    # de-dupe preserving order
    seen: set[str] = set()
    return [e for e in cleaned if not (e in seen or seen.add(e))]


async def _load_webhook(r: aioredis.Redis, webhook_id: str) -> dict[str, str]:
    record = await r.hgetall(_k_webhook(webhook_id))
    if not record:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "resource": "webhook",
                    "resource_id": webhook_id},
        )
    return record


def _summary_from_record(record: dict[str, str]) -> dict[str, Any]:
    last_delivery_at = record.get("last_delivery_at")
    return {
        "webhook_id": record.get("webhook_id", ""),
        "brand_id": record.get("brand_id", ""),
        "target_url": record.get("target_url", ""),
        "event_types": _decode_event_types(record.get("event_types")),
        "status": record.get("status", "unknown"),
        "description": record.get("description") or None,
        "created_at": int(record.get("created_at") or 0),
        "last_delivery_at": (
            int(last_delivery_at) if last_delivery_at else None
        ),
        "failed_streak": int(record.get("failed_streak") or 0),
    }


def _delivery_from_record(record: dict[str, str]) -> dict[str, Any]:
    rc = record.get("response_code")
    delivered_at = record.get("delivered_at")
    next_attempt_at = record.get("next_attempt_at")
    return {
        "delivery_id": record.get("delivery_id", ""),
        "webhook_id": record.get("webhook_id", ""),
        "event_type": record.get("event_type", ""),
        "status": record.get("status", "unknown"),
        "attempts": int(record.get("attempts") or 0),
        "response_code": int(rc) if rc else None,
        "last_error": record.get("last_error") or None,
        "created_at": int(float(record.get("created_at") or 0)),
        "delivered_at": (
            int(float(delivered_at)) if delivered_at else None
        ),
        "next_attempt_at": (
            int(float(next_attempt_at)) if next_attempt_at else None
        ),
    }


# ── Public producer API (called by other routers / workers) ──────────────


async def deliver_webhook(
    webhook_id: str,
    event_type: str,
    payload: dict[str, Any],
    r: aioredis.Redis,
) -> str:
    """Enqueue a single webhook delivery. Worker handles signing + retry.

    Returns the minted ``delivery_id``.
    """
    delivery_id = _mint_delivery_id()
    now = time.time()
    body = json.dumps(payload, separators=(",", ":"), default=str)
    pipe = r.pipeline()
    pipe.hset(
        _k_delivery(delivery_id),
        mapping={
            "delivery_id": delivery_id,
            "webhook_id": webhook_id,
            "event_type": event_type,
            "payload": body,
            "status": "pending",
            "attempts": "0",
            "created_at": str(now),
        },
    )
    pipe.expire(_k_delivery(delivery_id), DELIVERY_TTL_SECONDS)
    pipe.zadd(_K_DELIVERY_QUEUE, {delivery_id: now})
    pipe.zadd(_k_webhook_deliveries(webhook_id), {delivery_id: now})
    pipe.zremrangebyrank(
        _k_webhook_deliveries(webhook_id),
        0,
        -(DELIVERY_INDEX_MAX + 1),
    )
    await pipe.execute()
    return delivery_id


async def fan_out_webhook_to_brand(
    brand_id: str,
    event_type: str,
    payload: dict[str, Any],
    r: aioredis.Redis,
) -> list[str]:
    """Fan out an event to every active webhook the brand subscribes to.

    Returns the list of ``delivery_id`` minted. Safe to call from any
    producer hot path — silently no-ops when nobody is subscribed.

    The shipped JSON payload is enriched with ``event_type``, ``event_id``,
    ``brand_id`` and an ``occurred_at`` timestamp so merchants get a
    self-describing envelope.
    """
    if event_type not in SUPPORTED_EVENT_TYPES:
        logger.debug("fan_out skipped: unsupported event_type=%s", event_type)
        return []

    # Intersect brand-owned webhooks with the event subscriber set so we
    # only pay for the brands that explicitly subscribed.
    candidates = await r.sinter(
        _k_brand_webhooks(brand_id),
        _k_event_subscribers(event_type),
    )
    if not candidates:
        return []

    # Pre-fetch status in one round-trip and drop disabled webhooks.
    pipe = r.pipeline()
    cand_list = list(candidates)
    for wid in cand_list:
        pipe.hget(_k_webhook(wid), "status")
    statuses = await pipe.execute()

    envelope = {
        "event_id": f"evt_{uuid4().hex[:22]}",
        "event_type": event_type,
        "brand_id": brand_id,
        "occurred_at": now_ts(),
        "data": payload,
    }

    delivery_ids: list[str] = []
    for wid, status in zip(cand_list, statuses):
        if status != "active":
            continue
        did = await deliver_webhook(wid, event_type, envelope, r)
        delivery_ids.append(did)
    return delivery_ids


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("/register", response_model=RegisterWebhookResponse, status_code=201)
async def register_webhook(
    body: RegisterWebhookRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> RegisterWebhookResponse:
    """Register a new outbound webhook for a brand."""
    event_types = _validate_event_types(body.event_types)
    target_url = str(body.target_url)
    if not target_url.lower().startswith(("https://", "http://")):
        # HttpUrl already enforces this, but keep belt-and-braces.
        raise HTTPException(
            status_code=422,
            detail={"error": "validation_failed", "field": "target_url",
                    "reason": "must be http(s)"},
        )

    webhook_id = _mint_webhook_id()
    signing_secret = _mint_signing_secret()
    created_at = now_ts()

    pipe = r.pipeline()
    pipe.hset(
        _k_webhook(webhook_id),
        mapping={
            "webhook_id": webhook_id,
            "brand_id": body.brand_id,
            "target_url": target_url,
            "event_types": ",".join(event_types),
            "signing_secret": signing_secret,
            "status": "active",
            "description": body.description or "",
            "created_at": str(created_at),
            "failed_streak": "0",
        },
    )
    pipe.sadd(_k_brand_webhooks(body.brand_id), webhook_id)
    for et in event_types:
        pipe.sadd(_k_event_subscribers(et), webhook_id)
    await pipe.execute()

    logger.info(
        "webhook registered: webhook_id=%s brand=%s events=%s",
        webhook_id, body.brand_id, event_types,
    )
    return RegisterWebhookResponse(
        webhook_id=webhook_id,
        signing_secret=signing_secret,
        status="active",
        target_url=target_url,
        event_types=event_types,
        created_at=created_at,
    )


@router.post("/{webhook_id}/test")
async def test_webhook(
    webhook_id: str,
    body: TestWebhookRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Fire a synchronous test delivery and return the merchant response.

    Bypasses the queue so merchants get immediate feedback in the portal.
    The test still uses the real signing secret so end-to-end verification
    can be exercised.
    """
    record = await _load_webhook(r, webhook_id)
    event_type = body.event_type.strip()
    if event_type not in SUPPORTED_EVENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail={"error": "validation_failed", "field": "event_type",
                    "reason": "unsupported", "value": event_type,
                    "supported": sorted(SUPPORTED_EVENT_TYPES)},
        )

    envelope = {
        "event_id": f"evt_{uuid4().hex[:22]}",
        "event_type": event_type,
        "brand_id": record.get("brand_id", ""),
        "occurred_at": now_ts(),
        "test": True,
        "data": body.payload or {"hello": "from KiX"},
    }
    payload_bytes = json.dumps(envelope, separators=(",", ":"), default=str).encode()

    ts = int(time.time())
    signature = sign_webhook(payload_bytes, record["signing_secret"], ts)
    delivery_id = _mint_delivery_id()

    # Lazy import — httpx may not be installed in every test env, but it
    # ships with FastAPI itself, so this is a hard dep at runtime.
    import httpx  # noqa: WPS433 — intentional local import

    target_url = record["target_url"]
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                target_url,
                content=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-KiX-Webhook-Signature": signature,
                    "X-KiX-Event-Type": event_type,
                    "X-KiX-Delivery-Id": delivery_id,
                    "X-KiX-Test": "true",
                },
            )
        body_preview = response.text[:RESPONSE_BODY_PREVIEW]
        ok = 200 <= response.status_code < 300
        return {
            "ok": ok,
            "delivery_id": delivery_id,
            "response_code": response.status_code,
            "response_body_preview": body_preview,
            "target_url": target_url,
            "signature_header": signature,
            "test": True,
        }
    except Exception as exc:  # noqa: BLE001 — surface to caller
        return {
            "ok": False,
            "delivery_id": delivery_id,
            "error": str(exc)[:200],
            "target_url": target_url,
            "signature_header": signature,
            "test": True,
        }


@router.get("/{webhook_id}/deliveries")
async def list_deliveries(
    webhook_id: str,
    status: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """List recent deliveries for a webhook (newest first)."""
    await _load_webhook(r, webhook_id)
    # Fetch newest first by descending score.
    raw_ids = await r.zrevrange(
        _k_webhook_deliveries(webhook_id), 0, limit - 1,
    )
    if not raw_ids:
        return list_response([], total=0, limit=limit, offset=0)

    pipe = r.pipeline()
    for did in raw_ids:
        pipe.hgetall(_k_delivery(did))
    raw_records = await pipe.execute()

    items: list[dict[str, Any]] = []
    for record in raw_records:
        if not record:
            continue
        if status and record.get("status") != status:
            continue
        items.append(_delivery_from_record(record))

    total_known = await r.zcard(_k_webhook_deliveries(webhook_id))
    return list_response(items, total=total_known, limit=limit, offset=0)


@router.post("/{webhook_id}/disable")
async def disable_webhook(
    webhook_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Mark a webhook inactive. In-flight deliveries are dropped by worker."""
    record = await _load_webhook(r, webhook_id)
    if record.get("status") == "disabled":
        return {"ok": True, "webhook_id": webhook_id, "status": "disabled",
                "already": True}
    await r.hset(_k_webhook(webhook_id), "status", "disabled")
    return {"ok": True, "webhook_id": webhook_id, "status": "disabled"}


@router.post("/{webhook_id}/enable")
async def enable_webhook(
    webhook_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Re-activate a disabled webhook."""
    await _load_webhook(r, webhook_id)
    await r.hset(_k_webhook(webhook_id), mapping={
        "status": "active",
        "failed_streak": "0",
    })
    return {"ok": True, "webhook_id": webhook_id, "status": "active"}


@router.post("/{webhook_id}/rotate-secret")
async def rotate_secret(
    webhook_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Mint and persist a fresh signing secret. Old secret invalid immediately."""
    await _load_webhook(r, webhook_id)
    new_secret = _mint_signing_secret()
    await r.hset(_k_webhook(webhook_id), "signing_secret", new_secret)
    logger.info("webhook secret rotated: webhook_id=%s", webhook_id)
    return {"ok": True, "webhook_id": webhook_id, "signing_secret": new_secret}


@router.delete("/{webhook_id}", status_code=204)
async def delete_webhook(
    webhook_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> None:
    """Permanently remove a webhook and its event-type subscriptions."""
    record = await _load_webhook(r, webhook_id)
    brand_id = record.get("brand_id", "")
    event_types = _decode_event_types(record.get("event_types"))

    pipe = r.pipeline()
    pipe.delete(_k_webhook(webhook_id))
    if brand_id:
        pipe.srem(_k_brand_webhooks(brand_id), webhook_id)
    for et in event_types:
        pipe.srem(_k_event_subscribers(et), webhook_id)
    # Keep the delivery index zset around for short-term audit, but flag it
    # for TTL-based GC so we don't leak.
    pipe.expire(_k_webhook_deliveries(webhook_id), DELIVERY_TTL_SECONDS)
    await pipe.execute()
    return None


@router.get("/{webhook_id}", response_model=WebhookSummary)
async def get_webhook(
    webhook_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return webhook metadata (does NOT expose the signing secret)."""
    record = await _load_webhook(r, webhook_id)
    return _summary_from_record(record)


@router.get("/brand/{brand_id}")
async def list_brand_webhooks(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """List every webhook a brand has registered."""
    wids = await r.smembers(_k_brand_webhooks(brand_id))
    if not wids:
        return list_response([], total=0, limit=0, offset=0)

    pipe = r.pipeline()
    wid_list = list(wids)
    for wid in wid_list:
        pipe.hgetall(_k_webhook(wid))
    records = await pipe.execute()

    items: list[dict[str, Any]] = [
        _summary_from_record(rec) for rec in records if rec
    ]
    items.sort(key=lambda d: d.get("created_at", 0), reverse=True)
    return list_response(items, total=len(items), limit=len(items), offset=0)


@router.get("/event-types/supported")
async def list_supported_event_types() -> dict[str, Any]:
    """Return the catalog of event types merchants may subscribe to."""
    return {"event_types": sorted(SUPPORTED_EVENT_TYPES),
            "count": len(SUPPORTED_EVENT_TYPES)}


# ── Worker-side helpers (also imported by the worker module) ─────────────


async def _record_success(
    r: aioredis.Redis,
    delivery_id: str,
    webhook_id: str,
    response_code: int,
    response_body: str,
    attempts: int,
) -> None:
    now = time.time()
    pipe = r.pipeline()
    pipe.hset(_k_delivery(delivery_id), mapping={
        "status": "delivered",
        "attempts": str(attempts),
        "response_code": str(response_code),
        "response_body": response_body[:RESPONSE_BODY_PREVIEW],
        "delivered_at": str(now),
    })
    pipe.expire(_k_delivery(delivery_id), DELIVERY_TTL_SECONDS)
    pipe.hset(_k_webhook(webhook_id), mapping={
        "last_delivery_at": str(int(now)),
        "failed_streak": "0",
    })
    await pipe.execute()


async def _record_retry(
    r: aioredis.Redis,
    delivery_id: str,
    webhook_id: str,
    attempts: int,
    error: str,
    response_code: int | None = None,
) -> None:
    """Schedule retry with exponential backoff; mark failed_permanent at cap."""
    if attempts >= MAX_RETRIES:
        pipe = r.pipeline()
        pipe.hset(_k_delivery(delivery_id), mapping={
            "status": "failed_permanent",
            "attempts": str(attempts),
            "last_error": error[:200],
            **({"response_code": str(response_code)}
               if response_code is not None else {}),
        })
        pipe.expire(_k_delivery(delivery_id), DELIVERY_TTL_SECONDS)
        pipe.hincrby(_k_webhook(webhook_id), "failed_streak", 1)
        await pipe.execute()
        return

    backoff = RETRY_BACKOFF_SECONDS[attempts - 1]
    next_attempt = time.time() + backoff

    pipe = r.pipeline()
    update = {
        "status": "retrying",
        "attempts": str(attempts),
        "next_attempt_at": str(next_attempt),
        "last_error": error[:200],
    }
    if response_code is not None:
        update["response_code"] = str(response_code)
    pipe.hset(_k_delivery(delivery_id), mapping=update)
    pipe.expire(_k_delivery(delivery_id), DELIVERY_TTL_SECONDS)
    pipe.zadd(_K_DELIVERY_QUEUE, {delivery_id: next_attempt})
    pipe.hincrby(_k_webhook(webhook_id), "failed_streak", 1)
    await pipe.execute()


async def _send_single_delivery(
    r: aioredis.Redis,
    delivery_id: str,
) -> dict[str, Any]:
    """Pop one delivery record, sign + POST it, persist outcome.

    Returns a small dict describing the outcome (for worker logging).
    """
    import httpx  # local import — only needed when the worker actually runs

    delivery = await r.hgetall(_k_delivery(delivery_id))
    if not delivery:
        return {"delivery_id": delivery_id, "result": "missing"}

    webhook_id = delivery.get("webhook_id", "")
    webhook = await r.hgetall(_k_webhook(webhook_id))
    if not webhook or webhook.get("status") != "active":
        await r.hset(_k_delivery(delivery_id), mapping={
            "status": "dropped_inactive_webhook",
            "last_error": "webhook_inactive_or_missing",
        })
        return {"delivery_id": delivery_id, "result": "dropped_inactive"}

    attempts = int(delivery.get("attempts") or 0) + 1
    payload_bytes = delivery.get("payload", "").encode("utf-8")
    ts = int(time.time())
    signature = sign_webhook(payload_bytes, webhook["signing_secret"], ts)

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                webhook["target_url"],
                content=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-KiX-Webhook-Signature": signature,
                    "X-KiX-Event-Type": delivery.get("event_type", ""),
                    "X-KiX-Delivery-Id": delivery_id,
                    "X-KiX-Attempt": str(attempts),
                },
            )
    except Exception as exc:  # noqa: BLE001 — network/timeout/etc
        await _record_retry(r, delivery_id, webhook_id, attempts,
                            f"transport:{exc!r}"[:200])
        return {"delivery_id": delivery_id, "result": "retry",
                "error": str(exc)[:200], "attempts": attempts}

    if 200 <= response.status_code < 300:
        await _record_success(
            r, delivery_id, webhook_id,
            response.status_code, response.text, attempts,
        )
        return {"delivery_id": delivery_id, "result": "delivered",
                "code": response.status_code, "attempts": attempts}

    await _record_retry(
        r, delivery_id, webhook_id, attempts,
        f"http_{response.status_code}:{response.text[:120]}",
        response_code=response.status_code,
    )
    return {"delivery_id": delivery_id, "result": "retry",
            "code": response.status_code, "attempts": attempts}


async def drain_due_deliveries(
    r: aioredis.Redis,
    batch_size: int = 50,
) -> dict[str, int]:
    """Process every delivery whose ``next_attempt_at`` is ``≤ now``.

    Worker entry point. Pops up to ``batch_size`` items per cycle and
    handles them sequentially-but-concurrently (asyncio.gather with a
    modest cap). Returns counters for the cycle.
    """
    now = time.time()
    raw_due = await r.zrangebyscore(
        _K_DELIVERY_QUEUE, "-inf", now, start=0, num=batch_size,
    )
    if not raw_due:
        return {"due": 0, "delivered": 0, "retried": 0, "dropped": 0}

    # Atomically claim each id (ZREM returns 1 only for the claimer).
    claimed: list[str] = []
    for did in raw_due:
        removed = await r.zrem(_K_DELIVERY_QUEUE, did)
        if removed:
            claimed.append(did)

    if not claimed:
        return {"due": 0, "delivered": 0, "retried": 0, "dropped": 0}

    results = await asyncio.gather(
        *[_send_single_delivery(r, did) for did in claimed],
        return_exceptions=True,
    )

    delivered = retried = dropped = 0
    for res in results:
        if isinstance(res, BaseException):
            retried += 1
            continue
        out = res.get("result")
        if out == "delivered":
            delivered += 1
        elif out == "retry":
            retried += 1
        else:
            dropped += 1
    return {"due": len(claimed), "delivered": delivered,
            "retried": retried, "dropped": dropped}
