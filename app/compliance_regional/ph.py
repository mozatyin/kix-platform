"""Philippines — Data Privacy Act of 2012 (RA 10173). Source: §3(b)
defines age of majority at 18; NPC Advisory Opinion 2017-049 on
parental consent for minors; FDA AO 2014-0030 halal-labeling rules for
F&B targeting Muslim audiences."""

from __future__ import annotations

from app.compliance_regional import ComplianceRuleSet


PH_RULES = ComplianceRuleSet(
    region="ph",
    law_name="Data Privacy Act 2012 (RA 10173)",
    age_of_consent=18,
    data_retention_max_days=365 * 5,
    requires_dpo=True,
    cross_border_transfer_allowed=True,
    consent_modes=["explicit"],
    breach_notification_hours=72,
    right_to_erasure=True,
    right_to_portability=True,
    do_not_sell_required=False,
    cookie_banner_required=True,
    age_gate_required=True,
    parental_consent_threshold=18,
    banned_content_categories=[
        "alcohol_to_minors",
        "tobacco",
        "online_gambling_unlicensed",
    ],
    required_disclosures=[
        "dpo_contact",
        "purpose_specification",
        "halal_certification_where_relevant",
    ],
    enforcement_authority="National Privacy Commission (NPC)",
)
