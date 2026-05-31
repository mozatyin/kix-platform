# RFC: Enterprise Tier (Salesforce + Brand Exclusivity + MSA)

**Status:** Draft for decision
**Author:** Founder / KiX engineering
**Date:** 2026-05-31
**Decision target:** Before first enterprise pilot signs (Q3 2026)
**Drives from:** Sandeep sim Wave J F-task:
> "I'd pilot if they can prove Salesforce integration and brand exclusivity,
>  but I would not spend money at scale until I see retention data from a
>  tier-1 QSR brand and get our legal team to review the customer data
>  terms — the 'pay per result' model is appealing but the operational
>  unknowns are too high for a S$2M budget owner."

## TL;DR

Self-serve KiX wins SMB. Enterprise tier (≥10 outlets, ≥S$10K/mo budget,
named loyalty manager) needs four things beyond the SMB product:

1. **Salesforce Service Cloud Loyalty connector** (CRM-of-record sync)
2. **Brand exclusivity policy** (12-mo exclusive within vertical+radius)
3. **Vetted MSA template** (WongPartnership review)
4. **Named CSM + founder QBR** (not founder-only)

This RFC scopes each and recommends sequencing.

---

## 1. Salesforce Service Cloud Loyalty connector

### Why
Sandeep (Starbucks Regional) and 2 of 5 enterprise evaluator interviews
specifically asked. Salesforce is the CRM-of-record for tier-1 QSR loyalty
programs. Without it, every enterprise pilot becomes a CSV-export tax.

### Scope
- OAuth handshake with Salesforce instance (org-level)
- Bi-directional sync: KiX consumer events → Salesforce loyalty member
  record + Salesforce loyalty redemption → KiX wallet charge confirmation
- Match key: phone (e164) OR salesforce-internal-customer-id (if uploaded)
- Custom field mapping wizard (KiX game ID ↔ Salesforce campaign code)
- Optional: Marketing Cloud Journey trigger from KiX game completion

### Effort: ~3 weeks (Backend + 1 SFDC partner consultant)
- Week 1: OAuth + read-only sync from SFDC (validate match key)
- Week 2: Write-back: KiX events → SFDC LoyaltyEngagement record
- Week 3: UI + docs + 1 partner pilot

### Risk
- Salesforce certification (AppExchange) takes 3-6 months — defer
- Direct integration (no AppExchange) is fine for first 5 enterprise pilots
- Long-term: AppExchange listing for marketing reach (year 2)

---

## 2. Brand exclusivity policy

### Why
Sandeep + Sarah both flagged: "what if competitor in the same mall buys
my customer attention?" Currently we cross-pool consumers across brands
within a vertical (it's a feature for SMBs). Enterprise won't pay for
that.

### Proposed policy
- **Self-serve tier:** Cross-brand pool enabled (current behavior)
- **Enterprise tier (≥10 outlets):** Optional 12-month exclusivity in:
  - Same vertical (e.g., specialty coffee)
  - Within 500m radius of any enterprise outlet
  - For consumers who've engaged with the enterprise's games
- **Implementation:** Routing layer rejects competitor game offers for
  matched-consumer + matched-geofence combos
- **Premium:** Adds 15% to negotiated CPA tier (covers our opportunity cost)

### Effort: ~5 days
- 2 days: Routing-layer geofence + vertical match
- 1 day: Consumer-state "exclusivity-flagged" flag in user record
- 1 day: Portal toggle + contract language
- 1 day: QA across 3 SG verticals

### Open question
- Should exclusivity be vertical-only, radius-only, or both? (Sandeep wants both)
- Geofence definition: outlet-centroid 500m vs polygon-drawn-by-merchant?
- Recommendation: both AND polygon-by-merchant for enterprise (more setup, more precision)

---

## 3. Vetted MSA template

### Why
Sandeep: "wouldn't spend until I get our legal team to review the customer
data terms." Sarah: similar pain. Current ToS is consumer-app oriented;
no enterprise-grade MSA exists.

### Scope
- 1-pager MSA Schedule A (new-customer definition — ALREADY SHIPPED
  Wave K3 at /landing/legal/new-customer-definition.html)
- Standard MSA template (8-12 pages) covering:
  - Service description
  - Pricing schedule (CPA/CPS + enterprise tier pricing)
  - SLA tier (99.5% uptime, P1 4hr, P2 24hr, P3 best-effort)
  - Data Protection Addendum (link to existing DPA)
  - Termination for convenience (30-day notice, no clawback)
  - Audit rights (read-replica access for finance team)
  - Brand exclusivity (link to §2 above, opt-in)
  - Force majeure, governing law (SG-jurisdiction default, MY/HK on request)

### Effort: ~2 weeks
- 1 week: Draft with WongPartnership SG (existing relationship)
- 1 week: Founder + first enterprise pilot review cycle + sign

### Cost
- Legal review: ~S$8K WongPartnership flat fee
- DPA addendum: included
- MY-jurisdiction variant: +S$3K (deferred until first MY enterprise)

---

## 4. Named CSM + founder QBR

### Why
"Account manager rotates every 6 months" is a documented enterprise SaaS
horror. KiX is small enough that this is solvable by structure:

### Proposed structure
- **First 12 months of enterprise relationship:** Founder is the named
  point of contact (calendar-bookable, WhatsApp-reachable)
- **Month 12+:** Transition to named CSM hire (year 2 budget)
- **Quarterly Business Review (QBR):** Founder hosts in-person (SG / MY)
  every 90 days for first year, then move to remote video
- **Slack/Teams shared channel:** Enterprise-only channel with founder +
  customer's named tech contact for fast async troubleshooting

### Effort: 0 days code · process change only
- Add to MSA as a contractual commitment
- Founder calendar reserves 2hr/wk per enterprise customer (cap 5 customers Y1)

---

## Sequencing recommendation

| Phase | What ships | When | Cost |
|---|---|---|---|
| Phase 1 (now) | MSA template + new-customer page + enterprise.html | 2 weeks | S$8K legal |
| Phase 2 (Q3) | Brand exclusivity + first enterprise pilot | 1 month after Phase 1 | engineering only |
| Phase 3 (Q4) | Salesforce connector + first SFDC pilot | 6 weeks | engineering + ~S$5K SFDC consultant |
| Phase 4 (Y2) | Named CSM hire + AppExchange listing | Year 2 | hire + listing fee |

## Open questions

1. Do we offer enterprise tier publicly (price-on-call) or invite-only first?
   - Recommend invite-only Q3, public Q4 after 2-3 pilots ship
2. What's the minimum-outlets bar? 10 vs 25 vs 50?
   - Recommend 10 (lowers barrier; we can serve smaller chains profitably)
3. Brand exclusivity 12 months — too long? too short?
   - Recommend 12 months with 90-day exit clause if no measurable lift

## Success metrics (post all 4 phases)

- [ ] First enterprise pilot signed by 2026-09-30
- [ ] First Salesforce-integrated enterprise pilot by 2026-12-31
- [ ] Sandeep-archetype sim verdict shifts from "wouldn't spend" to "pilot Q4"
- [ ] At least 1 tier-1 QSR brand in enterprise.html "reference customer" line by 2027-Q1

## References
- Sandeep sim transcripts: `/Users/mozat/a-docs/sim-v2-*enterprise_manager*`
- Sarah sim transcripts: `/Users/mozat/a-docs/sim-v2-*skeptical_owner*`
- New-customer definition (Schedule A): `/landing/legal/new-customer-definition.html`
- Enterprise landing: `/landing/enterprise.html`
- DPA (existing): `/landing/legal/dpa.html`
