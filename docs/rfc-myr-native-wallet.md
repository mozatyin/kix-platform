# RFC: Native MYR Wallet (vs SGD-billed-with-FX)

**Status:** Draft for discussion — no code yet
**Author:** Founder / KiX engineering
**Date:** 2026-05-31
**Decision target:** Before MY launch (Q3 2026)
**Drives from:** Ahmad sim Wave I.A-2/I.A-3 (and 2 other MY chain CEO interviews):
> "SGD billing with RM pricing creates reconciliation hell. My finance
>  controller will push back. Just bill in MYR or let me prepay in RM
>  and convert once."

---

## TL;DR

We currently bill all merchants in SGD via Stripe, regardless of country.
For MY operators with RM-denominated P&Ls, this creates monthly reconciliation
friction (FX gain/loss, multiple GL entries, audit trail complexity).

This RFC proposes a **native MYR wallet** path: merchants top up in MYR, are
charged in MYR, and see only RM in their portal. Treasury handles the
SGD↔MYR conversion in the background.

Three options compared. Recommendation: **Option C (Phased — start with
MYR display + FX-rate-locked SGD billing, native MYR settlement Q1 2027)**.

---

## Today's behavior (SGD-only)

- Merchant tops up via Stripe; charged SGD on their MY card → FX spread on
  card-issuer side (typically 1–3%, invisible to us)
- Portal shows wallet in MYR (Wave I.A-2 currency map) — but billed in SGD
- Invoice shows SGD line items
- Reconciliation: merchant's accountant matches RM-quoted CPA to SGD-paid
  charge + FX delta → manual mental math every month
- We see SGD revenue; FX risk = merchant's card issuer

## Why this matters now

| Signal | Source |
|---|---|
| "Wallet billed in SGD at live FX… my finance team will hate reconciling SGD invoices against RM budgets" | Ahmad sim R3 (verbatim) |
| "RM pricing? This is SGD—what's the forex risk?" | Ahmad sim R1 |
| 3 of 5 MY chain operators interviewed in 2026-04 raised this | Founder interview notes |
| MY launches Q3 2026 → ~30 founding-100 slots projected | Country slots forecast |

If we leave SGD-billing as the only option, we lose enterprise MY merchants
to "we can't reconcile" finance objection — even though the product fits.

---

## Three options

### Option A — Status quo: SGD-only billing, MYR display only

**Implementation:** Already shipped (Wave I.A-2 currency map).
**Cost:** $0 (done).
**Pro:**
- Simplest treasury — single SGD bank account
- FX risk pushed entirely to merchant's card issuer
**Con:**
- Reconciliation friction = enterprise dealbreaker for MY chains
- Ahmad-archetype operators will refuse pilot or churn after 1 invoice
- Sim-validated: blocks at least 2 of 5 archetype operators

**Verdict:** Ship-blocker for MY enterprise tier.

### Option B — Full native MYR wallet (greenfield)

**Implementation effort:** ~6–8 weeks
- New MYR ledger table (currency-typed money fields)
- Stripe sub-account / multi-currency Stripe (or BillPlz / Razer for MY)
- MYR-denominated invoices, MYR P&L roll-up
- Bank account: open MYR account at OCBC-MY or RHB
- Tax: GST/SST registration in MY
- Treasury: end-of-month MYR→SGD sweep based on policy
**Cost:** ~$8K setup (legal/accounting) + recurring ops
**Pro:**
- Best merchant experience — fully MYR end-to-end
- Removes 100% of reconciliation friction for MY ops
- Positions us as serious MY operator, not opportunistic
**Con:**
- 6–8 weeks blocks MY launch
- Doubles treasury complexity from day 1
- Locks us into MY-specific banking before we know the demand curve

**Verdict:** Right destination, wrong timing for Q3 2026 launch.

### Option C — Phased: FX-locked SGD invoices now, native MYR wallet Q1 2027 [RECOMMENDED]

**Phase 1 (ship by 2026-06-30, ~2 weeks):**
- Keep SGD billing
- BUT: lock the FX rate at top-up time (not at charge time) — so an
  RM-denominated top-up gives you a SGD wallet at a fixed exchange rate
- Invoice shows BOTH currencies side-by-side (RM 700 = S$200 at fixed 3.50)
- Portal already shows MYR (Wave I.A-2)
- Bonus: monthly FX summary PDF for accountant (auto-generated)
- Treasury: take small FX spread (~0.5%) as buffer; manage SGD→MYR exposure
  via 30-day forward contract at SCB-SG (existing relationship)

**Phase 2 (Q1 2027, after 20+ MY founding merchants live):**
- Native MYR wallet (Option B implementation) if demand validates
- Migration: existing SGD-wallet MY merchants get auto-converted at next
  invoice cycle (their choice to opt in)

**Cost Phase 1:** ~1 sprint (1 backend, 1 day finance/legal review)
**Pro:**
- Removes 80% of reconciliation friction at 10% of effort
- Buys data: we'll know if "RM display" alone is enough or if "RM bank
  charges" matter more, before committing to Phase 2
- Doesn't block Q3 MY launch
- Treasury risk capped via FX hedging on existing SCB relationship
**Con:**
- Still SGD on the bank statement (FX-sensitive accountants may push back)
- 0.5% FX buffer is a small revenue tax on us until Phase 2
- Phase 2 needed eventually anyway

**Verdict:** Right scope for Q3 MY launch; preserves option to upgrade.

---

## Recommendation

**Adopt Option C.** Ship Phase 1 by 2026-06-30 (well before MY soft launch).
Decide on Phase 2 in 2026-Q4 based on 20+ MY merchant feedback.

### Phase 1 work items
- [ ] Backend: `currency_lock_at_topup` field on wallet_transactions
- [ ] Backend: invoice generator dual-currency output
- [ ] Backend: monthly FX summary PDF endpoint
- [ ] Portal UI: show locked-FX-rate at top-up + on every charge line
- [ ] Treasury: open 30-day forward contract with SCB-SG
- [ ] Legal: update MY ToS to disclose FX lock + 0.5% buffer
- [ ] Sales: 1-pager for MY accountants explaining reconciliation flow
- [ ] Sim verification: re-run Ahmad on /pricing.html after Phase 1 ships

### Phase 1 success metric
Ahmad-archetype sim verdict (re-run v2 with Phase 1 changes) shifts from
"SGD billing creates reconciliation hell" to "OK, the FX lock + dual-currency
invoice handles it for now."

### Phase 2 trigger
- ≥20 MY founding merchants live AND
- ≥3 of them have explicitly raised "we need MYR billing" in QBR

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| MYR depreciates >2% in 30 days, FX buffer underwater | M | M | 0.5% buffer + 30-day forward contract; can widen buffer if needed |
| Phase 1 ships late, MY launch delayed | L | H | 2-week scope is conservative; can fall back to "MYR display only" (status quo) if blocked |
| Phase 2 never happens because Phase 1 is "good enough" | M | L | OK — that's a successful outcome if merchants accept it |
| Stripe MY rejects multi-currency model | L | H | Validated with Stripe SG sales rep 2026-05-15; confirmed OK as long as primary entity is SG |

---

## Open questions

1. Do we offer dual-currency invoice as opt-in or default?
   - Default-on simpler for ops; opt-in respects merchants who prefer SGD-only
2. What FX source for the lock-in rate?
   - Stripe FX (built-in, mid-market+spread) vs OCBC daily reference (cheaper but manual)
3. Does MY tax (SST 6%) apply on the dual-currency invoice?
   - Need MY tax accountant review before Phase 1 ships
4. How does this interact with the country-slots founding-100 mechanic?
   - Doesn't — pricing is 0% take rate either way; only matters for FX of top-up amount

---

## References
- Ahmad sim transcripts: `/Users/mozat/a-docs/sim-v2-*ahmad_kopi_chain*`
- MY merchant interview notes: founder Notion / Q2-MY-research
- Pricing display Wave I.A-2: `landing/pricing.html` + `landing/portal.html`
- Treasury policy doc: TBD
