"""Per-region compliance rule sets — KiX 200-country rollout spine.

This module is additive and DOES NOT touch ``app/routers/compliance.py``
which carries the CN-only banned-phrase + sensitive-PI rules (sacrosanct
PIPL §51 audit data). Instead, ``compliance_regional`` provides a
declarative ruleset per non-CN region covering:

* Data privacy law (GDPR, LGPD, DPDP, PDPA, …)
* Age of consent + parental thresholds
* Data retention / breach notification windows
* Cross-border transfer + DPO requirements
* Consent modes (explicit / implicit / opt_out)
* Cookie banner + age gate + Do-Not-Sell flags
* Banned content categories (alcohol, gambling, tobacco …)
* Required disclosures + enforcement authority

Siblings should import ``get_compliance_for_region`` to gate flows.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

# Sources are cited inline per file. Single comment block per region.


@dataclass(frozen=True)
class ComplianceRuleSet:
    """Declarative compliance rule set for one region.

    All fields are required; defaults exist only for list types so each
    region file reads as a flat table. ``frozen=True`` because rule sets
    are configuration, not runtime state.
    """

    region: str
    law_name: str
    age_of_consent: int
    data_retention_max_days: int
    requires_dpo: bool
    cross_border_transfer_allowed: bool
    consent_modes: list[str]
    breach_notification_hours: int
    right_to_erasure: bool
    right_to_portability: bool
    do_not_sell_required: bool
    cookie_banner_required: bool
    age_gate_required: bool
    parental_consent_threshold: int
    banned_content_categories: list[str] = field(default_factory=list)
    required_disclosures: list[str] = field(default_factory=list)
    enforcement_authority: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Imports happen at module bottom to keep registry definition tidy.
from app.compliance_regional.sg import SG_RULES  # noqa: E402
from app.compliance_regional.id_ import ID_RULES  # noqa: E402
from app.compliance_regional.th import TH_RULES  # noqa: E402
from app.compliance_regional.vn import VN_RULES  # noqa: E402
from app.compliance_regional.ph import PH_RULES  # noqa: E402
from app.compliance_regional.eu import EU_RULES  # noqa: E402
from app.compliance_regional.us import US_RULES  # noqa: E402
from app.compliance_regional.br import BR_RULES  # noqa: E402
from app.compliance_regional.in_ import IN_RULES  # noqa: E402


REGIONAL_RULES: dict[str, ComplianceRuleSet] = {
    "sg": SG_RULES,
    "id": ID_RULES,
    "th": TH_RULES,
    "vn": VN_RULES,
    "ph": PH_RULES,
    "eu": EU_RULES,
    "us": US_RULES,
    "br": BR_RULES,
    "in": IN_RULES,
}


def get_compliance_for_region(region: str) -> ComplianceRuleSet:
    """Lookup rule set by ISO-style region code (case-insensitive).

    Raises ``KeyError`` if region is not supported — caller decides
    whether to 404 or fall back to a conservative default.
    """
    key = (region or "").lower().strip()
    if key not in REGIONAL_RULES:
        raise KeyError(f"unsupported region: {region!r}")
    return REGIONAL_RULES[key]


async def get_compliance_for_user(user_id: str) -> ComplianceRuleSet:
    """Resolve a user's region via the kix-id profile, then look up rules.

    Read-only: pulls ``region`` from the user's KiX ID profile hash in
    Redis. Falls back to ``sg`` (most permissive APAC baseline) if the
    profile carries no region — never raises on missing data.
    """
    from app.redis_client import get_redis

    r = await get_redis()
    raw = await r.hget(f"kid:{user_id}", "region")
    if raw is None:
        raw_profile = await r.hget(f"user:{user_id}:profile", "region")
        raw = raw_profile
    region = (raw or "sg").lower() if raw else "sg"
    if isinstance(region, bytes):
        region = region.decode()
    if region not in REGIONAL_RULES:
        region = "sg"
    return REGIONAL_RULES[region]


def check_age_gate_required(region: str, user_age: int | None) -> bool:
    """Return True if the user must pass an age gate before continuing.

    * Region with ``age_gate_required=False`` -> never gated.
    * Unknown age + gated region -> True (must collect age).
    * Age below ``parental_consent_threshold`` -> True (parental flow).
    """
    rules = get_compliance_for_region(region)
    if not rules.age_gate_required:
        return False
    if user_age is None:
        return True
    return user_age < rules.parental_consent_threshold


def check_content_allowed(
    region: str, content_category: str
) -> tuple[bool, str]:
    """Check if a content category is allowed in a region.

    Returns ``(allowed, reason)``. ``reason`` is empty when allowed,
    otherwise carries a human-readable legal basis for the block.
    """
    rules = get_compliance_for_region(region)
    cat = (content_category or "").lower().strip()
    if cat in {c.lower() for c in rules.banned_content_categories}:
        return (
            False,
            f"category {cat!r} restricted under {rules.law_name} "
            f"in {rules.region}",
        )
    return True, ""


__all__ = [
    "ComplianceRuleSet",
    "REGIONAL_RULES",
    "get_compliance_for_region",
    "get_compliance_for_user",
    "check_age_gate_required",
    "check_content_allowed",
]
