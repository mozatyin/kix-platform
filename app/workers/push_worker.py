"""Push notification dispatcher.

Consumes ``push:outbound:queue`` (LIST written by
``app.routers.push_engine.dispatch``) and routes each push to the right
platform gateway based on the kid's registered devices:

* ``ios``     → APNS (Apple Push Notification Service)
* ``android`` → FCM  (Firebase Cloud Messaging)
* ``wechat``  → WeChat template / subscribe message
* ``web``     → Web Push (browser notification, VAPID)

Real production wires this to:

* ``firebase_admin.messaging`` for FCM
* ``apns2.client.APNsClient`` (or ``aioapns``) for APNS
* WeChat Open Platform ``cgi-bin/message/template/send`` HTTP API
* ``pywebpush`` for Web Push

MVP behaviour: simulated success + structured log to
``push:outbound:log`` so the rest of the pipeline (delivery state,
metrics, merchant billing, inbox surfacing) can be validated end-to-end
without external dependencies. The platform-routing seam (
``_send_to_platform``) is the single place to swap in real SDK calls.

Queue payload contract
----------------------
``push_engine.dispatch`` currently ``LPUSH``-es a bare ``push_id``
string. We accept BOTH forms transparently:

* bare push_id string → we ``HGETALL push:{push_id}`` to recover the
  ``kid``/``title``/``body``/``deep_link`` fields.
* JSON object payload → we read ``push_id`` / ``kid`` directly off the
  payload (used by retries and any future enqueuers that want to ship a
  self-contained envelope).

Usage
-----
::

    .venv/bin/python -m app.workers.push_worker --once   # one cycle
    .venv/bin/python -m app.workers.push_worker          # loop forever

Redis schema written by this worker
-----------------------------------
::

    push:outbound:log           LIST  (newest first, capped 10k)
    push:outbound:retry         LIST  payloads pending backoff
    push_device:{device_id}     HASH  {kid, platform, token, registered_at}
    kid:{kid}:push_devices      SET   of device_ids
    push:{push_id}              HASH  (mutated: delivered_at, delivery_status,
                                       delivery_attempts, last_error)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from typing import Any

from app.redis_client import close_redis, get_redis, init_redis

logger = logging.getLogger("push_worker")

# ── Tunables ──────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS = 5
BATCH_SIZE = 50
MAX_DELIVERY_ATTEMPTS = 3
RETRY_BASE_BACKOFF_SECONDS = 30
OUTBOUND_LOG_MAX = 9999
OUTBOUND_QUEUE_KEY = "push:outbound:queue"
RETRY_QUEUE_KEY = "push:outbound:retry"
OUTBOUND_LOG_KEY = "push:outbound:log"


# ── Payload helpers ──────────────────────────────────────────────────────


def _decode_queue_item(item: str) -> dict[str, Any]:
    """Accept either a bare ``push_id`` string or a JSON object.

    Returns a dict with at minimum a ``push_id`` (possibly empty) and any
    additional fields the producer chose to carry.
    """
    if not item:
        return {}
    s = item.strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    # Treat as opaque push_id string.
    return {"push_id": s}


async def _hydrate_from_push_hash(r, payload: dict[str, Any]) -> dict[str, Any]:
    """If the payload only carries push_id, pull kid/title/body from
    the persisted push hash so we can route to the user's devices."""
    push_id = payload.get("push_id")
    if not push_id:
        return payload
    if payload.get("kid"):
        return payload  # already hydrated
    record = await r.hgetall(f"push:{push_id}")
    if not record:
        return payload  # let downstream report no_kid / no_devices
    merged = dict(record)
    merged.update({k: v for k, v in payload.items() if v not in (None, "")})
    return merged


# ── Gateway routing ──────────────────────────────────────────────────────


async def _send_to_platform(
    platform: str, token: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Stub for actual gateway send. Replace with real SDK calls in prod.

    In production this is the single seam where we wire in:

    * ``ios``      → ``APNsClient(...).send_notification(token, ...)``
    * ``android``  → ``messaging.send(messaging.Message(token=token, ...))``
    * ``wechat``   → ``wechat_api.send_template_message(openid=token, ...)``
    * ``web``      → ``pywebpush.webpush(subscription_info=..., data=...)``

    All branches return the same envelope so the caller can treat them
    uniformly.
    """
    if not platform:
        return {"success": False, "error": "missing_platform"}
    if not token:
        return {"success": False, "error": "missing_token", "platform": platform}

    # MVP: simulate a tiny amount of network latency so the metrics look
    # roughly realistic in dev/staging dashboards.
    await asyncio.sleep(0.05)

    title = payload.get("title", "")
    body = payload.get("body", "")
    logger.info(
        "push.deliver platform=%s token=%s… push_id=%s title=%r",
        platform, token[:8], payload.get("push_id"), title[:40],
    )

    return {
        "success": True,
        "platform": platform,
        "simulated": True,
        "title": title,
        "body_preview": body[:64],
        "ts": time.time(),
    }


async def deliver_push(payload: dict[str, Any]) -> dict[str, Any]:
    """Route push to every device registered against payload['kid']."""
    kid = payload.get("kid")
    if not kid:
        return {"success": False, "error": "no_kid"}

    r = await get_redis()
    devices = await r.smembers(f"kid:{kid}:push_devices")
    if not devices:
        return {"success": False, "error": "no_devices_registered", "kid": kid}

    results: list[dict[str, Any]] = []
    for device_id in devices:
        device_info = await r.hgetall(f"push_device:{device_id}")
        if not device_info:
            # Stale set member — clean up so we don't keep retrying.
            await r.srem(f"kid:{kid}:push_devices", device_id)
            continue

        platform = device_info.get("platform")
        token = device_info.get("token")

        result = await _send_to_platform(platform, token, payload)
        results.append({"device_id": device_id, "platform": platform, **result})

    if not results:
        return {"success": False, "error": "no_live_devices", "kid": kid}

    success = any(item.get("success") for item in results)
    return {"success": success, "deliveries": results}


# ── Queue processing ─────────────────────────────────────────────────────


async def process_batch(r) -> tuple[int, int]:
    """Pop a batch from the outbound queue and deliver each item.

    Returns ``(delivered, failed)`` counters for the cycle.
    """
    delivered = 0
    failed = 0

    for _ in range(BATCH_SIZE):
        item = await r.lpop(OUTBOUND_QUEUE_KEY)
        if not item:
            break

        try:
            payload = _decode_queue_item(item)
            payload = await _hydrate_from_push_hash(r, payload)

            result = await deliver_push(payload)

            push_id = payload.get("push_id")
            if push_id:
                push_key = f"push:{push_id}"
                prev_attempts = int(
                    (await r.hget(push_key, "delivery_attempts")) or 0
                )
                update = {
                    "delivery_status": (
                        "delivered" if result.get("success") else "failed"
                    ),
                    "delivery_attempts": str(prev_attempts + 1),
                }
                if result.get("success"):
                    update["delivered_at"] = str(time.time())
                else:
                    err = str(result.get("error") or "unknown")
                    update["last_error"] = err[:200]
                # Only HSET if the push record exists — avoid resurrecting
                # an expired key with partial state.
                if await r.exists(push_key):
                    await r.hset(push_key, mapping=update)

            log_entry = {
                "push_id": payload.get("push_id"),
                "kid": payload.get("kid"),
                "result": result,
                "ts": time.time(),
            }
            await r.lpush(OUTBOUND_LOG_KEY, json.dumps(log_entry))
            await r.ltrim(OUTBOUND_LOG_KEY, 0, OUTBOUND_LOG_MAX)

            if result.get("success"):
                delivered += 1
            else:
                failed += 1
                attempts = int(payload.get("attempts", 0)) + 1
                if attempts < MAX_DELIVERY_ATTEMPTS:
                    retry_envelope = dict(payload)
                    retry_envelope["attempts"] = attempts
                    retry_envelope["last_attempt_at"] = time.time()
                    await r.rpush(
                        RETRY_QUEUE_KEY, json.dumps(retry_envelope)
                    )

        except Exception as exc:
            logger.exception("Failed to process push item: %s", exc)
            failed += 1

    return delivered, failed


async def process_retry(r) -> int:
    """Promote ready retry-queue items back onto the outbound queue.

    Each item carries ``attempts`` and ``last_attempt_at``. We use
    exponential backoff (``2**attempts * base``) — items not yet ripe go
    to the tail of the retry queue so we round-robin through them
    without busy-spinning.

    Returns the number of items promoted in this cycle (one).
    """
    item = await r.lpop(RETRY_QUEUE_KEY)
    if not item:
        return 0

    try:
        payload = json.loads(item)
    except json.JSONDecodeError:
        logger.warning("dropping malformed retry item: %r", item[:80])
        return 0

    last_attempt_at = float(payload.get("last_attempt_at") or 0)
    attempts = int(payload.get("attempts") or 1)

    backoff = (2 ** attempts) * RETRY_BASE_BACKOFF_SECONDS  # 60, 120, 240
    if time.time() < last_attempt_at + backoff:
        # Not ready — return to tail so other items get a turn first.
        await r.rpush(RETRY_QUEUE_KEY, json.dumps(payload))
        return 0

    # Ready: re-enqueue onto the main outbound queue.
    await r.rpush(OUTBOUND_QUEUE_KEY, json.dumps(payload))
    return 1


# ── Device registration helpers (called from routers) ────────────────────


async def device_register(
    r,
    kid: str,
    platform: str,
    token: str,
    device_id: str | None = None,
) -> str:
    """Register a push device for a kid.

    Returns the resolved ``device_id``. If ``device_id`` is supplied, the
    record is upserted in place (handy for re-registering after token
    rotation on the same physical device). Otherwise we mint a stable id
    by hashing the token.
    """
    if not kid or not platform or not token:
        raise ValueError("kid, platform, token are all required")
    if not device_id:
        device_id = f"pd_{int(time.time())}_{hash(token) & 0xFFFFFFFF:x}"

    await r.hset(
        f"push_device:{device_id}",
        mapping={
            "kid": kid,
            "platform": platform,
            "token": token,
            "registered_at": str(time.time()),
        },
    )
    await r.sadd(f"kid:{kid}:push_devices", device_id)
    return device_id


async def device_unregister(r, kid: str, device_id: str) -> bool:
    """Remove a registered push device. Returns True if anything was deleted."""
    if not kid or not device_id:
        return False
    info = await r.hgetall(f"push_device:{device_id}")
    if info and info.get("kid") and info.get("kid") != kid:
        # device belongs to a different kid — refuse silently to avoid
        # leaking cross-user info.
        return False
    removed_set = await r.srem(f"kid:{kid}:push_devices", device_id)
    removed_hash = await r.delete(f"push_device:{device_id}")
    return bool(removed_set or removed_hash)


# ── Loop ─────────────────────────────────────────────────────────────────


async def run_once() -> dict[str, int]:
    r = await get_redis()
    delivered, failed = await process_batch(r)
    promoted = await process_retry(r)
    return {"delivered": delivered, "failed": failed, "retried": promoted}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    await init_redis()
    try:
        if "--once" in sys.argv:
            result = await run_once()
            print(json.dumps(result))
            return
        logger.info("push_worker started: polling every %ss", POLL_INTERVAL_SECONDS)
        while True:
            try:
                result = await run_once()
                if result["delivered"] or result["failed"] or result["retried"]:
                    logger.info("cycle: %s", result)
            except Exception as exc:
                logger.exception("cycle failed: %s", exc)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    finally:
        await close_redis()


if __name__ == "__main__":
    asyncio.run(main())
