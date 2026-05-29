"""European Union — GDPR (Regulation 2016/679) + ePrivacy Directive
2002/58/EC. Source: art. 8 GDPR age of consent default 16 (member states
may lower to 13); art. 33 breach notification within 72 hours; art. 37
DPO mandatory for public bodies and large-scale processing; ePrivacy
art. 5(3) cookie consent; EU 1924/2006 nutrient/health claim regulation
governs F&B advertising."""

from __future__ import annotations

from app.compliance_regional import ComplianceRuleSet


EU_RULES = ComplianceRuleSet(
    region="eu",
    law_name="GDPR + ePrivacy",
    age_of_consent=16,
    data_retention_max_days=365 * 6,
    requires_dpo=True,
    cross_border_transfer_allowed=False,
    consent_modes=["explicit"],
    breach_notification_hours=72,
    right_to_erasure=True,
    right_to_portability=True,
    do_not_sell_required=False,
    cookie_banner_required=True,
    age_gate_required=True,
    parental_consent_threshold=16,
    banned_content_categories=[
        "alcohol_to_minors",
        "tobacco",
        "misleading_nutrient_claims",
        "unapproved_health_claims",
    ],
    required_disclosures=[
        "controller_identity",
        "dpo_contact",
        "lawful_basis",
        "cookie_categories",
        "nutrient_claim_regulation_eu_1924_2006",
    ],
    enforcement_authority="National DPAs + EDPB",
)
