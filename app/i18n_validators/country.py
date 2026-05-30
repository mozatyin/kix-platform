"""Country metadata helpers — ISO 3166-1 alpha-2 lookups.

Pure format/code library. The data here is well-known public
information — no demographics, no inference, no LLM. Every field is
either a hard ISO standard (alpha-2, alpha-3, calling code) or a
sensible default surfaced from CLDR-style assumptions (default locale,
default currency).

Public surface
==============

* ``parse_country_code(raw) -> str``  — accepts:
    * ISO alpha-2  ("SG", "us")
    * ISO alpha-3  ("SGP", "USA")
    * Full / common English name ("Singapore", "United States")
    * Regional Indicator emoji flag ("🇸🇬", "🇺🇸")
* ``get_country_locale_default(country) -> str``
* ``get_country_currency_default(country) -> str``
* ``get_country_calling_code(country) -> str``
* ``list_countries() -> list[dict]``

Error contract
--------------

``parse_country_code`` raises :class:`CountryLookupError` on unknown
input. The lookup helpers raise the same error when an ISO alpha-2 is
not in the registry.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CountryLookupError",
    "parse_country_code",
    "get_country_locale_default",
    "get_country_currency_default",
    "get_country_calling_code",
    "list_countries",
]


@dataclass
class CountryLookupError(ValueError):
    """Raised when a country cannot be resolved."""

    error_code: str
    message: str

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {"error_code": self.error_code, "message": self.message}


# ── Country registry ──────────────────────────────────────────────────
#
# Minimum required set per task: SG, CN, US, GB, JP, ID, TH, IN, BR, AU.
# We include a broader practical set for the KiX network's likely reach;
# all entries are pure ISO metadata.

_COUNTRIES: dict[str, dict] = {
    "SG": {"name": "Singapore", "alpha3": "SGP", "calling": "65",
           "locale": "en-SG", "currency": "SGD"},
    "CN": {"name": "China", "alpha3": "CHN", "calling": "86",
           "locale": "zh-CN", "currency": "CNY"},
    "US": {"name": "United States", "alpha3": "USA", "calling": "1",
           "locale": "en-US", "currency": "USD"},
    "GB": {"name": "United Kingdom", "alpha3": "GBR", "calling": "44",
           "locale": "en-GB", "currency": "GBP"},
    "JP": {"name": "Japan", "alpha3": "JPN", "calling": "81",
           "locale": "ja-JP", "currency": "JPY"},
    "ID": {"name": "Indonesia", "alpha3": "IDN", "calling": "62",
           "locale": "id-ID", "currency": "IDR"},
    "TH": {"name": "Thailand", "alpha3": "THA", "calling": "66",
           "locale": "th-TH", "currency": "THB"},
    "IN": {"name": "India", "alpha3": "IND", "calling": "91",
           "locale": "en-IN", "currency": "INR"},
    "BR": {"name": "Brazil", "alpha3": "BRA", "calling": "55",
           "locale": "pt-BR", "currency": "BRL"},
    "AU": {"name": "Australia", "alpha3": "AUS", "calling": "61",
           "locale": "en-AU", "currency": "AUD"},
    "MY": {"name": "Malaysia", "alpha3": "MYS", "calling": "60",
           "locale": "ms-MY", "currency": "MYR"},
    "PH": {"name": "Philippines", "alpha3": "PHL", "calling": "63",
           "locale": "en-PH", "currency": "PHP"},
    "VN": {"name": "Vietnam", "alpha3": "VNM", "calling": "84",
           "locale": "vi-VN", "currency": "VND"},
    "KR": {"name": "South Korea", "alpha3": "KOR", "calling": "82",
           "locale": "ko-KR", "currency": "KRW"},
    "HK": {"name": "Hong Kong", "alpha3": "HKG", "calling": "852",
           "locale": "zh-HK", "currency": "HKD"},
    "TW": {"name": "Taiwan", "alpha3": "TWN", "calling": "886",
           "locale": "zh-TW", "currency": "TWD"},
    "FR": {"name": "France", "alpha3": "FRA", "calling": "33",
           "locale": "fr-FR", "currency": "EUR"},
    "DE": {"name": "Germany", "alpha3": "DEU", "calling": "49",
           "locale": "de-DE", "currency": "EUR"},
    "CA": {"name": "Canada", "alpha3": "CAN", "calling": "1",
           "locale": "en-CA", "currency": "CAD"},
    "NZ": {"name": "New Zealand", "alpha3": "NZL", "calling": "64",
           "locale": "en-NZ", "currency": "NZD"},
    "AE": {"name": "United Arab Emirates", "alpha3": "ARE",
           "calling": "971", "locale": "ar-AE", "currency": "AED"},
}


# Reverse indexes built once at import-time.
_BY_ALPHA3 = {v["alpha3"]: k for k, v in _COUNTRIES.items()}
_BY_NAME = {v["name"].lower(): k for k, v in _COUNTRIES.items()}

# Common name aliases (lowercased) → alpha-2.
_NAME_ALIASES = {
    "usa": "US",
    "u.s.a.": "US",
    "u.s.": "US",
    "america": "US",
    "uk": "GB",
    "u.k.": "GB",
    "britain": "GB",
    "great britain": "GB",
    "england": "GB",
    "prc": "CN",
    "mainland china": "CN",
    "viet nam": "VN",
    "korea": "KR",
    "south korea": "KR",
    "republic of korea": "KR",
    "hk": "HK",
    "hong kong sar": "HK",
    "uae": "AE",
}


# ── Emoji flag decoder ────────────────────────────────────────────────
#
# Unicode regional-indicator letters live in U+1F1E6 ('A') .. U+1F1FF ('Z').
# A flag is just two of those in sequence, encoding the alpha-2 directly.

_RI_BASE = 0x1F1E6  # 'A'


def _decode_flag(s: str) -> str | None:
    """Return alpha-2 if ``s`` is a 2-codepoint regional-indicator flag."""
    cps = [ord(c) for c in s]
    if len(cps) != 2:
        return None
    a, b = cps
    if not (_RI_BASE <= a <= _RI_BASE + 25 and _RI_BASE <= b <= _RI_BASE + 25):
        return None
    return chr(ord("A") + (a - _RI_BASE)) + chr(ord("A") + (b - _RI_BASE))


# ── Public helpers ────────────────────────────────────────────────────


def parse_country_code(raw: str | None) -> str:
    """Resolve free-form input to canonical ISO alpha-2 (upper-case).

    Order of attempts (cheapest first):
      1. Already a 2-letter alpha-2.
      2. 3-letter alpha-3.
      3. Common alias (lowercased lookup).
      4. Exact country name (lowercased).
      5. Regional-indicator emoji flag.
    """
    if raw is None or not str(raw).strip():
        raise CountryLookupError(
            error_code="country.empty", message="country code is empty"
        )
    s = str(raw).strip()

    # 1. alpha-2
    if len(s) == 2 and s.isalpha():
        code = s.upper()
        if code in _COUNTRIES:
            return code

    # 2. alpha-3
    if len(s) == 3 and s.isalpha():
        code = s.upper()
        if code in _BY_ALPHA3:
            return _BY_ALPHA3[code]

    # 3. alias
    lowered = s.lower()
    if lowered in _NAME_ALIASES:
        return _NAME_ALIASES[lowered]

    # 4. full name
    if lowered in _BY_NAME:
        return _BY_NAME[lowered]

    # 5. emoji flag
    flag_code = _decode_flag(s)
    if flag_code and flag_code in _COUNTRIES:
        return flag_code

    raise CountryLookupError(
        error_code="country.unknown",
        message=f"could not resolve country {raw!r} to ISO alpha-2",
    )


def _require(country: str) -> dict:
    code = parse_country_code(country)
    return _COUNTRIES[code]


def get_country_locale_default(country: str) -> str:
    """Default CLDR-style locale (e.g. ``en-SG``) for a country."""
    return _require(country)["locale"]


def get_country_currency_default(country: str) -> str:
    """Default ISO-4217 currency code for a country."""
    return _require(country)["currency"]


def get_country_calling_code(country: str) -> str:
    """E.164 calling-code prefix (no leading ``+``)."""
    return _require(country)["calling"]


def list_countries() -> list[dict]:
    """All registered countries as a stable list of dicts."""
    return [
        {
            "code": code,
            "name": data["name"],
            "alpha3": data["alpha3"],
            "calling_code": data["calling"],
            "locale": data["locale"],
            "currency": data["currency"],
        }
        for code, data in sorted(_COUNTRIES.items())
    ]
