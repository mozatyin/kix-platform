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
