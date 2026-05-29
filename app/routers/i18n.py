"""Debug endpoints for the i18n formatting layer.

Frontend engineers regularly need to know "what would Babel render for
this (amount, currency, locale) tuple?". Rather than ship a
Python-execution playground we expose a tiny readonly endpoint that
mirrors :func:`app.i18n.formatting.format_currency`.

Mounted at ``/api/v1/i18n/`` in :mod:`app.main`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query

from app.i18n.context import get_current_locale
from app.i18n.currency import (
    currency_decimals,
    get_primary_currency,
    get_supported_currencies,
    is_valid_currency,
)
from app.i18n.formatting import (
    format_currency,
    format_date,
    format_datetime,
    format_number,
    format_percent,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/format")
async def format_debug(
    amount_cents: int = Query(..., description="Integer minor units (×100)"),
    currency: str = Query(..., min_length=3, max_length=8),
    locale: str | None = Query(None, description="BCP 47 tag (en-SG, zh-Hans-CN, …)"),
) -> dict[str, Any]:
    """Render an ``(amount, currency, locale)`` tuple for frontend debugging.

    Example:
        ``GET /api/v1/i18n/format?amount_cents=10000&currency=SGD&locale=en-SG``
        → ``{"formatted": "S$100.00", "currency": "SGD", "locale": "en-SG"}``
    """
    active_locale = locale or get_current_locale()
    formatted = format_currency(amount_cents, currency, active_locale)
    return {
        "formatted": formatted,
        "currency": currency.upper(),
        "locale": active_locale,
        "amount_cents": amount_cents,
        "decimals": currency_decimals(currency),
        "valid_iso": is_valid_currency(currency),
    }


@router.get("/preview")
async def preview_locale(
    locale: str | None = Query(None),
    region: str | None = Query(None),
) -> dict[str, Any]:
    """Return a rendered sample of every formatter for ``locale``.

    Handy when QA-ing a new locale catalog — one request shows currency,
    number, date, datetime and percent rendering side by side.
    """
    active_locale = locale or get_current_locale()
    primary = get_primary_currency(region)
    now = datetime.now()
    return {
        "locale": active_locale,
        "region": region,
        "primary_currency": primary,
        "supported_currencies": get_supported_currencies(region),
        "samples": {
            "currency_primary": format_currency(10000, primary, active_locale),
            "currency_usd":     format_currency(10000, "USD", active_locale),
            "currency_jpy":     format_currency(150000, "JPY", active_locale),
            "currency_idr":     format_currency(150000, "IDR", active_locale),
            "number":           format_number(1234567.89, active_locale),
            "date":             format_date(now.date(), active_locale),
            "datetime":         format_datetime(now, active_locale),
            "percent":          format_percent(0.456, active_locale),
        },
    }
