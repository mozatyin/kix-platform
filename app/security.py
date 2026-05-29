"""Security helpers — centralized timing-safe comparisons.

All admin/API token comparisons across the platform MUST go through these
helpers. The naive ``token == expected`` pattern is vulnerable to remote
timing attacks (an attacker can measure response-time deltas to recover the
token character-by-character) — ``hmac.compare_digest`` short-circuits in
constant time and avoids that leakage.

Usage::

    from app.security import constant_time_eq, check_admin_token

    if not constant_time_eq(provided, expected):
        raise HTTPException(403, ...)

    if not check_admin_token(provided):
        raise HTTPException(403, ...)
"""
from __future__ import annotations

import hmac
import os


def constant_time_eq(token: str | None, expected: str | None) -> bool:
    """Constant-time string equality for security-sensitive tokens.

    Always use this for admin/API token comparisons to prevent timing
    attacks. Returns ``False`` whenever either side is empty/None so that
    callers can write ``if not constant_time_eq(a, b): reject()`` without
    additional null guards.

    Both inputs are coerced to ``str`` before delegating to
    :func:`hmac.compare_digest`, which requires homogeneous types.
    """
    if not token or not expected:
        return False
    return hmac.compare_digest(str(token), str(expected))


def check_admin_token(provided: str | None) -> bool:
    """Check whether ``provided`` matches the ``KIX_ADMIN_TOKEN`` env var.

    Returns ``False`` if no admin token is configured (fail-closed) or if
    the comparison does not match. Constant-time under the hood.
    """
    expected = os.environ.get("KIX_ADMIN_TOKEN")
    if not expected:
        return False  # No admin token configured → reject all.
    return constant_time_eq(provided, expected)
