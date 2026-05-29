"""Read-only HTTP API over the regional payment-method registry.

Endpoints (mounted at ``/api/v1/payments`` by main.py):

* ``GET  /methods?country=sg``           — list methods in a country
* ``GET  /methods?currency=SGD``         — list methods accepting a currency
* ``GET  /method/{code}``                — full details for one method
* ``GET  /recommend?country=…&amount_cents=…&currency=…&user_pref=…``
* ``GET  /matrix``                       — country × method capability grid

The router does NO writes — the registry lives in code (versioned,
reviewed in PRs) not in a database. If a checkout flow needs to
override fees at runtime that belongs to a separate fees-engine,
not here.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status

from app.payments_regional import (
    PaymentMethod,
    all_methods,
    calculate_fee,
    get_method,
    get_methods_for_country,
    get_methods_for_currency,
    matrix,
    recommend_method,
)
from app.payments_regional.settlement import get_settlement_currency

router = APIRouter()


def _to_dict(m: PaymentMethod) -> dict:
    """Dataclass → JSON-safe dict (lists already serialise)."""
    return asdict(m)


@router.get("/methods")
async def list_methods(
    country: Optional[str] = Query(None, min_length=2, max_length=2),
    currency: Optional[str] = Query(None, min_length=3, max_length=3),
) -> dict:
    """List methods by country and/or currency.

    Both filters may be combined (intersection). If neither is
    given we return the full catalog — handy for admin tools, not
    a typical consumer call.
    """
    if country and currency:
        # AND-filter: must be in both sets
        in_country = {m.code for m in get_methods_for_country(country)}
        results = [
            m for m in get_methods_for_currency(currency) if m.code in in_country
        ]
    elif country:
        results = get_methods_for_country(country)
    elif currency:
        results = get_methods_for_currency(currency)
    else:
        results = all_methods()

    return {
        "count": len(results),
        "methods": [_to_dict(m) for m in results],
    }


@router.get("/method/{code}")
async def method_detail(code: str) -> dict:
    """Full details for a single method, by code."""
    m = get_method(code)
    if m is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"payment method {code!r} not found",
        )
    return _to_dict(m)


@router.get("/recommend")
async def recommend(
    country: str = Query(..., min_length=2, max_length=2),
    amount_cents: int = Query(..., ge=0),
    currency: str = Query(..., min_length=3, max_length=3),
    user_pref: Optional[str] = Query(None),
) -> dict:
    """Recommend a single payment method for this checkout.

    Returns ``{"method": {...}, "fee_cents": …, "net_cents": …,
    "settlement_currency": "…"}`` or HTTP 404 if no method fits.
    """
    m = recommend_method(country, amount_cents, currency, user_pref=user_pref)
    if m is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"no payment method available for "
                f"country={country!r} currency={currency!r}"
            ),
        )
    fee_cents, net_cents = calculate_fee(m.code, amount_cents)
    settles = get_settlement_currency(country, m.code)
    return {
        "method": _to_dict(m),
        "fee_cents": fee_cents,
        "net_cents": net_cents,
        "settlement_currency": settles,
    }


@router.get("/matrix")
async def capability_matrix() -> dict:
    """Country × method codes table. JSON: ``{"SG": ["paynow",...]}``."""
    return {"matrix": matrix()}


__all__ = ["router"]
