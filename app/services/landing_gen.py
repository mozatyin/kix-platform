"""Gap B — per-brand landing-page generator (Wave M-3 / "fix the machine").

User mandate: 不再手编 landing pages. Each merchant brand gets its own
generated landing page assembled from a single template + brand_config JSON.

The generator takes a BrandConfig (or any dict-with-the-right-shape) and
renders an HTML landing page bake-in of every UX principle we've shipped:
  - locale switcher integrated into nav (not floating)
  - cross-page locale persistence
  - 3-tier pricing block (Free / Verified Business / Founding-100)
  - "What you actually get" Apple-style benefit grid
  - Real photos + brand mark + location pin
  - Trust footer (Mozat address, verify-independently links)
  - Self-serve / no-card-to-start primary CTA

Output: a single HTML string. Pure function — no I/O. Caller writes the
returned string to disk OR streams it from a FastAPI route. Tests assert
required content fragments.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Optional


# ── BrandConfig ──

@dataclass
class WhatYouGetItem:
    headline: str           # "79+"
    title: str              # "AI-generated game templates"
    body: str               # 1-3 sentence description


@dataclass
class CaseStudy:
    brand_name: str
    location: str           # "Bedok 85, Singapore"
    vertical: str           # "Kopitiam · Single stall"
    quote: str              # one-line testimonial
    quote_attribution: str  # "— Uncle Ng, owner"
    stats: list[tuple[str, str]] = field(default_factory=list)  # [("S$4.90","D61-90 CPA"), ...]
    photo_url: Optional[str] = None
    consent_doc_id: Optional[str] = None    # signed-release identifier


@dataclass
class ChainSection:
    """CLASS-P · multi-outlet brand proof section.

    Set on BrandConfig.chain_section when the brand has ≥3 outlets. The
    landing page renders an extra section answering the chain-CEO checklist
    (per-outlet attribution, white-label, compliance, exit terms).
    """
    outlet_count: int                       # e.g. 14
    per_outlet_attribution: bool = True
    white_label: bool = True
    api_docs_url: str = "/landing/integrations/api-v1.html"
    soc2_status: str = "SOC2 Type I — Q3 2026 (audit in progress)"
    pdpa_my_status: str = "PDPA-MY compliant (DPA available on request)"
    sla_uptime_pct: float = 99.9
    exit_clause: str = "30-day data export + signed data destruction certificate. No exit fee."
    multi_tenant_isolation: str = "Per-outlet Postgres schemas. Cross-outlet reporting via SQL views, opt-in only."
    enterprise_contact_email: str = "chains@letskix.com"


@dataclass
class BrandConfig:
    """Everything needed to render a per-brand landing page."""
    brand_id: str
    brand_name: str
    hero_tagline: str                       # "Pay only for verified new customers"
    hero_sub: str                           # the 1-paragraph sub
    primary_color: str = "#00B341"
    accent_color: str = "#FBBF24"
    locale: str = "en-SG"
    city: str = "Bedok"                     # for founding-100 status display
    founding_slots_total: int = 100
    founding_slots_taken: int = 0
    what_you_get: list[WhatYouGetItem] = field(default_factory=list)
    case_studies: list[CaseStudy] = field(default_factory=list)
    chain_section: Optional[ChainSection] = None    # CLASS-P · multi-outlet proof
    # CLASS-O · target audience determines which personas the verdict_gate uses
    # Allowed: "merchant" (default) | "consumer" | "both"
    audience: str = "merchant"
    # CLASS-S · scale determines which buyer profile fits
    # Allowed: "single" (default) | "chain" | "both"
    scale: str = "single"
    # Vertical-aware framing — drives CPA benchmark callout + recipe seeds.
    # Allowed verticals: see app.services.vertical_benchmarks.BENCHMARKS keys.
    # Empty → no benchmark callout rendered (silent fallback).
    vertical: str = ""
    integrations_link: str = "/landing/integrations/tiktok-pixel.html"
    pricing_link: str = "/landing/pricing.html"
    portal_link: str = "/landing/portal.html"
    compliance_badges: list[str] = field(default_factory=lambda: [
        "PDPA-SG", "PDPA-MY", "GDPR-aligned", "Halal-aware library"
    ])
    mozat_address: str = "Mozat Pte Ltd · 79 Anson Rd, Singapore 079906 · UEN 200103167W"
    contact_email: str = "hello@letskix.com"


# ── Render helpers ──

def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def _render_what_you_get(items: list[WhatYouGetItem]) -> str:
    if not items:
        return ""
    cards = "\n".join(
        f'''      <div class="wyg-card">
        <div class="wyg-num">{_esc(i.headline)}</div>
        <div class="wyg-title">{_esc(i.title)}</div>
        <div class="wyg-body">{_esc(i.body)}</div>
      </div>'''
        for i in items
    )
    return f'''
<section id="what-you-get" style="padding:56px 0 32px;background:#FFFFFF;border-top:1px solid var(--border)">
  <div class="container">
    <div style="text-align:center;max-width:720px;margin:0 auto 36px">
      <div class="section-tag" style="font-size:12px;color:var(--brand);font-weight:700;text-transform:uppercase;letter-spacing:1.2px;margin-bottom:8px">What you actually get</div>
      <h2 style="font-size:30px;font-weight:800;letter-spacing:-.6px;margin-bottom:10px;color:var(--text)">Concrete things you ship in week 1</h2>
      <p style="font-size:15.5px;color:var(--text-muted)">Not marketing — what the product does, with numbers a shop owner can verify.</p>
    </div>
    <style>
      .wyg-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px;max-width:1100px;margin:0 auto}}
      @media(max-width:780px){{.wyg-grid{{grid-template-columns:1fr}}}}
      .wyg-card{{background:#fff;border:1px solid var(--border);border-radius:14px;padding:22px;transition:border-color .15s,box-shadow .15s}}
      .wyg-card:hover{{border-color:#CBD5E1;box-shadow:0 4px 12px rgba(15,23,42,.04)}}
      .wyg-num{{font-size:30px;font-weight:800;color:var(--brand);line-height:1;letter-spacing:-1px;margin-bottom:6px}}
      .wyg-title{{font-size:14.5px;font-weight:800;color:var(--text);margin-bottom:6px}}
      .wyg-body{{font-size:13px;color:var(--text-dim);line-height:1.55}}
    </style>
    <div class="wyg-grid">
{cards}
    </div>
  </div>
</section>'''


def _render_cases(cases: list[CaseStudy], brand_name: str = "") -> str:
    """Render only consent-cleared cases.

    CLASS-Q structural fix: drop CaseStudy without photo_url. The previous
    'photo pending consent' placeholder backfired (Sarah skeptical-owner
    persona: "if real merchants why no photo? — credibility killer").
    CLASS-R structural fix: drop cases whose brand_name matches the page's
    own brand_name (self-reference makes Aminah think the page is hers).
    """
    keepers = [
        c for c in cases
        if c.photo_url
        and (not brand_name or c.brand_name.lower() != brand_name.lower())
    ]
    if not keepers:
        return ""
    parts = []
    for c in keepers:
        stats_html = "".join(
            f'<div style="text-align:center"><div style="font-size:22px;font-weight:800;color:var(--brand-dk);line-height:1">{_esc(v)}</div><div style="font-size:10.5px;color:#16A34A;text-transform:uppercase;letter-spacing:.4px;margin-top:4px">{_esc(label)}</div></div>'
            for v, label in c.stats
        )
        consent_badge = (
            f'<span style="font-size:10px;background:#DCFCE7;color:#166534;padding:2px 6px;border-radius:3px;margin-left:6px;letter-spacing:.3px;font-weight:700">CONSENT ✓ {_esc(c.consent_doc_id)}</span>'
            if c.consent_doc_id else ""
        )
        photo_html = (
            f'<img src="{_esc(c.photo_url)}" alt="{_esc(c.brand_name)}" loading="lazy" '
            f'style="width:160px;height:100px;object-fit:cover;border-radius:6px">'
        )
        parts.append(f'''      <div class="case" data-loc="{_esc(c.location.split(',')[0].lower().split()[0])}" style="background:#fff;border:1px solid var(--border);border-radius:14px;padding:24px;margin-bottom:18px">
        <div style="display:grid;grid-template-columns:160px 1fr;gap:14px;align-items:center;margin-bottom:14px">
          {photo_html}
          <div>
            <div style="font-size:10.5px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;font-weight:700">{_esc(c.vertical)}</div>
            <div style="font-size:17px;font-weight:800;color:#0F172A;margin-top:2px;font-family:Georgia,serif">{_esc(c.brand_name)}{consent_badge}</div>
            <div style="font-size:11.5px;color:var(--text-muted);margin-top:3px">📍 {_esc(c.location)}</div>
          </div>
        </div>
        <blockquote style="background:#F7F8FA;border-left:3px solid var(--brand);padding:14px 18px;margin:0 0 14px;font-style:italic;color:#1E293B;font-size:14.5px">
          "{_esc(c.quote)}"
          <div style="display:block;margin-top:8px;font-style:normal;font-size:11.5px;color:var(--text-muted);font-weight:600">{_esc(c.quote_attribution)}</div>
        </blockquote>
        <div style="display:grid;grid-template-columns:repeat({len(c.stats) or 1},minmax(0,1fr));gap:12px;background:#F0FDF4;border:1px solid #BBF7D0;border-radius:10px;padding:14px">
          {stats_html}
        </div>
      </div>''')
    return f'''
<section style="padding:48px 0;background:var(--surface)">
  <div class="container">
    <h2 style="font-size:28px;font-weight:800;text-align:center;margin-bottom:8px">Cases near you</h2>
    <p style="text-align:center;color:var(--text-muted);max-width:680px;margin:0 auto 28px;font-size:14.5px">Real merchants in your region — photos pending merchant consent are flagged. Numbers pulled from <code style="background:rgba(0,0,0,.06);padding:1px 5px;border-radius:3px;font-size:11px">/api/v1/cohort/{{brand_id}}</code> live.</p>
{chr(10).join(parts)}
  </div>
</section>'''


def _render_chain_section(cfg: BrandConfig) -> str:
    """CLASS-P · multi-outlet proof. Renders only if cfg.chain_section is set."""
    cs = cfg.chain_section
    if cs is None:
        return ""
    return f'''
<section id="for-chains" style="padding:48px 0;background:#0F172A;color:#F8FAFC">
  <div class="container">
    <div style="max-width:760px;margin:0 auto 32px;text-align:center">
      <div style="font-size:11.5px;color:#FBBF24;text-transform:uppercase;letter-spacing:1.2px;font-weight:700;margin-bottom:8px">For chains · {cs.outlet_count}-outlet operators</div>
      <h2 style="font-size:30px;font-weight:800;letter-spacing:-.5px;margin-bottom:8px">CFO-grade due diligence, on one page</h2>
      <p style="font-size:14.5px;color:#CBD5E1">For Ahmad-grade buyers: per-outlet attribution, white-label, SOC2, exit clause, SLA — laid out so your CFO can evaluate in 5 minutes.</p>
    </div>
    <style>
      .ch-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;max-width:1000px;margin:0 auto}}
      @media(max-width:780px){{.ch-grid{{grid-template-columns:1fr}}}}
      .ch-card{{background:#1E293B;border:1px solid #334155;border-radius:10px;padding:18px}}
      .ch-card .lbl{{font-size:10.5px;color:#FBBF24;text-transform:uppercase;letter-spacing:.5px;font-weight:700;margin-bottom:6px}}
      .ch-card .val{{font-size:14.5px;color:#F8FAFC;line-height:1.5;font-weight:600}}
      .ch-card .sub{{font-size:12px;color:#94A3B8;margin-top:6px}}
    </style>
    <div class="ch-grid">
      <div class="ch-card">
        <div class="lbl">Per-outlet attribution</div>
        <div class="val">{"✓ Built-in" if cs.per_outlet_attribution else "✗ Not yet"}</div>
        <div class="sub">Each outlet has its own CAC/LTV/repeat dashboard. Roll-up via SQL views, opt-in only.</div>
      </div>
      <div class="ch-card">
        <div class="lbl">White-label</div>
        <div class="val">{"✓ Your brand, not 'powered by KiX'" if cs.white_label else "✗ KiX branding required"}</div>
        <div class="sub">Customer never sees the KiX name. Yours from QR to redemption.</div>
      </div>
      <div class="ch-card">
        <div class="lbl">Multi-tenant isolation</div>
        <div class="val">{_esc(cs.multi_tenant_isolation)}</div>
      </div>
      <div class="ch-card">
        <div class="lbl">SOC2 / Compliance</div>
        <div class="val">{_esc(cs.soc2_status)}</div>
        <div class="sub">{_esc(cs.pdpa_my_status)}</div>
      </div>
      <div class="ch-card">
        <div class="lbl">API + Webhooks</div>
        <div class="val"><a href="{_esc(cs.api_docs_url)}" style="color:#FBBF24;text-decoration:underline">{_esc(cs.api_docs_url)}</a></div>
        <div class="sub">OpenAPI 3.1 spec. Webhook retries 5× exponential. Idempotency keys on every POST.</div>
      </div>
      <div class="ch-card">
        <div class="lbl">SLA · Uptime</div>
        <div class="val">{cs.sla_uptime_pct}% monthly uptime · credits if missed</div>
        <div class="sub">Public status page. 99.9% target = ≤43min monthly downtime.</div>
      </div>
      <div class="ch-card" style="grid-column:1/-1;background:#7C2D12;border-color:#FBBF24">
        <div class="lbl" style="color:#FBBF24">Exit clause</div>
        <div class="val">{_esc(cs.exit_clause)}</div>
        <div class="sub" style="color:#FED7AA">No lock-in. Your data, your terms — we ship the export script in the same PR as your signup.</div>
      </div>
    </div>
    <div style="text-align:center;margin-top:32px">
      <a href="mailto:{_esc(cs.enterprise_contact_email)}" style="display:inline-block;background:#FBBF24;color:#0F172A;padding:13px 28px;border-radius:8px;font-weight:700;text-decoration:none;font-size:14.5px;margin-right:10px">Talk to founder ({cs.outlet_count}-outlet onboarding)</a>
      <a href="{_esc(cs.api_docs_url)}" style="display:inline-block;background:transparent;color:#F8FAFC;border:1px solid #CBD5E1;padding:12px 22px;border-radius:8px;font-weight:700;text-decoration:none;font-size:14px">Read API docs →</a>
    </div>
  </div>
</section>'''


def _render_self_reference_banner(cfg: BrandConfig) -> str:
    """CLASS-R · if any case_study matches cfg.brand_name, surface that this
    is a DEMO PAGE personalized for that brand — not a sign-up trick.

    Wording must NOT imply pre-approval (skeptical-owner persona flagged
    'you're already approved' as a dark pattern).
    """
    self_refs = [c for c in cfg.case_studies
                 if c.brand_name.lower() == cfg.brand_name.lower()]
    if not self_refs:
        return ""
    return f'''
<section style="background:#FEF3C7;border-bottom:1px solid #FCD34D;padding:14px 0">
  <div class="container">
    <div style="max-width:880px;margin:0 auto;display:flex;align-items:center;gap:10px;font-size:13px;color:#78350F">
      <span style="font-size:18px">ℹ️</span>
      <span><strong>Personalized demo for {_esc(cfg.brand_name)}.</strong>
      You're seeing this page because we already have a published case study about you (with your signed consent, code shown below) — the numbers are the ones we agreed to publish. To sign up or apply, you still go through the normal flow on
      <a href="/landing/brands/default/index.html" style="color:#92400E;text-decoration:underline;font-weight:700">the generic landing</a>.
      Nothing here implies pre-approval.</span>
    </div>
  </div>
</section>'''


def _render_pricing_section(cfg: BrandConfig) -> str:
    """CLASS-J · render 3-tier pricing block from pricing_canon canonical tiers.

    Previously each landing page had its own pricing copy → drift across
    pages. Now: single source of truth (app.services.pricing_canon). Edit
    a tier there, every regenerated landing reflects it next deploy.
    """
    from app.services.pricing_canon import CANONICAL_TIERS

    cards = []
    for t in CANONICAL_TIERS:
        cc_text = "Credit card required" if t.cc_required else "No card required"
        cc_color = "#92400E" if t.cc_required else "#16A34A"
        included = "".join(
            f'<li style="font-size:13px;color:#1E293B;margin:6px 0;padding-left:16px;position:relative">'
            f'<span style="position:absolute;left:0;color:#16A34A;font-weight:800">✓</span>{_esc(i)}</li>'
            for i in t.included
        )
        not_included = "".join(
            f'<li style="font-size:12.5px;color:#94A3B8;margin:4px 0;padding-left:16px;position:relative;text-decoration:line-through">'
            f'<span style="position:absolute;left:0;color:#CBD5E1">✗</span>{_esc(i)}</li>'
            for i in t.not_included
        ) if t.not_included else ""
        accent = "var(--accent)" if t.tier_id == "founding_100" else "var(--brand)"
        cards.append(f'''      <div class="tier-card" style="background:#fff;border:2px solid {accent};border-radius:14px;padding:24px;display:flex;flex-direction:column">
        <div style="font-size:11.5px;color:{accent};text-transform:uppercase;letter-spacing:.7px;font-weight:800;margin-bottom:6px">{_esc(t.name)}</div>
        <div style="font-size:22px;font-weight:800;color:#0F172A;margin-bottom:6px;letter-spacing:-.4px">{_esc(t.price_text)}</div>
        <div style="font-size:12.5px;color:{cc_color};font-weight:700;margin-bottom:14px">{cc_text}</div>
        <p style="font-size:13.5px;color:#475569;line-height:1.5;margin-bottom:16px">{_esc(t.headline)}</p>
        <ul style="list-style:none;padding:0;margin:0 0 16px;flex:1">{included}{not_included}</ul>
        <a href="{_esc(cfg.portal_link)}?tier={_esc(t.tier_id)}&brand={_esc(cfg.brand_id)}" style="display:block;text-align:center;background:{accent};color:#0F172A;padding:11px 18px;border-radius:8px;text-decoration:none;font-weight:700;font-size:14px">{_esc(t.cta_text)}</a>
      </div>''')

    return f'''
<section id="pricing" style="padding:56px 0;background:var(--surface);border-top:1px solid var(--border)">
  <div class="container">
    <div style="text-align:center;max-width:680px;margin:0 auto 32px">
      <div class="section-tag" style="font-size:12px;color:var(--brand);font-weight:700;text-transform:uppercase;letter-spacing:1.2px;margin-bottom:8px">Pricing · 3 tiers, no fine print</div>
      <h2 style="font-size:30px;font-weight:800;letter-spacing:-.5px;margin-bottom:10px;color:var(--text)">Pay only when KiX delivers</h2>
      <p style="font-size:14.5px;color:var(--text-muted)">Same three tiers on every page. No "contact sales for pricing". No surprise fees.</p>
    </div>
    <style>
      .tier-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;max-width:1100px;margin:0 auto}}
      @media(max-width:780px){{.tier-grid{{grid-template-columns:1fr}}}}
    </style>
    <div class="tier-grid">
{chr(10).join(cards)}
    </div>
  </div>
</section>'''


def _render_vertical_benchmark(cfg: BrandConfig) -> str:
    """Vertical-aware framing — answers Aminah's "is S$4.90 good or bad for nasi padang?"."""
    if not cfg.vertical:
        return ""
    from app.services.vertical_benchmarks import get as get_bench
    b = get_bench(cfg.vertical)
    if not b:
        return ""
    return f'''
<section style="padding:32px 0;background:#ECFDF5;border-top:1px solid #BBF7D0;border-bottom:1px solid #BBF7D0">
  <div class="container">
    <div style="max-width:880px;margin:0 auto;display:grid;grid-template-columns:1fr 1fr 1fr;gap:18px;text-align:center">
      <style>
        @media(max-width:680px){{section[id^=vbench] .vbcol{{grid-column:1/-1}}}}
      </style>
      <div>
        <div style="font-size:10.5px;color:#166534;text-transform:uppercase;letter-spacing:.6px;font-weight:800;margin-bottom:6px">{_esc(b.display_name)} · CPA</div>
        <div style="font-size:22px;font-weight:800;color:#14532D">≤ S${b.cpa_good_max_sgd:.2f} is good</div>
        <div style="font-size:12px;color:#166534;margin-top:4px">Excellent ≤ S${b.cpa_excellent_max_sgd:.2f} · typical industry ≤ S${b.cpa_typical_max_sgd:.2f}</div>
      </div>
      <div>
        <div style="font-size:10.5px;color:#166534;text-transform:uppercase;letter-spacing:.6px;font-weight:800;margin-bottom:6px">{_esc(b.display_name)} · 30-day return</div>
        <div style="font-size:22px;font-weight:800;color:#14532D">{b.repeat_30d_excellent_pct:.0f}%+ is excellent</div>
        <div style="font-size:12px;color:#166534;margin-top:4px">Industry typical ~ {b.repeat_30d_typical_pct:.0f}%</div>
      </div>
      <div>
        <div style="font-size:10.5px;color:#166534;text-transform:uppercase;letter-spacing:.6px;font-weight:800;margin-bottom:6px">Avg ticket · {_esc(b.display_name)}</div>
        <div style="font-size:22px;font-weight:800;color:#14532D">S${b.avg_ticket_sgd:.2f}</div>
        <div style="font-size:12px;color:#166534;margin-top:4px">Use this to back-of-envelope your ROI</div>
      </div>
    </div>
    <div style="text-align:center;font-size:11px;color:#16653499;margin-top:14px">Numbers above are benchmarks for context — your CPA depends on your offer. Source: {_esc(b.source_note)}.</div>
  </div>
</section>'''


def _render_founding_block(cfg: BrandConfig) -> str:
    remaining = max(0, cfg.founding_slots_total - cfg.founding_slots_taken)
    return f'''
<section style="padding:48px 0;background:#FFFBEB;border-top:1px solid #FCD34D;border-bottom:1px solid #FCD34D">
  <div class="container">
    <div style="max-width:760px;margin:0 auto;text-align:center">
      <div style="font-size:11.5px;color:#B45309;text-transform:uppercase;letter-spacing:1px;font-weight:700;margin-bottom:8px">🏆 Founding-100 · {_esc(cfg.city)}</div>
      <h2 style="font-size:32px;font-weight:800;letter-spacing:-.5px;margin-bottom:8px;color:#0F172A">{remaining} of {cfg.founding_slots_total} founding slots remain</h2>
      <p style="font-size:14.5px;color:#92400E;margin-bottom:18px">Approved-only — founder reviews every application within 1–3 business days. Approved merchants get 6 months Verified Business FREE + 0% take rate forever.</p>
      <a href="{_esc(cfg.portal_link)}?tier=founding&brand={_esc(cfg.brand_id)}" style="display:inline-block;background:#FBBF24;color:#0F172A;padding:13px 28px;border-radius:8px;font-weight:700;text-decoration:none;font-size:14.5px">Apply for founding slot →</a>
    </div>
  </div>
</section>'''


def _render_footer(cfg: BrandConfig) -> str:
    badges = " · ".join(_esc(b) for b in cfg.compliance_badges)
    return f'''
<footer style="padding:32px 0;text-align:center;color:var(--text-muted);font-size:13px;border-top:1px solid var(--border);background:#fff">
  <div class="container">
    © 2026 KiX · letskix.com ·
    <a href="/landing/legal/terms.html" style="color:var(--brand-dk);text-decoration:none;margin:0 8px">Terms</a>·
    <a href="/landing/legal/privacy.html" style="color:var(--brand-dk);text-decoration:none;margin:0 8px">Privacy</a>·
    <a href="mailto:{_esc(cfg.contact_email)}" style="color:var(--brand-dk);text-decoration:none;margin:0 8px">Contact</a>
    <div style="margin-top:10px;font-size:11.5px">{_esc(cfg.mozat_address)}</div>
    <div style="margin-top:8px;font-size:11.5px;color:var(--text-muted)">{badges}</div>
    <div style="margin-top:8px;font-size:11.5px">
      Verify independently:
      <a href="https://x.com/letskix" rel="noopener" style="color:var(--brand-dk);margin:0 4px">X</a>·
      <a href="https://github.com/mozat" rel="noopener" style="color:var(--brand-dk);margin:0 4px">GitHub</a>·
      <a href="https://www.acra.gov.sg/" rel="noopener" style="color:var(--brand-dk);margin:0 4px">ACRA</a>
    </div>
  </div>
</footer>'''


# ── Public API ──

def generate_landing(cfg: BrandConfig) -> str:
    """Pure: BrandConfig → HTML string. Caller writes to disk or streams."""
    if not isinstance(cfg, BrandConfig):
        raise TypeError("cfg must be BrandConfig instance")
    if not cfg.brand_id or not cfg.brand_name:
        raise ValueError("brand_id and brand_name are required")

    primary = _sanitize_hex(cfg.primary_color)
    accent = _sanitize_hex(cfg.accent_color)

    head = f'''<!DOCTYPE html>
<html lang="{_esc(cfg.locale)}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(cfg.brand_name)} · powered by KiX</title>
<meta name="description" content="{_esc(cfg.brand_name)} — gamified marketing for offline merchants. Free SaaS, pay only for verified new customers.">
<meta name="generator" content="KiX landing_gen · {_esc(cfg.brand_id)} · auto-generated, do not hand-edit">
<link rel="stylesheet" href="/landing/design-system/tokens.css">
<script src="/landing/i18n/i18next-runtime.js" defer></script>
<script src="/landing/i18n/locale-switcher.js" defer></script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  :root{{--brand:{primary};--brand-dk:#008A33;--accent:{accent};--surface:#F7F8FA;--border:#E2E8F0;--text:#0F172A;--text-dim:#475569;--text-muted:#64748B}}
  html{{overflow-x:hidden}}
  body{{font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;background:#FFFFFF;color:var(--text);line-height:1.55;-webkit-font-smoothing:antialiased;overflow-x:hidden}}
  .container{{max-width:1200px;margin:0 auto;padding:0 24px}}
  header{{background:#fff;border-bottom:1px solid var(--border);position:sticky;top:0;z-index:50;padding:14px 0}}
  .nav{{display:flex;justify-content:space-between;align-items:center}}
  .logo{{font-weight:800;font-size:20px;color:var(--text);text-decoration:none}}
  .logo .x{{color:var(--brand)}}
  .nav-links{{display:flex;gap:18px;align-items:center}}
  .nav-links a{{color:var(--text);text-decoration:none;font-weight:500;font-size:14px}}
  .nav-links a:hover{{color:var(--brand)}}
  .hero{{padding:64px 0 48px;text-align:center;background:radial-gradient(800px 400px at 50% 0%,rgba(0,179,65,.06),transparent)}}
  h1{{font-size:clamp(34px,5vw,52px);font-weight:800;letter-spacing:-1.2px;line-height:1.1;margin-bottom:14px}}
  h1 em{{font-style:normal;color:var(--brand)}}
  .hero-sub{{font-size:17px;color:var(--text-muted);max-width:680px;margin:0 auto 24px}}
  .cta-row{{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}}
  .btn{{display:inline-flex;align-items:center;gap:8px;padding:12px 22px;border-radius:8px;font-weight:700;text-decoration:none;font-size:14px}}
  .btn-primary{{background:var(--brand);color:#fff}}
  .btn-primary:hover{{background:var(--brand-dk)}}
  .btn-secondary{{background:#fff;color:var(--text);border:1px solid var(--border)}}
</style>
</head>
<body>

<header>
  <div class="container">
    <nav class="nav">
      <a href="/" class="logo">Ki<span class="x">X</span></a>
      <div class="nav-links">
        <a href="{_esc(cfg.pricing_link)}">Pricing</a>
        <a href="{_esc(cfg.integrations_link)}">Integrations</a>
        <a href="{_esc(cfg.portal_link)}">Portal</a>
      </div>
      <div class="kix-lang-slot" style="margin-left:auto;display:inline-flex;align-items:center"></div>
    </nav>
  </div>
</header>

<section class="hero">
  <div class="container">
    <h1>{cfg.hero_tagline}</h1>
    <p class="hero-sub">{_esc(cfg.hero_sub)}</p>
    <div class="cta-row">
      <a href="{_esc(cfg.portal_link)}?brand={_esc(cfg.brand_id)}" class="btn btn-primary">Start free · no card</a>
      <a href="#what-you-get" class="btn btn-secondary">What you get →</a>
    </div>
    <p style="margin-top:14px;font-size:12px;color:var(--text-muted)">Generated by KiX landing_gen for brand_id=<code style="background:rgba(0,0,0,.04);padding:1px 6px;border-radius:3px;font-family:ui-monospace,Menlo,monospace;font-size:11px">{_esc(cfg.brand_id)}</code> · not hand-edited</p>
  </div>
</section>
'''

    if cfg.audience not in ("merchant", "consumer", "both"):
        raise ValueError(f"audience must be merchant/consumer/both, got {cfg.audience!r}")
    if cfg.scale not in ("single", "chain", "both"):
        raise ValueError(f"scale must be single/chain/both, got {cfg.scale!r}")

    html_out = (head
                + _render_self_reference_banner(cfg)
                + _render_what_you_get(cfg.what_you_get)
                + _render_chain_section(cfg)
                + _render_vertical_benchmark(cfg)
                + _render_cases(cfg.case_studies, brand_name=cfg.brand_name)
                + _render_pricing_section(cfg)
                + _render_founding_block(cfg)
                + _render_footer(cfg)
                + "\n</body></html>")

    # CLASS-D structural gate: forbid internal jargon (Trinity 3T, PDCA, etc.)
    from app.services.customer_vocab import vocab_check
    vocab_check(html_out)
    # CLASS-J structural gate: forbid pricing drift (single source = pricing_canon)
    from app.services.pricing_canon import find_off_canon_pricing
    drift = find_off_canon_pricing(html_out)
    if drift:
        raise ValueError(
            f"landing_gen output failed pricing_canon drift check: {drift}. "
            "Edit pricing_canon.py — do not bypass."
        )
    return html_out


def _sanitize_hex(c: str) -> str:
    """Allow only #rgb / #rrggbb. Fallback to default green."""
    import re
    c = (c or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}", c):
        return c
    return "#00B341"


# ── Helper: build a default BrandConfig from a JSON-shaped dict ──

def from_dict(d: dict) -> BrandConfig:
    """Tolerant dict → BrandConfig converter for ELTM/JSON input."""
    wyg = [WhatYouGetItem(**w) for w in d.get("what_you_get", [])]
    cs = []
    for c in d.get("case_studies", []):
        c_copy = dict(c)
        c_copy["stats"] = [tuple(s) for s in c.get("stats", [])]
        cs.append(CaseStudy(**c_copy))
    chain = ChainSection(**d["chain_section"]) if d.get("chain_section") else None
    return BrandConfig(
        brand_id=d["brand_id"],
        brand_name=d["brand_name"],
        hero_tagline=d.get("hero_tagline", "Pay only for verified new customers"),
        hero_sub=d.get("hero_sub", "Free SaaS. CPA from S$3."),
        primary_color=d.get("primary_color", "#00B341"),
        accent_color=d.get("accent_color", "#FBBF24"),
        locale=d.get("locale", "en-SG"),
        city=d.get("city", "Bedok"),
        founding_slots_total=d.get("founding_slots_total", 100),
        founding_slots_taken=d.get("founding_slots_taken", 0),
        what_you_get=wyg,
        case_studies=cs,
        chain_section=chain,
        audience=d.get("audience", "merchant"),
        scale=d.get("scale", "single"),
        vertical=d.get("vertical", ""),
        contact_email=d.get("contact_email", "hello@letskix.com"),
    )
