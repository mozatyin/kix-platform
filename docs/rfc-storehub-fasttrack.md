# RFC: Fast-track StoreHub integration (Q3 2026 → ASAP)

**Status:** Draft for decision
**Author:** Founder / KiX engineering
**Date:** 2026-05-31
**Decision target:** Within 14 days (blocks Ahmad's 5→100 outlet expansion)
**Drives from:** Ahmad sim Wave J D-task (POS page eval):
> "I'll pilot 5 outlets... but won't scale past 20 outlets until StoreHub
>  integration is live and I see the PDPA consent flow with my own eyes —
>  this feels 70% ready for Malaysia, 30% 'we'll figure it out.'"

## TL;DR

StoreHub is the dominant POS for Malaysian F&B. Our current StoreHub
integration is "Q3 2026" in our matrix. Three out of three MY chain CEOs
interviewed have it. **It IS the scale-blocker for MY enterprise.**

This RFC proposes fast-tracking it to **live by 2026-07-15** (vs Q3 = end Sep).

---

## Why now

| Signal | Source |
|---|---|
| Ahmad blocks at 20 outlets without StoreHub live | Wave J D-sim (verbatim) |
| 3 of 3 MY chain CEOs use StoreHub | Founder interviews 2026-04 |
| StoreHub claims 15K+ MY merchant accounts | StoreHub public marketing |
| Pilot data from Heng Heng (no POS) caps at S$890/mo spend | sg-case-studies cohort |
| Our POS 1-pager says "Q3 2026" — Sarah's "vaporware" trigger | Wave I.A-3 ship |

If StoreHub stays at Q3, we ship MY in Q3 with a credibility gap that
Ahmad-archetype CEOs will smell. Better to delay the page than ship
"coming soon" labels.

## What's needed (effort breakdown)

StoreHub has a [public REST API](https://help.storehub.com/api) + webhooks.
We need:

| Workstream | Effort | Owner | Done-when |
|---|---|---|---|
| OAuth handshake (merchant authorises KiX) | 0.5 day | Backend | First successful token exchange |
| Webhook receiver (order events) | 1 day | Backend | Test order in StoreHub sandbox flows to KiX |
| Event mapper (StoreHub order → KiX Tracked Transaction) | 1.5 days | Backend | All 5 SH order fields mapped per spec |
| De-dup + fraud check on inbound events | 0.5 day | Backend | Existing pipeline reused |
| Setup wizard in portal (paste API key, test connection) | 1 day | Frontend | One-click test passes for sandbox + 1 live MY pilot |
| Documentation + 1-pager for merchants | 0.5 day | Founder | Published in /landing/integrations/storehub.html |
| End-to-end test with 1 SG-based StoreHub demo account | 0.5 day | Both | Real S$1 transaction lights up KiX dashboard |
| Soft launch with 1 willing MY pilot merchant (Q3 outlet) | 1 day | Founder | First MY-merchant POS-tracked redeem completed |
| **Total** | **6.5 days · ~1.5 weeks** | | |

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| StoreHub API rate-limits hit during pilot scale | M | M | Implement backoff + batching from day 1 |
| StoreHub webhook signing format changes | L | L | Versioned signature verification |
| MY pilot merchant cancels mid-test | M | M | Sign up 2 willing pilots, use SG StoreHub demo as fallback |
| OAuth flow security review takes >1 week | L | H | Get WongPartnership signoff in parallel (their input on MSA) |
| Found gaps in StoreHub API for tip-adjusted CPS | L | M | Document gap, ship CPA-only for v1, CPS later |

## Decision needed

**Approve the 6.5-day spend (~$3K dev cost at burned-rate-equivalent)?**

Recommendation: **Yes.** Pre-ship StoreHub by 2026-07-15. Update POS
1-pager matrix to "live" before MY soft launch. Re-run Ahmad sim post-ship
to verify "won't scale past 20" blocker dissolves.

## Implementation notes (for engineering)

1. Reuse existing webhook receiver shape from Stripe Terminal adapter
2. StoreHub uses HMAC-SHA256 signature header `X-StoreHub-Signature`
3. Order events to subscribe: `order.completed`, `order.refunded`
4. Match key: phone (e164 normalised) on order.customer.phone — hash-then-compare to KiX-internal de-dup table
5. Event idempotency via `storehub_order_id` → existing dedup table
6. Sandbox testing: StoreHub sandbox costs ~RM 50 / month, expense as integration cost

## Out of scope (Phase 2)

- StoreHub Promotions sync (their loyalty points engine integration)
- StoreHub Inventory tie-in for menu-item-specific games
- Multi-outlet bulk-onboard wizard (single-outlet onboard is enough for v1)
- MY-residency for StoreHub event data (waits for MY data-residency Q1 2027)

## Success metrics post-launch

- [ ] StoreHub integration shows "Live" in POS 1-pager matrix
- [ ] First MY merchant POS-tracked redeem completes by 2026-07-15
- [ ] Ahmad v2 sim verdict shifts from "won't scale past 20" to "scaling to 50"
- [ ] At least 3 MY merchants connected via StoreHub by end of Q3 2026

## References
- Ahmad sim transcripts: `/Users/mozat/a-docs/sim-v2-*ahmad_kopi_chain*pos*`
- POS 1-pager: `landing/integrations/pos-integrations.html`
- StoreHub API docs: https://help.storehub.com/api
- KiX webhook receiver pattern: `app/routers/integrations/stripe_terminal.py` (reference)
