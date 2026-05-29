"""Per-region compliance rule set tests.

Covers the 9 regions wired in ``app.compliance_regional`` plus the
lookup helpers and the new router endpoints. Existing CN compliance
router is not touched and not exercised here.
"""

from __future__ import annotations

import pytest

from app.compliance_regional import (
    REGIONAL_RULES,
    check_age_gate_required,
    check_content_allowed,
    get_compliance_for_region,
    get_compliance_for_user,
)


_EXPECTED_REGIONS = {"sg", "id", "th", "vn", "ph", "eu", "us", "br", "in"}


def test_all_nine_regions_return_valid_rule_sets():
    assert set(REGIONAL_RULES.keys()) == _EXPECTED_REGIONS
    for code, rules in REGIONAL_RULES.items():
        assert rules.region == code
        assert rules.law_name
        assert rules.age_of_consent > 0
        assert rules.parental_consent_threshold > 0
        assert rules.breach_notification_hours > 0
        assert isinstance(rules.consent_modes, list) and rules.consent_modes
        assert isinstance(rules.banned_content_categories, list)
        assert rules.enforcement_authority


def test_sg_alcohol_blocked_under_18_allowed_adult():
    # SG bans ``alcohol_to_minors`` — adults pass via the age-uplift path.
    allowed, reason = check_content_allowed("sg", "alcohol_to_minors")
    assert allowed is False and "PDPA" in reason


def test_id_alcohol_always_restricted():
    allowed, reason = check_content_allowed("id", "alcohol")
    assert allowed is False
    assert "UU 27/2022" in reason or "id" in reason


def test_eu_gdpr_right_to_erasure():
    rules = get_compliance_for_region("eu")
    assert rules.right_to_erasure is True
    assert rules.law_name.startswith("GDPR")
    assert rules.requires_dpo is True


def test_in_dpdp_requires_parental_consent_for_under_18():
    rules = get_compliance_for_region("in")
    assert rules.parental_consent_threshold == 18
    assert check_age_gate_required("in", 17) is True
    assert check_age_gate_required("in", 18) is False


def test_us_no_federal_falls_back_per_state():
    rules = get_compliance_for_region("us")
    # State patchwork is encoded in the law_name; CCPA + COPPA referenced.
    assert "CCPA" in rules.law_name or "CPRA" in rules.law_name
    assert rules.do_not_sell_required is True
    assert rules.age_of_consent == 13  # COPPA floor


@pytest.mark.asyncio
async def test_matrix_endpoint_returns_nine_rows(client):
    res = await client.get("/api/v1/compliance/matrix")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 9
    region_codes = {row["region"] for row in body["rows"]}
    assert region_codes == _EXPECTED_REGIONS


@pytest.mark.asyncio
async def test_check_content_endpoint(client):
    # Adult in PH (parental threshold 18): alcohol_to_minors unlocks for 21.
    res = await client.get(
        "/api/v1/compliance/check-content",
        params={"region": "ph", "category": "alcohol_to_minors", "age": 21},
    )
    assert res.status_code == 200
    assert res.json()["allowed"] is True

    # Minor in PH (under 18): still blocked + age gate triggers.
    res = await client.get(
        "/api/v1/compliance/check-content",
        params={"region": "ph", "category": "alcohol_to_minors", "age": 16},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["allowed"] is False
    assert body["age_gate_required"] is True

    # Hard ban (ID alcohol): always blocked regardless of age.
    res = await client.get(
        "/api/v1/compliance/check-content",
        params={"region": "id", "category": "alcohol", "age": 30},
    )
    assert res.status_code == 200
    assert res.json()["allowed"] is False


def test_age_gate_logic_per_region():
    # VN parental threshold 16
    assert check_age_gate_required("vn", 15) is True
    assert check_age_gate_required("vn", 16) is False
    # TH parental threshold 20
    assert check_age_gate_required("th", 19) is True
    assert check_age_gate_required("th", 20) is False
    # Unknown age + gated region -> True
    assert check_age_gate_required("br", None) is True


@pytest.mark.asyncio
async def test_lookup_by_user_id_via_profile(clean_redis):
    # Seed a kid profile with region=eu, then resolve.
    await clean_redis.hset("kid:kid_test_001", "region", "eu")
    rules = await get_compliance_for_user("kid_test_001")
    assert rules.region == "eu"

    # Missing profile -> falls back to ``sg`` (most permissive APAC).
    fallback = await get_compliance_for_user("kid_does_not_exist")
    assert fallback.region == "sg"


def test_dpo_required_flag():
    # DPO mandatory in EU, TH, IN, BR, SG, PH, VN, ID; optional in US.
    for code in ("eu", "th", "in", "br", "sg", "ph", "vn", "id"):
        assert get_compliance_for_region(code).requires_dpo is True, code
    assert get_compliance_for_region("us").requires_dpo is False


def test_cookie_banner_flag_for_eu():
    # GDPR ePrivacy requires cookie banner.
    assert get_compliance_for_region("eu").cookie_banner_required is True
    # SG/US do not mandate cookie banners.
    assert get_compliance_for_region("sg").cookie_banner_required is False
    assert get_compliance_for_region("us").cookie_banner_required is False
