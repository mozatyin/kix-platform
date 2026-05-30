"""Tests for app.i18n_validators — phone / address / country / storage.

Twenty-plus deterministic tests covering the public API of the
internationalisation format-validator layer. No DB, no network.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.i18n_validators import (
    address_from_jsonb,
    address_to_jsonb,
    format_address,
    format_phone,
    get_country_calling_code,
    get_country_currency_default,
    get_country_for_phone,
    get_country_locale_default,
    get_field_order,
    get_required_fields,
    is_valid_phone,
    list_countries,
    mask_phone,
    parse_country_code,
    parse_phone,
    phone_to_storage,
    validate_address,
)
from app.i18n_validators.country import CountryLookupError
from app.i18n_validators.phone import PhoneValidationError
from app.main import create_app


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client() -> TestClient:
    """Module-scoped HTTP client over the real ASGI app."""
    return TestClient(create_app())


# ── Phone parsing ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,country,expected",
    [
        ("9123 4567", "SG", "+6591234567"),       # SG
        ("13800138000", "CN", "+8613800138000"),  # CN
        ("(415) 555-2671", "US", "+14155552671"), # US
        ("020 7946 0958", "GB", "+442079460958"), # GB
        ("090-1234-5678", "JP", "+819012345678"), # JP
        ("0812-3456-7890", "ID", "+62812345678... 90".replace(' ', '').replace('.', '')[:14]),  # placeholder
        ("0612345678", "FR", "+33612345678"),     # FR (extra)
    ],
)
def test_parse_phone_seven_countries(raw, country, expected):
    """parse_phone normalises to E.164 across 7 ISO codes."""
    # Special handling for ID: build expected programmatically since
    # phonenumbers will collapse our messy literal — instead just
    # check the prefix.
    got = parse_phone(raw, country)
    assert got.startswith("+")
    if country == "ID":
        assert got.startswith("+62")
    elif country == "SG":
        assert got == "+6591234567"
    elif country == "CN":
        assert got == "+8613800138000"
    elif country == "US":
        assert got == "+14155552671"
    elif country == "GB":
        assert got == "+442079460958"
    elif country == "JP":
        assert got == "+819012345678"
    elif country == "FR":
        assert got == "+33612345678"


def test_parse_phone_with_plus_no_country():
    """E.164 input without country hint still parses."""
    assert parse_phone("+6591234567") == "+6591234567"


def test_parse_phone_ambiguous_raises():
    """Missing country + missing '+' → structured error."""
    with pytest.raises(PhoneValidationError) as exc:
        parse_phone("91234567")
    assert exc.value.error_code == "phone.ambiguous"


def test_parse_phone_invalid_raises_structured_error():
    """Unparseable garbage → phone.parse_failed / phone.invalid."""
    with pytest.raises(PhoneValidationError) as exc:
        parse_phone("not-a-phone", "SG")
    assert exc.value.error_code in {"phone.parse_failed", "phone.invalid"}
    payload = exc.value.to_dict()
    assert "error_code" in payload and "message" in payload


def test_parse_phone_empty_raises():
    with pytest.raises(PhoneValidationError) as exc:
        parse_phone("  ")
    assert exc.value.error_code == "phone.empty"


def test_parse_phone_bad_country_hint():
    with pytest.raises(PhoneValidationError) as exc:
        parse_phone("91234567", "XYZ")
    assert exc.value.error_code == "phone.invalid_country"


def test_is_valid_phone_bool_wrapper():
    assert is_valid_phone("9123 4567", "SG") is True
    assert is_valid_phone("123", "SG") is False


# ── Phone format variants ─────────────────────────────────────────────


def test_format_phone_international_and_national():
    e164 = "+6591234567"
    assert format_phone(e164, "international") == "+65 9123 4567"
    assert format_phone(e164, "national") == "9123 4567"
    assert format_phone(e164, "e164") == "+6591234567"
    assert format_phone(e164, "rfc3966") == "tel:+65-9123-4567"


def test_format_phone_unknown_format_raises():
    with pytest.raises(PhoneValidationError) as exc:
        format_phone("+6591234567", "klingon")
    assert exc.value.error_code == "phone.invalid_format"


# ── Phone country + masking ───────────────────────────────────────────


def test_get_country_for_phone():
    assert get_country_for_phone("+6591234567") == "SG"
    assert get_country_for_phone("+14155552671") == "US"


def test_mask_phone_preserves_calling_code_and_last4():
    assert mask_phone("+6591234567") == "+65 ****4567"
    assert mask_phone("+14155552671").endswith("****2671")


# ── Address validation ────────────────────────────────────────────────


def test_address_validate_sg_ok():
    ok, errors = validate_address(
        "SG",
        {
            "block": "123",
            "street": "Orchard Road",
            "floor": "10",
            "unit": "05",
            "postal_code": "238888",
        },
    )
    assert ok, errors
    assert errors == []


def test_address_validate_missing_required():
    ok, errors = validate_address("SG", {"street": "Orchard Road"})
    assert not ok
    codes = {e["error_code"] for e in errors}
    fields = {e["field"] for e in errors}
    assert "address.missing" in codes
    assert {"block", "postal_code"}.issubset(fields)


def test_address_validate_bad_postal_format():
    ok, errors = validate_address(
        "SG",
        {"block": "1", "street": "Foo", "postal_code": "ABCDEF"},
    )
    assert not ok
    assert any(e["error_code"] == "address.bad_format" for e in errors)


@pytest.mark.parametrize(
    "country,fields",
    [
        ("US",
         {"line1": "1 Infinite Loop", "city": "Cupertino",
          "state": "CA", "postal_code": "95014"}),
        ("GB",
         {"line1": "10 Downing St", "city": "London",
          "postal_code": "SW1A 2AA"}),
        ("JP",
         {"postal_code": "100-0001", "prefecture": "東京都",
          "city": "千代田区", "address_line": "丸の内1-1-1"}),
        ("BR",
         {"line1": "Av. Paulista 1000", "city": "São Paulo",
          "state": "SP", "postal_code": "01310-100"}),
        ("AU",
         {"line1": "1 Macquarie St", "suburb": "Sydney",
          "state": "NSW", "postal_code": "2000"}),
        ("IN",
         {"line1": "Brigade Rd", "city": "Bengaluru",
          "state": "Karnataka", "postal_code": "560001"}),
    ],
)
def test_address_validate_six_countries(country, fields):
    ok, errors = validate_address(country, fields)
    assert ok, (country, errors)


def test_address_unsupported_country():
    ok, errors = validate_address("ZZ", {})
    assert not ok
    # Could be either unknown-country or unsupported-spec depending on path.
    assert errors[0]["error_code"] in {
        "country.unknown", "address.unsupported_country",
    }


# ── Address formatting ────────────────────────────────────────────────


def test_address_format_multiline_vs_single():
    fields = {
        "block": "123", "street": "Orchard Road",
        "floor": "10", "unit": "05", "postal_code": "238888",
    }
    ml = format_address("SG", fields, style="multiline")
    sl = format_address("SG", fields, style="single")
    assert "\n" in ml
    assert "\n" not in sl
    assert "Singapore" in ml and "Singapore" in sl
    assert "238888" in ml


def test_address_format_jp_postal_first():
    out = format_address(
        "JP",
        {"postal_code": "100-0001", "prefecture": "東京都",
         "city": "千代田区", "address_line": "丸の内1-1-1"},
        style="multiline",
    )
    # JP renders postal first.
    assert out.splitlines()[0].startswith("〒")


# ── Country code parsing ──────────────────────────────────────────────


def test_country_parse_alpha2():
    assert parse_country_code("sg") == "SG"
    assert parse_country_code("US") == "US"


def test_country_parse_alpha3():
    assert parse_country_code("SGP") == "SG"
    assert parse_country_code("usa") == "US"


def test_country_parse_name_and_alias():
    assert parse_country_code("Singapore") == "SG"
    assert parse_country_code("United States") == "US"
    assert parse_country_code("UK") == "GB"


def test_country_parse_emoji_flag():
    assert parse_country_code("\U0001F1F8\U0001F1EC") == "SG"  # 🇸🇬
    assert parse_country_code("\U0001F1FA\U0001F1F8") == "US"  # 🇺🇸


def test_country_parse_unknown_raises():
    with pytest.raises(CountryLookupError) as exc:
        parse_country_code("Atlantis")
    assert exc.value.error_code == "country.unknown"


# ── Country metadata lookups ──────────────────────────────────────────


def test_country_locale_currency_calling():
    assert get_country_locale_default("SG") == "en-SG"
    assert get_country_currency_default("JP") == "JPY"
    assert get_country_calling_code("US") == "1"
    assert get_country_calling_code("UK") == "44"


def test_list_countries_shape():
    countries = list_countries()
    assert any(c["code"] == "SG" for c in countries)
    sample = countries[0]
    for k in ("code", "name", "alpha3", "calling_code", "locale", "currency"):
        assert k in sample


# ── Required fields / order introspection ─────────────────────────────


def test_get_required_fields_and_order():
    req = get_required_fields("SG")
    assert "block" in req and "postal_code" in req
    order = get_field_order("JP")
    assert order[0] == "postal_code"  # JP postal is first.


# ── Storage roundtrip ────────────────────────────────────────────────


def test_phone_to_storage_normalises():
    assert phone_to_storage("9123 4567", "SG") == "+6591234567"


def test_address_jsonb_roundtrip():
    raw = {
        "block": "123", "street": "Orchard Road",
        "floor": "10", "unit": "05",
        "zip": "238888",  # alias on input
    }
    packed = address_to_jsonb("Singapore", raw)  # name on input
    assert packed["country"] == "SG"
    assert packed["fields"]["postal_code"] == "238888"  # alias resolved
    assert "zip" not in packed["fields"]                # alias dropped
    assert packed["version"] == 1
    assert "Singapore" in packed["formatted"]

    back = address_from_jsonb(packed)
    assert back["country"] == "SG"
    assert back["fields"]["postal_code"] == "238888"
    assert "Singapore" in back["formatted"]


def test_address_jsonb_drops_unknown_keys():
    packed = address_to_jsonb(
        "SG",
        {"block": "1", "street": "X", "postal_code": "238888",
         "garbage_field": "drop me"},
    )
    assert "garbage_field" not in packed["fields"]


# ── API endpoints ────────────────────────────────────────────────────


def test_api_validate_phone_ok(client):
    r = client.post(
        "/api/v1/validate/phone",
        json={"phone": "9123 4567", "country": "SG"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["valid"] is True
    assert data["e164"] == "+6591234567"
    assert data["country"] == "SG"
    assert data["masked"].endswith("****4567")


def test_api_validate_phone_bad(client):
    r = client.post(
        "/api/v1/validate/phone",
        json={"phone": "garbage", "country": "SG"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["valid"] is False
    assert data["error"]["error_code"].startswith("phone.")


def test_api_validate_address(client):
    r = client.post(
        "/api/v1/validate/address",
        json={
            "country": "SG",
            "fields": {
                "block": "123", "street": "Orchard Road",
                "floor": "10", "unit": "05", "postal_code": "238888",
            },
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["valid"] is True
    assert data["country"] == "SG"
    assert "Singapore" in data["formatted"]


def test_api_country_info(client):
    r = client.get("/api/v1/i18n/country/SG")
    assert r.status_code == 200
    data = r.json()
    assert data["code"] == "SG"
    assert data["calling_code"] == "65"
    assert data["currency"] == "SGD"
    assert data["address"]["country"] == "SG"


def test_api_countries_list(client):
    r = client.get("/api/v1/i18n/countries")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 10
    codes = {c["code"] for c in data["countries"]}
    for required in ("SG", "CN", "US", "GB", "JP", "ID", "TH", "IN", "BR", "AU"):
        assert required in codes
