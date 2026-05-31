"""C · Single source of truth for personas.

Before this module, persona data lived in 3 places:
  - scripts/sim_users_deepseek.py · PERSONAS dict (name/role/context)
  - scripts/verify_generated_brands.py · PERSONA_PROFILES dict (duplicate)
  - scripts/verify_generated_brands.py · PERSONA_AXES dict (axis matching)

Three sources = three drift surfaces. This module is the canon. All
3 callers import from here. Adding a persona = one edit, not three.

Per `feedback_structural_fix_pattern` — the structural fix is to delete
the duplicate sources, not warn about them.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PersonaAxes:
    """Which (audience, scale) page-axes a persona is qualified to evaluate.
    Used by scripts/verify_generated_brands.py to route persona LLM calls
    only to relevant pages. Frozen so callers can't mutate by mistake."""
    audience: str   # "merchant" | "consumer" | "both"
    scale: str      # "single" | "chain" | "enterprise" | "both"


@dataclass(frozen=True)
class Persona:
    """Single persona = name + role + context + axes + LLM eval kwargs."""
    persona_id: str
    name: str
    role: str          # 1-3 sentences — used as system-prompt context
    context: str       # 1-2 sentences — situational note
    axes: PersonaAxes
    score_floor_override: int = 0   # 0 = use gate default min_score_floor
    temperature: float = 0.4
    for_gate: bool = True   # if False, persona is for ad-hoc critic sweeps only
                            # — does NOT participate in the production verdict_gate


# ── The canon ──

PERSONAS: dict[str, Persona] = {
    "aminah_first_time_merchant": Persona(
        persona_id="aminah_first_time_merchant",
        name="Aminah Binti",
        role=(
            "First-time merchant. Halal nasi padang stall, Tampines hawker centre. "
            "Never used SaaS. Marketing = IG stories. Tech-cautious but motivated to grow."
        ),
        context=(
            "Halal-only. Strong family priority. Speaks Malay+English. Hates jargon. "
            "Trusts WhatsApp founder contact over forms."
        ),
        axes=PersonaAxes(audience="merchant", scale="single"),
    ),
    "skeptical_owner": Persona(
        persona_id="skeptical_owner",
        name="Sarah Chen",
        role=(
            "Café owner, single outlet, 4 years in. Burned by 2 prior loyalty SaaS platforms. "
            "Reads the small print. Doesn't sign anything she can't cancel in 1 click."
        ),
        context=(
            "Skeptical of 'free' tiers (hidden costs?). Skeptical of case-study photos (real?). "
            "Needs proof, not pitch."
        ),
        axes=PersonaAxes(audience="merchant", scale="single"),
    ),
    "ahmad_kopi_chain": Persona(
        persona_id="ahmad_kopi_chain",
        name="Ahmad bin Hassan",
        role=(
            "CEO of a 14-outlet kopitiam chain (KL + Penang). Drives a Mercedes. "
            "Looks at unit economics, not feelings. Compares vendors on CFO dimensions."
        ),
        context=(
            "Has IT team, payment integration team. Needs: per-outlet attribution, "
            "multi-tenant data, white-label, SOC2/PDPA-MY compliance, exit clause."
        ),
        axes=PersonaAxes(audience="merchant", scale="chain"),
    ),
    "enterprise_manager": Persona(
        persona_id="enterprise_manager",
        name="Sandeep Kumar",
        role=(
            "Regional Loyalty Manager at Starbucks SG. 38 years old. "
            "Manages S$2M/year promotion budget. Reports to APAC marketing director. "
            "Buys from Salesforce, Klaviyo, Eber today. Evaluating KiX as add-on or replacement."
        ),
        context=(
            "15-min evaluation window. Needs: enterprise contract terms, SSO/SAML, "
            "data residency in SG, CDP integration, multi-brand reporting roll-up "
            "(across 6 Starbucks sub-brands). Will scrutinize SOC2 / pen-test / DPA / "
            "breach SLA. 'Founding-100' is startup signal, not enterprise."
        ),
        axes=PersonaAxes(audience="merchant", scale="enterprise"),
        score_floor_override=45,   # enterprise floor stricter than default 40
    ),
    "consumer": Persona(
        persona_id="consumer",
        name="Ben Tan",
        role=(
            "Office worker. Lunch in CBD. Will scan a QR if it's <3 seconds. "
            "Won't install an app. Won't fill a form for free coffee."
        ),
        context=(
            "Cynical about ad tracking. Likes vouchers. Hates 'gamification' that's "
            "actually just spam."
        ),
        axes=PersonaAxes(audience="consumer", scale="both"),
    ),
    # ── Wave N buyer-journey personas (multi-page conversion simulation) ──
    "enterprise_skeptic_cn": Persona(
        persona_id="enterprise_skeptic_cn",
        name="王经理 / Mr Wang",
        role=(
            "CMO of a 380-store McDonald's-tier QSR brand. HQ in Shanghai, "
            "stores across mainland China + Singapore + Malaysia. Reports to "
            "Group COO. Has S$2-5M annual marketing-tech budget. Compares 5+ "
            "vendors per RFP. Has been pitched 30 SaaS platforms this year. "
            "Default answer is 'no' — vendors must overcome serious doubt."
        ),
        context=(
            "Visiting KiX because his Singapore franchisee mentioned it at a "
            "Q1 review. Has 15 min before next meeting. Will leave at the first "
            "vague claim. Wants: (a) is this real? (b) does it fit my scale? "
            "(c) what's the actual 12-month cost-and-ROI for 380 stores? "
            "(d) can I exit if it doesn't work? Authorization to spend up to "
            "S$50K on a 6-month pilot without further approval. Anything bigger "
            "needs board sign-off."
        ),
        axes=PersonaAxes(audience="merchant", scale="enterprise"),
        score_floor_override=55,
        temperature=0.4,
    ),
    "smb_entrepreneur_sgcn": Persona(
        persona_id="smb_entrepreneur_sgcn",
        name="陈老板 / Boss Chen",
        role=(
            "Owner of 3 bubble-tea shops (1 in Singapore Bedok, 2 in Shenzhen). "
            "Hands-on operator — works the counter on busy days. Marketing "
            "spend ~S$800/month (mostly IG + Xiaohongshu). Will subscribe to "
            "ONE more SaaS if it pays for itself in ≤2 months. Already has "
            "Shopify POS + WeChat mini-program."
        ),
        context=(
            "Saw KiX on a LinkedIn ad. Has 8 min to decide whether to bookmark "
            "or close. Wants: (a) will it work for bubble tea? "
            "(b) ~how much per month? (c) can I cancel? (d) any pilot/free "
            "trial so I can test without risk? Will subscribe at S$499/mo if "
            "convinced ROI is real and cancel is 1-click. Hates 'enterprise' "
            "sales calls — wants self-serve checkout."
        ),
        axes=PersonaAxes(audience="merchant", scale="single"),
        score_floor_override=50,
        temperature=0.4,
    ),
    # ── Wave N Phase-B buyer-types (chain CFO · agency owner · franchise consultant) ──
    "chain_cfo_franchise": Persona(
        persona_id="chain_cfo_franchise",
        name="林总 / Mr Lim",
        role=(
            "CFO of a 67-outlet franchise group (HK-listed, ops in HK + Macau + "
            "Shenzhen + Guangzhou + Singapore). Reports to CEO + Board. Signs "
            "any contract > S$100K with board sign-off; up to S$150K alone. "
            "Hates 'pilot' phrasing — wants 'evaluation period' with clear KPIs. "
            "Reads MSA + DPA + SOC2 before any first call."
        ),
        context=(
            "Visiting because a board member forwarded a Bloomberg article. "
            "Has 20 min. Will eval against Capillary Loyalty, Comarch, and 2 "
            "Tencent CDP partners. Needs: signed financial KPIs, franchise-tier "
            "P&L visibility, payment reconciliation against 4 banks, Chinese-language "
            "support contract, fapiao monthly, BCP / DR plan."
        ),
        axes=PersonaAxes(audience="merchant", scale="enterprise"),
        score_floor_override=50,
    ),
    "agency_marketing_owner": Persona(
        persona_id="agency_marketing_owner",
        name="Rachel Lim",
        role=(
            "Owner of a 6-person digital agency in Singapore. 18 F&B clients "
            "billed monthly retainer (S$2-5K/client). Sees KiX as either: "
            "(a) a tool she resells/wraps in her service, or (b) a competitor "
            "if it self-serves her clients away from her. Decision is whether "
            "to recommend it to her clients (=she stays in the middle) or "
            "ignore it (=she stays safe). Will subscribe at S$499/mo only if "
            "she controls 5+ client accounts under her umbrella."
        ),
        context=(
            "Will look for: white-label / agency-tier / sub-account / billing-on-behalf. "
            "If KiX is direct-to-merchant only she leaves and discourages her clients. "
            "If KiX offers an agency tier she becomes a multiplier (5-18 referrals)."
        ),
        axes=PersonaAxes(audience="merchant", scale="chain"),
        score_floor_override=45,
    ),
    "franchise_consultant": Persona(
        persona_id="franchise_consultant",
        name="Dr. James Khoo",
        role=(
            "Independent franchise consultant. 22-year career. Advises franchisors "
            "and franchisees on tech stack decisions during franchise expansion. "
            "Charges S$15K-50K per engagement. His reputation = paid by results, "
            "so he ONLY recommends platforms his clients can't get burned by. "
            "If KiX wins his recommendation, he funnels 6-12 franchise networks/year."
        ),
        context=(
            "Skeptical of anything < 3 years old. Wants: 5+ named franchise references "
            "(not single-store testimonials), failure-case transparency (have you ever "
            "had a franchise leave? why?), termination clause for franchisee even if "
            "franchisor is locked in, regulatory-compliance proof per market. Will not "
            "convert this visit — convert = 'will mention in next franchise consult'."
        ),
        axes=PersonaAxes(audience="merchant", scale="chain"),
        score_floor_override=55,
    ),
    "steve_jobs": Persona(
        persona_id="steve_jobs",
        name="Steve Jobs",
        role=(
            "UX critic. Channels the 1997-2011 Apple aesthetic + Pixar storytelling. "
            "Loathes clutter, jargon, fake-friendly copy. Demands every word earn its place."
        ),
        context=(
            "Critiques landing pages as if reviewing a Macworld keynote slide. Will call "
            "specific elements 'a disaster' if they fail. No half-praise — verdict is "
            "binary: 'ship it' or 'kill it'."
        ),
        axes=PersonaAxes(audience="merchant", scale="both"),
        temperature=0.6,   # more opinionated
        for_gate=False,    # critic — never gates production. Run via sweep script only.
    ),
}


# ── API ──

def get(persona_id: str) -> Persona:
    if persona_id not in PERSONAS:
        raise KeyError(f"unknown persona: {persona_id}. "
                       f"Known: {list(PERSONAS.keys())}")
    return PERSONAS[persona_id]


def list_ids() -> list[str]:
    return list(PERSONAS.keys())


def _matches(persona_axis_val: str, page_val: str) -> bool:
    return persona_axis_val == page_val or "both" in (persona_axis_val, page_val)


def for_page(audience: str, scale: str, include_critics: bool = False) -> list[Persona]:
    """Return personas qualified to evaluate a page with (audience, scale).
    By default, EXCLUDES for_gate=False personas (critics like Steve Jobs).
    Pass include_critics=True for ad-hoc sweep scripts."""
    return [
        p for p in PERSONAS.values()
        if (include_critics or p.for_gate)
        and _matches(p.axes.audience, audience)
        and _matches(p.axes.scale, scale)
    ]


def for_page_ids(audience: str, scale: str, include_critics: bool = False) -> list[str]:
    return [p.persona_id for p in for_page(audience, scale, include_critics)]
