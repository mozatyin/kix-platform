"""CLASS-J structural fix — canonical pricing tiers (single source of truth).

Symptom history (pre-fix):
- /pricing said "no CC required" on Free tier
- /enterprise said "CC required" on its self-serve tier
- /trinity-artifacts didn't mention pricing at all
- /for-chains had a different 90-day pilot timeline than /pricing

Root cause: pricing copy was free-text in 4 different HTML files.

Fix: 3 canonical PricingTier dataclasses. landing_gen reads from these.
Any drift = lint failure (test_pricing_canon asserts no other landing
file mentions a tier that contradicts the canon).

Founder-cited rules baked in:
- Free tier: NO credit card required, ever
- Verified Business: CC required for fraud-protection; no charge until
  first successful campaign
- Founding-100: city-scoped, approval-gated, 6 months Premium free,
  0% take rate forever for first 100 approved merchants per city
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PricingTier:
    """One pricing tier — frozen so callers can't mutate canonical values."""
    tier_id: str
    name: str
    headline: str
    price_text: str               # "Free forever" / "Pay-as-you-go" / "S$0 forever"
    cc_required: bool
    no_charge_until: str          # plain-English condition
    take_rate_pct: float          # KiX's cut; 0.0 for founding
    scope: str                    # "global" / "per-city"
    approval_required: bool
    included: tuple[str, ...]     # bullet list of what's included
    not_included: tuple[str, ...] = field(default_factory=tuple)
    cta_text: str = "Start free"
    cta_action: str = "signup"    # signup / contact / apply


# ── The canon ──

TIER_FREE = PricingTier(
    tier_id="free",
    name="Free",
    headline="Try the platform end-to-end. No card, no time limit.",
    price_text="Free forever",
    cc_required=False,
    no_charge_until="N/A — this tier is permanently free",
    take_rate_pct=0.0,
    scope="global",
    approval_required=False,
    included=(
        "1 game live at a time",
        "Up to 100 plays per month",
        "Geofence radius 100m",
        "Email-only support",
        "KiX branding on game footer",
    ),
    not_included=(
        "Multi-game campaigns",
        "Cohort retention analytics",
        "Remove KiX branding",
        "API access",
    ),
    cta_text="Start free — no card",
    cta_action="signup",
)


TIER_VERIFIED_BUSINESS = PricingTier(
    tier_id="verified_business",
    name="Verified Business · Pro",
    headline="S$499/mo flat OR pay-per-customer. Most merchants cut their IG/FB ad spend 35-50% within month 1 — S$499 REPLACES that budget, not adds to it.",
    price_text="S$499/mo flat · OR pay-as-you-go from S$3 CPA",
    cc_required=True,
    no_charge_until="14-day free trial · no card needed at any point during trial · only enter card on day 14 IF you choose to continue · auto-downgrades to Free tier if no card provided",
    take_rate_pct=10.0,
    scope="global",
    approval_required=False,
    included=(
        "S$499/mo flat: unlimited campaigns + ~1,000-2,500 new customers/month for most F&B",
        "OR pay-as-you-go: S$3-30 CPA depending on vertical (no monthly fee)",
        "Switch between Pro and CPA anytime · 1 click",
        "Unlimited games + campaigns",
        "Geofence up to 500m",
        "Cohort retention analytics (D0/14/30/60/90)",
        "Remove KiX branding (white-label)",
        "API access + webhooks",
        "Same-day chat support",
        "6 PSPs · PayNow · GrabPay · OVO · Alipay · WeChat (CN ready) · Stripe Terminal",
    ),
    not_included=(
        "Founding-100 zero-take-rate",
        "On-site founder onboarding",
    ),
    cta_text="Start 14-day free trial · no card · cancel 1-click",   # CLASS-EE R12 · matches no_charge_until copy exactly
    cta_action="subscribe",
)


TIER_FOUNDING_100 = PricingTier(
    tier_id="founding_100",
    name="Founding-100",
    headline="First 100 approved merchants per city. 6mo Premium free. 0% take rate forever.",
    price_text="S$0 forever (first 100 per city only)",
    cc_required=False,
    no_charge_until="never — founding-100 status is permanent",
    take_rate_pct=0.0,
    scope="per-city",
    approval_required=True,
    included=(
        "EVERYTHING in Verified Business tier",
        "6 months Premium tier free (priority support + custom branding)",
        "0% take rate forever — KiX never takes a cut from your sales",
        "On-site founder onboarding (Singapore + Malaysia only)",
        "Direct line to founder for product feedback",
        "Public co-marketing as a launch partner",
    ),
    not_included=(
        "Available outside the founding 100 (apply when slots reopen)",
    ),
    cta_text="Apply for founding-100",
    cta_action="apply",
)


# All canonical tiers, in display order.
CANONICAL_TIERS: tuple[PricingTier, ...] = (
    TIER_FREE,
    TIER_VERIFIED_BUSINESS,
    TIER_FOUNDING_100,
)

BY_ID: dict[str, PricingTier] = {t.tier_id: t for t in CANONICAL_TIERS}


# ── API ──

def get_tier(tier_id: str) -> PricingTier:
    if tier_id not in BY_ID:
        raise KeyError(f"unknown pricing tier: {tier_id}. "
                       f"Canonical: {list(BY_ID.keys())}")
    return BY_ID[tier_id]


def render_short_label(tier: PricingTier) -> str:
    """One-line label for nav / footer. Examples:
       'Free · no card'
       'Pay-as-you-go · CPA from S$3'
       'S$0 forever (Founding-100, per city, approval required)'
    """
    if tier.tier_id == "free":
        return "Free · no card"
    if tier.tier_id == "verified_business":
        return tier.price_text
    if tier.tier_id == "founding_100":
        return f"{tier.price_text} (Founding-100, per city, approval required)"
    return tier.price_text


# ── Detection: text-level drift checks ──

# If any landing page contains these phrases, they MUST come from the canon.
# Used by tests + lint to catch off-canon copy.
CANONICAL_PHRASES: dict[str, str] = {
    # phrase that MUST be tied to a tier_id
    "no card": "free",
    "no charge until": "verified_business",
    "0% take rate forever": "founding_100",
    "6mo premium free": "founding_100",
    "6 months premium tier free": "founding_100",
    "first 100 approved merchants per city": "founding_100",
}


def find_off_canon_pricing(text: str) -> list[str]:
    """Return list of suspicious phrases — non-canonical pricing claims.

    Detects:
    - "free for X months" without "founding-100" qualifier (we don't offer that)
    - "lifetime discount" (not a tier we offer)
    - "% commission" (we use "take rate"; commission implies sales-rep model)
    """
    import re
    findings: list[str] = []
    lower = text.lower()

    # "free for N months" / "N months free" outside founding-100 context
    if re.search(r"free for \d+ months", lower) or re.search(r"\d+ months free", lower):
        if "founding" not in lower:
            findings.append("free-period claim without founding-100 qualifier")

    if "lifetime discount" in lower:
        findings.append("'lifetime discount' is not a canonical KiX tier")

    if re.search(r"\d+%\s*commission", lower):
        findings.append("'commission' wording; canon uses 'take rate'")

    if "no credit card ever" in lower and "free" not in lower[:200]:
        findings.append("'no credit card ever' should appear only near Free tier")

    return findings
