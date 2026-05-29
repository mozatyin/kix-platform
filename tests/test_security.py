"""Tests for app.security — timing-safe token comparisons.

The point of these tests is *not* to measure timing (CI noise makes that
unreliable) — it's to lock down the API surface so that we can detect any
future regression away from ``hmac.compare_digest``.
"""
from __future__ import annotations

import hmac
import inspect
import os
from unittest import mock

import pytest

from app import security
from app.security import check_admin_token, constant_time_eq


# ── constant_time_eq ─────────────────────────────────────────────────────


def test_constant_time_eq_different_lengths() -> None:
    """Mismatched-length inputs must return False without raising."""
    assert not constant_time_eq("aaa", "aaaa")
    assert not constant_time_eq("aaaa", "aaa")


def test_constant_time_eq_empty_string() -> None:
    """Empty strings on either side reject — never accidentally match."""
    assert not constant_time_eq("", "aaa")
    assert not constant_time_eq("aaa", "")
    assert not constant_time_eq("", "")


def test_constant_time_eq_none_inputs() -> None:
    """``None`` on either side rejects (no AttributeError)."""
    assert not constant_time_eq(None, "aaa")
    assert not constant_time_eq("aaa", None)
    assert not constant_time_eq(None, None)


def test_constant_time_eq_equal_strings() -> None:
    """Identical secrets compare equal."""
    assert constant_time_eq("secret", "secret")
    assert constant_time_eq("a" * 64, "a" * 64)


def test_constant_time_eq_different_same_length() -> None:
    """Same-length non-equal strings reject."""
    assert not constant_time_eq("secret", "secreX")
    assert not constant_time_eq("aaaa", "bbbb")


def test_compare_digest_used() -> None:
    """Admin token comparison must be constant-time.

    Sentinel test from the security audit spec — kept verbatim.
    """
    # Different lengths
    assert not constant_time_eq("aaa", "aaaa")
    # Empty
    assert not constant_time_eq("", "aaa")
    assert not constant_time_eq(None, "aaa")
    # Equal
    assert constant_time_eq("secret", "secret")


def test_constant_time_eq_calls_hmac_compare_digest() -> None:
    """Lock down the implementation: must delegate to ``hmac.compare_digest``.

    A naive ``==`` regression would silently pass the behavioural tests
    above but lose the timing-safety property. This guards against that
    by spying on the underlying primitive.
    """
    with mock.patch.object(
        security.hmac, "compare_digest", wraps=hmac.compare_digest
    ) as spy:
        assert constant_time_eq("secret", "secret")
        assert spy.call_count == 1


def test_constant_time_eq_source_uses_compare_digest() -> None:
    """Static check: the implementation references ``compare_digest``."""
    src = inspect.getsource(constant_time_eq)
    assert "compare_digest" in src, (
        "constant_time_eq must use hmac.compare_digest — found:\n" + src
    )


# ── check_admin_token ────────────────────────────────────────────────────


def test_check_admin_token_no_env_rejects() -> None:
    """Fail-closed when ``KIX_ADMIN_TOKEN`` is not configured."""
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KIX_ADMIN_TOKEN", None)
        assert not check_admin_token("anything")
        assert not check_admin_token(None)
        assert not check_admin_token("")


def test_check_admin_token_match() -> None:
    with mock.patch.dict(os.environ, {"KIX_ADMIN_TOKEN": "s3cret"}):
        assert check_admin_token("s3cret")


def test_check_admin_token_mismatch() -> None:
    with mock.patch.dict(os.environ, {"KIX_ADMIN_TOKEN": "s3cret"}):
        assert not check_admin_token("wrong")
        assert not check_admin_token("")
        assert not check_admin_token(None)


# ── Regression guard: no naive `!=` token comparisons in routers ─────────


@pytest.mark.parametrize(
    "router",
    [
        "campaigns",
        "kix_id",
        "consent",
        "vouchers",
        "reservations",
        "moderation",
        "payouts",
        "fx",
        "brand_subscriptions",
    ],
)
def test_routers_use_constant_time_eq(router: str) -> None:
    """Ensure each touched router imports the shared helper.

    Catches drift where a future patch reintroduces ``token != expected``.
    """
    path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "app",
        "routers",
        f"{router}.py",
    )
    with open(path, encoding="utf-8") as f:
        text = f.read()
    assert "constant_time_eq" in text or "compare_digest" in text, (
        f"{router}.py lost its timing-safe comparison helper"
    )
