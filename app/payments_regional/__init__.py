"""Per-country payment method capability registry.

This module is the **data layer** for KiX's regional checkout flows.
It does *not* perform any payment processing — actual integrations
already exist via :mod:`app.routers.payment_methods` (Stripe-style
card-on-file) and the various Stripe / Adyen calls in payouts.

Goal
----
Future checkout flows (and the merchant onboarding wizard) need to
answer four questions for every (country, currency) pair:

  1. Which payment methods are accepted here?
  2. What are their fees, settlement currency & T+N timing?
  3. Which methods support recurring (subscriptions)?
  4. Which method should we recommend by default?

The registry below is hand-curated from public published rate cards
(PayNow, GoPay, Razorpay, Stripe SEA, etc.) as of 2026-05. Where a
provider does not publish a public rate, a sensible market-typical
fee was used and flagged with ``integration_status="planned"`` so
that finance can override it before turning on live routing.

Strict guard-rails
------------------
* This module **does not import** from :mod:`app.region` or
  :mod:`app.routers.payment_methods` — those are owned by other
  agents and we must not couple to their internals.
* The :class:`PaymentMethod` dataclass is **frozen** to make the
  registry immutable from caller code.
* All lookup helpers return *copies* (``list(...)``) so callers
  cannot mutate the global registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PaymentMethod:
    """A single payment method available in one or more countries.

    Attributes mirror what an checkout-quote API would need to
    surface to the merchant + user.
    """

    code: str
    display_name_en: str
    display_name_local: str
    countries: list[str]
    currencies: list[str]
    payment_type: str  # card | wallet | bank_transfer | qr | buy_now_pay_later
    settles_to_currency: str
    settlement_days: int
    fee_bps: int  # basis points; 200 = 2.00%
    fixed_fee_cents: int  # in the *settlement* currency
    requires_3ds: bool
    supports_recurring: bool
    consumer_protection: str  # strong | medium | weak
    integration_status: str  # live | scaffold | planned
    # Wave C addition: dotted-path to the per-PSP wrapper module in
    # ``app.services.payment_psps``. Empty for methods that still route
    # through Stripe or do not yet have a dedicated wrapper. Default
    # empty keeps every existing row in the registry valid without edits.
    client_module: str = ""

    def __post_init__(self) -> None:  # pragma: no cover — invariants only
        assert self.payment_type in {
            "card",
            "wallet",
            "bank_transfer",
            "qr",
            "buy_now_pay_later",
        }, f"invalid payment_type {self.payment_type!r}"
        assert self.consumer_protection in {"strong", "medium", "weak"}
        assert self.integration_status in {"live", "scaffold", "planned"}
        assert self.fee_bps >= 0 and self.fixed_fee_cents >= 0
        assert self.settlement_days >= 0


# ─────────────────────────────────────────────────────────────────
# Registry — all methods, one row per provider.
#
# A single provider that operates in N countries (e.g. GrabPay)
# gets ONE row with ``countries=[...]`` rather than N near-duplicate
# rows. This keeps the matrix sane and avoids drift between region
# entries. Settlement currency for multi-country wallets follows
# the wallet's home country — see settlement.py for overrides.
# ─────────────────────────────────────────────────────────────────
_METHODS: list[PaymentMethod] = [
    # ── Singapore ────────────────────────────────────────────────
    PaymentMethod(
        code="paynow",
        display_name_en="PayNow",
        display_name_local="PayNow",
        countries=["SG"],
        currencies=["SGD"],
        payment_type="bank_transfer",
        settles_to_currency="SGD",
        settlement_days=0,
        fee_bps=0,
        fixed_fee_cents=50,  # S$0.50 fixed
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="live",
        client_module="app.services.payment_psps.paynow_sg",
    ),
    PaymentMethod(
        code="nets",
        display_name_en="NETS",
        display_name_local="NETS",
        countries=["SG"],
        currencies=["SGD"],
        payment_type="bank_transfer",
        settles_to_currency="SGD",
        settlement_days=1,
        fee_bps=80,
        fixed_fee_cents=30,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="favepay",
        display_name_en="FavePay",
        display_name_local="FavePay",
        countries=["SG", "MY"],
        currencies=["SGD", "MYR"],
        payment_type="wallet",
        settles_to_currency="SGD",
        settlement_days=2,
        fee_bps=250,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="medium",
        integration_status="planned",
    ),
    # ── Multi-country wallets ────────────────────────────────────
    PaymentMethod(
        code="grabpay",
        display_name_en="GrabPay",
        display_name_local="GrabPay",
        countries=["SG", "MY", "TH", "VN", "PH", "ID"],
        currencies=["SGD", "MYR", "THB", "VND", "PHP", "IDR"],
        payment_type="wallet",
        settles_to_currency="SGD",  # default; settlement.py overrides per merchant
        settlement_days=2,
        fee_bps=250,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="medium",
        integration_status="live",
        client_module="app.services.payment_psps.grabpay",
    ),
    PaymentMethod(
        code="apple_pay",
        display_name_en="Apple Pay",
        display_name_local="Apple Pay",
        countries=[
            "SG", "MY", "TH", "VN", "PH", "ID",
            "JP", "KR", "HK", "TW",
            "US", "CA", "AU", "NZ",
            "GB", "DE", "FR", "NL", "BE", "IT", "ES", "PL", "AT",
        ],
        currencies=[
            "SGD", "MYR", "THB", "VND", "PHP", "IDR",
            "JPY", "KRW", "HKD", "TWD",
            "USD", "CAD", "AUD", "NZD",
            "GBP", "EUR", "PLN",
        ],
        payment_type="card",  # tokenised card under the hood
        settles_to_currency="USD",
        settlement_days=2,
        fee_bps=290,
        fixed_fee_cents=30,
        requires_3ds=False,  # device biometric replaces 3DS
        supports_recurring=True,
        consumer_protection="strong",
        integration_status="live",
    ),
    PaymentMethod(
        code="google_pay",
        display_name_en="Google Pay",
        display_name_local="Google Pay",
        countries=[
            "SG", "MY", "TH", "VN", "PH", "ID", "IN",
            "JP", "KR", "TW",
            "US", "CA", "AU",
            "GB", "DE", "FR", "NL", "BE", "IT", "ES", "PL",
            "BR",
        ],
        currencies=[
            "SGD", "MYR", "THB", "VND", "PHP", "IDR", "INR",
            "JPY", "KRW", "TWD",
            "USD", "CAD", "AUD",
            "GBP", "EUR", "PLN",
            "BRL",
        ],
        payment_type="card",
        settles_to_currency="USD",
        settlement_days=2,
        fee_bps=290,
        fixed_fee_cents=30,
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="strong",
        integration_status="live",
    ),
    PaymentMethod(
        code="credit_card",
        display_name_en="Credit/Debit Card",
        display_name_local="Credit/Debit Card",
        countries=[
            "SG", "MY", "TH", "VN", "PH", "ID", "IN",
            "JP", "KR", "CN", "HK", "TW",
            "US", "CA", "AU", "NZ",
            "GB", "DE", "FR", "NL", "BE", "IT", "ES", "PL", "AT",
            "BR",
        ],
        currencies=[
            "SGD", "MYR", "THB", "VND", "PHP", "IDR", "INR",
            "JPY", "KRW", "CNY", "HKD", "TWD",
            "USD", "CAD", "AUD", "NZD",
            "GBP", "EUR", "PLN",
            "BRL",
        ],
        payment_type="card",
        settles_to_currency="USD",
        settlement_days=2,
        fee_bps=290,
        fixed_fee_cents=30,
        requires_3ds=True,
        supports_recurring=True,
        consumer_protection="strong",
        integration_status="live",
    ),
    # ── Indonesia ────────────────────────────────────────────────
    PaymentMethod(
        code="gopay",
        display_name_en="GoPay",
        display_name_local="GoPay",
        countries=["ID"],
        currencies=["IDR"],
        payment_type="wallet",
        settles_to_currency="IDR",
        settlement_days=2,
        fee_bps=200,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="medium",
        integration_status="live",
    ),
    PaymentMethod(
        code="ovo",
        display_name_en="OVO",
        display_name_local="OVO",
        countries=["ID"],
        currencies=["IDR"],
        payment_type="wallet",
        settles_to_currency="IDR",
        settlement_days=2,
        fee_bps=200,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="medium",
        integration_status="live",
        client_module="app.services.payment_psps.ovo_indonesia",
    ),
    PaymentMethod(
        code="dana",
        display_name_en="DANA",
        display_name_local="DANA",
        countries=["ID"],
        currencies=["IDR"],
        payment_type="wallet",
        settles_to_currency="IDR",
        settlement_days=2,
        fee_bps=200,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="medium",
        integration_status="live",
    ),
    PaymentMethod(
        code="shopeepay",
        display_name_en="ShopeePay",
        display_name_local="ShopeePay",
        countries=["ID", "MY", "TH", "PH", "VN", "SG"],
        currencies=["IDR", "MYR", "THB", "PHP", "VND", "SGD"],
        payment_type="wallet",
        settles_to_currency="IDR",
        settlement_days=3,
        fee_bps=220,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="medium",
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="linkaja",
        display_name_en="LinkAja",
        display_name_local="LinkAja",
        countries=["ID"],
        currencies=["IDR"],
        payment_type="wallet",
        settles_to_currency="IDR",
        settlement_days=2,
        fee_bps=180,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="medium",
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="bca_va",
        display_name_en="BCA Virtual Account",
        display_name_local="BCA Virtual Account",
        countries=["ID"],
        currencies=["IDR"],
        payment_type="bank_transfer",
        settles_to_currency="IDR",
        settlement_days=1,
        fee_bps=0,
        fixed_fee_cents=400000,  # Rp 4,000 fixed
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="live",
    ),
    PaymentMethod(
        code="bri_va",
        display_name_en="BRI Virtual Account",
        display_name_local="BRI Virtual Account",
        countries=["ID"],
        currencies=["IDR"],
        payment_type="bank_transfer",
        settles_to_currency="IDR",
        settlement_days=1,
        fee_bps=0,
        fixed_fee_cents=400000,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="qris",
        display_name_en="QRIS",
        display_name_local="QRIS",
        countries=["ID"],
        currencies=["IDR"],
        payment_type="qr",
        settles_to_currency="IDR",
        settlement_days=1,
        fee_bps=70,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="live",
    ),
    PaymentMethod(
        code="akulaku",
        display_name_en="Akulaku PayLater",
        display_name_local="Akulaku PayLater",
        countries=["ID", "PH"],
        currencies=["IDR", "PHP"],
        payment_type="buy_now_pay_later",
        settles_to_currency="IDR",
        settlement_days=7,
        fee_bps=350,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="medium",
        integration_status="planned",
    ),
    # ── Thailand ─────────────────────────────────────────────────
    PaymentMethod(
        code="promptpay",
        display_name_en="PromptPay",
        display_name_local="พร้อมเพย์",
        countries=["TH"],
        currencies=["THB"],
        payment_type="bank_transfer",
        settles_to_currency="THB",
        settlement_days=0,
        fee_bps=0,
        fixed_fee_cents=1000,  # ฿10 fixed
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="live",
    ),
    PaymentMethod(
        code="truemoney",
        display_name_en="TrueMoney Wallet",
        display_name_local="ทรูมันนี่ วอลเล็ท",
        countries=["TH", "VN", "PH", "MM", "KH"],
        currencies=["THB", "VND", "PHP", "MMK", "KHR"],
        payment_type="wallet",
        settles_to_currency="THB",
        settlement_days=2,
        fee_bps=200,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="medium",
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="rabbit_line_pay",
        display_name_en="Rabbit LINE Pay",
        display_name_local="แรบบิท ไลน์ เพย์",
        countries=["TH"],
        currencies=["THB"],
        payment_type="wallet",
        settles_to_currency="THB",
        settlement_days=2,
        fee_bps=220,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="medium",
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="k_plus",
        display_name_en="K PLUS (Kasikorn)",
        display_name_local="K PLUS",
        countries=["TH"],
        currencies=["THB"],
        payment_type="bank_transfer",
        settles_to_currency="THB",
        settlement_days=1,
        fee_bps=50,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="planned",
    ),
    PaymentMethod(
        code="scb_easy",
        display_name_en="SCB Easy",
        display_name_local="SCB Easy",
        countries=["TH"],
        currencies=["THB"],
        payment_type="bank_transfer",
        settles_to_currency="THB",
        settlement_days=1,
        fee_bps=50,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="planned",
    ),
    # ── Vietnam ──────────────────────────────────────────────────
    PaymentMethod(
        code="momo",
        display_name_en="MoMo",
        display_name_local="Ví MoMo",
        countries=["VN"],
        currencies=["VND"],
        payment_type="wallet",
        settles_to_currency="VND",
        settlement_days=2,
        fee_bps=220,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="medium",
        integration_status="live",
    ),
    PaymentMethod(
        code="zalopay",
        display_name_en="ZaloPay",
        display_name_local="ZaloPay",
        countries=["VN"],
        currencies=["VND"],
        payment_type="wallet",
        settles_to_currency="VND",
        settlement_days=2,
        fee_bps=220,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="medium",
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="vnpay",
        display_name_en="VNPay",
        display_name_local="VNPay",
        countries=["VN"],
        currencies=["VND"],
        payment_type="qr",
        settles_to_currency="VND",
        settlement_days=1,
        fee_bps=180,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="viettelpay",
        display_name_en="ViettelPay",
        display_name_local="ViettelPay",
        countries=["VN"],
        currencies=["VND"],
        payment_type="wallet",
        settles_to_currency="VND",
        settlement_days=2,
        fee_bps=200,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="medium",
        integration_status="planned",
    ),
    PaymentMethod(
        code="bidv_va",
        display_name_en="BIDV Virtual Account",
        display_name_local="BIDV Virtual Account",
        countries=["VN"],
        currencies=["VND"],
        payment_type="bank_transfer",
        settles_to_currency="VND",
        settlement_days=1,
        fee_bps=0,
        fixed_fee_cents=1100000,  # ~ ₫11,000 fixed
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="planned",
    ),
    # ── Philippines ──────────────────────────────────────────────
    PaymentMethod(
        code="gcash",
        display_name_en="GCash",
        display_name_local="GCash",
        countries=["PH"],
        currencies=["PHP"],
        payment_type="wallet",
        settles_to_currency="PHP",
        settlement_days=2,
        fee_bps=230,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="medium",
        integration_status="live",
    ),
    PaymentMethod(
        code="paymaya",
        display_name_en="Maya (PayMaya)",
        display_name_local="Maya",
        countries=["PH"],
        currencies=["PHP"],
        payment_type="wallet",
        settles_to_currency="PHP",
        settlement_days=2,
        fee_bps=230,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="medium",
        integration_status="live",
    ),
    PaymentMethod(
        code="coins_ph",
        display_name_en="Coins.ph",
        display_name_local="Coins.ph",
        countries=["PH"],
        currencies=["PHP"],
        payment_type="wallet",
        settles_to_currency="PHP",
        settlement_days=2,
        fee_bps=250,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="medium",
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="instapay",
        display_name_en="InstaPay",
        display_name_local="InstaPay",
        countries=["PH"],
        currencies=["PHP"],
        payment_type="bank_transfer",
        settles_to_currency="PHP",
        settlement_days=0,
        fee_bps=0,
        fixed_fee_cents=2500,  # ₱25 typical
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="scaffold",
    ),
    # ── Malaysia ─────────────────────────────────────────────────
    PaymentMethod(
        code="tng_ewallet",
        display_name_en="Touch'n Go eWallet",
        display_name_local="Touch'n Go eWallet",
        countries=["MY"],
        currencies=["MYR"],
        payment_type="wallet",
        settles_to_currency="MYR",
        settlement_days=2,
        fee_bps=200,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="medium",
        integration_status="live",
    ),
    PaymentMethod(
        code="boost",
        display_name_en="Boost",
        display_name_local="Boost",
        countries=["MY"],
        currencies=["MYR"],
        payment_type="wallet",
        settles_to_currency="MYR",
        settlement_days=2,
        fee_bps=200,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="medium",
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="fpx",
        display_name_en="FPX (Online Banking)",
        display_name_local="FPX",
        countries=["MY"],
        currencies=["MYR"],
        payment_type="bank_transfer",
        settles_to_currency="MYR",
        settlement_days=1,
        fee_bps=0,
        fixed_fee_cents=100,  # RM 1.00 fixed typical
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="live",
    ),
    # ── China ────────────────────────────────────────────────────
    PaymentMethod(
        code="alipay",
        display_name_en="Alipay",
        display_name_local="支付宝",
        countries=["CN", "HK", "SG", "MY"],
        currencies=["CNY", "HKD", "SGD", "MYR"],
        payment_type="wallet",
        settles_to_currency="CNY",
        settlement_days=1,
        fee_bps=60,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="strong",
        integration_status="live",
        client_module="app.services.payment_psps.alipay_global",
    ),
    PaymentMethod(
        code="wechat_pay",
        display_name_en="WeChat Pay",
        display_name_local="微信支付",
        countries=["CN", "HK"],
        currencies=["CNY", "HKD"],
        payment_type="wallet",
        settles_to_currency="CNY",
        settlement_days=1,
        fee_bps=60,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="strong",
        integration_status="live",
        client_module="app.services.payment_psps.wechat_pay",
    ),
    PaymentMethod(
        code="unionpay",
        display_name_en="UnionPay",
        display_name_local="银联",
        countries=["CN", "HK"],
        currencies=["CNY", "HKD"],
        payment_type="card",
        settles_to_currency="CNY",
        settlement_days=2,
        fee_bps=80,
        fixed_fee_cents=0,
        requires_3ds=True,
        supports_recurring=True,
        consumer_protection="strong",
        integration_status="scaffold",
    ),
    # ── Japan ────────────────────────────────────────────────────
    PaymentMethod(
        code="konbini",
        display_name_en="Konbini (Convenience Store)",
        display_name_local="コンビニ決済",
        countries=["JP"],
        currencies=["JPY"],
        payment_type="bank_transfer",
        settles_to_currency="JPY",
        settlement_days=3,
        fee_bps=290,
        fixed_fee_cents=12000,  # ¥120 fixed typical
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="bipl",
        display_name_en="Bank Transfer (Pay-Easy)",
        display_name_local="銀行振込",
        countries=["JP"],
        currencies=["JPY"],
        payment_type="bank_transfer",
        settles_to_currency="JPY",
        settlement_days=2,
        fee_bps=0,
        fixed_fee_cents=30000,  # ¥300 fixed
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="planned",
    ),
    PaymentMethod(
        code="jcb",
        display_name_en="JCB",
        display_name_local="JCB",
        countries=["JP", "KR", "TW", "TH", "SG"],
        currencies=["JPY", "KRW", "TWD", "THB", "SGD"],
        payment_type="card",
        settles_to_currency="JPY",
        settlement_days=2,
        fee_bps=320,
        fixed_fee_cents=0,
        requires_3ds=True,
        supports_recurring=True,
        consumer_protection="strong",
        integration_status="live",
    ),
    # ── Korea ────────────────────────────────────────────────────
    PaymentMethod(
        code="kakaopay",
        display_name_en="KakaoPay",
        display_name_local="카카오페이",
        countries=["KR"],
        currencies=["KRW"],
        payment_type="wallet",
        settles_to_currency="KRW",
        settlement_days=2,
        fee_bps=250,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="strong",
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="naver_pay",
        display_name_en="Naver Pay",
        display_name_local="네이버페이",
        countries=["KR"],
        currencies=["KRW"],
        payment_type="wallet",
        settles_to_currency="KRW",
        settlement_days=2,
        fee_bps=250,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="strong",
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="toss",
        display_name_en="Toss",
        display_name_local="토스",
        countries=["KR"],
        currencies=["KRW"],
        payment_type="wallet",
        settles_to_currency="KRW",
        settlement_days=2,
        fee_bps=250,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="strong",
        integration_status="planned",
    ),
    # ── India ────────────────────────────────────────────────────
    PaymentMethod(
        code="upi",
        display_name_en="UPI",
        display_name_local="UPI",
        countries=["IN"],
        currencies=["INR"],
        payment_type="bank_transfer",
        settles_to_currency="INR",
        settlement_days=1,
        fee_bps=0,
        fixed_fee_cents=0,  # NPCI mandates zero MDR on UPI for P2M
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="strong",
        integration_status="live",
    ),
    PaymentMethod(
        code="razorpay",
        display_name_en="Razorpay",
        display_name_local="Razorpay",
        countries=["IN"],
        currencies=["INR"],
        payment_type="card",
        settles_to_currency="INR",
        settlement_days=2,
        fee_bps=200,
        fixed_fee_cents=0,
        requires_3ds=True,
        supports_recurring=True,
        consumer_protection="strong",
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="mobikwik",
        display_name_en="MobiKwik",
        display_name_local="MobiKwik",
        countries=["IN"],
        currencies=["INR"],
        payment_type="wallet",
        settles_to_currency="INR",
        settlement_days=2,
        fee_bps=190,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="medium",
        integration_status="planned",
    ),
    # ── Brazil ───────────────────────────────────────────────────
    PaymentMethod(
        code="pix",
        display_name_en="PIX",
        display_name_local="PIX",
        countries=["BR"],
        currencies=["BRL"],
        payment_type="bank_transfer",
        settles_to_currency="BRL",
        settlement_days=0,
        fee_bps=99,  # ~0.99% typical merchant rate
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="strong",
        integration_status="live",
    ),
    PaymentMethod(
        code="boleto",
        display_name_en="Boleto Bancário",
        display_name_local="Boleto Bancário",
        countries=["BR"],
        currencies=["BRL"],
        payment_type="bank_transfer",
        settles_to_currency="BRL",
        settlement_days=3,
        fee_bps=290,
        fixed_fee_cents=350,  # R$3.50 typical
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="mercado_pago",
        display_name_en="Mercado Pago",
        display_name_local="Mercado Pago",
        countries=["BR", "AR", "MX", "CL", "CO"],
        currencies=["BRL", "ARS", "MXN", "CLP", "COP"],
        payment_type="wallet",
        settles_to_currency="BRL",
        settlement_days=2,
        fee_bps=399,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="medium",
        integration_status="scaffold",
    ),
    # ── Europe ───────────────────────────────────────────────────
    PaymentMethod(
        code="sepa_direct_debit",
        display_name_en="SEPA Direct Debit",
        display_name_local="SEPA Lastschrift",
        countries=[
            "DE", "FR", "NL", "BE", "IT", "ES", "PL", "AT", "PT",
            "IE", "FI", "GR", "SK", "SI", "EE", "LV", "LT", "LU",
            "MT", "CY",
        ],
        currencies=["EUR"],
        payment_type="bank_transfer",
        settles_to_currency="EUR",
        settlement_days=3,
        fee_bps=80,
        fixed_fee_cents=35,
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="strong",
        integration_status="live",
    ),
    PaymentMethod(
        code="ideal",
        display_name_en="iDEAL",
        display_name_local="iDEAL",
        countries=["NL"],
        currencies=["EUR"],
        payment_type="bank_transfer",
        settles_to_currency="EUR",
        settlement_days=1,
        fee_bps=0,
        fixed_fee_cents=29,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="live",
    ),
    PaymentMethod(
        code="bancontact",
        display_name_en="Bancontact",
        display_name_local="Bancontact",
        countries=["BE"],
        currencies=["EUR"],
        payment_type="bank_transfer",
        settles_to_currency="EUR",
        settlement_days=1,
        fee_bps=140,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="giropay",
        display_name_en="Giropay",
        display_name_local="Giropay",
        countries=["DE"],
        currencies=["EUR"],
        payment_type="bank_transfer",
        settles_to_currency="EUR",
        settlement_days=1,
        fee_bps=140,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="eps",
        display_name_en="EPS",
        display_name_local="EPS Überweisung",
        countries=["AT"],
        currencies=["EUR"],
        payment_type="bank_transfer",
        settles_to_currency="EUR",
        settlement_days=1,
        fee_bps=140,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="blik",
        display_name_en="BLIK",
        display_name_local="BLIK",
        countries=["PL"],
        currencies=["PLN"],
        payment_type="bank_transfer",
        settles_to_currency="PLN",
        settlement_days=1,
        fee_bps=120,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="scaffold",
    ),
    # ── United States ────────────────────────────────────────────
    PaymentMethod(
        code="ach",
        display_name_en="ACH Bank Transfer",
        display_name_local="ACH Bank Transfer",
        countries=["US"],
        currencies=["USD"],
        payment_type="bank_transfer",
        settles_to_currency="USD",
        settlement_days=3,
        fee_bps=80,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=True,
        consumer_protection="strong",
        integration_status="live",
    ),
    PaymentMethod(
        code="zelle",
        display_name_en="Zelle",
        display_name_local="Zelle",
        countries=["US"],
        currencies=["USD"],
        payment_type="bank_transfer",
        settles_to_currency="USD",
        settlement_days=0,
        fee_bps=0,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="weak",  # Zelle famously has no chargeback
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="venmo",
        display_name_en="Venmo",
        display_name_local="Venmo",
        countries=["US"],
        currencies=["USD"],
        payment_type="wallet",
        settles_to_currency="USD",
        settlement_days=1,
        fee_bps=190,
        fixed_fee_cents=10,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="medium",
        integration_status="scaffold",
    ),
    # ── Australia ────────────────────────────────────────────────
    PaymentMethod(
        code="payid",
        display_name_en="PayID",
        display_name_local="PayID",
        countries=["AU"],
        currencies=["AUD"],
        payment_type="bank_transfer",
        settles_to_currency="AUD",
        settlement_days=0,
        fee_bps=0,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="osko",
        display_name_en="Osko",
        display_name_local="Osko",
        countries=["AU"],
        currencies=["AUD"],
        payment_type="bank_transfer",
        settles_to_currency="AUD",
        settlement_days=0,
        fee_bps=0,
        fixed_fee_cents=0,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="scaffold",
    ),
    PaymentMethod(
        code="afterpay",
        display_name_en="Afterpay",
        display_name_local="Afterpay",
        countries=["AU", "NZ", "US", "GB", "CA"],
        currencies=["AUD", "NZD", "USD", "GBP", "CAD"],
        payment_type="buy_now_pay_later",
        settles_to_currency="AUD",
        settlement_days=1,
        fee_bps=600,
        fixed_fee_cents=30,
        requires_3ds=False,
        supports_recurring=False,
        consumer_protection="strong",
        integration_status="live",
    ),
]


# Sanity check: codes must be unique. Done at import-time so any
# accidental duplicate in the registry fails the test suite loudly.
def _validate_registry() -> None:
    codes = [m.code for m in _METHODS]
    if len(codes) != len(set(codes)):
        dupes = {c for c in codes if codes.count(c) > 1}
        raise RuntimeError(f"duplicate payment-method codes: {sorted(dupes)}")


_validate_registry()


# ─────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────
def all_methods() -> list[PaymentMethod]:
    """Return a *copy* of the full registry (defensive)."""
    return list(_METHODS)


def get_method(code: str) -> Optional[PaymentMethod]:
    """Look up a single method by code (case-insensitive)."""
    code = code.lower()
    for m in _METHODS:
        if m.code == code:
            return m
    return None


def _norm_country(country: str) -> str:
    return country.upper()


def _norm_currency(currency: str) -> str:
    return currency.upper()


def get_methods_for_country(country: str) -> list[PaymentMethod]:
    """All methods available in the given ISO-3166-1 alpha-2 country."""
    c = _norm_country(country)
    return [m for m in _METHODS if c in m.countries]


def get_methods_for_currency(currency: str) -> list[PaymentMethod]:
    """All methods that can settle / accept the given ISO-4217 currency."""
    cur = _norm_currency(currency)
    return [m for m in _METHODS if cur in m.currencies]


def get_methods_supporting_recurring(country: str) -> list[PaymentMethod]:
    """Subset of country methods that can be charged on a schedule."""
    return [m for m in get_methods_for_country(country) if m.supports_recurring]


_STATUS_RANK = {"live": 0, "scaffold": 1, "planned": 2}


def _effective_fee_cents(m: PaymentMethod, amount_cents: int) -> int:
    """Total fee = amount × bps/10000 + fixed."""
    pct = (amount_cents * m.fee_bps) // 10000
    return pct + m.fixed_fee_cents


def recommend_method(
    country: str,
    amount_cents: int,
    currency: str,
    user_pref: Optional[str] = None,
) -> Optional[PaymentMethod]:
    """Recommend a single payment method for a checkout.

    Decision order:
      1. ``user_pref`` wins if the named method exists, supports the
         (country, currency) pair, and is at least ``scaffold``.
      2. Otherwise: filter to country ∩ currency, prefer
         ``integration_status==live``, then lowest *effective fee*
         at this amount, then most consumer protection (tie-breaker).

    Returns ``None`` if nothing matches at all.
    """
    if amount_cents < 0:
        raise ValueError("amount_cents must be >= 0")
    c = _norm_country(country)
    cur = _norm_currency(currency)

    if user_pref:
        m = get_method(user_pref)
        if m and c in m.countries and cur in m.currencies and m.integration_status != "planned":
            return m

    candidates = [
        m for m in _METHODS
        if c in m.countries and cur in m.currencies
    ]
    if not candidates:
        return None

    protection_rank = {"strong": 0, "medium": 1, "weak": 2}

    def _key(m: PaymentMethod) -> tuple:
        return (
            _STATUS_RANK[m.integration_status],
            _effective_fee_cents(m, amount_cents),
            protection_rank[m.consumer_protection],
            m.code,  # deterministic tie-break
        )

    candidates.sort(key=_key)
    return candidates[0]


def calculate_fee(method_code: str, amount_cents: int) -> tuple[int, int]:
    """Return ``(fee_cents, net_cents)`` for the given method + amount.

    ``net_cents`` is the merchant net (``amount_cents - fee_cents``).
    Raises :class:`KeyError` for unknown methods. Raises
    :class:`ValueError` for negative amounts.
    """
    if amount_cents < 0:
        raise ValueError("amount_cents must be >= 0")
    m = get_method(method_code)
    if m is None:
        raise KeyError(f"unknown payment method: {method_code!r}")
    fee = _effective_fee_cents(m, amount_cents)
    net = amount_cents - fee
    return fee, net


def matrix() -> dict[str, list[str]]:
    """Country × method-code map. Useful for admin dashboards.

    Returns ``{ "SG": ["paynow", "grabpay", ...], ... }``.
    """
    out: dict[str, list[str]] = {}
    for m in _METHODS:
        for c in m.countries:
            out.setdefault(c, []).append(m.code)
    for c in out:
        out[c].sort()
    return out


__all__ = [
    "PaymentMethod",
    "all_methods",
    "get_method",
    "get_methods_for_country",
    "get_methods_for_currency",
    "get_methods_supporting_recurring",
    "recommend_method",
    "calculate_fee",
    "matrix",
]
