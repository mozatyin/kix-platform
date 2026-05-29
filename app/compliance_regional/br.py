"""Brazil — LGPD (Lei Geral de Proteção de Dados, Lei 13.709/2018).
Source: art. 14 specific parental consent for under-12, age of majority
18 (parental for 12–17); art. 41 DPO (encarregado) required; art. 48
breach notification "reasonable time" — ANPD guideline 2 business days
in serious cases; ANVISA RDC 727/2022 governs F&B labeling and health
claims."""

from __future__ import annotations

from app.compliance_regional import ComplianceRuleSet


BR_RULES = ComplianceRuleSet(
    region="br",
    law_name="LGPD (Lei 13.709/2018)",
    age_of_consent=18,
    data_retention_max_days=365 * 5,
    requires_dpo=True,
    cross_border_transfer_allowed=True,
    consent_modes=["explicit"],
    breach_notification_hours=48,
    right_to_erasure=True,
    right_to_portability=True,
    do_not_sell_required=False,
    cookie_banner_required=True,
    age_gate_required=True,
    parental_consent_threshold=18,
    banned_content_categories=[
        "alcohol_to_minors",
        "tobacco",
        "unapproved_health_claims_anvisa",
    ],
    required_disclosures=[
        "encarregado_contact",
        "anvisa_food_labeling",
        "lawful_basis",
    ],
    enforcement_authority=(
        "Autoridade Nacional de Proteção de Dados (ANPD) + ANVISA"
    ),
)
