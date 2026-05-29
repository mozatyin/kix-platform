"""India — Digital Personal Data Protection Act 2023 (DPDP Act). Source:
§9 verifiable parental consent required for all under-18 processing
(strict majority gate); §10 Significant Data Fiduciary registration with
the Data Protection Board; §16 cross-border transfer subject to
government notification; FSSAI Food Safety & Standards Act 2006 governs
food labeling and advertising claims."""

from __future__ import annotations

from app.compliance_regional import ComplianceRuleSet


IN_RULES = ComplianceRuleSet(
    region="in",
    law_name="DPDP Act 2023",
    age_of_consent=18,
    data_retention_max_days=365 * 3,
    requires_dpo=True,
    cross_border_transfer_allowed=False,
    consent_modes=["explicit"],
    breach_notification_hours=72,
    right_to_erasure=True,
    right_to_portability=True,
    do_not_sell_required=False,
    cookie_banner_required=True,
    age_gate_required=True,
    parental_consent_threshold=18,
    banned_content_categories=[
        "alcohol_advertising",
        "tobacco",
        "online_real_money_gaming_unlicensed",
        "unapproved_health_claims_fssai",
    ],
    required_disclosures=[
        "data_fiduciary_registration",
        "fssai_license_for_food",
        "parental_consent_under_18",
        "grievance_officer_contact",
    ],
    enforcement_authority="Data Protection Board of India + FSSAI",
)
