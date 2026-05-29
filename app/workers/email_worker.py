"""Email outbox worker.

Drains ``email_queue:brand:{brand_id}`` lists into the actual mail
transport. In production this would call SES / SMTP / SendGrid; in
tests + dev it's a structured no-op — we log the envelope at INFO
and return it so callers (e.g. integration tests) can assert on the
side effect without monkey-patching network calls.

The worker is deliberately *thin* — all rendering happened upstream
in :mod:`app.services.email_template_service`. By the time an
envelope lands here, ``subject`` + ``body_text`` + ``body_html`` are
already final strings.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.services.email_template_service import email_queue_key, push_queue_key

logger = logging.getLogger(__name__)

__all__ = [
    "drain_email_queue",
    "drain_push_queue",
    "deliver_email",
    "deliver_push",
    "run_email_worker",
]


# ── Transport stubs ───────────────────────────────────────────────────────


def deliver_email(envelope: dict[str, Any]) -> dict[str, Any]:
    """Stub: in prod would call boto3 SES.send_email or similar.

    Returns a delivery receipt dict. Logs at INFO so production
    operators can see traffic shape without enabling DEBUG.
    """
    logger.info(
        "email.deliver template=%s locale=%s recipient=%s subject=%r",
        envelope.get("template_id"),
        envelope.get("locale"),
        envelope.get("recipient"),
        envelope.get("subject"),
    )
    return {"delivered": True, "transport": "stub"}


def deliver_push(envelope: dict[str, Any]) -> dict[str, Any]:
    """Stub: in prod would call FCM / APNS."""
    logger.info(
        "push.deliver template=%s locale=%s kid=%s title=%r",
        envelope.get("template_id"),
        envelope.get("locale"),
        envelope.get("recipient_kid"),
        envelope.get("title"),
    )
    return {"delivered": True, "transport": "stub"}


# ── Queue drain ───────────────────────────────────────────────────────────


async def drain_email_queue(
    redis: Any, brand_id: str, max_messages: int = 100,
) -> list[dict[str, Any]]:
    """Pop up to ``max_messages`` envelopes from a brand's email queue.

    Returns the list of *delivery receipts* (one per envelope). On
    JSON-decode failure the bad payload is logged + skipped — we do
    not block the queue on a single corrupt entry.
    """
    receipts: list[dict[str, Any]] = []
    queue = email_queue_key(brand_id)
    for _ in range(max_messages):
        raw = await redis.lpop(queue)
        if raw is None:
            break
        try:
            payload = raw if isinstance(raw, (bytes, bytearray)) else raw
            envelope = json.loads(payload)
        except (ValueError, TypeError) as exc:
            logger.warning("email_worker: bad payload on %s: %s", queue, exc)
            continue
        receipts.append(deliver_email(envelope))
    return receipts


async def drain_push_queue(
    redis: Any, brand_id: str, max_messages: int = 100,
) -> list[dict[str, Any]]:
    """Same as ``drain_email_queue`` but for push outbox."""
    receipts: list[dict[str, Any]] = []
    queue = push_queue_key(brand_id)
    for _ in range(max_messages):
        raw = await redis.lpop(queue)
        if raw is None:
            break
        try:
            envelope = json.loads(raw)
        except (ValueError, TypeError) as exc:
            logger.warning("push_worker: bad payload on %s: %s", queue, exc)
            continue
        receipts.append(deliver_push(envelope))
    return receipts


# ── Long-running worker entrypoint (prod) ─────────────────────────────────


async def run_email_worker(redis: Any, *, poll_interval: float = 1.0) -> None:  # pragma: no cover
    """Forever-loop drain across every queue key matching the pattern.

    Not exercised in unit tests (it would race the test loop); the
    drainer functions above are the testable surface.
    """
    while True:
        cursor: int = 0
        while True:
            cursor, keys = await redis.scan(
                cursor=cursor, match="email_queue:brand:*", count=100
            )
            for key in keys:
                key_str = key.decode() if isinstance(key, (bytes, bytearray)) else key
                brand_id = key_str.rsplit(":", 1)[-1]
                await drain_email_queue(redis, brand_id)
            if cursor == 0:
                break
        await asyncio.sleep(poll_interval)
