"""United States — no federal privacy law; state-by-state patchwork.
Source: California CCPA/CPRA (Civ. Code §1798.100+) Do-Not-Sell + opt-out;
Virginia VCDPA, Colorado CPA, Connecticut CTDPA, Utah UCPA mirror CCPA
with opt-out model; COPPA (15 U.S.C. §§ 6501–6506) hard floor at 13 with
verifiable parental consent; FDA 21 CFR Part 101 nutrition labeling
governs F&B claims."""

from __future__ import annotations

from app.compliance_regional import ComplianceRuleSet


US_RULES = ComplianceRuleSet(
    region="us",
    law_name="State patchwork: CCPA/CPRA + COPPA (federal floor)",
    age_of_consent=13,
    data_retention_max_days=365 * 7,
    requires_dpo=False,
    cross_border_transfer_allowed=True,
    consent_modes=["opt_out", "implicit"],
    breach_notification_hours=72,
    right_to_erasure=True,
    right_to_portability=True,
    do_not_sell_required=True,
    cookie_banner_required=False,
    age_gate_required=True,
    parental_consent_threshold=13,
    banned_content_categories=[
        "alcohol_to_minors",
        "tobacco_to_minors",
        "unapproved_health_claims_fda",
        "misleading_nutrition_claims",
    ],
    required_disclosures=[
        "do_not_sell_link",
        "ccpa_privacy_rights",
        "fda_nutrition_facts_panel",
        "coppa_under_13_notice",
    ],
    enforcement_authority=(
        "FTC (federal/COPPA) + state AGs (CA AG, VA AG, CO AG, CT AG, UT AG)"
    ),
)
