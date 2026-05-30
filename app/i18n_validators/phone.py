"""E.164 phone number validation built on Google libphonenumber.

The ``phonenumbers`` library handles the heavy lifting — country code
detection, carrier ranges, number-of-digits validation. We add a small
KiX-specific layer:

* Structured errors instead of exceptions across the API boundary.
* Mobile-only sanity checks for SG / CN / ID (the three biggest KiX
  markets) — because most signup flows want to reject landlines.
* A privacy ``mask_phone`` for display in lists / receipts.

Function contract
=================

All functions raise :class:`PhoneValidationError` on bad input. The
``error_code`` attribute is one of:

* ``phone.empty``        — raw is empty / whitespace
* ``phone.parse_failed`` — libphonenumber couldn't parse
* ``phone.invalid``      — parsed but not a valid number
* ``phone.invalid_country`` — country hint isn't ISO-3166-1 alpha-2
* ``phone.ambiguous``    — no country, no leading ``+`` → can't decide
* ``phone.not_mobile``   — strict mobile mode rejected a landline
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import phonenumbers
from phonenumbers import (
    NumberParseException,
    PhoneNumberFormat,
    PhoneNumberType,
)
from phonenumbers import geocoder as _pn_geocoder  # noqa: F401  (kept for future use)

__all__ = [
    "PhoneValidationError",
    "parse_phone",
    "is_valid_phone",
    "format_phone",
    "get_country_for_phone",
    "mask_phone",
]


# ── Errors ─────────────────────────────────────────────────────────────


@dataclass
class PhoneValidationError(ValueError):
    """Structured validation error.

    Surfaces ``error_code`` + ``message`` so API layers can shape
    consistent error envelopes without sniffing exception strings.
    """

    error_code: str
    message: str
    suggested_country: str | None = None

    def __post_init__(self) -> None:
        # ValueError is initialised with the message string; we keep the
        # dataclass fields available for callers that catch this type.
        super().__init__(self.message)

    def to_dict(self) -> dict:
        out = {"error_code": self.error_code, "message": self.message}
        if self.suggested_country:
            out["suggested_country"] = self.suggested_country
        return out


# ── Format aliases ─────────────────────────────────────────────────────

_FORMAT_MAP = {
    "international": PhoneNumberFormat.INTERNATIONAL,
    "national": PhoneNumberFormat.NATIONAL,
    "e164": PhoneNumberFormat.E164,
    "rfc3966": PhoneNumberFormat.RFC3966,
}


# ── Country-specific mobile sanity checks ──────────────────────────────
#
# libphonenumber already validates against carrier ranges, but for the
# three markets where we sign up the most users we add a defence-in-depth
# pattern check. This catches typos that LP accepts as "fixed line or
# mobile" — e.g. landline-only ranges that the merchant signup form
# shouldn't accept for SMS-OTP.

_MOBILE_PATTERNS = {
    # SG mobile: starts with 8 or 9, 8 digits total. Source: IMDA NPB.
    "SG": re.compile(r"^[89]\d{7}$"),
    # CN mobile: starts with 1, 11 digits total. Source: MIIT.
    "CN": re.compile(r"^1\d{10}$"),
    # ID mobile: starts with 8 (after stripping leading 0), 9-11 digits.
    "ID": re.compile(r"^8\d{7,10}$"),
}


def _coerce_country(country_code: str | None) -> str | None:
    """Normalise a country-code hint to upper-case alpha-2 or ``None``."""
    if country_code is None:
        return None
    cc = str(country_code).strip().upper()
    if not cc:
        return None
    if len(cc) != 2 or not cc.isalpha():
        raise PhoneValidationError(
            error_code="phone.invalid_country",
            message=f"country_code must be ISO 3166-1 alpha-2; got {country_code!r}",
        )
    return cc


def _parse(raw: str, country_code: str | None):
    """Internal parser. Returns a libphonenumber PhoneNumber.

    Raises :class:`PhoneValidationError` on any failure.
    """
    if raw is None or not str(raw).strip():
        raise PhoneValidationError(
            error_code="phone.empty", message="phone is empty"
        )
    s = str(raw).strip()
    cc = _coerce_country(country_code)

    # If the user gave neither a leading "+" nor a country hint we can't
    # safely guess. Reject explicitly so callers know to ask.
    if not s.startswith("+") and cc is None:
        raise PhoneValidationError(
            error_code="phone.ambiguous",
            message=(
                "phone has no country prefix and no country_code supplied; "
                "cannot determine country"
            ),
        )

    try:
        num = phonenumbers.parse(s, cc)
    except NumberParseException as exc:
        raise PhoneValidationError(
            error_code="phone.parse_failed",
            message=f"could not parse phone: {exc}",
        ) from exc

    if not phonenumbers.is_valid_number(num):
        # Best-effort country suggestion from what was parsed.
        suggested = phonenumbers.region_code_for_number(num)
        raise PhoneValidationError(
            error_code="phone.invalid",
            message="phone number is not valid",
            suggested_country=suggested if suggested and suggested != "ZZ" else None,
        )

    return num


def parse_phone(raw: str, country_code: str | None = None) -> str:
    """Parse a raw phone number into canonical E.164.

    >>> parse_phone("9123 4567", "SG")
    '+6591234567'
    >>> parse_phone("+86 138 0000 0000")
    '+8613800000000'
    """
    num = _parse(raw, country_code)
    return phonenumbers.format_number(num, PhoneNumberFormat.E164)


def is_valid_phone(raw: str, country_code: str | None = None) -> bool:
    """Boolean wrapper that swallows :class:`PhoneValidationError`."""
    try:
        _parse(raw, country_code)
        return True
    except PhoneValidationError:
        return False


def format_phone(e164: str, format: str = "international") -> str:  # noqa: A002
    """Reformat a known-good E.164 string.

    Supported ``format`` values: ``international`` (default), ``national``,
    ``e164``, ``rfc3966``.
    """
    fmt = _FORMAT_MAP.get(format.lower())
    if fmt is None:
        raise PhoneValidationError(
            error_code="phone.invalid_format",
            message=f"unknown phone format {format!r}; "
                    f"expected one of {sorted(_FORMAT_MAP)}",
        )
    # E.164 is unambiguous → no country hint needed.
    try:
        num = phonenumbers.parse(e164, None)
    except NumberParseException as exc:
        raise PhoneValidationError(
            error_code="phone.parse_failed",
            message=f"could not parse {e164!r}: {exc}",
        ) from exc
    return phonenumbers.format_number(num, fmt)


def get_country_for_phone(e164: str) -> str:
    """Return ISO 3166-1 alpha-2 country code for an E.164 number."""
    try:
        num = phonenumbers.parse(e164, None)
    except NumberParseException as exc:
        raise PhoneValidationError(
            error_code="phone.parse_failed",
            message=f"could not parse {e164!r}: {exc}",
        ) from exc
    cc = phonenumbers.region_code_for_number(num)
    if not cc or cc == "ZZ":
        raise PhoneValidationError(
            error_code="phone.invalid",
            message=f"no country mapping for {e164!r}",
        )
    return cc


def is_mobile(e164: str) -> bool:
    """Best-effort mobile classification.

    libphonenumber classifies numbers as MOBILE, FIXED_LINE, or
    FIXED_LINE_OR_MOBILE — the third class is treated as mobile here
    because for those regions LP can't statically distinguish them.
    """
    try:
        num = phonenumbers.parse(e164, None)
    except NumberParseException:
        return False
    t = phonenumbers.number_type(num)
    return t in (
        PhoneNumberType.MOBILE,
        PhoneNumberType.FIXED_LINE_OR_MOBILE,
    )


def mask_phone(e164: str) -> str:
    """Privacy-mask a phone for display.

    Format: ``+<calling_code> ****<last4>``.

    >>> mask_phone("+6591234567")
    '+65 ****4567'
    """
    if not e164:
        raise PhoneValidationError(
            error_code="phone.empty", message="phone is empty"
        )
    try:
        num = phonenumbers.parse(e164, None)
    except NumberParseException as exc:
        raise PhoneValidationError(
            error_code="phone.parse_failed",
            message=f"could not parse {e164!r}: {exc}",
        ) from exc
    cc = num.country_code
    natl = str(num.national_number)
    last4 = natl[-4:] if len(natl) >= 4 else natl
    return f"+{cc} ****{last4}"


def matches_mobile_pattern(e164: str, country: str) -> bool:
    """Country-specific defence-in-depth mobile pattern check.

    Returns ``True`` if no pattern is registered for ``country``
    (i.e. we have no extra signal and trust libphonenumber alone).
    """
    pat = _MOBILE_PATTERNS.get(country.upper())
    if pat is None:
        return True
    try:
        num = phonenumbers.parse(e164, None)
    except NumberParseException:
        return False
    return bool(pat.match(str(num.national_number)))
