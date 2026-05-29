"""Currency registry & helpers.

A thin wrapper around :mod:`babel.numbers` plus KiX's :mod:`app.region`
table. Centralises three concerns:

1. **Defaults per region** — `get_primary_currency("sg") == "SGD"`.
2. **Decimal precision** — KiX overrides CLDR for currencies that
   business teams render as integers (IDR, VND, JPY). Babel/CLDR says
   IDR has 2 fractional digits; the local convention is 0.
3. **FX conversion stub** — placeholder for the FX service the platform
   will eventually wire in. Returns the input amount unchanged and emits
   a WARN log so production traffic is never silently wrong.

The auto-refund threshold lookup (``get_auto_refund_threshold``) lives
here because it's effectively a currency-policy decision: each region
gets a ~USD-equivalent small-claim cap in its primary currency cents.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from babel.numbers import list_currencies

from app.region import get_region_config

logger = logging.getLogger(__name__)


# ── Decimal overrides ────────────────────────────────────────────────────
# Currencies that KiX always renders without a fractional part. CLDR is
# inconsistent for some of these — IDR especially — so we hard-code the
# operational convention here.
_ZERO_DECIMAL_CURRENCIES: frozenset[str] = frozenset(
    {
        "JPY",  # CLDR agrees (0)
        "IDR",  # CLDR says 2, but Indonesia renders Rp without decimals
        "VND",  # CLDR agrees (0)
        "KRW",  # CLDR agrees (0)
        "CLP",  # Chilean peso — 0 in CLDR
        "ISK",
        "TWD",  # commonly rendered with 0
        "HUF",
    }
)

# Fallback decimals for currencies not in CLDR (extremely rare).
_DEFAULT_DECIMALS = 2


# ── Region → auto-refund threshold ───────────────────────────────────────
# Small-claim cap below which a dispute auto-refunds. Each value is in
# the region's primary-currency MINOR units (cents / fen / rupiah).
# Loose USD equivalence:
#   cn ¥10        ≈ $1.4   (cheap rides / small purchases)
#   sg S$15       ≈ $11
#   id Rp150 000  ≈ $9.6   (no decimals → 150_000 cents == Rp 150 000)
#   us $10
#   eu €10
# These mirror the comments in app/routers/disputes.py:70.
_AUTO_REFUND_THRESHOLD_CENTS: dict[str, int] = {
    "cn": 1000,         # ¥10
    "sg": 1500,         # S$15
    "id": 150_000,      # Rp 150 000 (IDR has 0 decimals → minor=major)
    "us": 1000,         # $10
    "eu": 1000,         # €10
}


@lru_cache(maxsize=1)
def _valid_iso_currencies() -> frozenset[str]:
    """Cached set of ISO 4217 codes Babel knows about."""
    return frozenset(c.upper() for c in list_currencies())


def is_valid_currency(currency: str) -> bool:
    """Return ``True`` iff *currency* is a known ISO 4217 code."""
    return currency.upper() in _valid_iso_currencies()


def get_primary_currency(region: str | None = None) -> str:
    """Return the region's primary currency (default region = current)."""
    return get_region_config(region)["primary_currency"]


def get_supported_currencies(region: str | None = None) -> list[str]:
    """Return the list of currencies the region's wallets accept."""
    return list(get_region_config(region)["supported_currencies"])


def currency_decimals(currency: str) -> int:
    """Number of fractional digits to render for *currency*.

    Returns 0 for JPY / IDR / VND / KRW / etc., 2 for everything else.
    Used by formatting helpers AND by wallet accounting logic that needs
    to know how to display ``amount_cents``.
    """
    c = currency.upper()
    if c in _ZERO_DECIMAL_CURRENCIES:
        return 0
    return _DEFAULT_DECIMALS


def convert(amount_cents: int, from_cur: str, to_cur: str) -> int:
    """**Stub** FX conversion.

    Returns ``amount_cents`` unchanged when ``from_cur == to_cur``.
    Otherwise logs a WARN and returns the same amount — never silently
    apply a wrong rate. Wire this to a real FX provider before enabling
    multi-currency settlement.
    """
    if from_cur.upper() == to_cur.upper():
        return amount_cents
    logger.warning(
        "fx_stub_conversion from=%s to=%s amount_cents=%s — "
        "no rate configured; returning input unchanged",
        from_cur, to_cur, amount_cents,
    )
    # Adjust for decimal-precision mismatch so callers don't end up with
    # a Rp value scaled wrongly when converting from CNY (2 decimals).
    src_dec = currency_decimals(from_cur)
    dst_dec = currency_decimals(to_cur)
    if src_dec == dst_dec:
        return amount_cents
    if src_dec > dst_dec:
        return amount_cents // (10 ** (src_dec - dst_dec))
    return amount_cents * (10 ** (dst_dec - src_dec))


def get_auto_refund_threshold(region: str | None = None) -> int:
    """Region-aware auto-refund threshold in primary-currency minor units.

    Replaces the global ``DEFAULT_AUTO_REFUND_UNDER_CENTS = 1000`` in
    :mod:`app.routers.disputes`.
    """
    from app.region import CURRENT_REGION  # local import to avoid cycle
    key = (region or CURRENT_REGION).lower()
    return _AUTO_REFUND_THRESHOLD_CENTS.get(
        key, _AUTO_REFUND_THRESHOLD_CENTS["cn"]
    )


# ── Subscription price book ──────────────────────────────────────────────
# Per-region MSRP for each (tier, billing) pair. The numbers below mirror
# the original CNY-only table in ``brand_subscriptions.py`` and apply a
# rough x/¥ multiplier per region (not a live FX rate — those land later
# via the FX router). When ``region`` lacks an entry we fall back to CN.

_SUBSCRIPTION_PRICE_BOOK: dict[str, dict[str, dict[str, int]]] = {
    "cn": {
        "free":       {"monthly_cents":      0, "annual_cents":      0},
        "starter":    {"monthly_cents":  19_900, "annual_cents":  199_000},
        "growth":     {"monthly_cents":  99_900, "annual_cents":  999_000},
        "enterprise": {"monthly_cents": 500_000, "annual_cents": 5_000_000},
    },
    "sg": {  # ~ ¥ / 5 → SGD; round to .00
        "free":       {"monthly_cents":      0, "annual_cents":      0},
        "starter":    {"monthly_cents":   3_900, "annual_cents":  39_000},
        "growth":     {"monthly_cents":  19_900, "annual_cents": 199_000},
        "enterprise": {"monthly_cents": 100_000, "annual_cents": 999_000},
    },
    "id": {  # IDR has 0 decimals — minor units == rupiah
        "free":       {"monthly_cents":        0, "annual_cents":         0},
        "starter":    {"monthly_cents":  450_000, "annual_cents":  4_500_000},
        "growth":     {"monthly_cents": 2_250_000, "annual_cents": 22_500_000},
        "enterprise": {"monthly_cents":11_250_000, "annual_cents":112_500_000},
    },
    "us": {
        "free":       {"monthly_cents":      0, "annual_cents":      0},
        "starter":    {"monthly_cents":   2_900, "annual_cents":  29_000},
        "growth":     {"monthly_cents":  14_900, "annual_cents": 149_000},
        "enterprise": {"monthly_cents":  75_000, "annual_cents": 750_000},
    },
    "eu": {
        "free":       {"monthly_cents":      0, "annual_cents":      0},
        "starter":    {"monthly_cents":   2_900, "annual_cents":  29_000},
        "growth":     {"monthly_cents":  14_900, "annual_cents": 149_000},
        "enterprise": {"monthly_cents":  75_000, "annual_cents": 750_000},
    },
}


def get_subscription_price_cents(
    tier: str,
    billing: str,
    region: str | None = None,
) -> int:
    """Look up ``(tier, billing)`` price for *region*'s primary currency.

    ``billing`` ∈ {"monthly", "annual"}. Unknown tiers → 0. Falls back to
    the CN price book when *region* isn't in the table.
    """
    from app.region import CURRENT_REGION
    key = (region or CURRENT_REGION).lower()
    book = _SUBSCRIPTION_PRICE_BOOK.get(key, _SUBSCRIPTION_PRICE_BOOK["cn"])
    plan = book.get(tier)
    if plan is None:
        return 0
    field = "annual_cents" if billing == "annual" else "monthly_cents"
    return plan.get(field, 0)
