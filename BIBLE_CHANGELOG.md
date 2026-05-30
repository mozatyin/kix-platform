# BIBLE_CHANGELOG

> Tracks changes to `KIX_GAMIFICATION_BIBLE.md`. The Bible is the single source of truth; this file is the diff trail.
>
> **Discipline**: every major Wave (alpha launch, regional launch, headline feature) **must** include a Bible update in the same PR. Drift > 5% on `scripts/bible_check.py` fails CI.

---

## v2.0 — Honest Edition · 2026-05-30 · HEAD `438fbd5`

Bible rewritten to match code reality. v1 had drifted into marketing-speak.

### Numbers corrected (Bible was wrong)

| Field | v1 claim | v2 reality | Source |
|---|---|---|---|
| Routers | 38 | **94** | `ls app/routers/*.py | grep -v __init__` |
| Endpoints | 726 | **925** | `grep '^@router' app/routers/*.py` |
| Test functions | 23 (still cited in `WORLD_CLASS_ROADMAP.md`) | **1,006** | `grep '^def test_\|^async def test_' tests/test_*.py` |
| Test files | n/a | **104** | `find tests -name 'test_*.py'` |
| Migrations | n/a | **7** | `ls migrations/versions/*.py` |
| Workers | n/a | **9** | `ls app/workers/*.py` (excluding __init__) |
| Services | n/a | **15** | `ls app/services/*.py` (excluding __init__) |
| Locales | 11 | **11** ✓ (but 7 still untranslated) | `ls app/i18n/catalogs/` |
| PSPs | "60+" | **5 scaffolded, 0 live in prod** | `ls app/services/payment_psps/` |
| LOC (Python) | n/a | **~109,800** | `find app -name '*.py' | xargs wc -l` |
| Recipes | 79 | **79** ✓ | `len(app/data/recipes_seed.json)` |
| Industries | 26 | **26** ✓ | unchanged |
| Industry sims | 18 | **18** ✓ | `ls scripts/sim_lao*.py` |

### Claims removed (no evidence)

- "world-class" / "TikTok-grade" / "battle-tested" — no measurable bar
- "5-min AI game" as a delivered claim — moved to Chapter 2 / Wave D (ELTM smoke not in CI)
- "60+ payment methods" as a delivered claim — moved to Chapter 2 (5 PSPs scaffolded, 0 live)
- "Stripe-ready" as a delivered claim — moved to Chapter 2 (mock by default)
- "Welcome kit (physical materials)" as a delivered claim — moved to Chapter 2 (HTML only)
- "Smart Bidding" as ML — moved to Chapter 2 with honest "rule-based with ML upgrade path"
- "11 Trinity rounds validated" framing — replaced with concrete pass/gap counts
- "LTV ¥9,300 / ROI 18-46x" hard claim — moved to Chapter 3 as ASPIRATIONAL
- "80%→20% funnel improvement" hard claim — moved to Chapter 3 as ASPIRATIONAL
- "90% Premium auto-renewal" hard claim — moved to Chapter 3 as ASPIRATIONAL
- "K > 0.5 sustained" — moved to Chapter 3 (no real merchants to measure on)

### Claims revised (now have status badges)

Every promise in the Bible now carries one of:
- ✅ **DELIVERED** — code + tests
- 🟡 **PARTIAL** — code exists, gaps documented
- 🔵 **SCAFFOLDED** — stub for future
- 📝 **ASPIRATIONAL** — vision, not built

### Claims newly DELIVERED (since v1)

- ✅ Push delivery is real (FCM + APNS) — commit `01c260f` replaced simulated dispatch
- ✅ Audit log persisted to PostgreSQL — migration `0007_audit_log.py` + `audit_log_service.py`
- ✅ Payouts atomicity via Redis `WATCH/MULTI` — `payouts.py:452-588`
- ✅ Saga coordinator for refund cascade — `saga_definitions.py:refund_cascade_saga`
- ✅ Trinity Engine institutionalised — `trinity_engine.py`, commit `96df36a`
- ✅ Tier 3 router test coverage — commit `438fbd5`
- ✅ i18n pseudo-loc prefixes removed (still need real translations for 7 locales)
- ✅ PSP scaffolds for 5 regional providers (`app/services/payment_psps/`)
- ✅ 7 PostgreSQL migrations (was 1 in v1)
- ✅ 9 background workers (was 4 in v1 — added alpha_cohort, audit_retention, email, listing, reservation, webhook)

### Structural changes

- **Chapter 1** — only contains things with code + tests + no documented P0
- **Chapter 2** — Wave D backlog with clear fix paths and time estimates
- **Chapter 3** — aspirational claims clearly separated, no marketing language
- **Appendix A** — auto-verified numbers; CI-checked drift
- **Appendix B** — honest gap registry (15 gaps tracked, 6 closed since v1)
- **Appendix C** — ADRs preserved, 2 new ADRs added (9: audit log durable, 10: Bible auto-check)

### Sections preserved from v1 (still valid)

- 5-layer architecture (Layer 1-5)
- 3-role architecture (user / KiX / brand)
- User journey 3-step funnel (scan → play → win → register)
- 7-step merchant journey with 3-month trial rationale
- GSP Vickrey auction math
- 7-day attribution default + 1-365 day configurability
- 5 bid strategies
- Single-counterparty contract model (Plenti-avoidance)
- All 8 ADRs from v1
- Glossary

### File state

- `KIX_GAMIFICATION_BIBLE.md` — rewritten (883 → ~560 lines, denser, no marketing fluff)
- `BIBLE_CHANGELOG.md` — NEW (this file)
- `scripts/bible_check.py` — NEW (CI-enforced drift detector)

---

## v1.0 — 2026-05-29 · pre-Honest-Edition

Original Bible. Marketing-heavy. Numbers drifted from code (claimed 38 routers / 23 tests; actually 94 / 1006). Removed/restructured in v2.

---

*Bible discipline: this changelog updates with every Bible edit. No silent rewrites.*

---

## v2.1 — 2026-05-31 · Wave G+H post-iteration sync

**Author**: KiX window session

**Changes**:
- Appendix A numbers refreshed (HEAD 7554eac, all 6 code-surface metrics):
  routers 94→123, endpoints 925→1064, workers 9→15, services 15→45,
  migrations 7→10, tests 1006→1492, LOC 109.8k→135.1k.
  `bible_check.py` now PASS at 0% drift on 6/11 metrics.
- ADR #4 status note clarified: explicit `tests/test_auction_adr4_new_users_only.py`
  (9 tests) shipped 2026-05-31 to lock the default behavior. Was implemented but
  unproven; now formally tested.

**Trinity context**: this update closes the Trinity Gap Analysis Section §2
finding (Bible drift FAIL on 2/6 metrics, ADR #4 marked uncertain).

**Net code added since v2.0**: Waves E/F/G/H shipped 25k LOC across 29 routers,
6 workers, 30 services, 3 migrations, 486 tests. Major surfaces: country slots
(Opp #3), wallet reconciliation, viral amplifier, retention engine, multi-week
arcs, KiX ID SSO, WhatsApp OTP, POS framework, prize fulfillment, 5 PSPs, +20
wavef_* promotion mechanics.

---

## v2.2 — 2026-05-31 · ADR #11 + #12 from Wave H

**Author**: KiX window session

**New ADRs**:
- **ADR #11 — First 100 merchants per country pay 0% take rate forever**.
  Wave H Opp #3. Backs v4 deck slide 13 + pricing.html. Migration 0010
  + atomic PG claim via FOR UPDATE SKIP LOCKED. Public counter + per-
  country open-slot grid. 15 tests in tests/test_country_slots.py.
  Shipped commit 1a17a1d. Founding merchants get take_rate_bps=0 forever
  via is_founding(brand_id) check; everyone else gets 500 (5%).

- **ADR #12 — Wallet ledger reconciliation worker runs hourly**.
  Wave H Opp #1. Surfaces drift caused by missed/double events at scale.
  100m sim found 50 P1 wallet_drift events per seed (sim was undercounting
  auto-recharges — real wallet was correct). Worker provides defense-in-
  depth real-money guard regardless. Severity tiers ok/warn/alert/critical
  + Redis-backed alert queue. 12 tests in tests/test_wallet_reconciliation.py.
  Shipped commit 8325181.

**Trinity context**: Both ADRs emerged from 100m × 90d sim verification
(seed 42/100/7777) + DeepSeek user simulation (Round 1/2/3, 25 personas
× 5 pages each). Round 3 confirmed `100free_unclear` dropped 3→3→0 once
the country-slot mechanism was visible to merchants on pricing.html.

**Net Bible state**: 12 ADRs (was 10), 0% drift on all 11 Appendix A
metrics, ADR #4 status note now cites the explicit test file.
