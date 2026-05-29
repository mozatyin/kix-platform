"""Per-region compliance rule API — additive to ``compliance.py``.

The CN router at ``app/routers/compliance.py`` keeps PIPL banned-phrase
scanning and sensitive-PI audit untouched. This sibling router exposes
the declarative rule sets in ``app.compliance_regional`` so ops and
client apps can inspect / enforce per-region privacy + content rules.

Endpoints
---------
GET /regions                     - list all supported region codes
GET /region/{code}               - full rule set for one region
GET /check-content               - eligibility check (region/category/age)
GET /matrix                      - flat comparison table for ops dashboard
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.compliance_regional import (
    REGIONAL_RULES,
    check_age_gate_required,
    check_content_allowed,
    get_compliance_for_region,
)


router = APIRouter()


@router.get("/regions")
async def list_regions() -> dict[str, Any]:
    """Return the supported region codes plus law name summary."""
    return {
        "count": len(REGIONAL_RULES),
        "regions": [
            {"code": code, "law_name": rules.law_name}
            for code, rules in sorted(REGIONAL_RULES.items())
        ],
    }


@router.get("/region/{code}")
async def get_region(code: str) -> dict[str, Any]:
    """Return the full rule set for one region."""
    try:
        rules = get_compliance_for_region(code)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown region: {code}")
    return rules.to_dict()


@router.get("/check-content")
async def check_content(
    region: str = Query(..., description="ISO-style region code"),
    category: str = Query(..., description="content category to check"),
    age: int | None = Query(None, description="user age, if known"),
) -> dict[str, Any]:
    """Return allowed/blocked + reason + whether age gate triggers.

    Categories with ``_to_minors`` suffix are allowed for age >=
    ``parental_consent_threshold`` even when the bare category appears
    in the region's banned list — this matches the F&B/alcohol-ads
    semantics in the strategy doc.
    """
    try:
        rules = get_compliance_for_region(region)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown region: {region}")

    allowed, reason = check_content_allowed(region, category)

    # Age uplift: a ``_to_minors``-suffixed restriction unlocks for adults.
    cat_lower = category.lower().strip()
    age_gate = check_age_gate_required(region, age)
    if not allowed and cat_lower.endswith("_to_minors"):
        if age is not None and age >= rules.parental_consent_threshold:
            allowed = True
            reason = ""

    return {
        "region": region.lower(),
        "category": cat_lower,
        "allowed": allowed,
        "reason": reason,
        "age_gate_required": age_gate,
        "parental_consent_threshold": rules.parental_consent_threshold,
    }


@router.get("/matrix")
async def matrix() -> dict[str, Any]:
    """Return a flat comparison matrix across all regions (ops view)."""
    rows = []
    for code in sorted(REGIONAL_RULES):
        rules = REGIONAL_RULES[code]
        rows.append({
            "region": code,
            "law_name": rules.law_name,
            "age_of_consent": rules.age_of_consent,
            "parental_consent_threshold": rules.parental_consent_threshold,
            "data_retention_max_days": rules.data_retention_max_days,
            "requires_dpo": rules.requires_dpo,
            "cross_border_transfer_allowed":
                rules.cross_border_transfer_allowed,
            "breach_notification_hours": rules.breach_notification_hours,
            "right_to_erasure": rules.right_to_erasure,
            "right_to_portability": rules.right_to_portability,
            "do_not_sell_required": rules.do_not_sell_required,
            "cookie_banner_required": rules.cookie_banner_required,
            "age_gate_required": rules.age_gate_required,
            "consent_modes": rules.consent_modes,
            "banned_content_categories": rules.banned_content_categories,
            "enforcement_authority": rules.enforcement_authority,
        })
    return {"count": len(rows), "rows": rows}
