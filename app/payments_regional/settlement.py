"""Settlement-currency router.

When a customer pays with a multi-country wallet (e.g. GrabPay,
which operates in 6 ASEAN countries) the funds settle to the
*merchant's* primary currency, not the wallet's home currency.
This module owns that mapping.

For single-country methods (PayNow, PIX, UPI, …) the answer is
trivial — the method only has one settles_to_currency in the
registry. For multi-country methods we look up the merchant's
country and return their primary currency.

This module is intentionally **decoupled** from app.region — we
do not want to pull in the full region config here, just the
country→currency mapping. If region.py changes its primary-
currency mapping we accept some drift; finance can override.
"""

from __future__ import annotations

from . import get_method


# Country → merchant primary settlement currency.
# Source: ISO 4217 + each country's tax-residence currency.
_MERCHANT_PRIMARY_CURRENCY: dict[str, str] = {
    "SG": "SGD",
    "MY": "MYR",
    "TH": "THB",
    "VN": "VND",
    "PH": "PHP",
    "ID": "IDR",
    "IN": "INR",
    "CN": "CNY",
    "HK": "HKD",
    "TW": "TWD",
    "JP": "JPY",
    "KR": "KRW",
    "US": "USD",
    "CA": "CAD",
    "AU": "AUD",
    "NZ": "NZD",
    "GB": "GBP",
    "DE": "EUR",
    "FR": "EUR",
    "NL": "EUR",
    "BE": "EUR",
    "IT": "EUR",
    "ES": "EUR",
    "PL": "PLN",
    "AT": "EUR",
    "PT": "EUR",
    "IE": "EUR",
    "FI": "EUR",
    "BR": "BRL",
    "AR": "ARS",
    "MX": "MXN",
    "CL": "CLP",
    "CO": "COP",
    "MM": "MMK",
    "KH": "KHR",
}


# Multi-country wallets that follow the *merchant's* currency.
# Single-country methods use their own settles_to_currency.
_MERCHANT_SETTLED_METHODS = {
    "grabpay",
    "shopeepay",
    "truemoney",
    "favepay",
    "alipay",  # cross-border Alipay settles to merchant currency
    "afterpay",
    "mercado_pago",
    "apple_pay",
    "google_pay",
    "credit_card",
    "jcb",
}


def get_settlement_currency(merchant_country: str, payment_method: str) -> str:
    """Return the ISO-4217 currency that funds will settle to.

    For single-country methods (PayNow, PIX, UPI…) we return the
    method's hard-coded ``settles_to_currency``. For multi-country
    wallets / card schemes we return the merchant's primary
    currency. If the merchant's country isn't in our mapping we
    fall back to the method's default.

    Raises :class:`KeyError` if ``payment_method`` is unknown.
    """
    m = get_method(payment_method)
    if m is None:
        raise KeyError(f"unknown payment method: {payment_method!r}")
    mc = merchant_country.upper()

    # Single-country method → its own settles_to_currency dominates.
    if m.code not in _MERCHANT_SETTLED_METHODS:
        return m.settles_to_currency

    # Multi-country method → follow the merchant.
    return _MERCHANT_PRIMARY_CURRENCY.get(mc, m.settles_to_currency)


__all__ = ["get_settlement_currency"]
