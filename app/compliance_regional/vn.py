"""Vietnam — Decree 13/2023/ND-CP on Personal Data Protection (effective
1 July 2023). Source: art. 20 parental consent under 16; art. 25
cross-border transfer impact assessment to MIC; alcohol advertising
restrictions under Law 44/2019/QH14 on Prevention of Alcohol Harm
(§12 bans broadcast alcohol ads to minors and on digital media before
18:00–21:00 windows)."""

from __future__ import annotations

from app.compliance_regional import ComplianceRuleSet


VN_RULES = ComplianceRuleSet(
    region="vn",
    law_name="Decree 13/2023/ND-CP",
    age_of_consent=16,
    data_retention_max_days=365 * 3,
    requires_dpo=True,
    cross_border_transfer_allowed=False,
    consent_modes=["explicit"],
    breach_notification_hours=72,
    right_to_erasure=True,
    right_to_portability=False,
    do_not_sell_required=False,
    cookie_banner_required=True,
    age_gate_required=True,
    parental_consent_threshold=16,
    banned_content_categories=[
        "alcohol_advertising_on_digital",
        "gambling",
        "tobacco",
        "politically_sensitive",
    ],
    required_disclosures=[
        "mic_registration_number",
        "controller_identity",
        "cross_border_transfer_impact_assessment",
    ],
    enforcement_authority="Ministry of Public Security (A05) + MIC",
)
