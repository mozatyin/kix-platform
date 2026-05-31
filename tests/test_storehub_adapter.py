"""Tests for app/services/storehub_adapter.py — Wave L StoreHub integration."""

import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest

from app.services.storehub_adapter import (
    StoreHubOrder,
    TrackedTransactionDraft,
    basic_fraud_check,
    map_to_tracked_transaction,
    parse_storehub_order,
    verify_storehub_signature,
    _normalize_e164,
    _to_cents,
    _sha256_hex,
)


# ── signature verify ──

def test_signature_verify_happy_path():
    secret = "test-secret-abc"
    body = b'{"order_id": "ORD-001", "total": 12.34}'
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_storehub_signature(body, f"sha256={sig}", secret)


def test_signature_verify_wrong_secret():
    body = b'{"order_id": "ORD-001"}'
    sig = hmac.new(b"wrong-secret", body, hashlib.sha256).hexdigest()
    assert not verify_storehub_signature(body, f"sha256={sig}", "actual-secret")


def test_signature_verify_no_header():
    assert not verify_storehub_signature(b"body", "", "secret")


def test_signature_verify_wrong_format():
    assert not verify_storehub_signature(b"body", "md5=abc123", "secret")


# ── E.164 normalization ──

def test_e164_full_intl_passes():
    assert _normalize_e164("+6591234567") == "+6591234567"
    assert _normalize_e164("+60123456789") == "+60123456789"


def test_e164_sg_local_8digit_assumed_65():
    assert _normalize_e164("91234567") == "+6591234567"


def test_e164_my_local_assumed_60():
    assert _normalize_e164("0123456789") == "+60123456789"


def test_e164_invalid_returns_none():
    assert _normalize_e164("abc") is None
    assert _normalize_e164("1234") is None


def test_e164_handles_spaces_and_dashes():
    assert _normalize_e164("+65 9123-4567") == "+6591234567"


# ── money normalization ──

def test_to_cents_decimal_currency():
    assert _to_cents(12.34, "SGD") == 1234
    assert _to_cents(12.34, "MYR") == 1234
    assert _to_cents("10.00", "SGD") == 1000
    assert _to_cents(0, "SGD") == 0


def test_to_cents_zero_decimal_currency():
    assert _to_cents(12000, "IDR") == 12000
    assert _to_cents(50000, "VND") == 50000


def test_to_cents_invalid_returns_zero():
    assert _to_cents("not-a-number", "SGD") == 0
    assert _to_cents(None, "SGD") == 0


# ── order parsing ──

def test_parse_minimal_payload():
    payload = {
        "order_id": "ORD-123",
        "completed_at": "2026-05-31T10:00:00Z",
        "total": 15.50,
        "currency": "SGD",
        "customer": {"phone": "+6591234567", "email": "alice@example.com"},
        "outlet_id": "OUT-A",
        "line_items": [{"sku": "kopi", "qty": 1, "price": 15.50}],
    }
    o = parse_storehub_order(payload)
    assert o.order_id == "ORD-123"
    assert o.total_cents == 1550
    assert o.currency == "SGD"
    assert o.customer_phone_e164 == "+6591234567"
    assert o.customer_email == "alice@example.com"
    assert o.outlet_id == "OUT-A"
    assert len(o.line_items) == 1


def test_parse_missing_order_id_raises():
    with pytest.raises(ValueError, match="order_id"):
        parse_storehub_order({"completed_at": "2026-05-31T10:00:00Z", "total": 0})


def test_parse_missing_completed_at_raises():
    with pytest.raises(ValueError, match="completed_at"):
        parse_storehub_order({"order_id": "X", "total": 0})


def test_parse_id_field_alias():
    payload = {
        "id": "ORD-X",  # not order_id
        "completed_at": "2026-05-31T10:00:00Z",
        "total": 5.00,
    }
    o = parse_storehub_order(payload)
    assert o.order_id == "ORD-X"


def test_parse_handles_email_normalization():
    payload = {
        "order_id": "X",
        "completed_at": "2026-05-31T10:00:00Z",
        "total": 1,
        "customer": {"email": "  ALICE@Example.COM  "},
    }
    o = parse_storehub_order(payload)
    assert o.customer_email == "alice@example.com"


def test_parse_handles_naive_timestamp_assumes_utc():
    payload = {
        "order_id": "X",
        "completed_at": "2026-05-31T10:00:00",  # no Z
        "total": 1,
    }
    o = parse_storehub_order(payload)
    assert o.completed_at.tzinfo is not None


# ── mapper ──

def test_map_basic_no_fraud():
    order = StoreHubOrder(
        order_id="ORD-1",
        completed_at=datetime(2026, 5, 31, tzinfo=timezone.utc),
        total_cents=1200, currency="SGD",
        customer_phone_e164="+6591234567",
        customer_email="alice@example.com",
        outlet_id="OUT-A", line_items=[], raw={"x": 1},
    )
    draft = map_to_tracked_transaction(order, brand_id="brand_xyz",
                                        matched_voucher_code=True)
    assert draft.storehub_order_id == "ORD-1"
    assert draft.brand_id == "brand_xyz"
    assert draft.amount_cents == 1200
    assert draft.hashed_consumer_phone == _sha256_hex("+6591234567")
    assert draft.hashed_consumer_email == _sha256_hex("alice@example.com")
    assert draft.redemption_code_match is True
    assert draft.fraud_flagged is False


def test_map_propagates_fraud():
    order = StoreHubOrder(
        order_id="ORD-2", completed_at=datetime(2026, 5, 31, tzinfo=timezone.utc),
        total_cents=0, currency="SGD",
        customer_phone_e164="+6591234567", customer_email=None,
        outlet_id=None, line_items=[], raw={},
    )
    draft = map_to_tracked_transaction(order, brand_id="brand_xyz",
                                        matched_voucher_code=False,
                                        fraud_check_result=(True, "zero_total"))
    assert draft.fraud_flagged is True
    assert draft.fraud_reason == "zero_total"


def test_map_handles_no_consumer_pii():
    order = StoreHubOrder(
        order_id="ORD-3", completed_at=datetime(2026, 5, 31, tzinfo=timezone.utc),
        total_cents=500, currency="SGD",
        customer_phone_e164=None, customer_email=None,
        outlet_id=None, line_items=[], raw={},
    )
    draft = map_to_tracked_transaction(order, brand_id="b",
                                        matched_voucher_code=False)
    assert draft.hashed_consumer_phone is None
    assert draft.hashed_consumer_email is None


# ── fraud check ──

def _make_order(phone="+6591234567", total_cents=1000):
    return StoreHubOrder(
        order_id="O", completed_at=datetime(2026,5,31,tzinfo=timezone.utc),
        total_cents=total_cents, currency="SGD",
        customer_phone_e164=phone, customer_email=None,
        outlet_id="A", line_items=[], raw={},
    )


def test_fraud_owner_phone_blocked():
    flagged, reason = basic_fraud_check(
        _make_order(phone="+6598765432"),
        velocity_window_24h_count=0,
        same_outlet_owner_phones={"+6598765432", "+6512345678"},
    )
    assert flagged
    assert "owner" in reason


def test_fraud_velocity_exceeded():
    flagged, reason = basic_fraud_check(
        _make_order(),
        velocity_window_24h_count=5,
        same_outlet_owner_phones=set(),
    )
    assert flagged
    assert "velocity" in reason


def test_fraud_zero_total():
    flagged, reason = basic_fraud_check(
        _make_order(total_cents=0),
        velocity_window_24h_count=0,
        same_outlet_owner_phones=set(),
    )
    assert flagged
    assert "zero" in reason


def test_fraud_legit_passes():
    flagged, reason = basic_fraud_check(
        _make_order(),
        velocity_window_24h_count=2,
        same_outlet_owner_phones=set(),
    )
    assert not flagged
    assert reason is None
