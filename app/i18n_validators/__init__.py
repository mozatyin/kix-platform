"""Per-country phone (E.164) and address validators for KiX Platform.

This module is intentionally narrow: it does NOT attempt a universal
address schema. Address shapes vary too wildly between countries
(SG block/unit, JP postal-first, ID kelurahan/kecamatan, BR CEP).
Instead we expose per-country builders + validators and pin the
"shape" of an address record to the country that produced it.

Public surface
==============

phone
-----
* ``parse_phone(raw, country_code=None) -> str`` (E.164)
* ``is_valid_phone(raw, country_code=None) -> bool``
* ``format_phone(e164, format="international") -> str``
* ``get_country_for_phone(e164) -> str``
* ``mask_phone(e164) -> str``

address
-------
* ``validate_address(country, fields) -> (bool, [errors])``
* ``format_address(country, fields, line_style="multiline") -> str``
* ``get_required_fields(country) -> [str]``
* ``get_field_order(country) -> [str]``

country
-------
* ``parse_country_code(raw) -> str``  (handles "Singapore" / "sg" / "🇸🇬")
* ``get_country_locale_default(country) -> str``
* ``get_country_currency_default(country) -> str``
* ``get_country_calling_code(country) -> str``
* ``list_countries() -> [{code, name, ...}]``

storage
-------
* ``phone_to_storage(raw, country) -> str``  (always E.164)
* ``address_to_jsonb(country, fields) -> dict``  (stable shape per country)
* ``address_from_jsonb(data) -> dict``  (reverse + ``formatted`` field)

Design notes
============

* No DB writes here — only **shape** helpers. Schema migrations are
  owned by Agent 7. Storage helpers exist so callers can persist
  consistently before migrations land.
* No LLM calls. Pure deterministic validation. The strategy doc
  explicitly defers phone/address to libphonenumber + per-country rules.
* This module is **additive**. It does not touch ``app/i18n/`` (Agents
  1+3), ``app/payments_regional/`` (parallel agent), or
  ``app/email_templates/``.
"""

from __future__ import annotations

from app.i18n_validators.address import (
    format_address,
    get_field_order,
    get_required_fields,
    validate_address,
)
from app.i18n_validators.country import (
    get_country_calling_code,
    get_country_currency_default,
    get_country_locale_default,
    list_countries,
    parse_country_code,
)
from app.i18n_validators.phone import (
    format_phone,
    get_country_for_phone,
    is_valid_phone,
    mask_phone,
    parse_phone,
)
from app.i18n_validators.storage import (
    address_from_jsonb,
    address_to_jsonb,
    phone_to_storage,
)

__all__ = [
    # phone
    "parse_phone",
    "is_valid_phone",
    "format_phone",
    "get_country_for_phone",
    "mask_phone",
    # address
    "validate_address",
    "format_address",
    "get_required_fields",
    "get_field_order",
    # country
    "parse_country_code",
    "get_country_locale_default",
    "get_country_currency_default",
    "get_country_calling_code",
    "list_countries",
    # storage
    "phone_to_storage",
    "address_to_jsonb",
    "address_from_jsonb",
]
