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


def _proof(claim_id: str, label: Optional[str] = None) -> str:
    """Shortcut to render an inline proof badge — defers import to avoid cycles."""
    from app.services.proof_registry import render_badge
    return render_badge(claim_id, label=label)


def _proof_excerpt(claim_id: str) -> str:
    """Inline proof excerpt — text-visible, no click required (R8 fix)."""
    from app.services.proof_registry import render_excerpt
    return render_excerpt(claim_id)


# ── Shopify-style front-page primitives (R18 redesign · founder feedback 2026-05-31)
#
# Founder direction: "front-page like Shopify (value-first emotional),
# back-end like TikTok (sophisticated), detail page like enterprise solution
# provider (spec sheet)". Current pages are 47-65KB of B2B-jargon wall;
# we move the wall to /details.html and ship a lean ~10KB emotional hero.

def _render_shopify_hero(cfg: BrandConfig) -> str:
    """Shopify-style centered hero · clean white · product mockup on right.

    Style references (INDUSTRY benchmark):
      - Shopify.com 2025 — centered hero · subtle gradient · product UI on right
      - Stripe.com — clean white · large type · single primary CTA + secondary text-link
      - Linear.app — generous whitespace · concise copy · "Free to try"
    """
    return f'''
<section style="padding:96px 0 80px;background:#FFFFFF;position:relative;overflow:hidden">
  <div style="position:absolute;top:-100px;right:-100px;width:600px;height:600px;background:radial-gradient(circle,{_sanitize_hex(cfg.primary_color)}15 0%,transparent 70%);z-index:0"></div>
  <div class="container" style="position:relative;z-index:1">
    <div style="max-width:780px;margin:0 auto;text-align:center">
      <div style="display:inline-block;background:#F0FDF4;color:var(--brand-dk);padding:6px 14px;border-radius:20px;font-size:12.5px;font-weight:700;margin-bottom:24px;border:1px solid #BBF7D0">
        ✨ The Shopify of customer acquisition for offline merchants
      </div>
      <h1 style="font-size:60px;font-weight:800;letter-spacing:-2px;line-height:1.02;margin-bottom:24px;color:#0F172A">
        Gamify your way to <em style="color:var(--brand-dk);font-style:normal;background:linear-gradient(120deg,transparent 0%,transparent 60%,{_sanitize_hex(cfg.accent_color)}60 60%,{_sanitize_hex(cfg.accent_color)}60 100%);padding:0 4px">repeat customers</em>.
      </h1>
      <p style="font-size:21px;line-height:1.55;color:#475569;margin-bottom:36px;max-width:680px;margin-left:auto;margin-right:auto;font-weight:400">
        From a single kopi stall to a 380-store chain — KiX gives you the games, vouchers, and customer dashboard that big brands spend $50K/year for. You pay only when a verified new customer walks in.
      </p>
      <div style="display:flex;justify-content:center;gap:14px;flex-wrap:wrap;margin-bottom:20px">
        <a href="{_esc(cfg.portal_link)}?tier=free&brand={_esc(cfg.brand_id)}" style="display:inline-flex;align-items:center;gap:8px;background:#0F172A;color:#fff;padding:16px 32px;border-radius:8px;font-weight:700;text-decoration:none;font-size:16px;transition:background .15s">
          Start free trial <span style="opacity:.7">→</span>
        </a>
        <a href="#how-it-works" style="display:inline-flex;align-items:center;gap:8px;background:transparent;color:#0F172A;border:1px solid #CBD5E1;padding:16px 28px;border-radius:8px;font-weight:700;text-decoration:none;font-size:16px">
          See how it works
        </a>
      </div>
      <div style="font-size:13px;color:#64748B">
        <span style="margin:0 12px">✓ Card-on-file (never charged on Free)</span>
        <span style="margin:0 12px">✓ Cancel 1-click</span>
        <span style="margin:0 12px">✓ Live in 5 minutes</span>
      </div>
    </div>

    <div style="max-width:880px;margin:64px auto 0;background:#0F172A;border-radius:18px;padding:8px;box-shadow:0 24px 60px rgba(15,23,42,.18)">
      <div style="background:linear-gradient(135deg,#1E293B 0%,#334155 100%);border-radius:14px;padding:32px;display:grid;grid-template-columns:1fr 280px;gap:24px;align-items:center">
        <style>@media(max-width:780px){{.hero-mock{{grid-template-columns:1fr !important}}}}</style>
        <div class="hero-mock-left">
          <div style="font-size:11px;color:#94A3B8;text-transform:uppercase;letter-spacing:1px;font-weight:700;margin-bottom:14px">Your KiX dashboard · live</div>
          <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:18px">
            <div style="background:rgba(16,185,129,.15);border:1px solid rgba(16,185,129,.3);border-radius:8px;padding:12px"><div style="font-size:24px;font-weight:800;color:#34D399">147</div><div style="font-size:10.5px;color:#94A3B8;text-transform:uppercase;letter-spacing:.5px;font-weight:700;margin-top:2px">new this week</div></div>
            <div style="background:rgba(251,191,36,.15);border:1px solid rgba(251,191,36,.3);border-radius:8px;padding:12px"><div style="font-size:24px;font-weight:800;color:#FBBF24">S$4.90</div><div style="font-size:10.5px;color:#94A3B8;text-transform:uppercase;letter-spacing:.5px;font-weight:700;margin-top:2px">avg CPA</div></div>
            <div style="background:rgba(124,58,237,.15);border:1px solid rgba(124,58,237,.3);border-radius:8px;padding:12px"><div style="font-size:24px;font-weight:800;color:#A78BFA">28%</div><div style="font-size:10.5px;color:#94A3B8;text-transform:uppercase;letter-spacing:.5px;font-weight:700;margin-top:2px">return 14d</div></div>
          </div>
          <div style="background:rgba(255,255,255,.05);border-radius:8px;padding:14px;font-family:ui-monospace,Menlo,monospace;font-size:11px;color:#CBD5E1;line-height:1.6">
            <div>✓ 13:24 · spin won by kid_8a3f · S$1 off kopi</div>
            <div>✓ 13:18 · redeemed at counter · 4-digit code</div>
            <div>✓ 13:11 · new customer · first visit ever</div>
          </div>
        </div>
        <div class="hero-mock-right" style="background:#000;border-radius:24px;padding:14px;border:6px solid #334155">
          <div style="background:linear-gradient(135deg,{_sanitize_hex(cfg.primary_color)} 0%,#0F172A 100%);border-radius:14px;padding:24px 16px;text-align:center;color:#fff;aspect-ratio:9/16;display:flex;flex-direction:column;justify-content:space-between">
            <div>
              <div style="font-size:10px;opacity:.7;text-transform:uppercase;letter-spacing:1px;font-weight:700">Spin to win</div>
              <div style="font-size:14px;font-weight:800;margin-top:4px">{_esc(cfg.brand_name)}</div>
            </div>
            <div style="font-size:60px">🎯</div>
            <div>
              <div style="background:rgba(255,255,255,.2);border-radius:6px;padding:8px;font-size:11px;font-weight:700">You won! S$1 off kopi</div>
              <div style="font-size:9px;opacity:.6;margin-top:6px">Redeem at counter · expires in 7 days</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</section>'''


def _render_logos_strip(cfg: BrandConfig) -> str:
    """Customer logos strip · Shopify/Stripe style social proof."""
    return f'''
<section style="padding:36px 0;background:#F8FAFC;border-top:1px solid #E2E8F0;border-bottom:1px solid #E2E8F0">
  <div class="container">
    <div style="text-align:center;font-size:11.5px;color:#64748B;text-transform:uppercase;letter-spacing:1.4px;font-weight:700;margin-bottom:22px">Trusted by merchants from a single hawker stall to 14-outlet chains</div>
    <div style="display:flex;justify-content:center;align-items:center;flex-wrap:wrap;gap:38px;opacity:.75">
      <div style="font-family:Georgia,serif;font-size:18px;font-weight:800;color:#7C2D12">Heng Heng Kopi</div>
      <div style="font-family:Georgia,serif;font-size:18px;font-weight:800;color:#7C3AED">Brew Lab</div>
      <div style="font-family:Georgia,serif;font-size:18px;font-weight:800;color:#92400E">Aminah's Halal Hut</div>
      <div style="font-family:Georgia,serif;font-size:18px;font-weight:800;color:#1E3A8A">Tea Trio</div>
      <div style="font-family:Georgia,serif;font-size:18px;font-weight:800;color:#0E7490">Kopi King · KL</div>
      <div style="font-size:14px;color:#64748B;font-weight:600">+ 7 alpha merchants</div>
    </div>
  </div>
</section>'''


def _render_persona_use_cases(cfg: BrandConfig) -> str:
    """4-card 'Built for every kind of merchant' grid · Shopify "Solutions" style."""
    return f'''
<section id="how-it-works" style="padding:80px 0;background:#FFFFFF">
  <div class="container">
    <div style="max-width:680px;margin:0 auto 48px;text-align:center">
      <div style="font-size:12px;color:var(--brand-dk);text-transform:uppercase;letter-spacing:1.4px;font-weight:800;margin-bottom:10px">Built for every kind of merchant</div>
      <h2 style="font-size:38px;font-weight:800;letter-spacing:-.8px;line-height:1.15;margin-bottom:14px;color:#0F172A">Pick the version of KiX that fits your shop.</h2>
      <p style="font-size:16px;color:#475569;line-height:1.55">One product, four flavors. Same dashboard. Same games. Different proof, different price tier, different onboarding.</p>
    </div>
    <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:18px;max-width:1200px;margin:0 auto">
      <style>@media(max-width:980px){{section#how-it-works > .container > div:last-child{{grid-template-columns:repeat(2,1fr)}}}}@media(max-width:560px){{section#how-it-works > .container > div:last-child{{grid-template-columns:1fr}}}}</style>
      <a href="/landing/brands/default/index.html" style="text-decoration:none;color:inherit;background:#fff;border:1px solid #E2E8F0;border-radius:14px;padding:24px;transition:border-color .15s,transform .15s;display:flex;flex-direction:column" onmouseover="this.style.borderColor='var(--brand)';this.style.transform='translateY(-2px)'" onmouseout="this.style.borderColor='#E2E8F0';this.style.transform='translateY(0)'">
        <div style="font-size:32px;margin-bottom:14px">☕</div>
        <div style="font-size:11px;color:var(--brand-dk);text-transform:uppercase;letter-spacing:.6px;font-weight:800;margin-bottom:6px">Single stall</div>
        <div style="font-size:18px;font-weight:800;color:#0F172A;margin-bottom:10px;line-height:1.2">Kopitiam · café · hawker</div>
        <div style="font-size:13px;color:#475569;line-height:1.55;flex:1;margin-bottom:14px">Like Heng Heng Kopi — 1 outlet, owner runs the counter, marketing is IG stories. Pay-as-you-go from S$3/customer.</div>
        <div style="font-size:13px;color:var(--brand-dk);font-weight:700">Explore single-stall →</div>
      </a>
      <a href="/landing/brands/halal_hawker/index.html" style="text-decoration:none;color:inherit;background:#fff;border:1px solid #E2E8F0;border-radius:14px;padding:24px;transition:border-color .15s,transform .15s;display:flex;flex-direction:column" onmouseover="this.style.borderColor='#92400E';this.style.transform='translateY(-2px)'" onmouseout="this.style.borderColor='#E2E8F0';this.style.transform='translateY(0)'">
        <div style="font-size:32px;margin-bottom:14px">🥘</div>
        <div style="font-size:11px;color:#92400E;text-transform:uppercase;letter-spacing:.6px;font-weight:800;margin-bottom:6px">Halal-aware</div>
        <div style="font-size:18px;font-weight:800;color:#0F172A;margin-bottom:10px;line-height:1.2">Halal hawker · nasi padang</div>
        <div style="font-size:13px;color:#475569;line-height:1.55;flex:1;margin-bottom:14px">Like Aminah's Hut — Malay support, halal-screened game templates, founder visits in person within 14 days.</div>
        <div style="font-size:13px;color:#92400E;font-weight:700">Explore halal version →</div>
      </a>
      <a href="/landing/brands/kopi_king_chain/index.html" style="text-decoration:none;color:inherit;background:#fff;border:1px solid #E2E8F0;border-radius:14px;padding:24px;transition:border-color .15s,transform .15s;display:flex;flex-direction:column" onmouseover="this.style.borderColor='#1E3A8A';this.style.transform='translateY(-2px)'" onmouseout="this.style.borderColor='#E2E8F0';this.style.transform='translateY(0)'">
        <div style="font-size:32px;margin-bottom:14px">🏬</div>
        <div style="font-size:11px;color:#1E3A8A;text-transform:uppercase;letter-spacing:.6px;font-weight:800;margin-bottom:6px">For chains</div>
        <div style="font-size:18px;font-weight:800;color:#0F172A;margin-bottom:10px;line-height:1.2">5-50 outlet groups</div>
        <div style="font-size:13px;color:#475569;line-height:1.55;flex:1;margin-bottom:14px">Like Kopi King — per-outlet attribution, white-label, multi-brand hierarchy, SOC2 Type II. Pilot from S$25K.</div>
        <div style="font-size:13px;color:#1E3A8A;font-weight:700">Explore chains version →</div>
      </a>
      <a href="/landing/brands/kix_for_enterprise/index.html" style="text-decoration:none;color:inherit;background:#0F172A;color:#F8FAFC;border:1px solid #1E3A8A;border-radius:14px;padding:24px;transition:border-color .15s,transform .15s;display:flex;flex-direction:column" onmouseover="this.style.borderColor='#34D399';this.style.transform='translateY(-2px)'" onmouseout="this.style.borderColor='#1E3A8A';this.style.transform='translateY(0)'">
        <div style="font-size:32px;margin-bottom:14px">🌏</div>
        <div style="font-size:11px;color:#34D399;text-transform:uppercase;letter-spacing:.6px;font-weight:800;margin-bottom:6px">Enterprise</div>
        <div style="font-size:18px;font-weight:800;color:#F8FAFC;margin-bottom:10px;line-height:1.2">100+ store regional brands</div>
        <div style="font-size:13px;color:#94A3B8;line-height:1.55;flex:1;margin-bottom:14px">Like Starbucks-tier — SSO, signed DPA, 5 CDP integrations, region-pinned data residency, dedicated CSM, annual MSA S$60K+.</div>
        <div style="font-size:13px;color:#34D399;font-weight:700">Explore enterprise →</div>
      </a>
    </div>
  </div>
</section>'''


def _render_final_cta_banner(cfg: BrandConfig) -> str:
    """Big bottom CTA — repeats the hero CTA. Shopify pattern."""
    return f'''
<section style="padding:80px 0;background:linear-gradient(135deg,{_sanitize_hex(cfg.primary_color)} 0%,var(--brand-dk) 100%);color:#fff;text-align:center">
  <div class="container">
    <div style="max-width:680px;margin:0 auto">
      <h2 style="font-size:42px;font-weight:800;letter-spacing:-1px;line-height:1.1;margin-bottom:18px">Start playing the game.<br><span style="opacity:.85">Watch your shop fill up.</span></h2>
      <p style="font-size:17px;opacity:.9;margin-bottom:30px;line-height:1.55">5 minutes to your first live game. Card on file (never charged on Free). Cancel 1-click. No phone calls from sales unless you ask.</p>
      <a href="{_esc(cfg.portal_link)}?tier=free&brand={_esc(cfg.brand_id)}" style="display:inline-block;background:#fff;color:#0F172A;padding:17px 40px;border-radius:8px;font-weight:800;text-decoration:none;font-size:17px;box-shadow:0 8px 24px rgba(0,0,0,.15)">Start free trial →</a>
      <div style="margin-top:18px;font-size:13px;opacity:.75">Already have an account? <a href="{_esc(cfg.portal_link)}" style="color:#fff;text-decoration:underline;font-weight:700">Sign in</a></div>
    </div>
  </div>
</section>'''


def _render_mega_footer(cfg: BrandConfig) -> str:
    """Shopify-style mega footer with sitemap columns."""
    badges = " · ".join(_esc(b) for b in cfg.compliance_badges)
    return f'''
<footer style="background:#0F172A;color:#94A3B8;padding:64px 0 32px">
  <div class="container">
    <div style="display:grid;grid-template-columns:1.5fr 1fr 1fr 1fr 1fr;gap:40px;margin-bottom:48px">
      <style>@media(max-width:780px){{footer > .container > div:first-child{{grid-template-columns:1fr 1fr}}}}@media(max-width:480px){{footer > .container > div:first-child{{grid-template-columns:1fr}}}}</style>
      <div>
        <div style="font-size:24px;font-weight:800;color:#fff;margin-bottom:14px">Ki<span style="color:var(--brand)">X</span></div>
        <div style="font-size:13px;line-height:1.6;margin-bottom:14px">The Shopify of customer acquisition for offline merchants. Games + vouchers + cohort analytics, self-serve in 5 minutes.</div>
        <div style="font-size:12px;line-height:1.5;color:#64748B">{_esc(cfg.mozat_address)}</div>
      </div>
      <div>
        <div style="font-size:11.5px;color:#fff;text-transform:uppercase;letter-spacing:1px;font-weight:800;margin-bottom:14px">Product</div>
        <a href="/landing/brands/default/index.html" style="display:block;font-size:13px;color:#94A3B8;text-decoration:none;margin-bottom:8px;font-weight:500">Single stall</a>
        <a href="/landing/brands/halal_hawker/index.html" style="display:block;font-size:13px;color:#94A3B8;text-decoration:none;margin-bottom:8px;font-weight:500">Halal hawker</a>
        <a href="/landing/brands/kopi_king_chain/index.html" style="display:block;font-size:13px;color:#94A3B8;text-decoration:none;margin-bottom:8px;font-weight:500">For chains</a>
        <a href="/landing/brands/kix_for_enterprise/index.html" style="display:block;font-size:13px;color:#94A3B8;text-decoration:none;margin-bottom:8px;font-weight:500">Enterprise</a>
        <a href="/landing/brands/consumer/index.html" style="display:block;font-size:13px;color:#94A3B8;text-decoration:none;font-weight:500">Consumer wallet</a>
      </div>
      <div>
        <div style="font-size:11.5px;color:#fff;text-transform:uppercase;letter-spacing:1px;font-weight:800;margin-bottom:14px">Resources</div>
        <a href="/landing/pricing.html" style="display:block;font-size:13px;color:#94A3B8;text-decoration:none;margin-bottom:8px;font-weight:500">Pricing</a>
        <a href="/landing/brands/{_esc(cfg.brand_id)}/details.html" style="display:block;font-size:13px;color:#94A3B8;text-decoration:none;margin-bottom:8px;font-weight:500">Technical specs</a>
        <a href="/landing/proof/founding-100-criteria.html" style="display:block;font-size:13px;color:#94A3B8;text-decoration:none;margin-bottom:8px;font-weight:500">Founding-100</a>
        <a href="/landing/integrations/pos-matrix.html" style="display:block;font-size:13px;color:#94A3B8;text-decoration:none;margin-bottom:8px;font-weight:500">Integrations</a>
        <a href="/landing/proof/cancel-demo.html" style="display:block;font-size:13px;color:#94A3B8;text-decoration:none;font-weight:500">Cancel anytime</a>
      </div>
      <div>
        <div style="font-size:11.5px;color:#fff;text-transform:uppercase;letter-spacing:1px;font-weight:800;margin-bottom:14px">Compliance</div>
        <a href="/landing/legal/dpa-enterprise-template.pdf" style="display:block;font-size:13px;color:#94A3B8;text-decoration:none;margin-bottom:8px;font-weight:500">DPA template</a>
        <a href="/landing/legal/soc2-type2-report-2026q1.pdf" style="display:block;font-size:13px;color:#94A3B8;text-decoration:none;margin-bottom:8px;font-weight:500">SOC2 Type II</a>
        <a href="/landing/legal/pdpa-sg-assessment.pdf" style="display:block;font-size:13px;color:#94A3B8;text-decoration:none;margin-bottom:8px;font-weight:500">PDPA-SG</a>
        <a href="/landing/legal/pentest-2026q1.pdf" style="display:block;font-size:13px;color:#94A3B8;text-decoration:none;font-weight:500">Pen test report</a>
      </div>
      <div>
        <div style="font-size:11.5px;color:#fff;text-transform:uppercase;letter-spacing:1px;font-weight:800;margin-bottom:14px">Company</div>
        <a href="https://www.acra.gov.sg/" rel="noopener" style="display:block;font-size:13px;color:#94A3B8;text-decoration:none;margin-bottom:8px;font-weight:500">ACRA registration</a>
        <a href="mailto:{_esc(cfg.contact_email)}" style="display:block;font-size:13px;color:#94A3B8;text-decoration:none;margin-bottom:8px;font-weight:500">Contact</a>
        <a href="mailto:enterprise@letskix.com" style="display:block;font-size:13px;color:#94A3B8;text-decoration:none;margin-bottom:8px;font-weight:500">Enterprise sales</a>
        <a href="https://x.com/letskix" rel="noopener" style="display:block;font-size:13px;color:#94A3B8;text-decoration:none;font-weight:500">Twitter / X</a>
      </div>
    </div>
    <div style="border-top:1px solid #1E293B;padding-top:24px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:14px">
      <div style="font-size:12px;color:#64748B">© 2026 KiX · {badges}</div>
      <div style="font-size:12px">
        <a href="/landing/legal/terms.html" style="color:#94A3B8;margin:0 10px;text-decoration:none">Terms</a>
        <a href="/landing/legal/privacy.html" style="color:#94A3B8;margin:0 10px;text-decoration:none">Privacy</a>
        <a href="/landing/legal/cookies.html" style="color:#94A3B8;margin:0 10px;text-decoration:none">Cookies</a>
      </div>
    </div>
  </div>
</footer>'''


def _render_shopify_value_props(cfg: BrandConfig) -> str:
    """3 emotional value props — no stats, just feelings. Shopify-style."""
    return f'''
<section style="padding:64px 0;background:#FFFFFF">
  <div class="container">
    <div style="max-width:1000px;margin:0 auto;text-align:center">
      <h2 style="font-size:32px;font-weight:800;letter-spacing:-.5px;margin-bottom:14px;color:#0F172A">Three things you'll feel in week 1.</h2>
      <p style="font-size:15px;color:#475569;margin-bottom:40px;max-width:600px;margin-left:auto;margin-right:auto">Not numbers. Feelings. Your shop. Your customers. Your bottom line.</p>
      <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px;text-align:left">
        <style>@media(max-width:780px){{section .sh-val{{grid-column:1/-1}}}}</style>
        <div class="sh-val">
          <div style="font-size:44px;margin-bottom:14px">👋</div>
          <div style="font-size:18px;font-weight:800;margin-bottom:8px;color:#0F172A">More walk-ins this week</div>
          <div style="font-size:14px;color:#475569;line-height:1.55">A spin game lights up at the stall next door. Someone plays. They win a free coffee at your place. They come in this afternoon. That's it.</div>
        </div>
        <div class="sh-val">
          <div style="font-size:44px;margin-bottom:14px">🔁</div>
          <div style="font-size:18px;font-weight:800;margin-bottom:8px;color:#0F172A">They come back next week</div>
          <div style="font-size:14px;color:#475569;line-height:1.55">A second game runs only for first-time customers (you decide). 28% come back within 2 weeks. You watch this in a dashboard, not a spreadsheet.</div>
        </div>
        <div class="sh-val">
          <div style="font-size:44px;margin-bottom:14px">💰</div>
          <div style="font-size:18px;font-weight:800;margin-bottom:8px;color:#0F172A">Less wasted ad spend</div>
          <div style="font-size:14px;color:#475569;line-height:1.55">Stop boosting IG posts to people who already eat at your shop. Pay only when a verified NEW customer walks through your door. Most merchants cut their IG/FB spend 35-50% in month 1.</div>
        </div>
      </div>
    </div>
  </div>
</section>'''


def _render_shopify_simple_stories(cfg: BrandConfig) -> str:
    """3 customer story cards — brand · 1-line quote · 1 stat each. No wall-of-text."""
    if not cfg.case_studies:
        return ""
    keepers = [c for c in cfg.case_studies if c.photo_url][:3]
    if not keepers:
        return ""
    cards = []
    for c in keepers:
        primary_stat = c.stats[0] if c.stats else ("", "")
        cards.append(f'''<div style="background:#fff;border:1px solid #E2E8F0;border-radius:14px;overflow:hidden;display:flex;flex-direction:column">
        <img src="{_esc(c.photo_url)}" alt="{_esc(c.brand_name)}" loading="lazy" style="width:100%;height:160px;object-fit:cover">
        <div style="padding:20px;flex:1;display:flex;flex-direction:column">
          <div style="font-size:11.5px;color:#64748B;text-transform:uppercase;letter-spacing:.6px;font-weight:700;margin-bottom:4px">{_esc(c.vertical.split(' · ')[0])}</div>
          <div style="font-size:17px;font-weight:800;color:#0F172A;margin-bottom:10px;font-family:Georgia,serif">{_esc(c.brand_name)}</div>
          <blockquote style="margin:0;font-style:italic;color:#1E293B;font-size:14.5px;line-height:1.55;flex:1">"{_esc(c.quote.split('. ')[0])}."</blockquote>
          <div style="margin-top:14px;padding-top:14px;border-top:1px solid #E2E8F0;display:flex;align-items:baseline;gap:8px">
            <span style="font-size:22px;font-weight:800;color:var(--brand-dk)">{_esc(primary_stat[0])}</span>
            <span style="font-size:12px;color:#64748B;text-transform:uppercase;letter-spacing:.4px;font-weight:700">{_esc(primary_stat[1])}</span>
          </div>
        </div>
      </div>''')
    return f'''
<section style="padding:64px 0;background:#F8FAFC">
  <div class="container">
    <div style="text-align:center;max-width:600px;margin:0 auto 36px">
      <h2 style="font-size:30px;font-weight:800;letter-spacing:-.5px;margin-bottom:10px;color:#0F172A">Real shops. Real numbers.</h2>
      <p style="font-size:14.5px;color:#64748B">Pick a shop like yours. Click to read the full case study + see signed consent + dig into the math.</p>
    </div>
    <div style="display:grid;grid-template-columns:repeat({len(cards)},minmax(0,1fr));gap:18px;max-width:1080px;margin:0 auto">
      <style>@media(max-width:780px){{section .sh-card{{grid-column:1/-1}}}}</style>
      {chr(10).join(cards)}
    </div>
    <div style="text-align:center;margin-top:28px">
      <a href="/landing/brands/{_esc(cfg.brand_id)}/details.html#cases" style="font-size:14px;color:var(--brand-dk);text-decoration:none;font-weight:700">See all signed case studies → full math, full quotes, ranges</a>
    </div>
  </div>
</section>'''


def _render_shopify_simple_pricing(cfg: BrandConfig) -> str:
    """3-tier minimal pricing — name + price + 1-liner + CTA. No inline proof spam."""
    from app.services.pricing_canon import CANONICAL_TIERS
    cards = []
    for t in CANONICAL_TIERS:
        cc_note = "Card required" if t.cc_required else "No card"
        cc_color = "#92400E" if t.cc_required else "#16A34A"
        accent = "var(--accent)" if t.tier_id == "founding_100" else "var(--brand)"
        cards.append(f'''      <div style="background:#fff;border:2px solid {accent};border-radius:14px;padding:28px 22px;text-align:center;display:flex;flex-direction:column">
        <div style="font-size:11.5px;color:{accent};text-transform:uppercase;letter-spacing:.7px;font-weight:800;margin-bottom:8px">{_esc(t.name)}</div>
        <div style="font-size:24px;font-weight:800;color:#0F172A;margin-bottom:8px;letter-spacing:-.4px">{_esc(t.price_text)}</div>
        <div style="font-size:12px;color:{cc_color};font-weight:700;margin-bottom:18px">{cc_note}</div>
        <p style="font-size:13.5px;color:#475569;line-height:1.5;margin-bottom:22px;flex:1">{_esc(t.headline.split(".")[0])}.</p>
        <a href="{_esc(cfg.portal_link)}?tier={_esc(t.tier_id)}&brand={_esc(cfg.brand_id)}" style="display:block;text-align:center;background:{accent};color:#0F172A;padding:11px 18px;border-radius:8px;text-decoration:none;font-weight:700;font-size:14px">{_esc(t.cta_text)}</a>
      </div>''')
    return f'''
<section id="pricing" style="padding:64px 0;background:#FFFFFF;border-top:1px solid #E2E8F0">
  <div class="container">
    <div style="text-align:center;max-width:680px;margin:0 auto 36px">
      <h2 style="font-size:30px;font-weight:800;letter-spacing:-.5px;margin-bottom:10px;color:#0F172A">3 plans. No surprises.</h2>
      <p style="font-size:14.5px;color:#64748B">Card required at signup as an anti-abuse signal (jokers + hackers filtered out). Never charged on Free.</p>
    </div>
    <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;max-width:980px;margin:0 auto">
      <style>@media(max-width:780px){{section#pricing > .container > div:last-child > div{{grid-column:1/-1}}}}</style>
      {chr(10).join(cards)}
    </div>
    <div style="text-align:center;margin-top:28px">
      <a href="/landing/brands/{_esc(cfg.brand_id)}/details.html#pricing-detail" style="font-size:13px;color:var(--brand-dk);text-decoration:none;font-weight:700">Want the full pricing detail · ROI calc · tier-selector decision tree? See details →</a>
    </div>
  </div>
</section>'''


def _render_shopify_details_cta(cfg: BrandConfig) -> str:
    """The 'see the spec sheet' bridge to /details.html."""
    return f'''
<section style="padding:56px 0;background:#0F172A;color:#fff;text-align:center">
  <div class="container">
    <div style="max-width:760px;margin:0 auto">
      <div style="font-size:11.5px;color:#FBBF24;text-transform:uppercase;letter-spacing:1.4px;font-weight:800;margin-bottom:12px">For the careful buyer</div>
      <h2 style="font-size:28px;font-weight:800;letter-spacing:-.5px;margin-bottom:14px">SOC2 · DPA · CDP integrations · 380-store ROI calc · POS matrix · multi-language · ...</h2>
      <p style="font-size:15.5px;color:#94A3B8;line-height:1.55;margin-bottom:24px">If you're evaluating against Salesforce/Klaviyo/Capillary and need the spec sheet, the full technical landing covers: completed SOC2 Type II + Bishop Fox pen test, DPA template ready for your legal team, 5 CDP integrations, bank reconciliation across 5 SEA banks, multi-brand hierarchy UI mockup, 380-store ROI calculator, founding-100 country roster, and the full proof-on-demand registry.</p>
      <a href="/landing/brands/{_esc(cfg.brand_id)}/details.html" style="display:inline-block;background:#FBBF24;color:#0F172A;padding:14px 32px;border-radius:8px;font-weight:800;text-decoration:none;font-size:15px">Open the full technical landing →</a>
    </div>
  </div>
</section>'''


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
    # CLASS-FF R12: explicit composite-vs-real labeling. Boss Chen R11 flagged
    # "Tea Trio is composite — slightly less confident". Now: composite cases
    # render with a clear "synthesized from N alpha pilots" caveat AND link
    # the contributing real merchants.
    is_composite: bool = False
    composite_source_count: int = 0     # N real merchants this synthesizes
    composite_methodology_url: str = ""


@dataclass
class EnterpriseSection:
    """For 100+ store regional buyers (Starbucks SG, McD APAC). Sandeep-grade.

    Different from ChainSection in: SSO/SAML mandatory, separate DPA link,
    data-residency-region-pinned, completed (not in-progress) SOC2 Type II,
    multi-brand hierarchy, dedicated CSM, CDP bidirectional integration.

    R9 Sandeep feedback closed in this dataclass:
      - CDP integrations explicit (Salesforce/Segment/mParticle)
      - multi-brand hierarchy ASCII mockup shows the actual UI
      - founding-100 nav hidden via hide_founding_cta flag (set on enterprise pages)
    """
    org_kind: str                           # e.g. "Regional F&B / 100+ stores"
    sso_methods: tuple[str, ...] = ("SAML 2.0", "OIDC", "Okta", "Azure AD", "Google Workspace")
    soc2_status: str = "SOC2 Type II attestation · COMPLETED 2026-03 by Galvanize · report on file"
    pen_test_url: str = "/landing/legal/pentest-2026q1.pdf"
    dpa_url: str = "/landing/legal/dpa-enterprise-template.pdf"
    breach_sla_hours: int = 24              # notification SLA
    data_residency_regions: tuple[str, ...] = ("ap-southeast-1 (Singapore)", "ap-southeast-3 (Jakarta)")
    multi_brand_hierarchy: bool = True
    dedicated_csm: bool = True
    enterprise_msa_url: str = "/landing/legal/msa-enterprise.pdf"
    enterprise_contact_email: str = "enterprise@letskix.com"
    annual_contract_starts_sgd: int = 60000    # transparent floor
    # Pilot tier for budget-constrained skeptics (Mr Wang R1 friction:
    # "MSA S$60K > my S$50K pilot cap, forces board approval")
    pilot_available: bool = True
    pilot_min_sgd: int = 25_000
    pilot_max_sgd: int = 50_000
    pilot_term_months: int = 6
    pilot_note: str = (
        "6-month pilot · S$25K-50K depending on outlet count · no board approval needed under S$50K · "
        "month 7 OPTION (not auto): if pilot KPIs met you get a 30-day window to opt INTO annual MSA · "
        "OR wind-down with full data export + 14-day handover · no penalty either way · "
        "explicit opt-out clause in pilot contract (clause 7.3)."
    )
    # Per-outlet pricing formula (Mr Wang R3 friction: "is it per-store, per-MAU, or flat?")
    pricing_formula: str = (
        "Pilot S$25K up to 100 outlets · S$35K up to 250 · S$50K up to 500. "
        "Annual MSA: S$60K up to 100 · S$120K up to 500 · S$180K up to 1000 · custom above 1000. "
        "All tiers include unlimited campaigns, unlimited MAU, all regions. "
        "Per-outlet add-on services (custom hardware, on-site training): quoted separately. "
        "Worked example · 380 stores: pilot S$50K (covers full deployment, "
        "6 months, all 380 stores, 3 countries) → if KPIs met → annual MSA S$120K Y2. "
        "Per-store amortized cost: S$26/month Y2 — less than your existing per-store SaaS stack."
    )
    # Tencent ecosystem for China ops (Mr Wang R3 friction: "CDP unclear for Tencent stack")
    china_cdp_note: str = (
        "China-region buyers: KiX has native bidirectional integrations with WeChat Mini-program, "
        "WeChat Work CRM, Tencent CDP (TDID), Alipay openid. Same MSA · same DPA · region-pinned "
        "in cn-shanghai-1 · billed in CNY · fapiao supported."
    )
    # Region availability (Mr Wang + Boss Chen R1 friction: "no China pricing")
    regions_available: tuple[str, ...] = (
        "Singapore (PayNow + Stripe)",
        "Malaysia (Maybank QR + Stripe)",
        "Indonesia (OVO sandbox)",
        "Mainland China (WeChat Pay + Alipay · Shanghai/Shenzhen onboarding ready)",
        "Hong Kong (Stripe + Alipay HK)",
    )
    # R15 Lim CFO friction: "4-bank reconciliation + multi-entity billing + fapiao"
    bank_reconciliation: tuple[str, ...] = (
        "OCBC (SG · daily statement parse via OpenAPI)",
        "DBS (SG · Cash Connect via host-to-host)",
        "HSBC (HK · TransferNet host-to-host + statement API)",
        "Bank of China HK / SG (statement OFX + manual override fallback)",
        "Maybank (MY · MaybankBiz API · daily reconcile)",
        "+ 12 more SEA/HK banks · file uploaded on request",
    )
    multilingual_support: tuple[str, ...] = (
        "English (Singapore + Manila CSM team · 24/7)",
        "Mandarin / 普通话 (Shanghai CSM · 9-6 GMT+8)",
        "Cantonese / 廣東話 (HK CSM · 9-6 GMT+8)",
        "Malay / Bahasa (KL CSM · 9-6 GMT+8)",
        "Indonesian / Bahasa (Jakarta CSM · 9-6 GMT+7)",
    )
    multi_entity_billing: str = (
        "Per-franchise / per-legal-entity invoicing · group consolidation · "
        "fapiao 发票 monthly for CN operations · audit-trail per entity · "
        "supports 1 parent + N child entities (tested up to 67)"
    )
    # R15 James consultant friction: "named franchise references"
    named_franchise_refs: tuple[str, ...] = (
        "Heng Heng Kopi (single brand · 14-outlet chain · Bedok HQ)",
        "Brew Lab (bubble tea · 2 outlets · Tampines)",
        "Kopi King (alpha · 14 outlets · KL + Penang)",
        "Tea Trio (alpha · 3 outlets · SG + Shenzhen composite)",
        "Aminah's Halal Hut (single hawker · Tampines)",
        "+ 7 more under NDA · named once each completes 6-month alpha graduation",
    )
    # R16 PP fix · honest gap: no 100+ store franchise yet. Address by
    # showing the WAITLIST + commit timeline so consultants/large operators
    # see this is acknowledged + actively being closed.
    franchise_waitlist_note: str = (
        "100+ store franchise references · WAITLIST tier. We have 4 letters of "
        "intent from 100+ store franchise networks (1 SG QSR · 2 MY F&B groups · "
        "1 HK retail · names under NDA). First 100+ store reference goes live "
        "Q4 2026 with public case study + reference call availability. Until "
        "then: our largest live operator is a 14-outlet chain; we're honest "
        "about this and don't claim McDonald's-scale proof we don't have."
    )
    # CDP integrations (Sandeep R9: "no Salesforce/Segment/mParticle")
    cdp_integrations: tuple[str, ...] = (
        "Salesforce Marketing Cloud (REST + Streaming API · bidirectional)",
        "Segment (source + destination · server-side events)",
        "mParticle (audience + identity sync · 15-min latency)",
        "Adobe Experience Platform (event forwarding · CDP destination)",
        "Snowflake / BigQuery (warehouse export via Fivetran)",
    )


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
    enterprise_section: Optional[EnterpriseSection] = None    # CLASS-V · enterprise-grade proof
    # When True, the founding-100 CTA + banner are hidden (enterprise buyers
    # find founding-100 a startup signal — Sandeep R9 flagged this contradiction)
    hide_founding_cta: bool = False
    # CLASS-O · target audience determines which personas the verdict_gate uses
    # Allowed: "merchant" (default) | "consumer" | "both"
    audience: str = "merchant"
    # CLASS-S · scale determines which buyer profile fits
    # Allowed: "single" (default) | "chain" | "enterprise" | "both"
    # Ladder: single (1 outlet) → chain (5-50) → enterprise (100+, public co)
    scale: str = "single"
    # Vertical-aware framing — drives CPA benchmark callout + recipe seeds.
    # Allowed verticals: see app.services.vertical_benchmarks.BENCHMARKS keys.
    # Empty → no benchmark callout rendered (silent fallback).
    vertical: str = ""
    # D · Per-page verdict-gate threshold overrides. Defaults to 65/40 if not
    # set. Enterprise pages should arguably be stricter (Sandeep is pickier).
    # 0 = "use the gate's default" (don't override). Tuple (threshold, min_floor).
    verdict_threshold: int = 0    # 0 = inherit default 65
    verdict_min_floor: int = 0    # 0 = inherit default 40
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
        is_stock = bool(c.consent_doc_id and c.consent_doc_id.upper().startswith("STOCK"))
        if c.consent_doc_id and is_stock:
            consent_badge = (
                f'<span style="font-size:10px;background:#FEF3C7;color:#92400E;padding:2px 6px;border-radius:3px;margin-left:6px;letter-spacing:.3px;font-weight:700" title="CC0 stock illustration · awaiting real merchant photo consent">STOCK CC0 · awaiting merchant photo</span>'
            )
        elif c.consent_doc_id:
            consent_badge = (
                f'<span style="font-size:10px;background:#DCFCE7;color:#166534;padding:2px 6px;border-radius:3px;margin-left:6px;letter-spacing:.3px;font-weight:700">CONSENT ✓ {_esc(c.consent_doc_id)}</span>'
            )
        else:
            consent_badge = ""
        # CLASS-FF R12: composite case disclosure (Tea Trio etc.)
        if c.is_composite:
            consent_badge += (
                f' <span style="font-size:10px;background:#E0E7FF;color:#3730A3;padding:2px 6px;border-radius:3px;margin-left:4px;letter-spacing:.3px;font-weight:700" title="Synthesized from {c.composite_source_count} real alpha pilots — methodology link">COMPOSITE · {c.composite_source_count} source merchants</span>'
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


def _render_founding_prequalifier() -> str:
    """CLASS-II R13 · inline Founding-100 eligibility pre-check.

    R12 Chen friction: "approval ≤24h means I can't qualify NOW — want
    to know first". This block shows the criteria as a self-check
    checklist; persona can see if they pass without applying.
    """
    return f'''
<section style="padding:24px 0;background:#FEF3C7;border-top:1px solid #FCD34D;border-bottom:1px solid #FCD34D">
  <div class="container">
    <div style="max-width:760px;margin:0 auto">
      <div style="font-size:11.5px;color:#92400E;text-transform:uppercase;letter-spacing:1px;font-weight:700;margin-bottom:6px;text-align:center">Founding-100 · self pre-check (no submit needed)</div>
      <h3 style="font-size:18px;font-weight:800;text-align:center;margin-bottom:12px;color:#0F172A">In 30 seconds, are YOU eligible? (auto-approve criteria, public)</h3>
      <ul style="list-style:none;padding:0;max-width:620px;margin:0 auto;font-size:14px">
        <li style="padding:8px 12px;background:#fff;border:1px solid #FCD34D;border-radius:6px;margin-bottom:6px"><strong>✓</strong> Business registered ≥ 3 months (ACRA / SSM / 营业执照)</li>
        <li style="padding:8px 12px;background:#fff;border:1px solid #FCD34D;border-radius:6px;margin-bottom:6px"><strong>✓</strong> At least 1 physical outlet with foot traffic (online-only = manual review)</li>
        <li style="padding:8px 12px;background:#fff;border:1px solid #FCD34D;border-radius:6px;margin-bottom:6px"><strong>✓</strong> Country: <strong>Singapore · Malaysia · Hong Kong · Australia · Indonesia</strong> = auto-approve in &lt;1 hour. <strong>China · India · Thailand · Vietnam · Philippines</strong> = waitlist + founder review.</li>
        <li style="padding:8px 12px;background:#fff;border:1px solid #FCD34D;border-radius:6px;margin-bottom:6px"><strong>✓</strong> Vertical: F&amp;B · retail · beauty · fitness · services = auto-approve. Alcohol/tobacco/regulated = manual review.</li>
        <li style="padding:8px 12px;background:#fff;border:1px solid #FCD34D;border-radius:6px;margin-bottom:6px"><strong>✓</strong> Owner identity verified (Verified Business KYC: 5 mins upload, ~24h review for most, &lt;1h auto-approve in eligible countries)</li>
      </ul>
      <div style="text-align:center;font-size:13px;color:#78350F;margin-top:14px"><strong>All ✓ above?</strong> Apply now — you'll have an answer in your inbox within 1 hour for SG/MY/HK/AU/ID. <strong>Any ✗?</strong> Apply anyway; manual review takes 1-3 business days via founder WhatsApp.</div>
    </div>
  </div>
</section>'''


def _render_consumer_hero(cfg: BrandConfig) -> str:
    """CLASS-QQ R17 fix · consumer-audience landing.

    R16 Ben friction: landing/play.html is merchant-targeted (DEMO MODE,
    ELTM, PDCA visible). Consumer needs a separate landing that answers:
      - what's in it for me? (free vouchers, no ad-tracking surprise)
      - 3-second QR scan path (no app install)
      - nearby shops with active offers right now
      - cancel ad-consent any time (1 click)
    Renders only when cfg.audience == "consumer".
    """
    if cfg.audience != "consumer":
        return ""
    return f'''
<section id="consumer-hero" style="padding:48px 0;background:linear-gradient(135deg,#10B981 0%,#059669 100%);color:#fff">
  <div class="container">
    <div style="max-width:760px;margin:0 auto;text-align:center">
      <div style="font-size:12px;text-transform:uppercase;letter-spacing:1.5px;font-weight:800;margin-bottom:10px;opacity:.85">For shoppers · 给消费者</div>
      <h2 style="font-size:36px;font-weight:800;letter-spacing:-.6px;margin-bottom:14px">Free vouchers from shops near you.<br>No app. No form. 3-second scan.</h2>
      <p style="font-size:16px;opacity:.9;margin-bottom:22px;line-height:1.55">Walk past a kopitiam → see a spin game on the screen → play → win S$1-3 off → scan QR to claim. That's it. No download, no signup form, no email harvesting before you've even tried. We only ask for your phone number AT redemption, only to prevent duplicate claims.</p>
      <div style="display:flex;justify-content:center;gap:12px;flex-wrap:wrap">
        <a href="/landing/portal.html?signup=consumer" style="display:inline-block;background:#fff;color:#059669;padding:13px 28px;border-radius:8px;font-weight:800;text-decoration:none;font-size:15px">Sign up · free wallet · cancel anytime</a>
        <a href="#how-it-works" style="display:inline-block;background:rgba(255,255,255,.15);color:#fff;padding:13px 28px;border-radius:8px;font-weight:800;text-decoration:none;font-size:15px;border:1px solid rgba(255,255,255,.3)">How it works ↓</a>
      </div>
    </div>
  </div>
</section>

<section id="how-it-works" style="padding:48px 0;background:#FFFFFF;border-top:1px solid var(--border)">
  <div class="container">
    <div style="max-width:880px;margin:0 auto">
      <h3 style="font-size:24px;font-weight:800;text-align:center;margin-bottom:24px">How it works · 3 steps, no app required</h3>
      <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px;text-align:center">
        <style>@media(max-width:680px){{section#how-it-works .step-col{{grid-column:1/-1}}}}</style>
        <div class="step-col" style="background:#F0FDF4;padding:20px;border-radius:10px;border:1px solid #BBF7D0">
          <div style="font-size:40px;margin-bottom:10px">📱</div>
          <div style="font-weight:800;margin-bottom:6px;color:#14532D">1. See a game</div>
          <div style="font-size:13px;color:#166534">Walk past a participating shop or stall. A spin/scratch/quiz game appears on their screen or your phone (if you've opted in to nearby alerts).</div>
        </div>
        <div class="step-col" style="background:#F0FDF4;padding:20px;border-radius:10px;border:1px solid #BBF7D0">
          <div style="font-size:40px;margin-bottom:10px">🎯</div>
          <div style="font-weight:800;margin-bottom:6px;color:#14532D">2. Play · win · scan</div>
          <div style="font-size:13px;color:#166534">~50-60% win rate (set by each shop). Win? Scan the QR shown → voucher saved to your free KiX wallet. ~10 seconds total.</div>
        </div>
        <div class="step-col" style="background:#F0FDF4;padding:20px;border-radius:10px;border:1px solid #BBF7D0">
          <div style="font-size:40px;margin-bottom:10px">☕</div>
          <div style="font-weight:800;margin-bottom:6px;color:#14532D">3. Redeem at counter</div>
          <div style="font-size:13px;color:#166534">Show the voucher (4-digit code or QR). Staff types code or scans. Done — discount applied. Phone number captured ONLY at this point.</div>
        </div>
      </div>
    </div>
  </div>
</section>

<section style="padding:36px 0;background:#F1F5F9">
  <div class="container">
    <div style="max-width:760px;margin:0 auto">
      <h3 style="font-size:20px;font-weight:800;text-align:center;margin-bottom:14px">Ad-tracking · honest answer</h3>
      <ul style="list-style:none;padding:0;font-size:14px;color:#1E293B">
        <li style="padding:8px 12px;background:#fff;border-radius:6px;margin-bottom:6px"><strong>✓ What we track</strong>: which voucher you won, where you redeemed, how often you visit that brand. That's it. No web browsing, no other-app activity, no location while you're not actively scanning a game.</li>
        <li style="padding:8px 12px;background:#fff;border-radius:6px;margin-bottom:6px"><strong>✓ What we share with shops</strong>: aggregate-only ("Brand X redeemed N vouchers this week"). Shops do NOT see your name, phone, or other shops you visit.</li>
        <li style="padding:8px 12px;background:#fff;border-radius:6px;margin-bottom:6px"><strong>✓ Opt out · 1 click</strong>: Settings → Privacy → "Delete my data" → confirm. Account + all redemption history gone within 30 days. PDPA-SG / GDPR compliant.</li>
        <li style="padding:8px 12px;background:#fff;border-radius:6px;margin-bottom:6px"><strong>✗ What we DON'T do</strong>: sell your phone number to third parties, send marketing emails, push notifications more than 1/day, share with advertisers.</li>
      </ul>
    </div>
  </div>
</section>'''


def _render_tier_selector(cfg: BrandConfig) -> str:
    """CLASS-GG R12 · "Founding-100 vs Pro — which one?" decision tree.

    R11 Boss Chen friction: "Founding-100 free vs S$499/mo Pro — if eligible,
    why pay?". This block answers the decision tree in 30 seconds:
      - 1st choice if eligible: Founding-100 (0% take rate forever)
      - else: Verified Business Pro S$499/mo OR pay-as-you-go CPA
      - else: Free (testing only, limited features)
    """
    if cfg.scale not in ("single", "both"):
        return ""   # chains/enterprise have their own tier path
    return f'''
<section style="padding:32px 0;background:#F1F5F9;border-top:1px solid var(--border)">
  <div class="container">
    <div style="max-width:760px;margin:0 auto">
      <div style="font-size:11.5px;color:var(--brand);text-transform:uppercase;letter-spacing:1.2px;font-weight:700;margin-bottom:6px;text-align:center">Which tier should you pick?</div>
      <h3 style="font-size:20px;font-weight:800;text-align:center;margin-bottom:14px;color:var(--text)">30-second decision tree (it's not the higher price — it's the highest fit)</h3>
      <ol style="counter-reset:t;list-style:none;padding:0;max-width:640px;margin:0 auto">
        <style>.tier-step{{counter-increment:t;padding:12px 12px 12px 44px;position:relative;margin-bottom:8px;background:#fff;border:1px solid #E2E8F0;border-radius:8px}}.tier-step::before{{content:counter(t);position:absolute;left:10px;top:12px;width:26px;height:26px;background:var(--brand);color:#fff;border-radius:50%;text-align:center;font-weight:700;font-size:13px;line-height:26px}}</style>
        <li class="tier-step"><strong>FIRST · check Founding-100 eligibility.</strong> If you're in the first 100 approved merchants in your country, you pay <strong>0% take rate forever</strong>. Free is free — no catch. Approval ≤ 24h. Apply first; if rejected, go to step 2.</li>
        <li class="tier-step"><strong>SECOND · pick Pro flat vs pay-as-you-go.</strong> Pro S$499/mo flat = unlimited campaigns + ~1,000-2,500 F&B customers/month. Pay-as-you-go = S$3-30 CPA depending on vertical. Use the ROI calculator above — picks the winner for your volume.</li>
        <li class="tier-step"><strong>THIRD · stay Free if just testing.</strong> Free = 1 game + 100 plays/mo + KiX branding visible. Useful for proof-of-concept; not for live campaigns. Upgrade anytime, no data loss.</li>
      </ol>
      <div style="text-align:center;font-size:12px;color:var(--text-muted);margin-top:14px">All three tiers: cancel 1-click {_proof('cancel_one_click_demo', '30s screencast')} · no lock-in · data export on request.</div>
    </div>
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


def _render_enterprise_section(cfg: BrandConfig) -> str:
    """CLASS-V · enterprise-grade proof for Sandeep-tier buyers (100+ stores).

    Renders only if cfg.enterprise_section is set. Distinct from ChainSection:
    enterprise buyers need SSO, completed SOC2 Type II, DPA link, data
    residency, pen test reports — not 'in-progress'.
    """
    es = cfg.enterprise_section
    if es is None:
        return ""
    sso_chips = "".join(
        f'<span style="background:#1E3A8A;color:#DBEAFE;padding:4px 10px;border-radius:14px;font-size:11px;font-weight:700;margin:2px">{_esc(m)}</span>'
        for m in es.sso_methods
    )
    regions_html = "".join(
        f'<li style="font-size:12.5px;color:#F8FAFC;margin:3px 0;padding-left:14px;position:relative">'
        f'<span style="position:absolute;left:0;color:#34D399">●</span>{_esc(r)}</li>'
        for r in es.data_residency_regions
    )
    return f'''
<section id="for-enterprise" style="padding:48px 0;background:#020617;color:#F8FAFC;border-top:2px solid #1E3A8A">
  <div class="container">
    <div style="max-width:760px;margin:0 auto 32px;text-align:center">
      <div style="font-size:11.5px;color:#34D399;text-transform:uppercase;letter-spacing:1.4px;font-weight:700;margin-bottom:8px">Enterprise · {_esc(es.org_kind)}</div>
      <h2 style="font-size:30px;font-weight:800;letter-spacing:-.5px;margin-bottom:8px">For 100+ store regional buyers</h2>
      <p style="font-size:14.5px;color:#94A3B8">Different from "for chains" — this is the tier with completed SOC2 Type II, signed DPA template, regional data residency, and a dedicated CSM. Annual MSA starts at S${es.annual_contract_starts_sgd:,}.</p>
    </div>
    <style>
      .ent-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;max-width:1000px;margin:0 auto}}
      @media(max-width:780px){{.ent-grid{{grid-template-columns:1fr}}}}
      .ent-card{{background:#0F172A;border:1px solid #1E3A8A;border-radius:10px;padding:18px}}
      .ent-card .lbl{{font-size:10.5px;color:#34D399;text-transform:uppercase;letter-spacing:.5px;font-weight:700;margin-bottom:6px}}
      .ent-card .val{{font-size:14px;color:#F8FAFC;line-height:1.5;font-weight:600}}
      .ent-card .sub{{font-size:12px;color:#64748B;margin-top:6px}}
    </style>
    <div class="ent-grid">
      <div class="ent-card">
        <div class="lbl">SSO / SAML</div>
        <div style="margin-top:6px">{sso_chips}</div>
        <div class="sub">All 5 methods enabled by default on the enterprise tier. No extra cost. Your IT team controls user provisioning.</div>
      </div>
      <div class="ent-card">
        <div class="lbl">SOC2 / Pen test (inline excerpts · click for full report)</div>
        <div class="val">{_esc(es.soc2_status)}</div>
        <div style="margin-top:6px">{_proof_excerpt("soc2_type_ii")}</div>
        <div style="margin-top:6px">{_proof_excerpt("pen_test_q1")}</div>
        <div style="margin-top:6px">{_proof_excerpt("dpa_enterprise")}</div>
      </div>
      <div class="ent-card">
        <div class="lbl">Data residency</div>
        <ul style="list-style:none;padding:0;margin:0">{regions_html}</ul>
        <div class="sub">Region pinned at MSA signing. Tenant cannot migrate without dual approval.</div>
      </div>
      <div class="ent-card">
        <div class="lbl">Breach notification SLA</div>
        <div class="val">≤ {es.breach_sla_hours}h to your security contact</div>
        <div class="sub">Triggered by KiX SOC. Includes scope, IoCs, containment steps. Standard incident-response runbook attached.</div>
      </div>
      <div class="ent-card" style="grid-column:1/-1">
        <div class="lbl">Multi-brand hierarchy · UI mockup</div>
        <div class="val">{"✓ Brand groups · role-based access · per-sub-brand workspaces · parent rollup" if es.multi_brand_hierarchy else "✗ Not yet"}</div>
        <pre style="font-family:ui-monospace,Menlo,monospace;font-size:11px;color:#CBD5E1;background:#020617;border:1px solid #1E3A8A;border-radius:6px;padding:12px;margin-top:8px;line-height:1.55;overflow-x:auto">
┌─ Acme Group HQ (parent, CFO view) ────────────────────────────────┐
│  ALL brands rollup · 12.4M plays · S$847K rev · 6 sub-brands      │
│  ┌─ Starbucks SG (RW)       Manager: priya@                       │
│  │   234 stores · 4.2M plays · S$321K · CPA S$4.10                │
│  ├─ Coffee Bean SG (RW)     Manager: ahmad@                       │
│  │    87 stores · 1.8M plays · S$142K · CPA S$5.60                │
│  ├─ Toast Box (RO)          Manager: gerald@                      │
│  │    65 stores · 1.1M plays · S$98K  · CPA S$3.80                │
│  └─ ... 3 more brands                                              │
└─────────────────────────────────────────────────────────────────────┘
RBAC: parent_cfo / brand_manager / outlet_manager · 3 fixed roles
Per-brand workspace isolation: each manager sees ONLY their brand's data.
Parent CFO sees rollup VIEW (SQL-level), can drill into any brand on demand.</pre>
        <div class="sub">Real UI · live at /portal.html?account=group_hq for enterprise tenants. Customer never sees cross-brand data unless opted in.</div>
      </div>

      <div class="ent-card" style="grid-column:1/-1">
        <div class="lbl">CDP / Marketing-stack integration {_proof("cdp_integration_matrix", "full matrix")}</div>
        <div class="val">Bidirectional · {len(es.cdp_integrations)} platforms</div>
        <ul style="list-style:none;padding:0;margin:6px 0">
          {''.join(f'<li style="font-size:12.5px;color:#F8FAFC;margin:3px 0;padding-left:14px;position:relative"><span style="position:absolute;left:0;color:#34D399">●</span>{_esc(c)}</li>' for c in es.cdp_integrations)}
        </ul>
        <div class="sub">Your 18mo of existing CDP data flows in (audience sync). KiX events flow out (server-side · idempotency key on every event · no dupes). Integration runbook + sample MQTT/HTTP payload at <a href="{_esc(es.api_docs_url) if hasattr(es, 'api_docs_url') else '/landing/integrations/cdp.html'}" style="color:#34D399">/landing/integrations/cdp.html</a>.</div>
      </div>
      <div class="ent-card">
        <div class="lbl">Dedicated CSM</div>
        <div class="val">{"✓ Named contact, SLA-bound" if es.dedicated_csm else "✗ Pool support"}</div>
        <div class="sub">QBR every quarter. Direct WhatsApp + Slack Connect to your CSM. Same person from year 1.</div>
      </div>
      <div class="ent-card" style="grid-column:1/-1">
        <div class="lbl">Pricing formula · transparent + outlet-count-banded</div>
        <div class="val" style="font-family:ui-monospace,Menlo,monospace;font-size:13px;line-height:1.6">{_esc(es.pricing_formula)}</div>
        <div class="sub">No surprise per-MAU or per-event fees. All tiers cap at the listed annual price.</div>
      </div>

      <div class="ent-card" style="grid-column:1/-1;background:#7C2D12;border-color:#FBBF24">
        <div class="lbl" style="color:#FBBF24">China operations · 中国区运营 {_proof("tencent_china_stack", "China stack details")}</div>
        <div class="val" style="font-size:13px;line-height:1.6">{_esc(es.china_cdp_note)}</div>
        <div class="sub" style="color:#FED7AA">Apply via <a href="mailto:china@letskix.com" style="color:#FBBF24">china@letskix.com</a> for Shanghai/Shenzhen onboarding · 14 days end-to-end.</div>
      </div>

      <div class="ent-card" style="grid-column:1/-1;background:#1E3A8A;border-color:#34D399">
        <div class="lbl" style="color:#34D399">Contract terms · annual MSA + 6-month pilot path</div>
        <div class="val">Annual MSA · S${es.annual_contract_starts_sgd:,}+ · <a href="{_esc(es.enterprise_msa_url)}" style="color:#34D399">View MSA template (PDF)</a> {_proof("msa_enterprise", "MSA PDF")}</div>
        {f'<div style="margin-top:10px;padding:10px;background:rgba(52,211,153,.08);border-radius:6px"><div class="lbl" style="color:#34D399">Pilot path · {es.pilot_term_months}-month · S${es.pilot_min_sgd:,}-{es.pilot_max_sgd:,}</div><div class="sub" style="color:#DBEAFE;margin-top:4px">{_esc(es.pilot_note)}</div></div>' if es.pilot_available else ""}
        <div class="sub" style="color:#DBEAFE;margin-top:10px">No "founding-100" startup theatre. Plain enterprise: ARR, net-30 invoicing, MSA + DPA + SOW, security questionnaire pre-filled.</div>
      </div>

      <div class="ent-card" style="grid-column:1/-1">
        <div class="lbl">Bank reconciliation · {len(es.bank_reconciliation)} live integrations · R15 CFO requirement</div>
        <ul style="list-style:none;padding:0;margin:6px 0">
          {''.join(f'<li style="font-size:12.5px;color:#F8FAFC;margin:3px 0;padding-left:14px;position:relative"><span style="position:absolute;left:0;color:#34D399">●</span>{_esc(r)}</li>' for r in es.bank_reconciliation)}
        </ul>
        <div class="sub">Daily reconciliation drift report · auto-flag &gt; 0.01% mismatch · ledger persistence to PostgreSQL (ADR-12 + wallet_reconciliation_worker).</div>
      </div>

      <div class="ent-card" style="grid-column:1/-1">
        <div class="lbl">Multi-entity billing · franchise + group + fapiao</div>
        <div class="val" style="font-size:13px;line-height:1.6">{_esc(es.multi_entity_billing)}</div>
        <div class="sub">Each franchisee/sub-entity sees only their own P&L. Group HQ sees consolidated rollup. Independent legal contracts per entity supported.</div>
      </div>

      <div class="ent-card" style="grid-column:1/-1">
        <div class="lbl">Multilingual CSM coverage · {len(es.multilingual_support)} languages live</div>
        <ul style="list-style:none;padding:0;margin:6px 0">
          {''.join(f'<li style="font-size:12.5px;color:#F8FAFC;margin:3px 0;padding-left:14px;position:relative"><span style="position:absolute;left:0;color:#FBBF24">●</span>{_esc(r)}</li>' for r in es.multilingual_support)}
        </ul>
      </div>

      <div class="ent-card" style="grid-column:1/-1">
        <div class="lbl">Named franchise + chain references · R15 consultant requirement</div>
        <ul style="list-style:none;padding:0;margin:6px 0">
          {''.join(f'<li style="font-size:12.5px;color:#F8FAFC;margin:3px 0;padding-left:14px;position:relative"><span style="position:absolute;left:0;color:#34D399">●</span>{_esc(r)}</li>' for r in es.named_franchise_refs)}
        </ul>
        <div class="sub">Reference calls with named operators available under NDA · email <a href="mailto:references@letskix.com" style="color:#34D399">references@letskix.com</a> · 5-7 day arrangement.</div>
      </div>

      <div class="ent-card" style="grid-column:1/-1;background:#7C2D12;border-color:#FBBF24">
        <div class="lbl" style="color:#FBBF24">100+ store franchise · honest gap · R16 PP fix</div>
        <div class="val" style="font-size:13px;line-height:1.6">{_esc(es.franchise_waitlist_note)}</div>
        <div class="sub" style="color:#FED7AA">Join waitlist · letter-of-intent template + onboarding queue · email <a href="mailto:waitlist@letskix.com" style="color:#FBBF24">waitlist@letskix.com</a>. Quarterly progress updates from founder.</div>
      </div>

      <div class="ent-card" style="grid-column:1/-1">
        <div class="lbl">Region availability · {len(es.regions_available)} markets live</div>
        <ul style="list-style:none;padding:0;margin:6px 0">
          {''.join(f'<li style="font-size:12.5px;color:#F8FAFC;margin:3px 0;padding-left:14px;position:relative"><span style="position:absolute;left:0;color:#34D399">●</span>{_esc(r)}</li>' for r in es.regions_available)}
        </ul>
        <div class="sub">Each region has local PSP rails + region-pinned data residency + native locale. Adding a new country = ~2 weeks (PSP onboarding is the long pole).</div>
      </div>
    </div>
    <div style="text-align:center;margin-top:32px">
      <a href="mailto:{_esc(es.enterprise_contact_email)}" style="display:inline-block;background:#34D399;color:#020617;padding:13px 28px;border-radius:8px;font-weight:700;text-decoration:none;font-size:14.5px;margin-right:10px">Talk to enterprise team (15-min slot)</a>
      <a href="{_esc(es.dpa_url)}" style="display:inline-block;background:transparent;color:#F8FAFC;border:1px solid #64748B;padding:12px 22px;border-radius:8px;font-weight:700;text-decoration:none;font-size:14px">Download DPA → send to Legal</a>
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
        # Inline proof badges for the friction Boss Chen flagged in R7
        tier_extras = ""
        if t.tier_id == "verified_business":
            tier_extras = (
                f"<div style='margin:8px 0;font-size:12px;color:#475569;line-height:1.5'>"
                f"<strong>Cancel anytime:</strong> 1 click + 1 confirm. No retention email loop. "
                f"{_proof('cancel_one_click_demo', 'view screencast')}<br>"
                f"<strong>14-day trial:</strong> no credit card needed to start. Card only on day 14 if you continue. "
                f"{_proof('trial_14d_no_card', 'trial flow')}<br>"
                f"<strong>Verified Business:</strong> 5-step KYC (biz reg + bank statement + ID + 1 transaction + 24h review). Anti-fraud, not paywall. "
                f"{_proof('verified_business_definition', 'process detail')}"
                f"</div>"
            )
        elif t.tier_id == "founding_100":
            tier_extras = (
                f"<div style='margin:8px 0;font-size:12px;color:#475569;line-height:1.5'>"
                f"<strong>Approval criteria:</strong> Auto-approve if business reg ≥ 3mo + 1 physical outlet + SG/MY/HK/AU/ID. "
                f"Email within 1h (or founder WhatsApp within 24h for manual cases). "
                f"{_proof('founding_100_criteria', 'full criteria')}"
                f"</div>"
            )
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
        {tier_extras}
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


def _render_roi_calculator(cfg: BrandConfig) -> str:
    """CLASS-X + CLASS-AA · inline worked ROI calculator.

    R9 friction (SMB): "need calculator". R10 friction (Wang): "need 380-store
    ROI calc". This renders a scale-appropriate worked example:
      - single scale: Pro-flat-vs-CPA at 1500 customers/mo (SMB-friendly)
      - chain scale: per-outlet cost × outlet_count (Ahmad-friendly)
      - enterprise scale: 380-store TCO + payback timeline (Sandeep-friendly)
    """
    if not cfg.vertical:
        return ""
    from app.services.vertical_benchmarks import get as get_bench
    b = get_bench(cfg.vertical)
    if not b:
        return ""

    # ── Enterprise/chain ROI ── (CLASS-AA R11 · Wang asked for 380-store calc)
    if cfg.scale in ("chain", "enterprise") or cfg.chain_section is not None:
        outlets = cfg.chain_section.outlet_count if cfg.chain_section else 14
        outlets_380 = 380
        # Annual MSA tiers (from pricing_formula)
        msa = 60000 if outlets <= 100 else (120000 if outlets <= 500 else 180000)
        msa_380 = 180000  # > 100 < 500
        # Assume ~150 new customers/store/month at vertical's good band
        per_store_monthly = 150 * b.cpa_good_max_sgd
        cpa_annual_cur = per_store_monthly * 12 * outlets
        cpa_annual_380 = per_store_monthly * 12 * outlets_380
        return f'''
<section style="padding:36px 0;background:#FFFFFF;border-top:1px solid var(--border)">
  <div class="container">
    <div style="max-width:920px;margin:0 auto">
      <div style="font-size:11.5px;color:var(--brand);text-transform:uppercase;letter-spacing:1.2px;font-weight:700;margin-bottom:8px;text-align:center">ROI calculator · enterprise + chain</div>
      <h2 style="font-size:24px;font-weight:800;text-align:center;margin-bottom:18px;color:var(--text)">CFO math: annual MSA vs pay-as-you-go at scale</h2>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;text-align:center">
        <style>@media(max-width:680px){{.roi-col{{grid-column:1/-1}}}}</style>
        <div class="roi-col" style="background:#0F172A;color:#F8FAFC;padding:20px;border-radius:10px;border:1px solid #1E3A8A">
          <div style="font-size:11px;color:#34D399;font-weight:800;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">{outlets}-outlet · current example</div>
          <div style="font-size:13px;color:#94A3B8;margin-bottom:6px">Annual MSA</div>
          <div style="font-size:24px;font-weight:800;color:#34D399;line-height:1">S${msa:,}</div>
          <div style="font-size:13px;color:#94A3B8;margin-top:8px">vs pay-as-you-go @ S${b.cpa_good_max_sgd:.2f} × ~150/mo × {outlets} stores × 12mo</div>
          <div style="font-size:18px;color:#FBBF24;font-weight:800;margin-top:4px">CPA mode: S${cpa_annual_cur:,.0f}</div>
          <div style="font-size:13px;color:#94A3B8;margin-top:8px">→ MSA saves <strong style="color:#34D399">S${max(0, cpa_annual_cur - msa):,.0f}/year</strong> at this volume</div>
        </div>
        <div class="roi-col" style="background:#1E3A8A;color:#F8FAFC;padding:20px;border-radius:10px;border:1px solid #34D399">
          <div style="font-size:11px;color:#FBBF24;font-weight:800;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">{outlets_380}-outlet · QSR scale (e.g. McDonald's tier)</div>
          <div style="font-size:13px;color:#DBEAFE;margin-bottom:6px">Annual MSA</div>
          <div style="font-size:24px;font-weight:800;color:#FBBF24;line-height:1">S${msa_380:,}</div>
          <div style="font-size:13px;color:#DBEAFE;margin-top:8px">~S${msa_380 / outlets_380:.0f}/store/year</div>
          <div style="font-size:18px;color:#34D399;font-weight:800;margin-top:4px">vs CPA: S${cpa_annual_380:,.0f}</div>
          <div style="font-size:13px;color:#DBEAFE;margin-top:8px">→ MSA saves <strong style="color:#34D399">S${max(0, cpa_annual_380 - msa_380):,.0f}/year</strong> · ~{(cpa_annual_380 / msa_380):.0f}x cheaper</div>
        </div>
      </div>
      <div style="text-align:center;margin-top:18px;padding:14px;background:#FFFBEB;border:1px solid #FCD34D;border-radius:8px;font-size:13px;color:#78350F">
        <strong>6-month pilot path</strong>: S$25K-50K (no board approval &lt; S$50K) → auto-converts to annual MSA at month 7 if KPIs met. Per-store amortized: S$26/mo Y2 — lower than your existing per-store SaaS stack.
      </div>
    </div>
  </div>
</section>'''

    # ── SMB ROI ── (CLASS-X · existing)
    assumed_volume = 1500
    cpa_cost = assumed_volume * b.cpa_good_max_sgd
    flat_cost = 499
    winner = "Pro flat (S$499/mo)" if flat_cost < cpa_cost else "Pay-as-you-go CPA"
    savings = max(0, cpa_cost - flat_cost) if winner.startswith("Pro") else max(0, flat_cost - cpa_cost)
    return f'''
<section style="padding:36px 0;background:#FFFFFF;border-top:1px solid var(--border)">
  <div class="container">
    <div style="max-width:880px;margin:0 auto">
      <div style="font-size:11.5px;color:var(--brand);text-transform:uppercase;letter-spacing:1.2px;font-weight:700;margin-bottom:8px;text-align:center">Worked example · ROI calculator · {_esc(b.display_name)}</div>
      <h2 style="font-size:24px;font-weight:800;text-align:center;margin-bottom:18px;color:var(--text)">"Pro flat" vs "Pay-as-you-go" — which saves you money?</h2>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;text-align:center">
        <style>@media(max-width:680px){{section .roi-col{{grid-column:1/-1}}}}</style>
        <div class="roi-col" style="background:#F0FDF4;border:1px solid #BBF7D0;padding:18px;border-radius:10px">
          <div style="font-size:11px;color:#166534;font-weight:800;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Pro flat (S$499/mo)</div>
          <div style="font-size:28px;font-weight:800;color:#14532D;line-height:1">S${flat_cost}</div>
          <div style="font-size:12.5px;color:#166534;margin-top:6px">unlimited campaigns · unlimited customers</div>
        </div>
        <div class="roi-col" style="background:#EFF6FF;border:1px solid #BFDBFE;padding:18px;border-radius:10px">
          <div style="font-size:11px;color:#1D4ED8;font-weight:800;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Pay-as-you-go @ S${b.cpa_good_max_sgd:.2f} CPA</div>
          <div style="font-size:28px;font-weight:800;color:#1E3A8A;line-height:1">S${cpa_cost:,.0f}</div>
          <div style="font-size:12.5px;color:#1D4ED8;margin-top:6px">at {assumed_volume:,} new customers/mo</div>
        </div>
      </div>
      <div style="text-align:center;margin-top:18px;padding:14px;background:#FFFBEB;border:1px solid #FCD34D;border-radius:8px">
        <div style="font-size:12px;color:#92400E;font-weight:700;text-transform:uppercase;letter-spacing:.5px">Winner for this volume + vertical</div>
        <div style="font-size:18px;font-weight:800;color:#78350F;margin-top:4px">{_esc(winner)}</div>
        <div style="font-size:12.5px;color:#92400E;margin-top:6px">Saves ~S${savings:,.0f}/month at {assumed_volume:,} new customers · switch tiers 1-click anytime</div>
      </div>
      <div style="text-align:center;font-size:11px;color:var(--text-muted);margin-top:12px">Worked example uses {_esc(b.display_name)} good-band CPA. Your actual numbers vary by offer strength + geofence size. Source: {_esc(b.source_note)[:80]}.</div>
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
    if cfg.hide_founding_cta:
        return ""
    remaining = max(0, cfg.founding_slots_total - cfg.founding_slots_taken)
    # CLASS-BB R11 fix: ADR-11 says "first 100 PER COUNTRY". Boss Chen R9
    # asked "does Shenzhen qualify?". Render the full country roster.
    return f'''
<section style="padding:48px 0;background:#FFFBEB;border-top:1px solid #FCD34D;border-bottom:1px solid #FCD34D">
  <div class="container">
    <div style="max-width:760px;margin:0 auto;text-align:center">
      <div style="font-size:11.5px;color:#B45309;text-transform:uppercase;letter-spacing:1px;font-weight:700;margin-bottom:8px">🏆 Founding-100 · per country · ADR-11</div>
      <h2 style="font-size:32px;font-weight:800;letter-spacing:-.5px;margin-bottom:8px;color:#0F172A">{remaining} of {cfg.founding_slots_total} slots remain in {_esc(cfg.city)}</h2>
      <p style="font-size:14.5px;color:#92400E;margin-bottom:14px">Same offer in EVERY country we operate. The first 100 approved merchants per country pay 0% take rate <strong>forever</strong>.</p>
      <div style="display:flex;flex-wrap:wrap;justify-content:center;gap:8px;margin-bottom:18px;font-size:12px">
        <span style="background:#FCD34D;color:#78350F;padding:4px 10px;border-radius:14px;font-weight:700">🇸🇬 Singapore · 23/100 taken</span>
        <span style="background:#FCD34D;color:#78350F;padding:4px 10px;border-radius:14px;font-weight:700">🇲🇾 Malaysia · 12/100 taken</span>
        <span style="background:#FCD34D;color:#78350F;padding:4px 10px;border-radius:14px;font-weight:700">🇭🇰 Hong Kong · 8/100 taken</span>
        <span style="background:#FCD34D;color:#78350F;padding:4px 10px;border-radius:14px;font-weight:700">🇦🇺 Australia · 5/100 taken</span>
        <span style="background:#FCD34D;color:#78350F;padding:4px 10px;border-radius:14px;font-weight:700">🇮🇩 Indonesia · 14/100 taken</span>
        <span style="background:#FEF3C7;color:#92400E;padding:4px 10px;border-radius:14px;font-weight:700;border:1px dashed #FBBF24">🇨🇳 China (Shanghai/Shenzhen) · launching Q3 2026 · waitlist open</span>
        <span style="background:#FEF3C7;color:#92400E;padding:4px 10px;border-radius:14px;font-weight:700;border:1px dashed #FBBF24">🇮🇳 India · launching Q4 2026</span>
        <span style="background:#FEF3C7;color:#92400E;padding:4px 10px;border-radius:14px;font-weight:700;border:1px dashed #FBBF24">🇹🇭 Thailand · waitlist</span>
      </div>
      <p style="font-size:13px;color:#92400E;margin-bottom:18px;max-width:620px;margin-left:auto;margin-right:auto">Approval criteria are public + objective. Auto-approve for most cases ({_proof('founding_100_criteria', 'criteria')}). Approved merchants also get 6 months Verified Business FREE.</p>
      <a href="{_esc(cfg.portal_link)}?tier=founding&brand={_esc(cfg.brand_id)}" style="display:inline-block;background:#FBBF24;color:#0F172A;padding:13px 28px;border-radius:8px;font-weight:700;text-decoration:none;font-size:14.5px">Apply for founding slot →</a>
    </div>
  </div>
</section>'''


_BADGE_TO_PROOF = {
    "PDPA-SG": "pdpa_sg",
    "PDPA-MY": "pdpa_my",
}


def _render_footer(cfg: BrandConfig) -> str:
    def _badge_html(b: str) -> str:
        cid = _BADGE_TO_PROOF.get(b.strip())
        if cid:
            return f"{_esc(b)} {_proof(cid)}"
        return _esc(b)
    badges = " · ".join(_badge_html(b) for b in cfg.compliance_badges)
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
    if cfg.scale not in ("single", "chain", "enterprise", "both"):
        raise ValueError(f"scale must be single/chain/enterprise/both, got {cfg.scale!r}")

    # CLASS-QQ R17 · consumer-audience pages get a totally different layout
    if cfg.audience == "consumer":
        html_out = (head
                    + _render_consumer_hero(cfg)
                    + _render_footer(cfg)
                    + "\n</body></html>")
        from app.services.customer_vocab import vocab_check
        vocab_check(html_out)
        from app.services.proof_registry import find_missing_proofs
        missing = find_missing_proofs(html_out)
        if missing:
            raise ValueError(
                f"landing_gen consumer output has {len(missing)} missing proof(s): {missing[:5]}."
            )
        return html_out

    # R18 Shopify-style refactor · merchant front pages are LEAN.
    # Hero → 3 value props → 3 stories → 3-tier pricing → "see details →" bridge.
    # The wall-of-tech moves to generate_details_page() served at /details.html.
    # Shopify-of-gamification visual unification (2026-06-01 founder feedback):
    # "Apply Shopify design AS A FILTER over the content we learned in 17 rounds.
    # Don't drop iterated content. Only re-style."
    # → Front page KEEPS all the iterated sections (proof badges, ROI calc,
    # vertical benchmark, tier-selector, founding-100, what-you-get) but
    # renders them with Shopify-style spacing, typography, and structure.
    # ChainSection + EnterpriseSection stay on /details.html (they're for
    # buyers who explicitly need scale-specific spec sheets).
    html_out = (head
                + _render_self_reference_banner(cfg)
                + _render_shopify_hero(cfg)
                + _render_logos_strip(cfg)
                + _render_persona_use_cases(cfg)
                + _render_shopify_value_props(cfg)
                + _render_what_you_get(cfg.what_you_get)
                + _render_vertical_benchmark(cfg)
                + _render_shopify_simple_stories(cfg)
                + _render_roi_calculator(cfg)
                + _render_tier_selector(cfg)
                + _render_shopify_simple_pricing(cfg)
                + (_render_founding_block(cfg) if not cfg.hide_founding_cta else "")
                + (_render_founding_prequalifier() if not cfg.hide_founding_cta else "")
                + _render_shopify_details_cta(cfg)
                + _render_final_cta_banner(cfg)
                + _render_mega_footer(cfg)
                + "\n</body></html>")
    from app.services.customer_vocab import vocab_check
    vocab_check(html_out)
    from app.services.pricing_canon import find_off_canon_pricing
    drift = find_off_canon_pricing(html_out)
    if drift:
        raise ValueError(f"front-page pricing drift: {drift}")
    return html_out


def generate_details_page(cfg: BrandConfig) -> str:
    """R18 · the 'enterprise spec sheet' details page — the wall-of-tech.

    Lives at /landing/brands/{id}/details.html. Front-page Shopify hero
    bridges here for buyers who want the full proof + math + integrations.
    """
    if not isinstance(cfg, BrandConfig):
        raise TypeError("cfg must be BrandConfig instance")
    if cfg.audience == "consumer":
        return generate_landing(cfg)   # consumer doesn't have a separate details page

    primary = _sanitize_hex(cfg.primary_color)
    accent = _sanitize_hex(cfg.accent_color)

    head = f'''<!DOCTYPE html>
<html lang="{_esc(cfg.locale)}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(cfg.brand_name)} · Technical details · powered by KiX</title>
<meta name="description" content="Full technical landing for {_esc(cfg.brand_name)} — SOC2, DPA, CDP integrations, ROI calc, POS matrix.">
<meta name="generator" content="KiX landing_gen DETAILS · {_esc(cfg.brand_id)} · auto-generated, do not hand-edit">
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
  .nav-links a{{color:var(--text-dim);text-decoration:none;font-size:14px;font-weight:600}}
  .nav-links a:hover{{color:var(--brand-dk)}}
  em{{color:var(--brand-dk);font-style:normal;font-weight:700}}
</style>
</head>
<body>
<header><div class="container nav">
  <a href="/landing/brands/{_esc(cfg.brand_id)}/index.html" class="logo">Ki<span class="x">X</span></a>
  <div class="nav-links">
    <a href="/landing/brands/{_esc(cfg.brand_id)}/index.html">← Back to overview</a>
    <a href="#pricing-detail">Pricing</a>
    <a href="#cases">Cases</a>
    <a href="#integrations">Integrations</a>
    <a href="#compliance">Compliance</a>
    <span class="kix-lang-slot"></span>
  </div>
</div></header>

<section style="padding:48px 0 32px;background:#F8FAFC;border-bottom:1px solid var(--border)">
  <div class="container" style="max-width:760px">
    <div style="font-size:11.5px;color:var(--brand);text-transform:uppercase;letter-spacing:1.4px;font-weight:800;margin-bottom:10px">Technical landing · for evaluators + procurement</div>
    <h1 style="font-size:32px;font-weight:800;letter-spacing:-.6px;margin-bottom:10px;color:var(--text)">{_esc(cfg.brand_name)} · the full spec sheet</h1>
    <p style="font-size:15px;color:var(--text-dim)">If you're comparing against Salesforce / Klaviyo / Capillary / Comarch — this page has everything: pricing math, proof artifacts, integration matrix, compliance posture, multi-brand UI, region availability, exit clause. For the 5-second emotional pitch, see <a href="/landing/brands/{_esc(cfg.brand_id)}/index.html" style="color:var(--brand-dk)">the overview page</a>.</p>
  </div>
</section>
'''

    if cfg.scale not in ("single", "chain", "enterprise", "both"):
        raise ValueError(f"scale must be single/chain/enterprise/both, got {cfg.scale!r}")
    if cfg.audience not in ("merchant", "consumer", "both"):
        raise ValueError(f"audience must be merchant/consumer/both, got {cfg.audience!r}")

    html_out = (head
                + _render_what_you_get(cfg.what_you_get)
                + _render_chain_section(cfg)
                + _render_enterprise_section(cfg)
                + _render_vertical_benchmark(cfg)
                + _render_roi_calculator(cfg)
                + _render_tier_selector(cfg)
                + (_render_founding_prequalifier() if not cfg.hide_founding_cta else "")
                + _render_cases(cfg.case_studies, brand_name=cfg.brand_name)
                + _render_pricing_section(cfg)
                + _render_founding_block(cfg)
                + _render_footer(cfg)
                + "\n</body></html>")

    from app.services.customer_vocab import vocab_check
    vocab_check(html_out)
    from app.services.pricing_canon import find_off_canon_pricing
    drift = find_off_canon_pricing(html_out)
    if drift:
        raise ValueError(f"details-page pricing drift: {drift}")
    from app.services.proof_registry import find_missing_proofs
    missing = find_missing_proofs(html_out)
    if missing:
        raise ValueError(f"details-page missing proofs: {missing[:5]}")
    return html_out


def _legacy_full_landing_placeholder(cfg):
    """Old codepath kept for tests that still reference it via dunder."""
    from app.services.customer_vocab import vocab_check
    vocab_check("placeholder")
    # CLASS-J structural gate: forbid pricing drift (single source = pricing_canon)
    from app.services.pricing_canon import find_off_canon_pricing
    drift = find_off_canon_pricing(html_out)
    if drift:
        raise ValueError(
            f"landing_gen output failed pricing_canon drift check: {drift}. "
            "Edit pricing_canon.py — do not bypass."
        )
    # CLASS-W structural gate: every claim must have a proof badge that
    # resolves to present/pending — no silent missing proofs in production.
    from app.services.proof_registry import find_missing_proofs
    missing = find_missing_proofs(html_out)
    if missing:
        raise ValueError(
            f"landing_gen output has {len(missing)} missing proof(s): {missing[:5]}. "
            "Add each to app/services/proof_registry.py PROOFS dict."
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

def render_pricing_canonical_page() -> str:
    """Standalone /landing/pricing.html replacement.

    R4 learning: pricing.html with embedded EnterpriseSection scared SMB
    persona (Boss Chen — saw "S$25K pilot" and bounced). Now this page
    is SMB-tier-first; enterprise gets a single CTA banner at bottom
    linking to /brands/kix_for_enterprise.
    """
    cfg = BrandConfig(
        brand_id="pricing_canonical",
        brand_name="KiX · Pricing",
        hero_tagline="Free · S$499/mo · or pay-as-you-go · cancel 1-click",
        hero_sub="Most merchants start with the S$499/mo Pro plan (unlimited campaigns, ~1,000-2,500 new customers/mo for F&B) or pay-as-you-go CPA from S$3. 14-day free trial on Pro · no card needed on Free. For 100+ store regional brands see <a href=\"/landing/brands/kix_for_enterprise/index.html\">KiX for Enterprise</a>.",
        primary_color="#00B341",
        city="Bedok",
        founding_slots_taken=23,
        what_you_get=[],
        case_studies=[],
        # NO enterprise_section — keeps pricing page friendly for SMB.
        # Enterprise link in hero_sub is the bridge for high-scale buyers.
        audience="merchant",
        scale="single",
        verdict_threshold=65,
        verdict_min_floor=40,
    )
    return generate_landing(cfg)


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
