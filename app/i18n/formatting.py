"""Locale-aware number / date / currency formatting.

Thin wrapper over :mod:`babel.numbers` and :mod:`babel.dates`. Every
helper accepts an explicit ``locale=`` and falls back to
:func:`app.i18n.context.get_current_locale` otherwise.

The KiX storage convention is **integer minor units** (cents, fen,
rupiah). Helpers here convert that to a human-readable string using the
locale's CLDR formatting rules.

Notes
-----
- SGD: CLDR renders ``$100.00`` in ``en_SG`` because Singaporeans drop
  the disambiguator domestically. The :func:`format_currency` helper
  forces ``S$`` so KiX UI consistently disambiguates from USD even when
  the request locale is ``en_SG``. (KiX is multi-currency by design and
  ``$`` alone is ambiguous in our context.)
- JPY / IDR / VND etc.: see ``currency.currency_decimals``. We render
  these with 0 fractional digits regardless of CLDR.
- Locale strings: callers pass BCP-47 (``en-SG``, ``zh-Hans-CN``); Babel
  wants POSIX (``en_SG``, ``zh_Hans_CN``). We normalise.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from babel import Locale, UnknownLocaleError
from babel.dates import format_date as _babel_format_date
from babel.dates import format_datetime as _babel_format_datetime
from babel.numbers import format_currency as _babel_format_currency
from babel.numbers import format_decimal, format_percent as _babel_format_percent

from app.i18n.context import DEFAULT_LOCALE, get_current_locale
from app.i18n.currency import currency_decimals

logger = logging.getLogger(__name__)


# ── Locale plumbing ──────────────────────────────────────────────────────
def _normalise_locale(locale: str | None) -> str:
    """Turn BCP-47 (``en-SG``) into Babel/POSIX (``en_SG``).

    Falls back to :data:`DEFAULT_LOCALE` (also normalised) when ``locale``
    is falsy or unknown. Never raises — invalid locales degrade gracefully
    rather than blow up format calls.
    """
    if not locale:
        locale = get_current_locale() or DEFAULT_LOCALE
    normalised = locale.replace("-", "_")
    try:
        Locale.parse(normalised)
    except (UnknownLocaleError, ValueError) as exc:
        logger.debug("locale_fallback requested=%s err=%s", locale, exc)
        normalised = DEFAULT_LOCALE.replace("-", "_")
        try:
            Locale.parse(normalised)
        except (UnknownLocaleError, ValueError):
            normalised = "en_US"
    return normalised


# ── Currency-symbol overrides ────────────────────────────────────────────
# CLDR uses ``$`` for SGD inside Singapore. KiX is multi-currency, so we
# always render the disambiguating prefix. Same idea for HKD / NZD / AUD
# in their own locales.
_FORCED_SYMBOLS: dict[str, str] = {
    "SGD": "S$",
    "HKD": "HK$",
    "AUD": "A$",
    "NZD": "NZ$",
    "CAD": "C$",
    "TWD": "NT$",
    # JPY: ja_JP CLDR uses U+FFE5 (full-width ￥); KiX UI standardises on
    # the half-width U+00A5 (¥) to match the CNY rendering.
    "JPY": "¥",
}


def format_currency(
    amount_cents: int,
    currency: str,
    locale: str | None = None,
) -> str:
    """Render ``amount_cents`` as a localised currency string.

    Examples
    --------
    >>> format_currency(10000, "SGD", "en-SG")
    'S$100.00'
    >>> format_currency(10000, "CNY", "zh-Hans-CN")
    '¥100.00'
    >>> format_currency(150000, "JPY", "ja-JP")
    '¥1,500'
    >>> format_currency(150000, "IDR", "id-ID")
    'Rp150.000'
    """
    babel_locale = _normalise_locale(locale)
    currency = currency.upper()
    decimals = currency_decimals(currency)
    # KiX storage convention: ``amount_cents`` is ALWAYS the major unit
    # × 100, regardless of whether the currency itself supports decimals.
    # This matches the wider codebase (``app/routers/user_wallet.py``,
    # ``transactions.py`` etc. — all amounts are integer cents). For
    # zero-decimal currencies we render without fractional digits, but
    # the scaling factor is still 100.
    major: Decimal = Decimal(amount_cents) / Decimal(100)

    # ``currency_digits=False`` lets us pin the fractional digits via the
    # format spec; otherwise Babel uses CLDR (which says IDR has 2).
    if decimals == 0:
        fmt_spec = "¤#,##0"
    else:
        fmt_spec = "¤#,##0." + ("0" * decimals)

    rendered = _babel_format_currency(
        major,
        currency,
        format=fmt_spec,
        locale=babel_locale,
        currency_digits=False,
        format_type="standard",
    )

    forced = _FORCED_SYMBOLS.get(currency)
    if forced is not None:
        # Babel emits either the symbol or the ISO code depending on the
        # locale. Normalise both cases to the forced KiX prefix so the
        # currency is never ambiguous (``$`` shared by 30+ countries).
        from babel.numbers import get_currency_symbol
        natural_sym = get_currency_symbol(currency, locale=babel_locale)
        if natural_sym and natural_sym in rendered and natural_sym != forced:
            rendered = rendered.replace(natural_sym, forced, 1)
        elif currency in rendered:
            rendered = rendered.replace(currency, forced, 1)
    return rendered


def format_number(n: float | int | Decimal, locale: str | None = None) -> str:
    """Render a plain number using the locale's grouping/decimal conventions."""
    return format_decimal(n, locale=_normalise_locale(locale))


def format_date(
    dt: date | datetime,
    locale: str | None = None,
    style: str = "medium",
) -> str:
    """Render a date using CLDR's `short` / `medium` / `long` / `full` styles."""
    return _babel_format_date(dt, format=style, locale=_normalise_locale(locale))


def format_datetime(
    dt: datetime,
    locale: str | None = None,
    tz: Any = None,
    style: str = "medium",
) -> str:
    """Render a datetime; ``tz`` accepts an IANA name or a tzinfo instance."""
    tz_arg: Any = tz
    if isinstance(tz, str):
        try:
            from zoneinfo import ZoneInfo
            tz_arg = ZoneInfo(tz)
        except Exception:  # pragma: no cover — invalid tz strings
            tz_arg = None
    return _babel_format_datetime(
        dt,
        format=style,
        tzinfo=tz_arg,
        locale=_normalise_locale(locale),
    )


def format_percent(p: float, locale: str | None = None) -> str:
    """Render ``p`` (0.0..1.0) as a localised percent string."""
    return _babel_format_percent(p, locale=_normalise_locale(locale))


__all__ = [
    "format_currency",
    "format_number",
    "format_date",
    "format_datetime",
    "format_percent",
]
