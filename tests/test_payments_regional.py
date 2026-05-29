"""Tests for the per-country payment-method capability registry.

These tests cover BOTH the in-process registry (``app.payments_regional``)
and the HTTP router (``app.routers.payments_regional``). They are
self-contained and do not require Redis state.
"""

from __future__ import annotations

import pytest

from app.payments_regional import (
    PaymentMethod,
    all_methods,
    calculate_fee,
    get_method,
    get_methods_for_country,
    get_methods_for_currency,
    get_methods_supporting_recurring,
    matrix,
    recommend_method,
)
from app.payments_regional.settlement import get_settlement_currency


# Every country listed in the strategy doc must have >= 3 methods.
# This is the contract the merchant-onboarding wizard will rely on.
COUNTRIES_WITH_MIN_METHODS = [
    "SG", "ID", "TH", "VN", "PH", "MY",   # SEA Phase 2
    "CN", "JP", "KR", "IN",                # APAC tier 1
    "BR",                                  # LATAM tier 1
    "US", "AU",                            # Anglosphere tier 1
    "DE",                                  # EU tier 1
]


# ─────────────────────────────────────────────────────────────────
# Registry coverage
# ─────────────────────────────────────────────────────────────────
def test_all_14_countries_have_at_least_three_methods():
    """Every Phase-2 target country must surface ≥3 methods."""
    for c in COUNTRIES_WITH_MIN_METHODS:
        methods = get_methods_for_country(c)
        assert len(methods) >= 3, (
            f"{c} only has {len(methods)} methods: "
            f"{[m.code for m in methods]}"
        )


def test_method_codes_are_unique():
    """No duplicate codes — registry sanity (also enforced at import)."""
    codes = [m.code for m in all_methods()]
    assert len(codes) == len(set(codes)), (
        f"duplicate codes: "
        f"{sorted({c for c in codes if codes.count(c) > 1})}"
    )


def test_consumer_protection_populated_for_all():
    """No method may ship without a consumer-protection rating."""
    for m in all_methods():
        assert m.consumer_protection in {"strong", "medium", "weak"}, m.code


def test_integration_status_reflects_reality():
    """Stripe-supported methods should be ``live``; the rest scaffold/planned."""
    # Methods Stripe documents as live in SEA + tier-1 markets.
    must_be_live = {
        "credit_card", "apple_pay", "google_pay",  # universal
        "paynow", "grabpay",                       # SG
        "gopay", "ovo", "dana", "qris", "bca_va",  # ID
        "promptpay",                               # TH
        "momo",                                    # VN
        "gcash", "paymaya",                        # PH
        "tng_ewallet", "fpx",                      # MY
        "alipay", "wechat_pay",                    # CN
        "jcb",                                     # JP
        "upi",                                     # IN
        "pix",                                     # BR
        "sepa_direct_debit", "ideal",              # EU
        "ach",                                     # US
        "afterpay",                                # AU/US/UK
    }
    live = {m.code for m in all_methods() if m.integration_status == "live"}
    missing = must_be_live - live
    assert not missing, f"expected live but not marked: {sorted(missing)}"


# ─────────────────────────────────────────────────────────────────
# Country-specific contracts
# ─────────────────────────────────────────────────────────────────
def test_sg_paynow_is_first_preference():
    """In SG, recommend() must pick PayNow over GrabPay/cards by fee."""
    chosen = recommend_method("SG", amount_cents=10000, currency="SGD")
    assert chosen is not None
    # PayNow has 0 bps + 50¢ fixed; cards are 290 bps + 30¢ — at S$100
    # PayNow's 50¢ << card's S$2.90+30¢. PayNow must win.
    assert chosen.code == "paynow", chosen.code


def test_id_has_full_wallet_trifecta():
    """ID requires OVO, GoPay, DANA — the three wallets the strategy doc names."""
    codes = {m.code for m in get_methods_for_country("ID")}
    for required in ("ovo", "gopay", "dana"):
        assert required in codes, f"ID missing {required}; have {sorted(codes)}"


def test_recommend_prefers_live_over_scaffold_over_planned():
    """Status rank must beat fee — a live high-fee method beats a planned 0% method."""
    # SG has PayNow (live), NETS (scaffold), FavePay (planned).
    # Even if NETS/FavePay were cheaper, PayNow's live status must dominate.
    chosen = recommend_method("SG", amount_cents=10000, currency="SGD")
    assert chosen.integration_status == "live"


def test_recommend_respects_user_preference_when_valid():
    """If user picks a supported live/scaffold method we honour it."""
    chosen = recommend_method(
        "SG", amount_cents=10000, currency="SGD", user_pref="grabpay"
    )
    assert chosen is not None
    assert chosen.code == "grabpay"


def test_recommend_ignores_user_pref_when_unsupported_in_country():
    """User pref for a method not available in the country is silently dropped."""
    # PIX is BR-only; in SG it should fall back to PayNow.
    chosen = recommend_method(
        "SG", amount_cents=10000, currency="SGD", user_pref="pix"
    )
    assert chosen is not None
    assert chosen.code != "pix"
    assert "SG" in chosen.countries


def test_recommend_returns_none_when_no_match():
    """Unknown country/currency → no recommendation."""
    chosen = recommend_method("ZZ", amount_cents=100, currency="XXX")
    assert chosen is None


# ─────────────────────────────────────────────────────────────────
# Filter helpers
# ─────────────────────────────────────────────────────────────────
def test_currency_filter_returns_only_matching():
    """get_methods_for_currency must filter strictly on the currency field."""
    sgd_methods = get_methods_for_currency("SGD")
    assert sgd_methods, "no SGD methods found"
    for m in sgd_methods:
        assert "SGD" in m.currencies


def test_recurring_filter_for_subscriptions():
    """Subscription flow needs methods with supports_recurring==True."""
    recurring_sg = get_methods_supporting_recurring("SG")
    assert recurring_sg, "SG must have at least one recurring-capable method"
    for m in recurring_sg:
        assert m.supports_recurring is True
        assert "SG" in m.countries
    # PayNow is one-shot bank transfer — must NOT appear here.
    assert "paynow" not in {m.code for m in recurring_sg}


def test_3ds_flag_for_european_cards():
    """Cards in EU must default to requires_3ds=True (PSD2 SCA)."""
    cc = get_method("credit_card")
    assert cc is not None
    assert cc.requires_3ds is True, "PSD2 SCA mandates 3DS on EU cards"


# ─────────────────────────────────────────────────────────────────
# Fee calculator
# ─────────────────────────────────────────────────────────────────
def test_paynow_fee_zero_bps_plus_fifty_cents():
    """PayNow: 0% + S$0.50 fixed. At S$100 fee should be exactly 50¢."""
    fee, net = calculate_fee("paynow", 10000)
    assert fee == 50, f"expected 50¢ fee, got {fee}"
    assert net == 9950


def test_grabpay_fee_two_point_five_percent():
    """GrabPay: 250 bps + 0 fixed. At S$100 (10000¢) → S$2.50 → 250¢."""
    fee, net = calculate_fee("grabpay", 10000)
    assert fee == 250
    assert net == 9750


def test_calculate_fee_rejects_unknown_method():
    """Unknown code → KeyError, not silent zero."""
    with pytest.raises(KeyError):
        calculate_fee("not_a_real_method", 1000)


def test_calculate_fee_rejects_negative_amount():
    with pytest.raises(ValueError):
        calculate_fee("paynow", -1)


# ─────────────────────────────────────────────────────────────────
# Settlement currency router
# ─────────────────────────────────────────────────────────────────
def test_settlement_paynow_always_sgd():
    """PayNow is SG-only — settles to SGD regardless of caller."""
    assert get_settlement_currency("SG", "paynow") == "SGD"
    # Even if a caller (wrongly) asks with merchant_country=MY,
    # PayNow is single-country and falls through to its native SGD.
    assert get_settlement_currency("MY", "paynow") == "SGD"


def test_settlement_grabpay_follows_merchant():
    """GrabPay is multi-country — settlement follows the merchant."""
    assert get_settlement_currency("SG", "grabpay") == "SGD"
    assert get_settlement_currency("ID", "grabpay") == "IDR"
    assert get_settlement_currency("PH", "grabpay") == "PHP"


def test_settlement_upi_always_inr():
    assert get_settlement_currency("IN", "upi") == "INR"


def test_settlement_wechat_pay_settles_to_cny():
    """WeChat Pay is China-domestic — always CNY (or HKD via HK rail)."""
    assert get_settlement_currency("CN", "wechat_pay") == "CNY"


def test_settlement_unknown_method_raises():
    with pytest.raises(KeyError):
        get_settlement_currency("SG", "not_a_method")


# ─────────────────────────────────────────────────────────────────
# Matrix
# ─────────────────────────────────────────────────────────────────
def test_matrix_has_full_country_grid():
    """Matrix must include every Phase-2 country with non-empty methods."""
    grid = matrix()
    for c in COUNTRIES_WITH_MIN_METHODS:
        assert c in grid, f"matrix missing {c}"
        assert len(grid[c]) >= 3, f"matrix[{c}] too sparse: {grid[c]}"


# ─────────────────────────────────────────────────────────────────
# HTTP endpoints
# ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_endpoint_list_methods_by_country(client):
    res = await client.get("/api/v1/payments/methods", params={"country": "sg"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] >= 3
    codes = {m["code"] for m in body["methods"]}
    assert "paynow" in codes
    assert "grabpay" in codes


@pytest.mark.asyncio
async def test_endpoint_list_methods_by_currency(client):
    res = await client.get("/api/v1/payments/methods", params={"currency": "SGD"})
    assert res.status_code == 200
    body = res.json()
    for m in body["methods"]:
        assert "SGD" in m["currencies"]


@pytest.mark.asyncio
async def test_endpoint_method_detail_404_on_unknown(client):
    res = await client.get("/api/v1/payments/method/no_such_code")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_endpoint_recommend_returns_full_payload(client):
    res = await client.get(
        "/api/v1/payments/recommend",
        params={
            "country": "id",
            "amount_cents": 10000,
            "currency": "IDR",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "method" in body
    assert "fee_cents" in body
    assert "net_cents" in body
    assert "settlement_currency" in body
    assert body["settlement_currency"] == "IDR"
    # Net + fee must reconcile.
    assert body["fee_cents"] + body["net_cents"] == 10000


@pytest.mark.asyncio
async def test_endpoint_matrix_returns_full_grid(client):
    res = await client.get("/api/v1/payments/matrix")
    assert res.status_code == 200
    body = res.json()
    assert "matrix" in body
    grid = body["matrix"]
    for c in ("SG", "ID", "TH", "VN", "PH", "MY", "BR", "US", "DE"):
        assert c in grid, f"matrix missing {c}"


# ─────────────────────────────────────────────────────────────────
# Dataclass immutability
# ─────────────────────────────────────────────────────────────────
def test_payment_method_is_frozen():
    """Defence-in-depth: registry callers can't mutate global state."""
    m = get_method("paynow")
    assert m is not None
    with pytest.raises((AttributeError, Exception)):
        m.fee_bps = 9999  # type: ignore[misc]
