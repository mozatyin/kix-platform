"""Thailand — PDPA B.E. 2562 (2019), effective 2022. Source: §19 explicit
consent, §20 age of majority 20 (parental for under-20), §41 DPO
mandatory when core activity is large-scale processing; alcohol ad ban
under Alcoholic Beverage Control Act B.E. 2551 §32; gambling banned
under Gambling Act B.E. 2478."""

from __future__ import annotations

from app.compliance_regional import ComplianceRuleSet


TH_RULES = ComplianceRuleSet(
    region="th",
    law_name="PDPA 2019 (B.E. 2562)",
    age_of_consent=20,
    data_retention_max_days=365 * 10,
    requires_dpo=True,
    cross_border_transfer_allowed=True,
    consent_modes=["explicit"],
    breach_notification_hours=72,
    right_to_erasure=True,
    right_to_portability=True,
    do_not_sell_required=False,
    cookie_banner_required=True,
    age_gate_required=True,
    parental_consent_threshold=20,
    banned_content_categories=[
        "alcohol_advertising",
        "gambling",
        "tobacco",
    ],
    required_disclosures=[
        "controller_identity",
        "dpo_contact",
        "lawful_basis",
    ],
    enforcement_authority="Personal Data Protection Committee (PDPC Thailand)",
)
