"""HTTP endpoints for the i18n format-validator library.

Thin, deterministic FastAPI surface over :mod:`app.i18n_validators`.
No DB. No LLM. No region/country gating. Pure format helpers an SDK or
front-end can hit to validate user input before persistence.

Endpoints
---------

* ``POST /api/v1/validate/phone``
* ``POST /api/v1/validate/address``
* ``GET  /api/v1/i18n/country/{code}``
* ``GET  /api/v1/i18n/countries``
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.i18n_validators.address import (
    format_address,
    get_country_address_spec,
    list_supported_countries,
    validate_address,
)
from app.i18n_validators.country import (
    CountryLookupError,
    get_country_calling_code,
    get_country_currency_default,
    get_country_locale_default,
    list_countries,
    parse_country_code,
)
from app.i18n_validators.phone import (
    PhoneValidationError,
    get_country_for_phone,
    mask_phone,
    parse_phone,
)

router = APIRouter()


# ── Schemas ────────────────────────────────────────────────────────────


class PhoneRequest(BaseModel):
    phone: str = Field(..., min_length=1, max_length=64)
    country: str | None = Field(
        None,
        description=(
            "Optional ISO 3166-1 alpha-2 country hint. Required when "
            "the phone is supplied without a leading '+'."
        ),
        max_length=4,
    )


class PhoneResponse(BaseModel):
    valid: bool
    e164: str | None = None
    country: str | None = None
    masked: str | None = None
    error: dict | None = None


class AddressRequest(BaseModel):
    country: str = Field(..., min_length=2, max_length=64)
    fields: dict[str, Any] = Field(default_factory=dict)
    style: str = Field(
        "multiline",
        description="format style: multiline | single | postal",
    )


class AddressResponse(BaseModel):
    valid: bool
    country: str
    errors: list[dict] = []
    formatted: str | None = None


# ── Endpoints ──────────────────────────────────────────────────────────


@router.post("/validate/phone", response_model=PhoneResponse)
def http_validate_phone(body: PhoneRequest) -> PhoneResponse:
    """Validate a phone number, returning canonical E.164 + country.

    Never raises HTTP 4xx for "merely invalid" input — the response
    envelope carries the structured error so callers can render it.
    """
    try:
        e164 = parse_phone(body.phone, country_code=body.country)
    except PhoneValidationError as exc:
        return PhoneResponse(valid=False, error=exc.to_dict())

    try:
        country = get_country_for_phone(e164)
    except PhoneValidationError:
        country = None
    try:
        masked = mask_phone(e164)
    except PhoneValidationError:
        masked = None
    return PhoneResponse(
        valid=True, e164=e164, country=country, masked=masked
    )


@router.post("/validate/address", response_model=AddressResponse)
def http_validate_address(body: AddressRequest) -> AddressResponse:
    """Per-country address validation + canonical formatted preview."""
    try:
        code = parse_country_code(body.country)
    except CountryLookupError as exc:
        return AddressResponse(
            valid=False, country=body.country, errors=[exc.to_dict()],
        )

    ok, errors = validate_address(code, body.fields)
    formatted: str | None
    try:
        formatted = format_address(code, body.fields, style=body.style)
    except Exception:  # noqa: BLE001 — never let formatting fail validation
        formatted = None
    return AddressResponse(
        valid=ok, country=code, errors=errors, formatted=formatted,
    )


@router.get("/i18n/country/{code}")
def http_country_info(code: str) -> dict:
    """Country metadata + address spec (if registered)."""
    try:
        canonical = parse_country_code(code)
    except CountryLookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=exc.to_dict(),
        ) from exc

    info: dict = {
        "code": canonical,
        "calling_code": get_country_calling_code(canonical),
        "locale": get_country_locale_default(canonical),
        "currency": get_country_currency_default(canonical),
    }
    if canonical in list_supported_countries():
        info["address"] = get_country_address_spec(canonical)
    return info


@router.get("/i18n/countries")
def http_countries() -> dict:
    """All registered countries; ``address_supported`` flags spec presence."""
    supported = set(list_supported_countries())
    out = []
    for c in list_countries():
        c["address_supported"] = c["code"] in supported
        out.append(c)
    return {"countries": out, "count": len(out)}
