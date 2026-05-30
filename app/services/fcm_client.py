"""FCM Client — Firebase Cloud Messaging wrapper.

This module is the single seam between the platform's push pipeline and
Google's Firebase Cloud Messaging service. It auto-detects mode based on
configuration:

* ``live``  — when ``FIREBASE_SERVICE_ACCOUNT`` env var is set (path to
  JSON service-account credentials) **and** ``firebase-admin`` is
  importable. All calls hit the real FCM HTTPS v1 API. APNS delivery is
  routed *through* FCM (Firebase forwards iOS pushes via APNS for us, so
  a single client covers Android + iOS + Web).
* ``mock`` — used in dev/test/CI when no credentials are present. All
  functions return plausible fake responses so the rest of the pipeline
  (worker, retry, metrics, billing) can be validated end-to-end without
  external dependencies.

Public surface (stable for callers):

    send_to_token(token, title, body, data=None, badge=None, sound=None,
                  platform=None) -> dict
    send_multicast(tokens, title, body, data=None) -> dict
    subscribe_to_topic(tokens, topic) -> dict
    unsubscribe_from_topic(tokens, topic) -> dict
    validate_token(token) -> bool
    get_mode() -> "live" | "mock"
    is_configured() -> bool
    record_last_sent() -> None              # internal use
    record_failure(reason: str) -> None     # internal use

Quota guard:
    Each project is rate-limited to ``MAX_PUSHES_PER_HOUR`` (default
    100,000) using a sliding-window counter in Redis. When the cap is
    exceeded, ``send_to_token`` / ``send_multicast`` return
    ``{"success": False, "error": "rate_limited", "retry_after_s": N}``.

Stale tokens:
    When FCM responds with ``UNREGISTERED`` or ``INVALID_ARGUMENT``, we
    surface ``stale=True`` in the result so callers can mark the device
    record inactive (see push_worker → cleanup pipeline).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Iterable

try:  # pragma: no cover — optional dep, exercised only when live
    import firebase_admin  # type: ignore
    from firebase_admin import credentials as _fb_credentials  # type: ignore
    from firebase_admin import messaging as _fb_messaging  # type: ignore
    _FIREBASE_AVAILABLE = True
except ImportError:  # pragma: no cover — dev/test path
    firebase_admin = None  # type: ignore[assignment]
    _fb_credentials = None  # type: ignore[assignment]
    _fb_messaging = None  # type: ignore[assignment]
    _FIREBASE_AVAILABLE = False


logger = logging.getLogger("fcm_client")

# ── Tunables ──────────────────────────────────────────────────────────────

MAX_PUSHES_PER_HOUR = int(os.environ.get("FCM_MAX_PUSHES_PER_HOUR", "100000"))
MULTICAST_BATCH_SIZE = 500  # FCM hard limit per send_each_for_multicast call
RATE_LIMIT_KEY_PREFIX = "fcm:ratelimit"
LAST_SENT_KEY = "fcm:last_sent_ts"
FAILURE_COUNTER_KEY = "fcm:failures:hourly"

# Mock-mode counters (process-local, for tests asserting send count).
_MOCK_STATS: dict[str, int] = {"sent": 0, "failed": 0, "subscribed": 0}


# ── Mode detection ────────────────────────────────────────────────────────


_INIT_DONE = False
_INIT_MODE: str = "mock"


def _init_firebase() -> str:
    """Initialise firebase_admin once; return resolved mode.

    Idempotent — safe to call repeatedly. Returns ``"live"`` if creds
    + firebase-admin are present and init succeeds, else ``"mock"``.
    """
    global _INIT_DONE, _INIT_MODE
    if _INIT_DONE:
        return _INIT_MODE

    creds_path = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not _FIREBASE_AVAILABLE or not creds_path:
        _INIT_MODE = "mock"
        _INIT_DONE = True
        logger.info(
            "FCM client running in mock mode "
            "(firebase_admin=%s, creds=%s)",
            _FIREBASE_AVAILABLE, bool(creds_path),
        )
        return _INIT_MODE

    try:  # pragma: no cover — only exercised when creds present
        if not firebase_admin._apps:  # type: ignore[union-attr]
            cred = _fb_credentials.Certificate(creds_path)  # type: ignore[union-attr]
            firebase_admin.initialize_app(cred)  # type: ignore[union-attr]
        _INIT_MODE = "live"
        logger.info("FCM client initialised in LIVE mode (creds=%s)", creds_path)
    except Exception as exc:  # pragma: no cover
        logger.error(
            "FCM client init failed, falling back to mock: %s", exc,
        )
        _INIT_MODE = "mock"
    _INIT_DONE = True
    return _INIT_MODE


def get_mode() -> str:
    """Return the current FCM client mode (``live`` or ``mock``)."""
    return _init_firebase()


def is_configured() -> bool:
    """True if running in live mode against real Firebase."""
    return get_mode() == "live"


# ── Quota guard (sliding-window via Redis) ────────────────────────────────


async def _check_and_increment_quota(count: int = 1) -> tuple[bool, int]:
    """Return (allowed, current_count_in_window).

    Best-effort: if Redis is unreachable we allow the send (we'd rather
    deliver the push than block on infra). Uses an hourly bucket key
    that auto-expires so we don't grow unbounded.
    """
    try:
        from app.redis_client import get_redis
        r = await get_redis()
    except Exception:  # pragma: no cover
        return True, 0

    bucket = int(time.time() // 3600)
    key = f"{RATE_LIMIT_KEY_PREFIX}:{bucket}"
    try:
        new_total = await r.incrby(key, count)
        if new_total == count:
            await r.expire(key, 3700)
    except Exception:  # pragma: no cover
        return True, 0

    if new_total > MAX_PUSHES_PER_HOUR:
        return False, int(new_total)
    return True, int(new_total)


async def record_last_sent() -> None:
    """Record successful send timestamp (best-effort)."""
    try:
        from app.redis_client import get_redis
        r = await get_redis()
        await r.set(LAST_SENT_KEY, str(time.time()))
    except Exception:  # pragma: no cover
        pass


async def record_failure(reason: str) -> None:
    """Record a delivery failure in a per-hour bucket (best-effort)."""
    try:
        from app.redis_client import get_redis
        r = await get_redis()
        bucket = int(time.time() // 3600)
        key = f"{FAILURE_COUNTER_KEY}:{bucket}"
        await r.hincrby(key, reason[:64], 1)
        await r.expire(key, 86400 + 3600)  # 25h, covers rolling 24h
    except Exception:  # pragma: no cover
        pass


async def failures_last_24h() -> int:
    """Sum failure counters from the last 24 hourly buckets."""
    try:
        from app.redis_client import get_redis
        r = await get_redis()
        total = 0
        now_bucket = int(time.time() // 3600)
        for offset in range(24):
            bucket = now_bucket - offset
            key = f"{FAILURE_COUNTER_KEY}:{bucket}"
            counters = await r.hgetall(key)
            for v in (counters or {}).values():
                try:
                    total += int(v)
                except (TypeError, ValueError):
                    pass
        return total
    except Exception:  # pragma: no cover
        return 0


async def last_sent_ts() -> float | None:
    """Return last successful send timestamp, or None."""
    try:
        from app.redis_client import get_redis
        r = await get_redis()
        raw = await r.get(LAST_SENT_KEY)
        return float(raw) if raw else None
    except Exception:  # pragma: no cover
        return None


# ── Token validation ──────────────────────────────────────────────────────


def validate_token(token: str | None) -> bool:
    """Cheap structural validation — no network call.

    FCM tokens are typically 140-300 chars, URL-safe base64 + colons.
    APNS tokens used to be 64-char hex but Firebase wraps them. We
    accept anything ≥ 32 chars without whitespace as plausible.
    """
    if not token or not isinstance(token, str):
        return False
    tok = token.strip()
    if len(tok) < 32 or len(tok) > 4096:
        return False
    if any(ch.isspace() for ch in tok):
        return False
    return True


# ── Mock-mode helpers ─────────────────────────────────────────────────────


def _mock_send_result(token: str, title: str, body: str) -> dict[str, Any]:
    _MOCK_STATS["sent"] += 1
    return {
        "success": True,
        "mode": "mock",
        "message_id": f"mock-msg-{_MOCK_STATS['sent']}-{int(time.time())}",
        "token_prefix": token[:8] if token else "",
        "title": title,
        "body_preview": (body or "")[:64],
        "ts": time.time(),
    }


def _mock_multicast_result(
    tokens: list[str], title: str, body: str
) -> dict[str, Any]:
    _MOCK_STATS["sent"] += len(tokens)
    return {
        "success": True,
        "mode": "mock",
        "success_count": len(tokens),
        "failure_count": 0,
        "responses": [
            {
                "success": True,
                "message_id": f"mock-msg-multi-{i}-{int(time.time())}",
                "token_prefix": t[:8] if t else "",
            }
            for i, t in enumerate(tokens)
        ],
        "title": title,
        "body_preview": (body or "")[:64],
        "ts": time.time(),
    }


def _reset_mock_stats() -> None:
    """Test helper — reset process-local mock counters."""
    _MOCK_STATS["sent"] = 0
    _MOCK_STATS["failed"] = 0
    _MOCK_STATS["subscribed"] = 0


def _mock_stats_snapshot() -> dict[str, int]:
    return dict(_MOCK_STATS)


# ── Core send API ─────────────────────────────────────────────────────────


async def send_to_token(
    token: str,
    title: str,
    body: str,
    data: dict[str, str] | None = None,
    badge: int | None = None,
    sound: str | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    """Send a single push to one device token.

    The ``platform`` arg is informational (ios / android / web) — FCM
    handles APNS routing transparently when the token was registered
    against a Firebase iOS app.

    Returns a dict with at minimum:
        ``success`` (bool)
        ``mode``    ("live" | "mock")
        ``error``   (str, when success=False)
        ``stale``   (bool, when token should be cleaned up)
    """
    if not validate_token(token):
        return {
            "success": False,
            "mode": get_mode(),
            "error": "invalid_token",
            "stale": True,
        }

    allowed, count = await _check_and_increment_quota(1)
    if not allowed:
        await record_failure("rate_limited")
        return {
            "success": False,
            "mode": get_mode(),
            "error": "rate_limited",
            "current_hour_count": count,
            "retry_after_s": 3600 - (int(time.time()) % 3600),
        }

    mode = get_mode()
    if mode == "mock":
        result = _mock_send_result(token, title, body)
        if data:
            result["data"] = {str(k): str(v) for k, v in data.items()}
        if badge is not None:
            result["badge"] = int(badge)
        if sound:
            result["sound"] = sound
        await record_last_sent()
        return result

    # ── Live mode ──────────────────────────────────────────────────────  # pragma: no cover
    try:
        notif = _fb_messaging.Notification(title=title, body=body)
        apns_payload = None
        if badge is not None or sound:
            apns_payload = _fb_messaging.APNSConfig(
                payload=_fb_messaging.APNSPayload(
                    aps=_fb_messaging.Aps(
                        badge=int(badge) if badge is not None else None,
                        sound=sound or None,
                    ),
                ),
            )
        msg = _fb_messaging.Message(
            token=token,
            notification=notif,
            data={str(k): str(v) for k, v in (data or {}).items()} or None,
            apns=apns_payload,
        )
        message_id = _fb_messaging.send(msg)
        await record_last_sent()
        return {
            "success": True,
            "mode": "live",
            "message_id": message_id,
            "token_prefix": token[:8],
            "ts": time.time(),
        }
    except Exception as exc:
        err_name = type(exc).__name__
        stale = err_name in (
            "UnregisteredError",
            "SenderIdMismatchError",
        ) or "registration-token-not-registered" in str(exc).lower()
        await record_failure(err_name)
        return {
            "success": False,
            "mode": "live",
            "error": f"{err_name}:{exc}",
            "stale": stale,
        }


async def send_multicast(
    tokens: list[str],
    title: str,
    body: str,
    data: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Send the same push to many tokens in batches of 500.

    Returns aggregated counters + per-token results so callers can act on
    per-token failure (e.g. clean up stale tokens).
    """
    # Filter invalid tokens up-front so they don't burn quota.
    valid = [t for t in tokens if validate_token(t)]
    invalid = [t for t in tokens if not validate_token(t)]

    if not valid:
        return {
            "success": False,
            "mode": get_mode(),
            "error": "no_valid_tokens",
            "success_count": 0,
            "failure_count": len(invalid),
            "invalid_tokens": invalid,
        }

    allowed, count = await _check_and_increment_quota(len(valid))
    if not allowed:
        await record_failure("rate_limited")
        return {
            "success": False,
            "mode": get_mode(),
            "error": "rate_limited",
            "current_hour_count": count,
            "retry_after_s": 3600 - (int(time.time()) % 3600),
            "success_count": 0,
            "failure_count": len(valid),
        }

    mode = get_mode()
    success_count = 0
    failure_count = 0
    stale_tokens: list[str] = []
    responses: list[dict[str, Any]] = []
    batches_sent = 0

    # Split into batches of 500.
    for start in range(0, len(valid), MULTICAST_BATCH_SIZE):
        batch = valid[start : start + MULTICAST_BATCH_SIZE]
        batches_sent += 1

        if mode == "mock":
            batch_result = _mock_multicast_result(batch, title, body)
            success_count += batch_result["success_count"]
            responses.extend(batch_result["responses"])
            continue

        try:  # pragma: no cover — live path
            multi = _fb_messaging.MulticastMessage(
                tokens=batch,
                notification=_fb_messaging.Notification(title=title, body=body),
                data={str(k): str(v) for k, v in (data or {}).items()} or None,
            )
            br = _fb_messaging.send_each_for_multicast(multi)
            success_count += br.success_count
            failure_count += br.failure_count
            for i, resp in enumerate(br.responses):
                if resp.success:
                    responses.append({
                        "success": True,
                        "message_id": resp.message_id,
                        "token_prefix": batch[i][:8],
                    })
                else:
                    err_name = (
                        type(resp.exception).__name__
                        if resp.exception
                        else "unknown"
                    )
                    is_stale = err_name in (
                        "UnregisteredError",
                        "SenderIdMismatchError",
                    )
                    if is_stale:
                        stale_tokens.append(batch[i])
                    responses.append({
                        "success": False,
                        "error": err_name,
                        "stale": is_stale,
                        "token_prefix": batch[i][:8],
                    })
        except Exception as exc:  # pragma: no cover
            failure_count += len(batch)
            await record_failure(type(exc).__name__)
            responses.extend([
                {"success": False, "error": str(exc), "token_prefix": t[:8]}
                for t in batch
            ])

    if success_count:
        await record_last_sent()

    return {
        "success": success_count > 0,
        "mode": mode,
        "success_count": success_count,
        "failure_count": failure_count + len(invalid),
        "invalid_tokens": invalid,
        "stale_tokens": stale_tokens,
        "responses": responses,
        "batches_sent": batches_sent,
    }


# ── Topic management ──────────────────────────────────────────────────────


async def subscribe_to_topic(
    tokens: list[str] | str, topic: str
) -> dict[str, Any]:
    """Subscribe one or many tokens to a topic.

    Topic names must match ``[a-zA-Z0-9-_.~%]+``. We do a cheap sanity
    check up-front so callers don't burn an API call on obviously bad
    topics (e.g. containing ``:`` or whitespace).
    """
    if not topic or not isinstance(topic, str):
        return {"success": False, "error": "missing_topic"}
    if any(ch in topic for ch in (" ", "\t", "\n", ":")):
        return {"success": False, "error": "invalid_topic_chars"}

    if isinstance(tokens, str):
        tokens = [tokens]
    valid = [t for t in tokens if validate_token(t)]
    if not valid:
        return {
            "success": False,
            "error": "no_valid_tokens",
            "success_count": 0,
            "failure_count": len(tokens),
        }

    mode = get_mode()
    if mode == "mock":
        _MOCK_STATS["subscribed"] += len(valid)
        return {
            "success": True,
            "mode": "mock",
            "topic": topic,
            "success_count": len(valid),
            "failure_count": 0,
        }

    try:  # pragma: no cover — live path
        resp = _fb_messaging.subscribe_to_topic(valid, topic)
        return {
            "success": True,
            "mode": "live",
            "topic": topic,
            "success_count": resp.success_count,
            "failure_count": resp.failure_count,
        }
    except Exception as exc:  # pragma: no cover
        await record_failure(type(exc).__name__)
        return {
            "success": False,
            "mode": "live",
            "topic": topic,
            "error": str(exc),
        }


async def unsubscribe_from_topic(
    tokens: list[str] | str, topic: str
) -> dict[str, Any]:
    """Unsubscribe one or many tokens from a topic."""
    if not topic:
        return {"success": False, "error": "missing_topic"}
    if isinstance(tokens, str):
        tokens = [tokens]
    valid = [t for t in tokens if validate_token(t)]
    if not valid:
        return {
            "success": False,
            "error": "no_valid_tokens",
            "success_count": 0,
            "failure_count": len(tokens),
        }

    mode = get_mode()
    if mode == "mock":
        return {
            "success": True,
            "mode": "mock",
            "topic": topic,
            "success_count": len(valid),
            "failure_count": 0,
        }

    try:  # pragma: no cover — live path
        resp = _fb_messaging.unsubscribe_from_topic(valid, topic)
        return {
            "success": True,
            "mode": "live",
            "topic": topic,
            "success_count": resp.success_count,
            "failure_count": resp.failure_count,
        }
    except Exception as exc:  # pragma: no cover
        await record_failure(type(exc).__name__)
        return {
            "success": False,
            "mode": "live",
            "topic": topic,
            "error": str(exc),
        }


async def send_to_topic(
    topic: str, title: str, body: str, data: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fan-out a single push to all subscribers of a topic.

    Topic-based fanout is the only efficient way to do "brand
    broadcast" — Firebase keeps the subscriber list server-side so we
    don't have to maintain it (or pay quota for N individual sends).
    """
    if not topic:
        return {"success": False, "error": "missing_topic"}

    allowed, count = await _check_and_increment_quota(1)
    if not allowed:
        await record_failure("rate_limited")
        return {
            "success": False,
            "error": "rate_limited",
            "current_hour_count": count,
        }

    mode = get_mode()
    if mode == "mock":
        result = _mock_send_result(f"topic:{topic}", title, body)
        result["topic"] = topic
        if data:
            result["data"] = {str(k): str(v) for k, v in data.items()}
        await record_last_sent()
        return result

    try:  # pragma: no cover — live path
        msg = _fb_messaging.Message(
            topic=topic,
            notification=_fb_messaging.Notification(title=title, body=body),
            data={str(k): str(v) for k, v in (data or {}).items()} or None,
        )
        message_id = _fb_messaging.send(msg)
        await record_last_sent()
        return {
            "success": True,
            "mode": "live",
            "topic": topic,
            "message_id": message_id,
        }
    except Exception as exc:  # pragma: no cover
        await record_failure(type(exc).__name__)
        return {
            "success": False,
            "mode": "live",
            "topic": topic,
            "error": str(exc),
        }


__all__ = [
    "send_to_token",
    "send_multicast",
    "send_to_topic",
    "subscribe_to_topic",
    "unsubscribe_from_topic",
    "validate_token",
    "get_mode",
    "is_configured",
    "record_last_sent",
    "record_failure",
    "failures_last_24h",
    "last_sent_ts",
    "MAX_PUSHES_PER_HOUR",
    "MULTICAST_BATCH_SIZE",
]
