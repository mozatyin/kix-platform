"""Singapore — PDPA 2012 (rev. 2020). Source: PDPC Advisory Guidelines on
the PDPA for Children's Personal Data (age 13); DPO mandatory under §11
when org processes personal data; cross-border transfers allowed under
§26 transfer limitation w/ comparable protection."""

from __future__ import annotations

from app.compliance_regional import ComplianceRuleSet


SG_RULES = ComplianceRuleSet(
    region="sg",
    law_name="PDPA 2012",
    age_of_consent=13,
    data_retention_max_days=365 * 7,
    requires_dpo=True,
    cross_border_transfer_allowed=True,
    consent_modes=["explicit", "deemed"],
    breach_notification_hours=72,
    right_to_erasure=True,
    right_to_portability=True,
    do_not_sell_required=False,
    cookie_banner_required=False,
    age_gate_required=True,
    parental_consent_threshold=13,
    banned_content_categories=[
        "alcohol_to_minors",
        "tobacco",
        "online_gambling_unlicensed",
    ],
    required_disclosures=[
        "purpose_of_collection",
        "dpo_contact",
        "halal_certification_if_muslim_targeted",
    ],
    enforcement_authority="Personal Data Protection Commission (PDPC)",
)
