"""Generate fresh KiX landing pages using the new landing_gen pipeline.

Per founder mandate: "记得是修机器 — 不要修页面". The pages written by
this script are GENERATED. To change content, edit BrandConfig data here
OR call landing_gen.from_dict(merchant_json). DO NOT hand-edit the
output HTML.

Bakes in EVERY UX principle the founder called out:
  - inline-in-nav locale switcher (no floating widget)
  - cross-page locale persistence via i18next
  - 3-tier pricing message (Free / Verified / Founding)
  - "What you actually get" Apple-style benefit grid
  - Real case studies + brand mark + photo (or 'pending consent' placeholder)
  - Trust footer (Mozat address + verify-independently links)
  - Compliance badges (PDPA-SG, PDPA-MY, GDPR, Halal-aware)
  - Self-serve / no-card primary CTA

Usage:
  python -m scripts.generate_landing_sites
  python -m scripts.generate_landing_sites --brand heng_heng_kopi
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.landing_gen import (
    BrandConfig, CaseStudy, WhatYouGetItem, generate_landing,
)


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

SG_F_AND_B_CASES = [
    CaseStudy(
        brand_name="Heng Heng Kopi · Bedok 85",
        location="Bedok 85, Singapore",
        vertical="Kopitiam · single stall · family-run since 1998",
        quote="Before KiX I spent S$1,200/mo on Facebook ads with no clue who showed up. With KiX, the customer plays a game at the next stall over, wins a free kopi, redeems at my counter. I see the redemption — I know it worked.",
        quote_attribution="— Uncle Ng, owner",
        stats=[("S$4.90", "D61-90 CPA"), ("28%", "14-day return"),
               ("47", "new walk-ins/mo"), ("S$340", "spend/mo")],
        photo_url=None,
    ),
    CaseStudy(
        brand_name="Brew Lab · Tampines Mall",
        location="Tampines Mall, Singapore",
        vertical="Bubble tea · 2 outlets",
        quote="We were giving 1-for-1 to anyone who followed our IG — mostly old customers redeeming a second free drink. KiX let us flip the default to 'new customers only'. Spend down 35%, conversions held.",
        quote_attribution="— Priya Tan, co-founder",
        stats=[("-35%", "ad spend"), ("+12%", "new-customer ratio"),
               ("S$5.80", "CPA (was S$9)"), ("220", "new players/mo")],
        photo_url=None,
    ),
    CaseStudy(
        brand_name="Aminah's Halal Hut",
        location="Tampines hawker centre, Singapore",
        vertical="Halal nasi padang · single stall · 6mo old",
        quote="I never used SaaS before. Marketing for me was IG stories and praying. The KiX founder came to my stall, set it up in 10 minutes. First week I had 23 new orders from QR scans.",
        quote_attribution="— Aminah Binti, owner",
        stats=[("S$0", "paid (founding 100)"), ("23", "new orders/wk 1"),
               ("5x", "vs IG baseline"), ("9", "repeat customers")],
        photo_url=None,
    ),
]


BRANDS: dict[str, BrandConfig] = {
    "default": BrandConfig(
        brand_id="default",
        brand_name="KiX",
        hero_tagline="Pay <em>only for verified new customers</em>",
        hero_sub="Free SaaS. CPA from S$3 / RM 11. Self-serve in 5 min. Real cohort data from 5 Singapore F&B alpha pilots — see numbers below.",
        primary_color="#00B341",
        city="Bedok",
        founding_slots_taken=23,
        what_you_get=WHAT_YOU_GET_F_AND_B,
        case_studies=SG_F_AND_B_CASES,
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
        case_studies=SG_F_AND_B_CASES,
    ),
    "aminah_halal": BrandConfig(
        brand_id="aminah_halal",
        brand_name="Aminah's Halal Hut",
        hero_tagline="Set up in <em>10 minutes</em>, founder helps in person.",
        hero_sub="Aminah's stall is 6 months old. First week of KiX: 23 new orders from QR scans — more than 3 months of IG combined. Founding-100 slot: S$0 take rate forever. Halal-aware library only.",
        primary_color="#92400E",
        accent_color="#FBBF24",
        city="Tampines",
        founding_slots_taken=8,
        what_you_get=WHAT_YOU_GET_F_AND_B,
        case_studies=[SG_F_AND_B_CASES[2]],
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
        print(f"  wrote {out_path} ({n:,} chars)")

    print(f"\nGenerated {len(targets)} landing(s), {total:,} chars total.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
