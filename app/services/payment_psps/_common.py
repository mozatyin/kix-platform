"""Shared helpers across PSP wrappers.

Internal-only — concrete wrappers compose these. Nothing here makes
network calls; each PSP wrapper owns its own (mock-safe) integration.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


# ── Per-PSP last-charge tracking (in-process, reset on reboot) ──────────
_LAST_CHARGE_TS: dict[str, float] = {}


def record_charge(psp: str) -> None:
    _LAST_CHARGE_TS[psp] = time.time()


def get_last_charge_ts(psp: str) -> float | None:
    return _LAST_CHARGE_TS.get(psp)


# ── Mode detection — shared logic across PSPs ───────────────────────────
def detect_mode(
    *,
    live_key: str,
    test_key: str,
    live_prefix: str = "",
) -> str:
    """Pick mode based on env vars.

    If neither ``live_key`` nor ``test_key`` is set → ``mock``.
    If ``live_key`` set → ``live`` (optionally requires ``live_prefix``).
    If ``test_key`` set → ``test``.
    """
    live = os.getenv(live_key, "").strip()
    test = os.getenv(test_key, "").strip()
    if live:
        if live_prefix and not live.startswith(live_prefix):
            logger.warning(
                "%s does not start with expected prefix %r; falling back to test",
                live_key,
                live_prefix,
            )
            return "test" if test else "mock"
        return "live"
    if test:
        return "test"
    return "mock"


# ── Deterministic mock IDs ──────────────────────────────────────────────
def mock_charge_id(prefix: str, seed: str | None = None) -> str:
    ref = seed or uuid4().hex[:16]
    return f"{prefix}_mock_{ref}"


# ── HMAC signature helpers ──────────────────────────────────────────────
def hmac_sign(payload: bytes | str, secret: str, algo: str = "sha256") -> str:
    """Hex-digest HMAC of ``payload`` with ``secret``."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if not isinstance(secret, (bytes, bytearray)):
        secret = secret.encode("utf-8")  # type: ignore[assignment]
    func = getattr(hashlib, algo)
    return hmac.new(secret, payload, func).hexdigest()


def hmac_verify(
    payload: bytes | str,
    signature: str,
    secret: str,
    algo: str = "sha256",
) -> bool:
    """Constant-time HMAC verification."""
    expected = hmac_sign(payload, secret, algo)
    return hmac.compare_digest(expected, signature or "")


# ── JSON helpers ────────────────────────────────────────────────────────
def parse_payload(payload: bytes | str) -> dict[str, Any]:
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    try:
        return json.loads(payload)
    except (TypeError, ValueError):
        return {}


# ── Currency normalisation ──────────────────────────────────────────────
def normalize_currency(currency: str | None, default: str = "USD") -> str:
    return (currency or default).strip().upper()


# ── Audit log emission ──────────────────────────────────────────────────
_AUDIT_LOG: list[dict[str, Any]] = []


def emit_audit(event: dict[str, Any]) -> None:
    """Append a structured audit entry.

    Bounded ring buffer (last 10k) so tests + dashboards can read it
    without unbounded memory growth.
    """
    entry = {"ts": time.time(), **event}
    _AUDIT_LOG.append(entry)
    if len(_AUDIT_LOG) > 10_000:
        del _AUDIT_LOG[: len(_AUDIT_LOG) - 10_000]
    logger.info("[psp_audit] %s", json.dumps(entry, ensure_ascii=False, default=str))


def read_audit_log() -> list[dict[str, Any]]:
    return list(_AUDIT_LOG)


def reset_audit_log() -> None:
    _AUDIT_LOG.clear()
