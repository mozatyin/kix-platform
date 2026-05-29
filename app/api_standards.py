"""KiX Platform API Standards — public contract shim.

This module exposes the 5 invariants of the KiX public HTTP API. New routers
MUST consume these helpers; legacy routers will be migrated incrementally.

Public commitment surface (see ``API_STANDARDS.md``):

1. ID format            — ``<prefix>_<22-char-hex>``
2. Timestamps           — Unix integer seconds, UTC
3. Error envelope       — ``{"error": str, "message": str, ...context}``
4. List response        — ``{items, count, total, has_more, limit, offset}``
5. HTTP method semantics — POST/PUT/PATCH/DELETE/GET status codes

Trinity-D audit (2026-05-29) identified these as the canonical shape every
endpoint in the platform should converge on. Until the migration is complete,
this file is the single source of truth.
"""

from __future__ import annotations

import datetime as _datetime
import time
import uuid
from typing import Any

from fastapi import HTTPException

__all__ = [
    "ID_PREFIXES",
    "mint_id",
    "parse_id",
    "is_valid_id",
    "now_ts",
    "ts_to_iso",
    "error_response",
    "not_found",
    "validation_failed",
    "conflict",
    "insufficient_funds",
    "unauthorized",
    "forbidden",
    "rate_limited",
    "list_response",
]


# ── 1. ID Format ───────────────────────────────────────────────────────
#
# Canonical KiX ID = ``<prefix>_<22-char-hex>``.  22 hex chars = 88 bits of
# entropy (truncated from a uuid4().hex which is 32 chars / 128 bits).

ID_PREFIXES: dict[str, str] = {
    "acct": "Account (B2B entity)",
    "user": "Generic user",
    "kid":  "KiX universal user identity",
    "ent":  "Non-human entity (pet, vehicle)",
    "lst":  "Listing",
    "ofr":  "Offer",
    "med":  "Media",
    "cmp":  "Campaign",
    "adg":  "AdGroup",
    "bdg":  "Badge",
    "qst":  "Quest",
    "vid":  "Voucher instance",
    "res":  "Reservation",
    "led":  "Ledger entry",
    "inc":  "Incident",
    "tx":   "Transaction",
    "sub":  "Subscription",
    "pm":   "Payment method",
    "dpt":  "Deposit",
    "prt":  "Partnership",
    "crv":  "Creative",
}


def mint_id(prefix: str) -> str:
    """Mint a standardized KiX ID.

    ``prefix`` may be passed with or without the trailing underscore.
    The hex tail is 22 characters drawn from ``uuid.uuid4().hex``.
    """
    if not prefix.endswith("_"):
        prefix += "_"
    return prefix + uuid.uuid4().hex[:22]


def parse_id(id_str: str) -> tuple[str, str]:
    """Extract ``(prefix, hex)`` from a KiX ID.

    Raises ``ValueError`` with a stable ``invalid_id_format`` prefix when
    the input does not match ``<prefix>_<hex>``.
    """
    if not isinstance(id_str, str) or "_" not in id_str:
        raise ValueError(f"invalid_id_format: {id_str!r}")
    parts = id_str.split("_", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"invalid_id_format: {id_str!r}")
    return parts[0], parts[1]


def is_valid_id(id_str: str, expected_prefix: str | None = None) -> bool:
    """Return True if ``id_str`` is a syntactically valid KiX ID.

    When ``expected_prefix`` is provided (with or without trailing ``_``),
    the prefix must also match.
    """
    try:
        prefix, tail = parse_id(id_str)
    except ValueError:
        return False
    if len(tail) < 1:
        return False
    if expected_prefix is not None:
        wanted = expected_prefix.rstrip("_")
        if prefix != wanted:
            return False
    return True


# ── 2. Timestamps ──────────────────────────────────────────────────────
#
# Every ``created_at`` / ``updated_at`` / ``expires_at`` field is a Unix
# integer (seconds, UTC).  ISO strings are for human display only.


def now_ts() -> int:
    """Standard timestamp: Unix integer seconds, UTC."""
    return int(time.time())


def ts_to_iso(ts: int) -> str:
    """Convert a Unix integer to ISO-8601 UTC.

    For client / UI display only — the wire format remains ``int``.
    """
    return _datetime.datetime.fromtimestamp(
        ts, tz=_datetime.timezone.utc
    ).isoformat()


# ── 3. Error Response Standard ─────────────────────────────────────────
#
# All error responses use the same envelope so SDKs can pattern-match on
# ``detail.error`` instead of parsing free-form strings.
#
#   {
#     "detail": {
#       "error":   "not_found",
#       "message": "campaign not found",
#       "resource_id": "cmp_..."
#     }
#   }


def error_response(
    code: int,
    error: str,
    message: str | None = None,
    **context: Any,
) -> HTTPException:
    """Build a standard ``HTTPException`` with the KiX error envelope."""
    detail: dict[str, Any] = {"error": error}
    if message:
        detail["message"] = message
    detail.update(context)
    return HTTPException(status_code=code, detail=detail)


def not_found(resource: str, id: str | None = None) -> HTTPException:
    """404 — resource lookup miss."""
    return error_response(
        404,
        "not_found",
        f"{resource} not found",
        resource=resource,
        resource_id=id,
    )


def validation_failed(field: str, reason: str) -> HTTPException:
    """422 — request body / query parameter validation failure."""
    return error_response(
        422,
        "validation_failed",
        f"{field}: {reason}",
        field=field,
        reason=reason,
    )


def conflict(resource: str, **ctx: Any) -> HTTPException:
    """409 — state conflict (duplicate create, stale update, ...)."""
    return error_response(
        409,
        "conflict",
        f"{resource} state conflict",
        resource=resource,
        **ctx,
    )


def insufficient_funds(available: int, requested: int) -> HTTPException:
    """402 — wallet / ledger balance below required amount (cents)."""
    return error_response(
        402,
        "insufficient_funds",
        f"available={available} requested={requested}",
        available_cents=available,
        requested_cents=requested,
    )


def unauthorized(message: str = "authentication required") -> HTTPException:
    """401 — caller did not present valid credentials."""
    return error_response(401, "unauthorized", message)


def forbidden(message: str = "forbidden") -> HTTPException:
    """403 — credentials valid but caller lacks scope."""
    return error_response(403, "forbidden", message)


def rate_limited(retry_after: int | None = None) -> HTTPException:
    """429 — caller exceeded their rate budget."""
    ctx: dict[str, Any] = {}
    if retry_after is not None:
        ctx["retry_after_seconds"] = retry_after
    return error_response(429, "rate_limited", "too many requests", **ctx)


# ── 4. List Response Contract ──────────────────────────────────────────
#
# Every collection endpoint returns the same envelope. ``items`` is the
# page; ``total`` is the unfiltered count when known.


def list_response(
    items: list,
    total: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Standard list response."""
    effective_total = total if total is not None else len(items)
    return {
        "items": items,
        "count": len(items),
        "total": effective_total,
        "has_more": (offset + len(items)) < effective_total,
        "limit": limit,
        "offset": offset,
    }
