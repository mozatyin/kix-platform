"""Shared helpers across POS adapters.

Internal-only — concrete adapters compose these. Nothing here makes
network calls; each POS adapter owns its own (mock-safe) integration.

The :class:`MockVoucherStore` is a deliberately tiny in-process voucher
catalogue used in mock mode so the adapters can verify / redeem without
talking to Redis. The real production redemption path lives in
``app.routers.vouchers`` and ``app.routers.pos_integration`` (which can
optionally fall back to this store when running mock mode in CI).
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


# ── Mode detection — shared logic across POS adapters ───────────────────
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
def mock_ref(prefix: str, seed: str | None = None) -> str:
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
def parse_payload(payload: bytes | str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
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
    """Append a structured audit entry (bounded ring buffer)."""
    entry = {"ts": time.time(), **event}
    _AUDIT_LOG.append(entry)
    if len(_AUDIT_LOG) > 10_000:
        del _AUDIT_LOG[: len(_AUDIT_LOG) - 10_000]
    logger.info("[pos_audit] %s", json.dumps(entry, ensure_ascii=False, default=str))


def read_audit_log() -> list[dict[str, Any]]:
    return list(_AUDIT_LOG)


def reset_audit_log() -> None:
    _AUDIT_LOG.clear()


# ── Mock voucher store ──────────────────────────────────────────────────
# Used by adapters in mock mode and by the in-memory test fixtures so
# tests don't need a Redis instance. Production code path goes through
# ``app.routers.vouchers`` (Redis-backed) instead.
class MockVoucherStore:
    """In-process voucher catalogue used in mock mode + tests."""

    def __init__(self) -> None:
        self._vouchers: dict[str, dict[str, Any]] = {}
        self._redeemed: dict[str, dict[str, Any]] = {}

    def upsert(
        self,
        voucher_id: str,
        *,
        value_cents: int,
        currency: str = "USD",
        brand_id: str | None = None,
        expires_at: int | None = None,
        master_pool_brands: list[str] | None = None,
    ) -> None:
        self._vouchers[voucher_id] = {
            "voucher_id": voucher_id,
            "value_cents": int(value_cents),
            "currency": normalize_currency(currency),
            "brand_id": brand_id,
            "expires_at": expires_at,
            "master_pool_brands": list(master_pool_brands or []),
        }

    def get(self, voucher_id: str) -> dict[str, Any] | None:
        return self._vouchers.get(voucher_id)

    def is_redeemed(self, voucher_id: str) -> bool:
        return voucher_id in self._redeemed

    def mark_redeemed(
        self, voucher_id: str, transaction_data: dict[str, Any]
    ) -> dict[str, Any]:
        ts = int(time.time())
        rec = {
            "voucher_id": voucher_id,
            "redeemed_at": ts,
            "transaction_data": dict(transaction_data or {}),
        }
        self._redeemed[voucher_id] = rec
        return rec

    def redemption_for(self, voucher_id: str) -> dict[str, Any] | None:
        return self._redeemed.get(voucher_id)

    def reset(self) -> None:
        self._vouchers.clear()
        self._redeemed.clear()

    def all_redemptions(self, brand_id: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for vid, red in self._redeemed.items():
            v = self._vouchers.get(vid, {})
            if brand_id and v.get("brand_id") != brand_id:
                # honour cross-brand pool: include if brand is in pool
                if brand_id not in (v.get("master_pool_brands") or []):
                    continue
            out.append({**v, **red})
        return out


_STORE = MockVoucherStore()


def get_mock_store() -> MockVoucherStore:
    """Process-global mock store used by all adapters in mock mode."""
    return _STORE
