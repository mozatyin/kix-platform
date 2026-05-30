"""Storage-shape helpers for phone + address fields.

The point of these helpers is *not* to write to a DB — schema migrations
are owned elsewhere. We define the canonical wire shapes here so that
every router/service writes consistently before migrations land.

Phone:
    Always stored as **E.164**. Period. Raw user input never enters the
    database — :func:`phone_to_storage` is the only path.

Address:
    Stored as JSONB with a stable envelope::

        {
          "country": "SG",                # ISO alpha-2 (canonical)
          "fields": {block: ..., street: ..., ...},  # canonical names only
          "formatted": "Block 123 ...\\nSingapore 654321\\nSingapore",
          "version": 1
        }

    ``country`` is always the canonical alpha-2 (callers can pass names
    or aliases — we resolve them here). ``fields`` carries only fields
    whose names appear in the country's spec; unknown keys are dropped
    so storage doesn't accidentally accumulate junk shape over time.
"""

from __future__ import annotations

from typing import Any

from app.i18n_validators.address import (
    format_address,
    get_country_address_spec,
)
from app.i18n_validators.country import parse_country_code
from app.i18n_validators.phone import parse_phone

__all__ = [
    "ADDRESS_SCHEMA_VERSION",
    "phone_to_storage",
    "address_to_jsonb",
    "address_from_jsonb",
]

ADDRESS_SCHEMA_VERSION = 1


def phone_to_storage(raw: str, country: str | None = None) -> str:
    """Normalise raw user phone input to canonical E.164 for storage.

    Raises :class:`PhoneValidationError` if the input cannot be parsed.
    """
    return parse_phone(raw, country_code=country)


def address_to_jsonb(
    country: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Pack an address into the canonical JSONB shape.

    * Resolves ``country`` to alpha-2 (so storing ``"Singapore"`` and
      ``"sg"`` both land as ``"SG"``).
    * Applies the country's alias map (``"zip"`` → ``"postal_code"``).
    * Drops keys that aren't in the spec.
    * Embeds a pre-rendered ``formatted`` line for display in lists /
      receipts without re-running the formatter.
    """
    code = parse_country_code(country)
    spec = get_country_address_spec(code)
    known = {f["name"] for f in spec["fields"]}
    aliases = spec["aliases"]

    canonical: dict[str, Any] = {}
    for k, v in (fields or {}).items():
        ck = aliases.get(k, k)
        if ck in known and v not in (None, ""):
            canonical[ck] = v.strip() if isinstance(v, str) else v

    formatted = format_address(code, canonical, style="multiline")
    return {
        "country": code,
        "fields": canonical,
        "formatted": formatted,
        "version": ADDRESS_SCHEMA_VERSION,
    }


def address_from_jsonb(data: dict[str, Any]) -> dict[str, Any]:
    """Reverse :func:`address_to_jsonb`.

    Returns a dict with ``country``, ``fields``, and a freshly
    rendered ``formatted`` line. We re-format on read because the
    pre-rendered line in storage may pre-date a formatter change.
    """
    if not isinstance(data, dict):
        raise ValueError("address_from_jsonb expects a dict")

    country = data.get("country") or ""
    fields = data.get("fields") or {}
    code = parse_country_code(country)
    formatted = format_address(code, fields, style="multiline")
    return {
        "country": code,
        "fields": dict(fields),
        "formatted": formatted,
        "version": int(data.get("version", ADDRESS_SCHEMA_VERSION)),
    }
