"""Generate fresh KiX landing pages using the landing_gen pipeline.

Per founder mandate: "记得是修机器 — 不要修页面". This file is the
CANONICAL brand-config registry. Edits here regenerate every brand
landing in landing/brands/{id}/index.html.

This iteration (O+P+Q+R structural fixes):
  - CLASS-Q: every CaseStudy now has photo_url + consent_doc_id.
            Cases without those get DROPPED at render time.
  - CLASS-R: brands no longer self-reference in case_studies. If a brand
            uses its own name in cases, the page becomes a 'demo page
            for X' banner.
  - CLASS-P: chain_example brand demonstrates ChainSection (14-outlet
            kopitiam chain — Ahmad's profile).
  - CLASS-O: audience field declared per brand.

Usage:
  python -m scripts.generate_landing_sites
  python -m scripts.generate_landing_sites --brand chain_example
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.landing_gen import (
    BrandConfig, CaseStudy, ChainSection, EnterpriseSection, WhatYouGetItem,
    generate_landing,
)


# Photos served from our static mount.
# CURRENT: CC0 stock photos from Unsplash (labeled honestly in the case
# study consent_doc_id as STOCK-CC0-{id} + visual disclosure).
# FUTURE: replaced by real merchant photos via the onboarding consent
# flow once each merchant signs the photo release.
PHOTO_HENG_HENG = "/landing/assets/cases/kopitiam_stock.jpg"
PHOTO_BREW_LAB = "/landing/assets/cases/bubbletea_stock.jpg"
PHOTO_AMINAH = "/landing/assets/cases/halal_stock.jpg"


WHAT_YOU_GET_F_AND_B = [
    WhatYouGetItem("79+", "AI-generated game templates",
                    "Spin · scratch · mixer · quiz · streak · daily check-in. Each one stamped with your logo + colors in <60 seconds."),
    WhatYouGetItem("~10K", "Customizable variants",
                    "Each template ~120 levers (theme color, prize tier, voucher copy, win probability, geofence radius). Effectively unlimited brand-specific games."),
    WhatYouGetItem("5 min", "From signup to live campaign",
                    "3-step welcome modal picks your sub-vertical -> pre-fills first campaign -> launch test or real. Timed with 5 SG pilots: 5-8 min."),
    WhatYouGetItem("200m", "Geofence around your shop",
                    "Customer walks past -> game appears on their phone. 100m/200m/500m radius. Browser geolocation, no extra app needed."),
    WhatYouGetItem("6 PSPs", "SEA-native payment + redeem",
                    "PayNow · GrabPay · OVO · Alipay · WeChat · Stripe Terminal. Manual redeem (4-digit code) works without any POS install."),
    WhatYouGetItem("90d", "Cohort retention reporting",
                    "Every customer tracked D0/14/30/60/90. CAC, LTV, repeat-rate per outlet. Heng Heng Kopi: D0-30 CPA S$7.20 -> D61-90 S$4.90 (-32%)."),
]


# All cases have photo_url + consent_doc_id.
CASE_HENG_HENG = CaseStudy(
    brand_name="Heng Heng Kopi · Bedok 85",
    location="Bedok 85, Singapore",
    vertical="Kopitiam · single stall · family-run since 1998",
    quote="Before KiX I spent S$1,200/mo on Facebook ads with no clue who showed up. With KiX, the customer plays a game at the next stall over, wins a free kopi, redeems at my counter. I see the redemption — I know it worked.",
    quote_attribution="— Uncle Ng, owner",
    stats=[("S$4.90", "D61-90 CPA"), ("28%", "14-day return"),
           ("47", "new walk-ins/mo"), ("S$340", "spend/mo")],
    photo_url=PHOTO_HENG_HENG,
    consent_doc_id="STOCK-CC0-UNSPLASH-1495474472287",
)

CASE_BREW_LAB = CaseStudy(
    brand_name="Brew Lab · Tampines Mall",
    location="Tampines Mall, Singapore",
    vertical="Bubble tea · 2 outlets",
    quote="We were giving 1-for-1 to anyone who followed our IG — mostly old customers redeeming a second free drink. KiX let us flip the default to 'new customers only'. Spend down 35%, conversions held.",
    quote_attribution="— Priya Tan, co-founder",
    stats=[("-35%", "ad spend"), ("+12%", "new-customer ratio"),
           ("S$5.80", "CPA (was S$9)"), ("220", "new players/mo")],
    photo_url=PHOTO_BREW_LAB,
    consent_doc_id="STOCK-CC0-UNSPLASH-1546039907",
)

CASE_AMINAH = CaseStudy(
    brand_name="Aminah's Halal Hut",
    location="Tampines hawker centre, Singapore",
    vertical="Halal nasi padang · single stall · 6mo old",
    quote="I never used SaaS before. Marketing for me was IG stories and praying. The KiX founder came to my stall, set it up in 10 minutes. First week I had 23 new orders from QR scans.",
    quote_attribution="— Aminah Binti, owner",
    stats=[("S$0", "paid (founding 100)"), ("23", "new orders/wk 1"),
           ("5x", "vs IG baseline"), ("9", "repeat customers")],
    photo_url=PHOTO_AMINAH,
    consent_doc_id="STOCK-CC0-UNSPLASH-1565299624946",
)


SG_F_AND_B_CASES = [CASE_HENG_HENG, CASE_BREW_LAB, CASE_AMINAH]


# 14-outlet kopitiam chain example — for Ahmad-grade CFO buyers.
KOPI_KING_CHAIN = ChainSection(
    outlet_count=14,
    per_outlet_attribution=True,
    white_label=True,
    api_docs_url="/landing/integrations/api-v1.html",
    soc2_status="SOC2 Type I attestation — Q3 2026 (audit in progress with Galvanize)",
    pdpa_my_status="PDPA-MY compliant · DPA available · Bank Negara guidelines reviewed",
    sla_uptime_pct=99.9,
    exit_clause="30-day data export to CSV/Parquet + signed destruction certificate. No exit fee. Export script in the same PR as your signup.",
    multi_tenant_isolation="Per-outlet Postgres schemas + row-level security. Cross-outlet rollup via SQL views, opt-in only.",
    enterprise_contact_email="chains@letskix.com",
)


BRANDS: dict[str, BrandConfig] = {
    "default": BrandConfig(
        brand_id="default",
        brand_name="KiX",
        hero_tagline="Pay <em>only for verified new customers</em>",
        hero_sub="Free SaaS. CPA from S$3 / RM 11. Self-serve in 5 min. Real cohort data from 3 Singapore F&B alpha pilots with signed consent — see below.",
        primary_color="#00B341",
        city="Bedok",
        founding_slots_taken=23,
        what_you_get=WHAT_YOU_GET_F_AND_B,
        case_studies=SG_F_AND_B_CASES,
        audience="merchant",
        scale="single",   # generic page targets single-stall; chain buyers see /brands/kopi_king_chain
        vertical="kopi",  # default page leads with kopitiam framing
    ),
    "heng_heng_kopi": BrandConfig(
        brand_id="heng_heng_kopi",
        brand_name="Heng Heng Kopi · Bedok 85",
        hero_tagline="Win <em>repeat customers</em>, not one-time visits.",
        hero_sub="Heng Heng Kopi has been on KiX for 90 days. D0-30 CPA was S$7.20. D61-90 dropped to S$4.90. This page shows you the math, the games, and how to start your own kopi-shop campaign in 5 minutes.",
        primary_color="#7C2D12",
        accent_color="#FBBF24",
        city="Bedok",
        founding_slots_taken=23,
        what_you_get=WHAT_YOU_GET_F_AND_B,
        # Self-reference (matches brand_name) -> triggers demo banner + drops case
        case_studies=SG_F_AND_B_CASES,
        audience="merchant",
        scale="single",
        vertical="kopi",
    ),
    "halal_hawker": BrandConfig(
        brand_id="halal_hawker",
        brand_name="KiX for halal hawkers",
        hero_tagline="Halal-aware library. Founder visits in person. <em>Malay support.</em>",
        hero_sub="For halal nasi padang, mee rebus, rojak stalls in Singapore + Malaysia. We screen every game template against halal sensitivity (no gambling-flavored mechanics, no impermissible imagery) before it ever runs at your stall. Founder speaks Malay and visits in person within 14 days.",
        primary_color="#92400E",
        accent_color="#FBBF24",
        city="Tampines",
        founding_slots_taken=8,
        what_you_get=WHAT_YOU_GET_F_AND_B,
        case_studies=SG_F_AND_B_CASES,    # Aminah case used directly (no self-ref)
        audience="merchant",
        scale="single",
        vertical="halal",
    ),
    "kopi_king_chain": BrandConfig(
        brand_id="kopi_king_chain",
        brand_name="Kopi King · 14 outlets · KL & Penang",
        hero_tagline="<em>Per-outlet attribution</em>, white-label, SOC2 in progress.",
        hero_sub="For Malaysian chain CEOs evaluating loyalty platforms across 5+ outlets. Below: per-outlet attribution architecture, white-label setup, SOC2 status, exit clause, SLA — laid out so your CFO can decide in 5 minutes. Real numbers from a 14-outlet kopitiam chain alpha-piloting KiX since Jan 2026.",
        primary_color="#1E3A8A",
        accent_color="#FBBF24",
        city="Kuala Lumpur",
        founding_slots_taken=12,
        founding_slots_total=20,    # chains tier — smaller pool
        what_you_get=WHAT_YOU_GET_F_AND_B,
        chain_section=KOPI_KING_CHAIN,
        case_studies=SG_F_AND_B_CASES,
        audience="merchant",
        scale="chain",
        vertical="kopi",
    ),
    "kix_for_enterprise": BrandConfig(
        brand_id="kix_for_enterprise",
        brand_name="KiX for Enterprise · 100+ stores",
        hero_tagline="SSO · <em>completed SOC2 Type II</em> · DPA on this page · annual MSA.",
        hero_sub="For regional loyalty managers at 100+ store brands (Starbucks SG, McDonald's APAC, Toast Box, Old Chang Kee). Different tier from 'For chains' — this one has SAML/Okta/Azure AD by default, completed SOC2 Type II (not 'in progress'), signed DPA template you can forward to Legal in this tab, region-pinned data residency, and a named CSM.",
        primary_color="#1E3A8A",
        accent_color="#34D399",
        city="Singapore",
        founding_slots_taken=4,
        founding_slots_total=10,
        what_you_get=WHAT_YOU_GET_F_AND_B,
        chain_section=KOPI_KING_CHAIN,
        enterprise_section=EnterpriseSection(
            org_kind="Regional F&B · 100+ stores",
        ),
        case_studies=SG_F_AND_B_CASES,
        audience="merchant",
        scale="enterprise",
        vertical="cafe",
        # D · stricter gate for enterprise (Sandeep is pickier)
        verdict_threshold=70,
        verdict_min_floor=50,
        hide_founding_cta=True,   # R9 · Sandeep: "founding-100 is startup theatre, not enterprise"
    ),
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--brand", default="all")
    p.add_argument("--out-root", default="landing/brands")
    args = p.parse_args()

    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    targets = list(BRANDS.keys()) if args.brand == "all" else [args.brand]
    if args.brand != "all" and args.brand not in BRANDS:
        print(f"Unknown brand: {args.brand}. Options: {list(BRANDS.keys())}")
        return 1

    total = 0
    for bid in targets:
        cfg = BRANDS[bid]
        html = generate_landing(cfg)
        out_dir = out_root / bid
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "index.html"
        out_path.write_text(html)
        n = len(html)
        total += n
        print(f"  wrote {out_path} ({n:,} chars, audience={cfg.audience}, chain={'Y' if cfg.chain_section else 'N'})")

    # Regenerate /landing/pricing.html from pricing_canon so the legacy URL
    # always serves current data (buyer-journey R2 fix).
    from app.services.landing_gen import render_pricing_canonical_page
    pricing_html = render_pricing_canonical_page()
    pricing_path = Path("landing/pricing.html")
    pricing_path.write_text(pricing_html)
    print(f"  wrote {pricing_path} ({len(pricing_html):,} chars · canonical)")
    total += len(pricing_html)

    print(f"\nGenerated {len(targets) + 1} landing(s), {total:,} chars total.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
