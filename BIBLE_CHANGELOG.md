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
