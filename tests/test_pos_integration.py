"""Wave E item 7 — POS integration tests.

Covers the 4 priority POS adapters (Toast / Square / Loyverse /
Foodzaps) in mock mode only — no real POS network calls.

Test matrix (15 tests):
  1. all four adapters initialise in mock mode
  2. toast verify happy path
  3. square verify happy path
  4. loyverse verify happy path
  5. foodzaps verify happy path
  6. expired voucher rejected (all adapters consistent)
  7. already-used voucher rejected
  8. unknown voucher rejected (NOT_FOUND, no raise)
  9. apply_discount happy path returns POS-side ref
 10. apply_discount rejects non-positive amount
 11. mark_redeemed marks store and returns redemption_id
 12. webhook_handler normalises events to canonical shape
 13. webhook signature verification rejects tampered payload
 14. cross-brand master pool: voucher redeemable at sibling brand
 15. receipt HTML renders + contains voucher tail + QR svg
"""

from __future__ import annotations

import json
import time

import pytest

from app.services.pos import (
    POSError,
    all_pos_codes,
    get_pos_adapter,
    reset_pos_registry,
)
from app.services.pos._common import (
    get_mock_store,
    hmac_sign,
    reset_audit_log,
)
from app.services.pos.receipt import generate_receipt_html


# ─────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_pos_state(monkeypatch):
    """Force mock mode for every adapter + clear shared state."""
    for env in [
        "TOAST_LIVE_CLIENT_SECRET", "TOAST_TEST_CLIENT_SECRET",
        "SQUARE_LIVE_ACCESS_TOKEN", "SQUARE_TEST_ACCESS_TOKEN",
        "LOYVERSE_LIVE_API_TOKEN", "LOYVERSE_TEST_API_TOKEN",
        "FOODZAPS_LIVE_API_KEY", "FOODZAPS_TEST_API_KEY",
    ]:
        monkeypatch.delenv(env, raising=False)
    reset_pos_registry()
    reset_audit_log()
    get_mock_store().reset()
    yield
    reset_pos_registry()
    reset_audit_log()
    get_mock_store().reset()


def _seed_voucher(
    voucher_id: str,
    *,
    value_cents: int = 500,
    currency: str = "USD",
    brand_id: str | None = "brand_a",
    expires_at: int | None = None,
    pool: list[str] | None = None,
):
    get_mock_store().upsert(
        voucher_id,
        value_cents=value_cents,
        currency=currency,
        brand_id=brand_id,
        expires_at=expires_at,
        master_pool_brands=pool or [],
    )


# ─────────────────────────────────────────────────────────────────────────
# 1. All four adapters initialise in mock mode
# ─────────────────────────────────────────────────────────────────────────
def test_01_all_adapters_mock_mode():
    assert sorted(all_pos_codes()) == ["foodzaps", "loyverse", "square", "toast"]
    for code in all_pos_codes():
        adapter = get_pos_adapter(code)
        assert adapter.pos_code == code
        assert adapter.get_mode() == "mock"


# ─────────────────────────────────────────────────────────────────────────
# 2-5. Per-adapter verify happy path
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("pos_code", ["toast", "square", "loyverse", "foodzaps"])
def test_02_05_verify_happy_path(pos_code):
    vid = "vch_happy_001"
    _seed_voucher(vid, value_cents=750, currency="USD", brand_id="brand_x")
    result = get_pos_adapter(pos_code).verify_voucher(vid)
    assert result["valid"] is True
    assert result["voucher_id"] == vid
    assert result["value_cents"] == 750
    assert result["brand_id"] == "brand_x"
    assert result["reason"] is None
    assert result["mode"] == "mock"
    assert result["pos"] == pos_code


# ─────────────────────────────────────────────────────────────────────────
# 6. Expired voucher rejected by all adapters
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("pos_code", ["toast", "square", "loyverse", "foodzaps"])
def test_06_expired_voucher_rejected(pos_code):
    vid = "vch_expired"
    _seed_voucher(vid, value_cents=500, expires_at=int(time.time()) - 60)
    r = get_pos_adapter(pos_code).verify_voucher(vid)
    assert r["valid"] is False
    assert r["reason"] == POSError.EXPIRED


# ─────────────────────────────────────────────────────────────────────────
# 7. Already-used voucher rejected
# ─────────────────────────────────────────────────────────────────────────
def test_07_already_used_rejected():
    vid = "vch_used"
    _seed_voucher(vid, value_cents=200)
    adapter = get_pos_adapter("toast")
    # Verify works first time
    assert adapter.verify_voucher(vid)["valid"] is True
    # Mark redeemed
    adapter.mark_redeemed(vid, {"order_id": "ord_1"})
    # Second verify rejected — consistent across all 4 adapters
    for code in all_pos_codes():
        r = get_pos_adapter(code).verify_voucher(vid)
        assert r["valid"] is False
        assert r["reason"] == POSError.ALREADY_USED


# ─────────────────────────────────────────────────────────────────────────
# 8. Unknown voucher → NOT_FOUND (no raise)
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("pos_code", ["toast", "square", "loyverse", "foodzaps"])
def test_08_unknown_voucher_not_found(pos_code):
    r = get_pos_adapter(pos_code).verify_voucher("vch_does_not_exist")
    assert r["valid"] is False
    assert r["reason"] == POSError.NOT_FOUND


# ─────────────────────────────────────────────────────────────────────────
# 9. apply_discount happy path returns POS-side ref
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("pos_code", ["toast", "square", "loyverse", "foodzaps"])
def test_09_apply_discount_happy(pos_code):
    vid = "vch_disc_001"
    _seed_voucher(vid, value_cents=1000)
    d = get_pos_adapter(pos_code).apply_discount(vid, "order_xyz", 500)
    assert d["applied"] is True
    assert d["discount_cents"] == 500
    assert d["pos_discount_ref"]
    assert pos_code in d["pos_discount_ref"] or "mock" in d["pos_discount_ref"]


# ─────────────────────────────────────────────────────────────────────────
# 10. apply_discount rejects zero / negative amount
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("pos_code", ["toast", "square", "loyverse", "foodzaps"])
def test_10_apply_discount_invalid_amount(pos_code):
    d = get_pos_adapter(pos_code).apply_discount("vch_x", "order_x", 0)
    assert d["applied"] is False
    assert d.get("reason") == POSError.INVALID_AMOUNT


# ─────────────────────────────────────────────────────────────────────────
# 11. mark_redeemed records redemption + returns id
# ─────────────────────────────────────────────────────────────────────────
def test_11_mark_redeemed_records_state():
    vid = "vch_red_001"
    _seed_voucher(vid, value_cents=300)
    adapter = get_pos_adapter("square")
    out = adapter.mark_redeemed(vid, {"order_id": "ord_99", "cashier_id": "cash_1"})
    assert out["voucher_id"] == vid
    assert out["redemption_id"].startswith("sq_red_")
    assert out["redeemed_at"] > 0
    assert get_mock_store().is_redeemed(vid)


# ─────────────────────────────────────────────────────────────────────────
# 12. webhook_handler normalises events to canonical shape
# ─────────────────────────────────────────────────────────────────────────
def test_12_webhook_handler_canonical_shape():
    # Toast event
    toast_evt = {
        "eventType": "ORDER_CLOSED",
        "orderGuid": "order_123",
        "amount": 1599,
        "currency": "USD",
        "metadata": {"voucher_id": "vch_wh"},
    }
    out = get_pos_adapter("toast").webhook_handler(json.dumps(toast_evt))
    assert out["pos"] == "toast"
    assert out["event_type"] == "order.closed"
    assert out["order_id"] == "order_123"
    assert out["amount_cents"] == 1599
    assert out["currency"] == "USD"
    assert out["voucher_id"] == "vch_wh"

    # Square event with nested data shape
    sq_evt = {
        "type": "loyalty.reward.redeemed",
        "data": {
            "object": {
                "id": "order_sq",
                "voucher_id": "vch_sq",
                "amount_money": {"amount": 500, "currency": "USD"},
            }
        },
    }
    out2 = get_pos_adapter("square").webhook_handler(json.dumps(sq_evt))
    assert out2["event_type"] == "voucher.redeemed"
    assert out2["voucher_id"] == "vch_sq"
    assert out2["amount_cents"] == 500


# ─────────────────────────────────────────────────────────────────────────
# 13. Webhook signature verification rejects tampered payload
# ─────────────────────────────────────────────────────────────────────────
def test_13_webhook_signature_tampering(monkeypatch):
    monkeypatch.setenv("TOAST_WEBHOOK_SECRET", "shared_secret_test")
    reset_pos_registry()
    adapter = get_pos_adapter("toast")
    body = json.dumps({"eventType": "ORDER_CLOSED", "orderGuid": "o1"}).encode()
    good_sig = hmac_sign(body, "shared_secret_test")
    assert adapter.verify_webhook_signature(body, good_sig) is True

    # Tampered body — same signature, different payload
    tampered = json.dumps({"eventType": "ORDER_CLOSED", "orderGuid": "o2"}).encode()
    assert adapter.verify_webhook_signature(tampered, good_sig) is False

    # Tampered signature
    assert adapter.verify_webhook_signature(body, "deadbeef") is False


# ─────────────────────────────────────────────────────────────────────────
# 14. Cross-brand master pool: voucher issued by brand_a redeemable at
#     sibling brand_b if brand_b is in the master pool.
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_14_cross_brand_pool_integration(client):
    # Register two paired merchants under the same master pool
    await client.post("/api/v1/pos/register-merchant", json={
        "brand_id": "brand_a", "pos_code": "loyverse",
        "pos_merchant_id": "lv_a",
    })
    await client.post("/api/v1/pos/register-merchant", json={
        "brand_id": "brand_b", "pos_code": "loyverse",
        "pos_merchant_id": "lv_b",
    })
    # Issue voucher owned by brand_a, pool includes brand_b
    vid = "vch_pool_001"
    _seed_voucher(
        vid, value_cents=800, brand_id="brand_a",
        pool=["brand_a", "brand_b"], currency="SGD",
    )
    # Verify via brand_b's POS works
    r = await client.get(f"/api/v1/pos/voucher/{vid}/verify",
                         params={"pos_code": "loyverse", "brand_id": "brand_b"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["valid"] is True
    assert j["brand_id"] == "brand_a"
    # Redeem at brand_b's POS
    r2 = await client.post(
        f"/api/v1/pos/voucher/{vid}/redeem",
        json={
            "pos_code": "loyverse",
            "order_id": "ord_pool_1",
            "amount_cents": 800,
            "cashier_id": "cashier_b",
        },
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["ok"] is True
    assert body["voucher"]["brand_id"] == "brand_a"


# ─────────────────────────────────────────────────────────────────────────
# 15. Receipt generation
# ─────────────────────────────────────────────────────────────────────────
def test_15_receipt_html_generation():
    vid = "vch_receipt_001"
    html_doc = generate_receipt_html(
        voucher_id=vid,
        redemption_id="red_abcdef123",
        pos_code="toast",
        brand_id="brand_z",
        brand_name="Brand Z",
        order_id="ORD-42",
        amount_cents=1234,
        currency="USD",
        cashier_id="cashier-7",
        redeemed_at=int(time.time()),
    )
    # Self-contained HTML doc
    assert html_doc.startswith("<!DOCTYPE html>")
    assert "</html>" in html_doc
    # Voucher short id (uppercased, 12 chars) embedded
    assert vid[:12].upper() in html_doc
    # Brand name escaped + present
    assert "Brand Z" in html_doc
    # Order ref + amount displayed
    assert "ORD-42" in html_doc
    assert "12.34" in html_doc
    # QR placeholder SVG present
    assert "<svg" in html_doc and "</svg>" in html_doc
    # Thermal-print sizing in CSS
    assert "80mm" in html_doc
