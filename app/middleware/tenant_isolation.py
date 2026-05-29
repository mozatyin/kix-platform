"""Multi-tenant isolation middleware.

Prevents one brand's heavy traffic from degrading another brand:

* per-brand RPM rate limit (tier-aware: free/starter/growth/enterprise)
* per-brand resource consumption tracking (requests + total ms)
* per-brand-per-operation circuit breaker

The middleware looks up the current brand id from the request path
(``/brand/{brand_id}/...``) or the ``brand_id`` query parameter. Requests
that do not carry a brand id are passed through unmodified — the system
still has many tenant-agnostic endpoints (auth, health, landing assets).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.redis_client import get_redis_sync

logger = logging.getLogger(__name__)

# Per-brand default rate limit (per minute). ~16 req/sec.
DEFAULT_RPM_LIMIT = 1000

# Static tier → RPM ceiling. Mirrors the brand_subscriptions tier names.
_TIER_RPM_LIMITS: dict[str, int] = {
    "free": 100,
    "starter": 500,
    "growth": 2000,
    "enterprise": 10000,
}

# Circuit breaker policy.
CIRCUIT_FAILURE_THRESHOLD = 5         # consecutive failures within window
CIRCUIT_FAILURE_WINDOW_SEC = 60
CIRCUIT_OPEN_DURATION_SEC = 300       # 5 minutes


def _date_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Circuit breaker primitives (module-level so routers can reuse) ────────


def _circuit_state_key(brand_id: str, operation: str) -> str:
    return f"circuit:{brand_id}:{operation}"


def _circuit_fail_key(brand_id: str, operation: str) -> str:
    return f"circuit:fail:{brand_id}:{operation}"


async def is_circuit_open(brand_id: str, operation: str, r: Any) -> bool:
    """True iff the circuit is currently open for ``(brand_id, operation)``.

    Callers should short-circuit the underlying operation when this returns
    True and emit a graceful "skipped" outcome.
    """
    state = await r.get(_circuit_state_key(brand_id, operation))
    return state == "open"


async def record_failure(brand_id: str, operation: str, r: Any) -> int:
    """Record one failure; open the circuit if the threshold is reached.

    Returns the new failure count within the window.
    """
    key = _circuit_fail_key(brand_id, operation)
    count = await r.incr(key)
    if count == 1:
        await r.expire(key, CIRCUIT_FAILURE_WINDOW_SEC)
    if count >= CIRCUIT_FAILURE_THRESHOLD:
        await r.set(
            _circuit_state_key(brand_id, operation),
            "open",
            ex=CIRCUIT_OPEN_DURATION_SEC,
        )
    return int(count)


async def record_success(brand_id: str, operation: str, r: Any) -> None:
    """On success: clear the failure counter (does not force-close an
    already-open circuit — that requires admin reset or natural expiry)."""
    await r.delete(_circuit_fail_key(brand_id, operation))


async def reset_circuit(brand_id: str, operation: str, r: Any) -> None:
    """Force-close the circuit for ``(brand_id, operation)``."""
    await r.delete(_circuit_state_key(brand_id, operation))
    await r.delete(_circuit_fail_key(brand_id, operation))


# ── Tier resolution ───────────────────────────────────────────────────────


async def _get_brand_rpm_limit(brand_id: str, r: Any) -> int:
    """Resolve the active brand's RPM limit from its subscription tier.

    Falls back to ``DEFAULT_RPM_LIMIT`` if the tier router is unavailable
    or any error occurs — we never want the middleware to crash the request
    pipeline.
    """
    try:
        from app.routers.brand_subscriptions import _get_brand_tier  # type: ignore
        tier = await _get_brand_tier(r, brand_id)
    except Exception:  # pragma: no cover — defensive
        return DEFAULT_RPM_LIMIT
    return _TIER_RPM_LIMITS.get(tier, DEFAULT_RPM_LIMIT)


# ── Middleware ────────────────────────────────────────────────────────────


class TenantIsolationMiddleware:
    """ASGI middleware enforcing per-brand quotas + usage tracking."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        brand_id = self._extract_brand_id(request)

        if not brand_id:
            await self.app(scope, receive, send)
            return

        r = get_redis_sync()
        if r is None:
            # Redis not initialised (e.g. test bootstrap). Fail open.
            await self.app(scope, receive, send)
            return

        # ── Per-minute rate limit ─────────────────────────────────────
        minute_bucket = int(time.time() // 60)
        minute_key = f"tenant:rl:{brand_id}:{minute_bucket}"

        try:
            count = await r.incr(minute_key)
            # Set expiry only on first hit of the bucket. We accept the
            # tiny race where two concurrent INCRs both observe count>1
            # and skip EXPIRE — the bucket will simply linger until the
            # default key-eviction policy reclaims it.
            if count == 1:
                await r.expire(minute_key, 70)
            limit = await _get_brand_rpm_limit(brand_id, r)
        except Exception:  # pragma: no cover — defensive
            logger.warning("tenant_isolation: rate-limit check failed", exc_info=True)
            await self.app(scope, receive, send)
            return

        if count > limit:
            response = JSONResponse(
                status_code=429,
                content={
                    "detail": {
                        "error": "rate_limited",
                        "message": (
                            f"Brand '{brand_id}' exceeded RPM limit "
                            f"({limit} req/min)."
                        ),
                        "limit_rpm": limit,
                        "current": int(count),
                    }
                },
            )
            await response(scope, receive, send)
            return

        # ── Pass through + per-tenant usage tracking ──────────────────
        start = time.monotonic()
        try:
            await self.app(scope, receive, send)
        finally:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            usage_key = f"tenant:usage:{brand_id}:{_date_today()}"
            try:
                await r.hincrby(usage_key, "requests", 1)
                await r.hincrby(usage_key, "total_ms", elapsed_ms)
                # Auto-expire usage hashes after 35 days so they don't
                # accumulate unbounded for churned brands.
                await r.expire(usage_key, 35 * 24 * 3600)
            except Exception:  # pragma: no cover — never fail the response
                logger.warning(
                    "tenant_isolation: usage tracking failed", exc_info=True
                )

    # ── Brand id extraction ───────────────────────────────────────────

    @staticmethod
    def _extract_brand_id(request: Request) -> str | None:
        """Locate the brand id in path / query.

        Recognised patterns (in priority order):
          * ``.../brand/{brand_id}/...``
          * ``.../brands/{brand_id}/...``  (plural, used by ``brands`` router)
          * ``?brand_id=...`` query parameter
        """
        path = request.url.path or ""
        for marker in ("/brand/", "/brands/"):
            if marker in path:
                tail = path.split(marker, 1)[1]
                candidate = tail.split("/", 1)[0]
                if candidate:
                    return candidate
        qp = request.query_params.get("brand_id")
        return qp or None
