# Wave J sim synthesis (Ahmad + Sarah + Sandeep)

**Date:** 2026-05-31
**Method:** v2 sims (playwright rendered DOM + Sonnet 4.5)
**Coverage:** 3 personas × multiple pages = 11 sims after Wave I.A-3 + Wave J prep shipped

## Verdict matrix

| Persona / Page | Verdict | Status |
|---|---|---|
| **Ahmad** / POS 1-pager (D) | "5-outlet pilot if (a) MY <100 (b) CPA <RM30, but won't scale past 20 until StoreHub live" | 🟢 70% closed |
| **Ahmad** / TikTok Pixel | "Yes I'd pilot — exactly what I've been looking for. 5 outlets by Friday." | 🟢 main hook landed |
| **Ahmad** / Pricing (v2) | "Proper BM not Google Translate. RM not USD. Someone did homework." | 🟢 localization works |
| **Sarah** / Index | "Playable taught me 'pay per performance' platforms bury the definition. Zero confidence." | 🔴 trust gap |
| **Sarah** / Pricing | "30 min investigating if Wei Lin vouches, but 3 months of data first." | 🔴 needs historical proof |
| **Sarah** / Portal | "Too messy, no proof, prepaid model = cash grab. ROI data first." | 🔴 wrong entry for evaluators |
| **Sarah** / Connect | "Cart before horse, burned before." | 🔴 connect.html is for consumer-side auth |
| **Sandeep** / Index | "Pilot if Salesforce + brand exclusivity. Won't allocate S$2M until tier-1 QSR reference." | 🟡 enterprise gap |
| **Sandeep** / Pricing | "Too SMB-focused for multi-million loyalty program. Integration docs + vetted contract needed." | 🟡 enterprise gap |
| **Sandeep** / Portal | (verdict empty - need re-run) | ⚪ unknown |
| **Sandeep** / Connect | "Founder sent me to consumer auth page = poor sales process." | 🔴 same as Sarah |
| **Aminah** / Welcome modal (B) | "Too confusing lah, bubble tea wrong, S$200 scary." | 🟡 driving Wave I.F-2 |

## Wave K candidates (next iteration)

### High-impact (multiple personas)
1. **Enterprise B2B landing page** — Sarah + Sandeep both bounced off consumer-focused index. Needs dedicated `/landing/enterprise.html` with proof-points, contract terms, exclusivity options, named reference.
2. **3-month historical proof block** — Sarah's #1 ask. Need 90-day cohort data from at least 1 SG pilot (Heng Heng Kopi closest).
3. **Contractual "new customer" definition** — Sarah's specific Playable trauma. Add legal one-pager `/landing/legal/new-customer-definition.html`.
4. **StoreHub LIVE not Q3 2026** — Ahmad's scale blocker. Worth fast-tracking even if rough.

### Enterprise-specific (Sandeep only)
5. Salesforce integration (Service Cloud Loyalty)
6. Brand exclusivity policy (within radius / vertical)
7. Tier-1 QSR brand reference (sales work, not eng)
8. PDPA consent flow walkthrough page (could share with Ahmad)

### Already addressed (don't re-do)
- TikTok Pixel mention ✅ Wave I.A-3
- BM/RM localization ✅ Wave I.A-2
- Trust signals / Mozat address ✅ Wave I.A-3
- POS not 3-month nightmare ✅ Wave I.A-3 + this commit
- MYR billing plan ✅ RFC drafted (Wave J prep)

## Cross-cutting insight

**Connect.html is consumer OAuth — wrong for B2B evaluators.** Both Sarah and
Sandeep landed there expecting a merchant demo and bounced. Either:
(a) Rename / redirect to a B2B-friendly explainer when accessed without
    OAuth params
(b) Build a separate enterprise.html as primary landing for B2B traffic

## Recommended Wave K scope (1 commit)
- /landing/enterprise.html (B2B-evaluator-friendly)
- Updated connect.html: detect missing OAuth params → show "you probably
  meant /landing/portal.html or /landing/enterprise.html" instead of error
- Optional: /landing/legal/new-customer-definition.html

Effort: ~2-3 hours. Targets Sarah + Sandeep simultaneously.

---

## Wave K post-ship update (2026-05-31)

Re-ran Sarah Chen + Sandeep Kumar sims against the new pages:

| Page | Sarah verdict | Sandeep verdict |
|------|---------------|-----------------|
| /enterprise.html | "S$500 pilot... most honest vendor page I've seen in 5 years" | "60-min deep-dive demo... transparency is refreshing" |
| /legal/new-customer-definition.html | "Finally someone who won't screw me on definitions" | "2 hours of discovery if Oracle POS + SEA QSR ref" |

Both moved from R0 "won't pay a dollar" → R1 "willing to invest time". The
contractual definition + transparent enterprise framing closed the trust gap.

### Remaining Wave L candidates

1. Live reference customer (Sarah's #1 ask) — sales work, sign 1st enterprise
2. Oracle POS integration (Sandeep) — add to POS matrix RFC scope
3. SEA QSR case study (Sandeep) — need first signed QSR
4. Customer-facing game preview link on /enterprise.html → /play.html?demo=1
5. Worked financial model template for evaluators (spreadsheet/calculator)

None block this commit; all are next-iteration.
