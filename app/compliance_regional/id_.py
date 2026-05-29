"""Indonesia — UU 27/2022 PDP Law (active enforcement Oct 2024). Source:
art. 5 children definition (under 17); art. 65 cross-border transfer
restrictions; alcohol heavily restricted under UU 11/2020 and MUI halal
certification mandatory for F&B under UU 33/2014."""

from __future__ import annotations

from app.compliance_regional import ComplianceRuleSet


ID_RULES = ComplianceRuleSet(
    region="id",
    law_name="UU 27/2022 PDP Law",
    age_of_consent=17,
    data_retention_max_days=365 * 5,
    requires_dpo=True,
    cross_border_transfer_allowed=False,
    consent_modes=["explicit"],
    breach_notification_hours=72,
    right_to_erasure=True,
    right_to_portability=True,
    do_not_sell_required=False,
    cookie_banner_required=True,
    age_gate_required=True,
    parental_consent_threshold=17,
    banned_content_categories=[
        "alcohol",
        "non_halal_food_to_muslim_audience",
        "online_gambling",
        "pork_products_advertising_unlabeled",
    ],
    required_disclosures=[
        "halal_certification_MUI",
        "controller_identity",
        "data_localization_notice_for_sensitive",
    ],
    enforcement_authority="Personal Data Protection Agency (Lembaga PDP)",
)
