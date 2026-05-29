"""Tests for the locale-aware formatting layer.

Covers the 12-case grid called out in the strategy doc § 4.1:

1. SGD format en-SG
2. CNY format zh-Hans-CN
3. JPY (no decimals) ja-JP
4. IDR (no decimals) id-ID
5. Negative numbers
6. Date format en-SG vs en-US
7. ``get_primary_currency`` per region
8. ``currency_decimals``
9. ``convert`` stub returns same (and logs WARN)
10. Multi-currency wallet GET balances
11. Debug endpoint
12. Locale fallback when invalid locale
"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from app.i18n.currency import (
    convert,
    currency_decimals,
    get_auto_refund_threshold,
    get_primary_currency,
    get_supported_currencies,
    get_subscription_price_cents,
    is_valid_currency,
)
from app.i18n.formatting import (
    format_currency,
    format_date,
    format_number,
    format_percent,
)


# ── 1. SGD format en-SG ──────────────────────────────────────────────────
def test_format_currency_sgd_en_sg():
    """``S$100.00`` — KiX forces ``S$`` even though en_SG CLDR uses ``$``."""
    assert format_currency(10000, "SGD", "en-SG") == "S$100.00"


# ── 2. CNY format zh-Hans-CN ─────────────────────────────────────────────
def test_format_currency_cny_zh_cn():
    assert format_currency(10000, "CNY", "zh-Hans-CN") == "¥100.00"


# ── 3. JPY (no decimals) ja-JP ───────────────────────────────────────────
def test_format_currency_jpy_no_decimals():
    """JPY renders as integer yen — 150000 cents == 1500 JPY."""
    out = format_currency(150000, "JPY", "ja-JP")
    assert out == "¥1,500"
    assert "." not in out


# ── 4. IDR (no decimals) id-ID ───────────────────────────────────────────
def test_format_currency_idr_no_decimals():
    """IDR is integer-only; ``.`` is the thousands separator in id_ID."""
    out = format_currency(150000, "IDR", "id-ID")
    assert out == "Rp1.500"
    assert "," not in out  # decimal sep is `,` in id_ID; we have no fraction


# ── 5. Negative numbers ──────────────────────────────────────────────────
def test_format_currency_negative():
    out = format_currency(-10000, "SGD", "en-SG")
    assert out.startswith("-")
    assert "100.00" in out
    assert "S$" in out


# ── 6. Date format en-SG vs en-US ────────────────────────────────────────
def test_format_date_locale_variants():
    d = date(2026, 5, 30)
    sg = format_date(d, "en-SG")
    us = format_date(d, "en-US")
    # en-SG is DD MMM YYYY (Commonwealth order); en-US is MMM D, YYYY.
    assert "30 May 2026" == sg
    assert "May 30, 2026" == us
    assert sg != us


# ── 7. get_primary_currency per region ───────────────────────────────────
@pytest.mark.parametrize(
    "region,expected",
    [
        ("cn", "CNY"),
        ("sg", "SGD"),
        ("id", "IDR"),
        ("us", "USD"),
        ("eu", "EUR"),
    ],
)
def test_get_primary_currency_per_region(region: str, expected: str):
    assert get_primary_currency(region) == expected


# ── 8. currency_decimals ─────────────────────────────────────────────────
def test_currency_decimals():
    assert currency_decimals("JPY") == 0
    assert currency_decimals("IDR") == 0
    assert currency_decimals("VND") == 0
    assert currency_decimals("KRW") == 0
    assert currency_decimals("SGD") == 2
    assert currency_decimals("CNY") == 2
    assert currency_decimals("USD") == 2
    assert currency_decimals("EUR") == 2
    # Case-insensitive
    assert currency_decimals("jpy") == 0


# ── 9. convert stub returns same (and logs WARN) ─────────────────────────
def test_convert_stub_returns_input_and_logs(caplog):
    """Same-currency convert is a no-op; cross-currency logs WARN."""
    assert convert(10000, "SGD", "SGD") == 10000   # no-op, no log
    with caplog.at_level(logging.WARNING, logger="app.i18n.currency"):
        out = convert(10000, "SGD", "USD")
    # Same-decimal currencies → unchanged
    assert out == 10000
    assert any("fx_stub_conversion" in rec.message for rec in caplog.records)


# ── 10. Multi-currency wallet GET balances ───────────────────────────────
async def test_multi_currency_wallet_balances(client, clean_redis):
    """Seed a brand wallet, then verify both the legacy and multi-currency
    endpoints surface the balance."""
    r = clean_redis
    brand_id = "brand_mc_test"
    await r.set(f"wallet:{brand_id}:balance", "25000")
    await r.set(f"wallet:{brand_id}:currency", "SGD")
    # Add a secondary CNY balance via the new HASH path
    await r.hset(f"wallet:brand:{brand_id}:balances", "CNY", "9999")

    resp = await client.get(f"/api/v1/wallet/{brand_id}/balances")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["brand_id"] == brand_id
    assert body["primary_currency"] == "SGD"
    assert body["balances"]["SGD"] == 25000
    assert body["balances"]["CNY"] == 9999

    # Alias endpoint returns only the primary.
    alias = await client.get(f"/api/v1/wallet/{brand_id}/balance")
    assert alias.status_code == 200, alias.text
    a = alias.json()
    assert a["currency"] == "SGD"
    assert a["balance_cents"] == 25000


# ── 11. Debug endpoint ───────────────────────────────────────────────────
async def test_debug_format_endpoint(client):
    resp = await client.get(
        "/api/v1/i18n/format",
        params={"amount_cents": 10000, "currency": "SGD", "locale": "en-SG"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["formatted"] == "S$100.00"
    assert body["currency"] == "SGD"
    assert body["locale"] == "en-SG"
    assert body["decimals"] == 2
    assert body["valid_iso"] is True


# ── 12. Locale fallback when invalid locale ──────────────────────────────
def test_locale_fallback_invalid_locale():
    """Unknown locale → falls back to DEFAULT_LOCALE; never raises."""
    out = format_currency(10000, "SGD", "xx-INVALID")
    # Default locale is en-SG → S$100.00
    assert out == "S$100.00"

    # Number formatting on an invalid locale should also not blow up
    assert format_number(1234.5, "totally-bogus") in {
        "1,234.5", "1.234,5", "1234.5",
    }


# ── Bonus sanity: get_supported_currencies + auto-refund + price book ────
def test_supported_currencies_and_auto_refund():
    assert "SGD" in get_supported_currencies("sg")
    assert "CNY" in get_supported_currencies("cn")
    # SG threshold should be > CN's (S$15 vs ¥10 in minor units)
    assert get_auto_refund_threshold("sg") == 1500
    assert get_auto_refund_threshold("cn") == 1000
    assert get_auto_refund_threshold("id") == 150_000


def test_subscription_price_book():
    # CN starter monthly == legacy ¥199
    assert get_subscription_price_cents("starter", "monthly", "cn") == 19_900
    # SG starter monthly is the SGD MSRP, different number
    assert get_subscription_price_cents("starter", "monthly", "sg") > 0
    assert (
        get_subscription_price_cents("starter", "monthly", "sg")
        != get_subscription_price_cents("starter", "monthly", "cn")
    )
    assert get_subscription_price_cents("free", "monthly", "cn") == 0


def test_format_percent_locale():
    out_us = format_percent(0.456, "en-US")
    assert "%" in out_us
    assert "46" in out_us


def test_is_valid_currency():
    assert is_valid_currency("SGD")
    assert is_valid_currency("usd")  # case-insensitive
    assert not is_valid_currency("ZZZ")
