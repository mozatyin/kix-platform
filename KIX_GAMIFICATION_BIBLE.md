# KIX GAMIFICATION BIBLE
## Single Source of Truth — Honest Edition

> "Software free, network paid." — KiX's only business model.
>
> This Bible matches code reality at HEAD `438fbd5` (2026-05-30).
> Marketing copy has been removed. Every claim now carries a status badge.
> If a claim is aspirational, it says so.

**Bible discipline**: numbers in this file are auto-checked by `scripts/bible_check.py`.
Drift > 5% breaks CI. The Bible updates with every major wave (no exceptions).

---

## Status Legend

| Badge | Meaning |
|---|---|
| ✅ **DELIVERED** | Code + tests + verified in code review. Production-grade. |
| 🟡 **PARTIAL** | Code exists, gaps documented. Works on the happy path. |
| 🔵 **SCAFFOLDED** | Structure/stub in place for a future wave. Not functional yet. |
| 📝 **ASPIRATIONAL** | Vision-level claim, not built. Belongs in Chapter 3. |

> Where you used to see "world-class" / "TikTok-grade" / "battle-tested", you now see one of the above. Evidence before assertions.

---

## 30-Second Executive Summary

KiX is **a gamification ads platform** for offline merchants. Architecturally, we've shipped:

- **123 routers** with **1,064 endpoints** (verified via `ls app/routers/*.py` + `grep '^@router' app/routers/*.py`)
- **145 test files** with **1,530 test functions** (verified via `find tests -name 'test_*.py'`)
- **11 PostgreSQL migrations** (verified via `ls migrations/versions/*.py`)
- **11 i18n locale catalogs** with **REAL translations** (en-SG, en-US, zh-Hans-SG, zh-Hans-CN, id-ID, ms-MY, th-TH, vi-VN, ar-EG, ar-SA, he-IL) — Wave I.B shipped 7 SEA+RTL translations via OpenRouter; no stub strings
- **5 payment PSP clients** (alipay_global, grabpay, ovo_indonesia, paynow_sg, wechat_pay) + Stripe live mode + Stripe Terminal POS (deck claims of "60+ payment methods" reflect the regional-route registry, NOT 60 wired backends — see §2.2 + §3.6)
- **15 background workers**, **47 services**, **18 industry merchant simulations**
- **79 gamification recipes** across **26 industries** (verified in `app/data/recipes_seed.json`)
- **~135K lines of Python** (per Appendix A drift check)

**What this means**: the *shape* is right. **What it does NOT mean**: every promise is production-ready. See Chapter 1 for what's actually delivered, Chapter 2 for what's in flight, Chapter 3 for what's still vision.

**Revenue model** (unchanged from v1):
1. **Auction** — merchants bid for new users via CPA/CPS/CPM/CPV/CPE (5 strategies, all wired)
2. **Subscription** — 4-tier brand subscriptions (FREE / STARTER ¥199 / GROWTH ¥999 / ENTERPRISE ¥5000) with 3-month trial (Apple Music strategy)

**Moat thesis** (philosophy, not yet validated at scale): software is being commoditized by AI; the only durable asset is cross-brand network effect (N² × marginal cost ≈ 0). Architecturally enforced via single-counterparty contracts: each brand only sees KiX, never another brand (Plenti-avoidance pattern).

---

## Table of Contents

- [Chapter 1 · What KiX IS today (verified, tested)](#chapter-1--what-kix-is-today-verified-tested)
- [Chapter 2 · What's coming (Wave D backlog)](#chapter-2--whats-coming-wave-d-backlog)
- [Chapter 3 · What's aspirational (long-term vision)](#chapter-3--whats-aspirational-long-term-vision)
- [Appendix A · Numbers](#appendix-a--numbers)
- [Appendix B · Honest gap registry](#appendix-b--honest-gap-registry)
- [Appendix C · ADRs (preserved from v1)](#appendix-c--adrs-preserved-from-v1)
- [Appendix D · Glossary](#appendix-d--glossary)

---

# Chapter 1 · What KiX IS today (verified, tested)

This chapter lists only claims with code AND tests AND no documented P0 gap. If you wouldn't show it to a regulator, it doesn't belong here.

## 1.1 Architecture (5-layer)

✅ **DELIVERED** — All 5 layers exist and route through `app/main.py`.

```
Layer 5 · Monetization      attribution + auction + wallet + campaigns + payouts
                             + fx + compliance + disputes + audiences + frequency_cap
                             + creative_gen + storefront + reservations + transactions
                             + fraud + brand_subscriptions + payment_methods
                             + dashboards + welcome_kit + ab_testing + invoices
Layer 4 · Merchant Portal   portal.html (Ads Manager) + storefront.html
                             + portal_api / portal_auth / portal_pixels / portal_settings
Layer 3 · Network           push_engine + master_accounts + kix_id + listings
                             + media + accounts + subscriptions + user_wallet
                             + deposits + pricing
Layer 2 · Gamification core progression + primitives + modules + network_effect
                             + commerce_loop + multiplayer + social + p2p
                             + group_actions + conditions + voucher_builder
                             + rule_engine + brand_modules + recipes
                             + recipe_generator + tutorials + vouchers + game
Layer 1 · Infrastructure    FastAPI + Redis + PostgreSQL (Alembic 7 migrations)
                             + Redis Streams + ELTM creative HTTP client
```

## 1.2 Auction & Quality Score

✅ **DELIVERED**
- GSP Vickrey with diversity floor (`app/routers/auction.py`, `test_auction_diversity.py`)
- 5 bid strategies CPA/CPS/CPM/CPV/CPE (`campaigns.py`, `auction.py`)
- Quality Score formula `0.3 + min(CTR×8, 0.4) + min(CVR×6, 0.3)` (`app/ml/inference.py:83-93`)
- Pacing controller — 338 LOC (`app/pacing_controller.py`)
- Bid floor + reserve price (`test_bid_floor.py`)
- Target audience filter `new_users_only / retargeting_only / all` (ADR-4)
- Frequency cap 10/day + 3/brand-day (`frequency_cap.py`, 8 endpoints)

## 1.3 Attribution

✅ **DELIVERED**
- 7-day last-touch default, configurable 1-365 days (`attribution.py`, 30+ endpoints)
- Multi-touch attribution (`test_multitouch_attribution.py`)
- Take-rate ladder 30-70%
- View-through + co-attribution + cohort
- Pixel JS SDK (`landing/sdk/kix-pixel.js`) + 7 endpoints

## 1.4 Wallet & Payouts (after Wave A fixes)

✅ **DELIVERED**
- Wallet auto-recharge (`wallet.py:502 _maybe_auto_recharge`, `test_wallet_autorecharge.py`)
- Atomic debit via Redis `WATCH/MULTI` (`payouts.py:452-588`) — was P0 before R12, fixed
- Audit log persisted to PostgreSQL via `app/services/audit_log_service.py` (migration `0007_audit_log.py`) — wired from `auth`, `payouts`, `campaigns`
- Saga coordinator for refund cascade (`app/saga.py` 349 LOC, `saga_definitions.py:refund_cascade_saga`)

🟡 **PARTIAL**
- Cross-brand commission transfer — atomic in code but **not battle-tested** under sustained load
- Bank-account payout — schema + ledger present; **no real bank API call** (see Chapter 2)

## 1.5 KiX ID + OAuth Connect

✅ **DELIVERED**
- Universal `kid` identity (`kix_id.py`, 17 endpoints)
- OAuth authorize/token flow + scope-filtered profile read
- Consent UI (`landing/connect.html`, 572 lines)
- Device fingerprint anti-fraud hook

## 1.6 Compliance scaffold

✅ **DELIVERED**
- Consent 15 scopes, 7 regulated (`consent.py`, 14 endpoints) — OTP gateway still external
- 70-phrase ad-law scanner (`compliance.py`)
- GDPR Article 15 export (`app/services/gdpr_export.py`)
- Audit log → PostgreSQL (was Redis-LIST-only in v1 Bible; **fixed in R12**, migration 0007)
- Per-region rule packs (`app/compliance_regional/*.py` — br/eu/id/in/ph/sg/th/us/vn)
- RTL CSS for ar/he (verified by `scripts/audit_rtl.py`)

🟡 **PARTIAL**
- Per-region rules are **data-only**; auction does not yet filter on them at serving time

## 1.7 Fraud / AML

✅ **DELIVERED** — `fraud.py` (13 endpoints) + `test_fraud.py`: trust score, device velocity, token replay, AML SAR primitives.

## 1.8 Network Effect tracking

✅ **DELIVERED** — `network_effect.py` (14 endpoints), K-factor tracker (`network_effect.py:1038`), `test_viral_kfactor.py`.
📝 Note: K > 0.5 sustained is aspirational until real merchants exist.

## 1.9 Brand Subscriptions & Quota

✅ **DELIVERED**
- 4-tier model (FREE / STARTER / GROWTH / ENTERPRISE) — `brand_subscriptions.py` (10 endpoints)
- 91-day trial cron (`app/workers/billing_cron.py`)
- Quota enforcement on campaigns/recipes/audiences/creative_gen
- PostgreSQL migration `0002_subscriptions.py`

🟡 **PARTIAL** — Day-91 auto-charge logic exists but executes against Stripe; **Stripe lives in test/mock by default** (see Chapter 2).

## 1.10 Trinity Engine

✅ **DELIVERED** — `app/services/trinity_engine.py` (institutionalises the 3T iteration loop as a callable engine, commit `96df36a`).

## 1.11 i18n scaffold

✅ **DELIVERED** structure
- 11 locale catalogs (`app/i18n/catalogs/`)
- Fluent (FTL) + ICU MessageFormat
- Brand translation service (migration `0004_i18n_brand_translations.py`)
- User locale preference (migration `0005_i18n_user_locale_pref.py`)
- Collation migration (`0006_i18n_collation.py`)
- LLM translation pipeline (`scripts/i18n_translate.py`)

🟡 **PARTIAL** — base locales (en-SG, zh-Hans-SG, en-US, zh-Hans-CN) reviewed; other 7 locales have `_translation_status.json` showing `needs_translation: 132/132` (id-ID), similar for vi/th/ms/ar/he. Pseudo-loc prefixes from v1 have been stripped, but copy still needs translation pass.

## 1.12 Push Engine

✅ **DELIVERED** (changed from v1 — commit `01c260f`)
- Real FCM via `firebase_admin` (`app/services/fcm_client.py`)
- Real APNS via `aioapns` (`app/services/apns_client.py`)
- Push worker dispatches to real providers; "simulated" code path removed for prod mode
- WeChat MP template push: see Chapter 2

## 1.13 Alpha Program

✅ **DELIVERED** — `alpha_program.py` (711 LOC) + `test_alpha_program.py` (465 LOC) + `landing/alpha.html`. Live cohorts: **0 real merchants onboarded** (sim cohorts only — see Chapter 2).

## 1.14 Test Coverage

🟡 **PARTIAL** — 1,006 test functions across 104 files. Tier 3 coverage push (commit `438fbd5`) covered game/social/p2p/multiplayer/modules. **Money-path routers (payouts, transactions, vouchers, wallet, subscriptions) now have unit tests** but coverage % is unmeasured. CI gate is **not** configured to fail under threshold.

## 1.15 Verified industry simulations

✅ **DELIVERED** — 18 `sim_lao*.py` scripts in `scripts/`. Sims pass against a fully-mocked PSP/push/ELTM stack. They are **smoke tests, not production proof**.

---

# Chapter 2 · What's coming (Wave D backlog)

These are short-cycle items: code present, gaps documented, fix-path known. **Estimates assume single dev-week; verify in `WORLD_CLASS_ROADMAP.md`.**

## 2.1 Stripe live mode 🟡 PARTIAL

- **State**: `app/services/stripe_live.py` distinguishes `mock` / `test` / `live` via `STRIPE_SECRET_KEY` prefix. Default is `sk_test_stub` → mock.
- **Gap**: Never run end-to-end against a real `sk_test_*` key + Stripe CLI webhook tunnel + real test card.
- **Risk**: Day-91 brand-subscription cron silently writes `mode: mock` to the ledger and reports `succeeded`. Zero revenue.
- **Wave D**: Real `sk_test_*` smoke + CI gate that fails if `is_mock()` in staging. ~3-5 days.

## 2.2 Payment PSPs beyond Stripe 🔵 SCAFFOLDED

- **State**: `app/services/payment_psps/` has 5 PSP clients (alipay_global, grabpay, ovo_indonesia, paynow_sg, wechat_pay) and a `_common.py` base. Stripe Terminal POS integration is live (Wave L).
- **Gap**: PSP clients are HTTP shells. Sandbox credentials not configured. `payments_regional` router exposes route entries for 60+ payment methods; only **5 PSPs + Stripe + Stripe Terminal** are backend-connected. Roadmap to **12 PSPs** by Q4: + FPX (MY), GCash (PH), Razorpay (IN), PromptPay (TH), TrueMoney (TH), DANA (ID), DuitNow (MY), MoMo (VN), ZaloPay (VN).
- **Truth-up note (2026-05-31)**: Deck v1/v4 phrase "60+ payment methods" refers to the catalog registry shape, NOT 60 wired backends. This Bible counts wired PSPs only (Appendix A · `psp clients = 5`).
- **Wave D**: Wire OVO + GrabPay sandboxes for ID/MY corridor; remaining PSPs as Wave E.

## 2.2a POS integrations 🟡 PARTIAL  (added 2026-05-31)

| Provider | Status | Notes |
|---|---|---|
| Stripe Terminal | ✅ Live | Wave L — full webhook + reconciliation |
| StoreHub (MY focus) | 🔵 Skeleton | `app/services/storehub_adapter.py` + 25 tests pass (Wave L). FastAPI router not yet wired — see `docs/rfc-storehub-fasttrack.md`. Target ship 2026-07-15. |
| Square | 📝 Aspirational | Listed in `/landing/integrations/pos-integrations.html` matrix; no router code yet. |
| Shopify | 📝 Aspirational | Listed; no router code. |
| Toast (US) | 📝 Aspirational | Listed; no router code. |
| Generic webhook bridge | ✅ Live | Stripe Webhook receiver supports custom JSON shapes via `psp_webhooks.py`. |

## 2.3 ELTM end-to-end 🟡 PARTIAL

- **State**: `creative_gen.py` calls `ELTM_BASE_URL=http://localhost:8001`. `scripts/eltm_smoke_test.py` exists.
- **Gap**: Smoke is run manually; not in CI. No fallback template gallery if ELTM is unreachable.
- **Wave D**: CI smoke gate + template-gallery fallback. ~2-3 days.

## 2.4 i18n real translation 🟡 PARTIAL

- **State**: Catalog structure done (Chapter 1). LLM batch pipeline (`scripts/i18n_translate.py`) ready.
- **Gap**: 7 of 11 locales (id, ms, th, vi, ar-EG, ar-SA, he) have `needs_translation > 100`. Pseudo-loc prefixes removed; copy still English.
- **Wave D**: Run LLM batch + human QA top 50 strings per locale. ~1-2 weeks.

## 2.5 Multi-region deployment 🔵 SCAFFOLDED

- **State**: `deployment/docker-compose.cn.yml`, `docker-compose.indonesia.yml`, `k8s/*.yaml`, `multi-region.md` (189 LOC), `dns-routing.md` (96 LOC), `failover_drill.py`.
- **Gap**: Single K8s namespace. Single Redis. Single PG. No DNS routing configured. No active-passive standby.
- **Wave D**: Redis cluster + PG read-replica in SG (3 weeks). HK passive standby is Wave E.

## 2.6 Welcome kit physical shipping 🔵 SCAFFOLDED

- **State**: `welcome_kit.py` renders HTML for table card / poster / sticker / standee.
- **Gap**: No PDF render, no print-on-demand partner, no courier integration. `request_shipping` pushes to a Redis queue with no consumer.
- **Wave D**: ReportLab PDF render + one CN print partner (凡科) + one courier (SF). ~2 weeks.

## 2.7 ML smart-bidding 🟡 PARTIAL

- **State**: `app/ml/trainer.py` + `inference.py` architected for LightGBM. `KIX_ML_ENABLED` defaults `false`. Heuristic fallback active. ML observability hooks live (`ml_observability.py`).
- **Gap**: No trained model artifact (`app/ml/_artifacts/` does not exist). Needs 30 days of real merchant labels.
- **Wave D**: Honest relabel as "rule-based bidding with ML upgrade path". Real model is Chapter 3.

## 2.8 Native mobile shell 🔵 SCAFFOLDED

- **State**: `landing/app/index.html` + `app.css` + `app.js` + `scan.html` is an H5 wrapper.
- **Gap**: No Capacitor/native shell. No store presence.
- **Wave D**: Capacitor iOS + Android wrap. ~4 weeks first store submission.

## 2.9 Dunning / payment-fail downgrade 🔵 SCAFFOLDED

- **State**: Brand subscription tier downgrade path exists. No grace-period dunning workflow.
- **Wave D**: 3-day grace + 7-day downgrade ladder.

## 2.10 Legal/contract paperwork 📝 BLOCKER

- **State**: No MSA, no privacy policy, no cookie policy, no merchant TOS in repo.
- **Wave D**: External counsel for 4 docs CN+SG. ~3 weeks calendar. This blocks signing the first real merchant.

## 2.11 Tax / invoice issuance 🔵 SCAFFOLDED

- **State**: `invoices.py` router exists.
- **Gap**: No tax-rule engine, no fapiao (CN) integration, no GST (SG) compliance.
- **Wave D**: First CN merchant invoice issuance.

## 2.12 Stream consumer workers 🟡 PARTIAL

- **State**: 3 stream consumers live (attribution, listing, reservation). Producers in 6+ routers.
- **Gap**: Consumer lag metric not exposed. XTRIM retention policy not enforced.
- **Wave D**: Lag metric + 24h XTRIM cron.

---

# Chapter 3 · What's aspirational (long-term vision)

These claims belong in pitch decks, not engineering meetings. **No promise here is wired to code.** If the team starts treating these as deliverables, the Bible should move them to Chapter 2 with a status badge.

## 3.1 N² network effect at 10K merchants 📝 ASPIRATIONAL

The N² thesis assumes density of cross-brand traffic. Until we have ≥100 real merchants in one geo with measurable cross-brand sessions, K-factor numbers from `network_effect.py` are computed on synthetic data.

## 3.2 LTV ¥9,300 / CAC ¥200-500 / ROI 18-46x 📝 ASPIRATIONAL

These are model outputs, not realized cohorts. No real merchant has paid KiX a single yuan. The model is documented in v1 Bible §1.3; it will be replaced with measured cohort data once 30+ merchants have completed a 12-month cycle.

## 3.3 80% → 20% funnel churn improvement 📝 ASPIRATIONAL

The "no-register-before-play" funnel is implemented (`/qr/scan` → device-fingerprint kid → play → win → register). The **churn delta is unmeasured** because zero real user funnel data exists.

## 3.4 90% merchant Premium auto-renew 📝 ASPIRATIONAL

The Apple-Music-style 3-month trial → auto-charge is wired. The 90% renewal rate is a hypothesis; it requires Wave D Stripe live + ~12 months of merchant cohorts to validate.

## 3.5 TriSoul behavior models in KiX 📝 ASPIRATIONAL

TriSoul lives in `/Users/mozat/mozat/` (separate repo). `app/routers/trisoul_integration.py` is a placeholder. End-to-end personalization signal from TriSoul → KiX recommendation is not wired.

## 3.6 60+ payment methods all live 📝 ASPIRATIONAL

Registry lists 60+. Wave D wires ~5 PSPs (Chapter 2.2). The remaining 55 are aspirational catalog entries pending demand.

## 3.7 100-merchant alpha + commerce flywheel 📝 ASPIRATIONAL

Alpha program scaffold is live (Chapter 1.13). Zero real merchants enrolled as of HEAD `438fbd5`. The 100-merchant cohort is the next 6-month operational goal.

---

# Appendix A · Numbers

Auto-verified by `scripts/bible_check.py`. CI fails if Bible drifts >5% from these.

```
HEAD                : 7554eac
Last commit         : feat(health): /api/v1/health/stripe-mode readiness surface (Wave H Opp #2)
Generated           : 2026-05-31

Code surface (after Wave G + H)
  routers           : 123           (was 94; +29: country_slots, wavef_*, support, retention, prizes, ...)
  endpoints         : 1,064         (was 925; +139)
  workers           : 15            (was 9; +6: wallet_reconciliation, voucher_lifecycle, support_sla, ...)
  services          : 47            (was 15; +32: wavef_*, retention, viral_amplifier, country_slots,
                                       storehub_adapter, whatsapp_template ...)
  migrations        : 11            (was 7; +4: audit_log_0007, prizes_0008, whatsapp_auth_0009,
                                       country_slots_0010, country_slots_all_iso_0011)
  total Python LOC  : ~135,800      (was 109,800; +26k from Waves E/F/G/H/I/J/K/L)

Test surface
  test files        : 145           (was 104; +41 — added test_storehub_adapter, test_whatsapp_template)
  test functions    : 1,530         (was 1,006; +524)
  test LOC          : ~32,500

Data
  recipes           : 79            (len(app/data/recipes_seed.json))
  industries        : 26
  industry sims     : 18            (ls scripts/sim_lao*.py)

i18n
  locales           : 11            (en-SG, en-US, zh-Hans-SG, zh-Hans-CN,
                                     id-ID, ms-MY, th-TH, vi-VN,
                                     ar-EG, ar-SA, he-IL)
  base locales done : 4             (en-SG, en-US, zh-Hans-SG, zh-Hans-CN)
  needs translation : 7 locales     (id, ms, th, vi, ar-EG, ar-SA, he)

PSPs
  scaffolded clients: 5             (alipay_global, grabpay, ovo_indonesia,
                                     paynow_sg, wechat_pay)
  live in prod      : 0             (Stripe defaults to mock)

Status counts in this Bible
  DELIVERED         : 14 sections
  PARTIAL           : 9 sections
  SCAFFOLDED        : 6 sections
  ASPIRATIONAL      : 7 sections
```

---

# Appendix B · Honest gap registry

For each P0/P1 gap, the current state, the fix path, and the Wave that owns it. Pulled from `/Users/mozat/a-docs/bible-vs-reality-gap-analysis.md` + `WORLD_CLASS_ROADMAP.md` + 2026-05-30 verification.

| ID | Gap | Severity | State | Wave |
|---|---|---|---|---|
| G-A1 | Stripe live mode never end-to-end | P0 | 🟡 PARTIAL | D-2.1 |
| G-A2 | OVO / GrabPay / WeChat Pay backends | P0 (regional) | 🔵 SCAFFOLDED | D-2.2 |
| G-A3 | ELTM creative end-to-end smoke | P0 | 🟡 PARTIAL | D-2.3 |
| G-A4 | 7 non-base locales need real translation | P0 (regional) | 🟡 PARTIAL | D-2.4 |
| G-A5 | Multi-region deployment | P1 | 🔵 SCAFFOLDED | D-2.5 |
| G-A6 | Welcome kit PDF + ship | P1 | 🔵 SCAFFOLDED | D-2.6 |
| G-A7 | ML smart-bidding labeled honestly | P1 | 🟡 PARTIAL | D-2.7 |
| G-A8 | Native mobile shell | P1 | 🔵 SCAFFOLDED | D-2.8 |
| G-A9 | Dunning / fail downgrade | P1 | 🔵 SCAFFOLDED | D-2.9 |
| G-A10 | Legal docs (MSA/TOS/Privacy/Cookie) | P0 (legal) | 📝 BLOCKER | D-2.10 |
| G-A11 | Tax / fapiao / GST | P0 (regional) | 🔵 SCAFFOLDED | D-2.11 |
| G-A12 | Stream consumer lag visibility | P1 | 🟡 PARTIAL | D-2.12 |
| G-A13 | SMS OTP gateway (Twilio/Aliyun) | P0 | 📝 not wired | D-future |
| G-A14 | Per-region compliance at auction filter | P1 | 🟡 PARTIAL | D-future |
| G-A15 | Coverage gate in CI | P1 | 🟡 PARTIAL | D-future |

**Closed since v1 Bible** (do not re-litigate):
- ✅ Push delivery (commit `01c260f` — real FCM + APNS)
- ✅ Audit log → PostgreSQL (migration `0007_audit_log.py`)
- ✅ Payouts WATCH/MULTI atomicity (`payouts.py:452-588`)
- ✅ Saga refund cascade (`saga_definitions.py`)
- ✅ Trinity Engine institutionalised (`trinity_engine.py`, commit `96df36a`)
- ✅ Tier 3 router test coverage (commit `438fbd5`)

---

# Appendix C · ADRs (preserved from v1)

| ADR # | Decision | When | Rationale | Still valid? |
|---|---|---|---|---|
| 1 | TikTok/Google single-counterparty model, **not** Plenti two-sided alliance | R5 | Plenti $100M / 3-year shutdown; 60% alliances die within 10 years | ✅ |
| 2 | 3-month trial, not 1-year | R11 | Apple Music strategy — accumulated switching cost > renewal cost | ✅ |
| 3 | Credit card on file mandatory at signup | R11 | One card / one account anti-fraud + day-91 auto-charge | ✅ (pending Stripe live) |
| 4 | Auction default `target_audience=new_users_only` | R5 | Don't buy back your own customers | ✅ (`auction.py:1310` enforced + 9 explicit tests in `tests/test_auction_adr4_new_users_only.py` 2026-05-31) |
| 5 | KiX is the user's single front-end (KiX App) | R5 | Like Facebook Connect — KiX owns the user relationship | 🟡 (H5 only today) |
| 6 | 7-day attribution default, configurable 1-365 days | R6 | F&B 7d, medical 365d, real estate 180d | ✅ |
| 7 | 79 recipes / 26 industries | R8 | Merchants choose by industry, don't design from scratch | ✅ |
| 8 | LLM for creative only, decisions deterministic | always | LLM non-determinism breaks money math + compliance evidence | ✅ |
| 9 | Audit log durable in PostgreSQL, not Redis LIST | R12 | PIPL §51 + GDPR Art 30 require regulator-grade retention | ✅ (new) |
| 10 | Bible auto-checked by `scripts/bible_check.py` | R12 | Documentation discipline contract-first, not post-hoc | ✅ (new) |
| 11 | **First 100 merchants per country pay 0% take rate forever** | Wave H (2026-05-31) | Founding-merchant scarcity drives global Day-1 land grab; same offer every country (Tanzania, Cambodia, Philippines, Singapore) prevents per-region dynamics. Atomic claim via `UPDATE...RETURNING ... FOR UPDATE SKIP LOCKED` — no race possible. Public counter on `pricing.html` makes it credible. | ✅ (`migrations/0010_country_slots.py` + `app/services/country_slots.py` + `app/routers/country_slots.py` + 15 tests `tests/test_country_slots.py`) |
| 12 | **Wallet ledger reconciliation worker** runs hourly to detect drift | Wave H (2026-05-31) | At 100m+ daily auction events, even 0.01% bookkeeping error = $millions silent loss. Worker computes expected = topups + auto-recharges − charges + refunds from durable HASHes; compares to live `wallet:{bid}:balance`. Severity tiers: ok / warn ($10) / alert ($100) / critical ($10k). Alerts to Redis LIST capped 1000. Never auto-repairs — humans review. | ✅ (`app/workers/wallet_reconciliation_worker.py` + 12 tests `tests/test_wallet_reconciliation.py`) |

---

# Appendix D · Glossary

| Term | Meaning |
|---|---|
| **kid** | KiX ID — universal user identity (`kid_xxxxxxx`) |
| **brand** | Merchant on KiX (1-N stores under one brand) |
| **master** | Multi-store parent account (e.g. Lao Wang's 10 milk-tea shops under one master) |
| **eid** | Entity ID — non-human entity (pet / property / vehicle) |
| **aid** | Account ID — B2B company entity (≠ master) |
| **GSP** | Generalized Second-Price auction — Google Ads-style |
| **CPA/CPS/CPM/CPV/CPE** | Bid strategies (per-acquisition / per-sale / per-mille / per-visit / per-engagement) |
| **target_audience** | new_users_only / retargeting_only / all |
| **Quality Score** | 0-1 float, ranks the auction |
| **Pacing** | Budget-vs-time smoothing |
| **Take Rate** | KiX's commission cut (30-70% of inter-brand transfer) |
| **NDR / GRR** | Net Dollar Retention / Gross Revenue Retention (SaaS KPIs) |
| **PSP** | Payment Service Provider (Stripe, OVO, GrabPay, etc.) |
| **ELTM** | External LLM creative generator (separate repo, HTTP'd from `creative_gen.py`) |

---

## Closing

> KiX has built the right *shape*. It has not yet built the right *outcomes*.
>
> The honest current state: 94 routers + 925 endpoints + 1,006 tests + 7 PG migrations + 5 PSP scaffolds + Trinity Engine + audit log durability + real FCM/APNS push + 18 industry simulations + zero real merchants.
>
> Wave D closes the launch-blocker gaps (Stripe live, ELTM smoke, i18n translation, legal docs). After Wave D, the Bible's headline claims become defensible. Until then, every status badge above is the unvarnished answer to "is that real?"

---

*KIX GAMIFICATION BIBLE · v2.0 · Honest Edition · Bible discipline enforced by `scripts/bible_check.py`*
