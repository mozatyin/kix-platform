# Trinity 三体 Gap Audit — Bible × Code × Reality

**Date**: 2026-05-31 · **HEAD**: `802a334` · **Author**: Wave M-4 Trinity audit

> Trinity Protocol applied to KiX Bible: **Industry** (Bible's intended state)
> × **Academic** (`scripts/bible_check.py` mechanized verification) × **Reality**
> (live code metrics + verdict-gate output). Each gap = root-cause + structural
> fix + owning Wave.

---

## TL;DR · 4 mechanical fixes shipped + 5 high-impact gaps surfaced

| Fix | Status | Where |
|---|---|---|
| **Bible Appendix A drift → 0%** | ✅ shipped | KIX_GAMIFICATION_BIBLE.md §App A |
| **bible_check.py claim audits (ADR-4/11/12 + StoreHub)** | ✅ shipped | scripts/bible_check.py |
| **G-A16..G-A20 added to Bible** (catalog, gates, deprecation) | ✅ shipped | KIX_GAMIFICATION_BIBLE.md §App B |
| **ADR-13/14/15 added to Bible** (structural fix · persona axes · generator marker) | ✅ shipped | KIX_GAMIFICATION_BIBLE.md §App C |
| **G-A15 coverage gate** | ✅ wired | scripts/cron_nightly_refresh.sh Stage 4b |
| **G-A3 ELTM smoke** | ✅ wired | scripts/cron_nightly_refresh.sh Stage 4a |
| G-A1 Stripe live mode | 🟡 still partial | needs external account |
| G-A2 PSP backends | 🔵 still scaffolded | needs sandbox creds |
| G-A4 7 locales translation | 🟡 still partial | needs OpenRouter run |
| G-A10 Legal docs | 📝 BLOCKER | needs external counsel |
| G-A13 SMS OTP | 📝 not wired | needs Twilio/Aliyun account |

---

## Trinity 三体 method applied

```
        INDUSTRY                 ACADEMIC                 REALITY
   (Bible claims)         (bible_check + tests)     (live code · gate)
        │                          │                          │
        └──── claim ────────────── verify ───────── observed delta
                                       │
                                       v
                                 GAP REGISTRY
                                       │
                                       v
                              Structural fix (per
                              feedback_structural_fix_pattern)
```

A "gap" = any claim where INDUSTRY says X but ACADEMIC can't verify OR REALITY contradicts. Three-body iteration converges by closing one gap at a time, then re-running the cycle.

---

## 1 · Mechanical drift — fixed this session

**Before** (run 2026-05-31 09:00):
```
workers           16  bible says 15  drift 6.7% FAIL
services          53  bible says 47  drift 12.8% FAIL
test files       154  bible says 145 drift 6.2% FAIL
test functions  1670  bible says 1530 drift 9.2% FAIL
```

**After** (run 2026-05-31 15:00 via `bible_check --strict`):
```
All 11 metrics  drift 0.0%
Claim audits    ADR-4, ADR-11, ADR-12, StoreHub — all OK
Exit            0
```

---

## 2 · Claim-level audits — new (gap from inventory item #5)

The original `bible_check.py` covered 11 numeric metrics but did NOT verify that the per-claim test counts cited in ADRs were honest. Inventory item #5 flagged 5 high-impact unchecked claims; this session implemented 4:

| Audit | Before | After |
|---|---|---|
| ADR-4 "9 explicit tests in test_auction_adr4_new_users_only.py" | manual claim | bible_check `_audit_adr_test_count` |
| ADR-11 "15 tests test_country_slots.py" | manual claim | bible_check `_audit_country_slots_tests` |
| ADR-12 "12 tests test_wallet_reconciliation.py" | manual claim | bible_check `_audit_wallet_reconciliation_tests` |
| StoreHub "25 tests pass" | manual claim | bible_check `_audit_storehub_tests_pass` |
| Workers/services enumeration | not done | bible_check `_enumerate_files` (informational) |

**Remaining unchecked claim** (inventory #5): load SLA enforcement (e.g. "10k/sec, p99 <500ms, no deadlocks over 1h"). Needs Wave N — load-test harness in CI.

---

## 3 · New Bible entries — Wave M machinery captured

The Bible was written before Wave M (landing_gen + verdict_gate + bug-class catalog). Five Bible additions this session:

**Appendix B gap registry**:
- G-A16 · Bug-class catalog + structural-fix discipline (23 classes A-V) ✅
- G-A17 · landing_gen + verdict_gate machinery (Class A/H) ✅
- G-A18 · customer_vocab + pricing_canon fail-closed gates (Class D/J) ✅
- G-A19 · Persona-axes evaluation (audience × scale × vertical) ✅
- G-A20 · Legacy page deprecation pipeline (Class N — 12 stamped) ✅

**Appendix C ADRs**:
- ADR-13 · Structural-fix discipline — remove the broken path, not guard it
- ADR-14 · Persona-axis-matched verdict gate
- ADR-15 · `<meta name=generator>` + deprecation registry replaces hand-edits

**Status counts updated**: DELIVERED 14 → 19, with 5 new sections (landing_gen, verdict_gate, customer_vocab, pricing_canon, nightly_creative_refresh).

---

## 4 · Original gap registry — current status

15 gaps from v2.0 Honest Edition, status updated to 2026-05-31:

| ID | Gap | Was | Now | Δ Wave |
|---|---|---|---|---|
| G-A1 | Stripe live mode never end-to-end | 🟡 | 🟡 | needs external acct |
| G-A2 | OVO/GrabPay/WeChat backends | 🔵 | 🔵 | needs sandbox creds |
| G-A3 | ELTM creative end-to-end smoke | 🟡 | 🟡→✅ Stage 4a | M-4 |
| G-A4 | 7 non-base locales translation | 🟡 | 🟡 | needs OpenRouter batch |
| G-A5 | Multi-region deployment | 🔵 | 🔵 | needs k8s cluster |
| G-A6 | Welcome kit PDF + ship | 🔵 | 🔵 | needs print partner |
| G-A7 | ML smart-bidding labelling | 🟡 | 🟡 | needs labelled data |
| G-A8 | Native mobile shell | 🔵 | 🔵 | needs Capacitor build |
| G-A9 | Dunning / fail downgrade | 🔵 | 🔵 | needs Stripe live first |
| G-A10 | Legal docs (MSA/TOS/Privacy) | 📝 BLOCKER | 📝 BLOCKER | needs external counsel |
| G-A11 | Tax / fapiao / GST | 🔵 | 🔵 | needs accountant |
| G-A12 | Stream consumer lag visibility | 🟡 | 🟡 | next: add metric exporter |
| G-A13 | SMS OTP gateway | 📝 not wired | 📝 not wired | needs Twilio/Aliyun |
| G-A14 | Per-region compliance filter | 🟡 | 🟡 | next: extend auction filter |
| G-A15 | Coverage CI gate | 🟡 | 🟡→✅ Stage 4b | M-4 |

**Closed this session**: G-A3 (ELTM smoke wired in nightly cron Stage 4a), G-A15 (coverage measurement wired in Stage 4b).

**5 BLOCKERS that cannot close without external dependencies** (legitimate, not technical debt):
- G-A1 Stripe live · needs Mozat Stripe Atlas Singapore account approval
- G-A2 SEA PSPs · needs OVO/GrabPay/WeChat sandbox onboarding
- G-A10 Legal docs · needs external commercial lawyer
- G-A11 Tax compliance · needs ACRA-registered tax accountant
- G-A13 SMS OTP · needs Twilio account (or Aliyun for CN)

These should NOT be solved by Claude/code; they need a human action item with a date.

---

## 5 · Improvement opportunities — Trinity-iterated findings

Beyond the gap-catalog closures, the audit surfaced these opportunities:

### A. **Load-SLA enforcement** (inventory #5)
- Bible says "cross-brand commission transfer atomic in code but not battle-tested under sustained load"
- No metric defined (max TPS, p99 latency, concurrency, deadlock count)
- **Fix**: Wave N — load-test harness using `locust` or `k6` targeting `app/services/wallet.py` cross-brand path. SLA: 10k/sec sustained for 60min, p99 <500ms, zero deadlocks. Gate into CI.

### B. **Bible auto-generation from code** (next-gen of bible_check)
- Today: bible_check verifies numbers; humans edit Bible
- Next: `bible_generate.py` writes Appendix A directly, removing edit-drift
- **Fix**: M-5 — Appendix A becomes a generated artifact (like `Cargo.lock`); Bible source contains only narrative chapters; CI re-generates on every PR.

### C. **Persona registry consolidation**
- `scripts/sim_users_deepseek.py` PERSONAS dict + `scripts/verify_generated_brands.py` PERSONA_PROFILES + `PERSONA_AXES` are three sources of truth for the same data
- **Fix**: M-5 — `app/services/persona_registry.py` single source; all 3 callers import. Add Sarah/Aminah/Ahmad/Sandeep to it with axes baked in.

### D. **Gate threshold per page-type**
- Currently `verdict_gate` uses threshold=65, min_floor=40 for all pages
- Enterprise pages should arguably have higher floor (Sandeep is pickier)
- **Fix**: M-5 — `BrandConfig.verdict_threshold` override per page; default 65, enterprise=70.

### E. **Bible chapter for landing_gen pipeline**
- The 5 new services + 1 worker are in Appendix A counts but not described in narrative chapters
- **Fix**: M-5 — write Chapter 14 "Per-merchant landing generation" describing landing_gen + verdict_gate + persona axes (1-2 pages, link to docs/all-bugs-catalog.md).

### F. **Cron observability**
- `cron_nightly_refresh.sh` exits non-zero on REJECT but no alerting wired
- **Fix**: Add Slack webhook or PagerDuty integration (`NIGHTLY_ALERT_WEBHOOK_URL` env). Log archive after 30 days.

### G. **Reverse Bible — Code → Bible diff bot**
- Today: humans update Bible when code changes
- Next: PR-bot that diffs code structure + writes proposed Bible-section change
- **Fix**: M-6 — GitHub Action runs on PR diff; auto-comment "this PR adds 2 services; suggested Bible diff: ..."

---

## 6 · Trinity 三体 — what each body contributed

| Body | What it caught | Example |
|---|---|---|
| **INDUSTRY (Bible)** | High-bar claim that the codebase makes | "47 services" |
| **ACADEMIC (bible_check)** | Mechanical drift between claim and reality | "47 cited, 53 actual = 12.8% drift, FAIL" |
| **REALITY (verdict gate + tests)** | Whether the code that exists actually WORKS | "5/5 ACCEPT at threshold=65" |

Each body alone is insufficient:
- INDUSTRY alone = marketing decks that drift from code
- ACADEMIC alone = mechanically-correct but irrelevant numbers
- REALITY alone = working code with no narrative

Trinity = the three bodies pull each other back to truth.

---

## 7 · Discipline going forward

- **Every PR that adds a service/worker/test file** must update Appendix A or be rejected by `bible_check --strict` in CI.
- **Every new ADR** must cite a test file (auditable by `_audit_*` functions in bible_check).
- **Every bug class closed** updates `docs/all-bugs-catalog.md` AND adds a memory entry (per `feedback_structural_fix_pattern`).
- **Nightly cron** runs the full Trinity sweep: drift → claim audit → coverage → ELTM smoke → verdict gate. Any failure pages humans.

---

## 8 · Open items the cron will surface (not blockers)

- G-A1/A2/A10/A11/A13 — external-dependency blockers; list in `docs/external-deps-blockers.md` for ops review
- G-A12/A14 — code-side fixes ready for prioritization
- Load-SLA (item A) and persona-registry consolidation (item C) — propose Wave N scope

**Estimated effort to close all non-blocker gaps**: ~5 dev-days. The blocker subset (A1/A2/A10/A11/A13) needs ~3 weeks of vendor onboarding cycles.

---

*Trinity 三体 Gap Audit · v1.0 · 2026-05-31 · HEAD `802a334`*
*Verified: `python -m scripts.bible_check --strict` exits 0*
