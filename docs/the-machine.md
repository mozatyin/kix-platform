# The KiX generation machine — audit + improvement roadmap (2026-05-31)

Per founder pivot: "**记得是修机器** — we don't modify the website; we modify the
mechanism that generates the website / games."

This doc maps what the machine produces today, where it lives, and the
specific gaps that need fixing in the generation layer (not in any specific
landing page).

---

## 1. What the machine produces today (verified)

### 1.1 Games (the core)
- **Output**: HTML files under `landing/games/{brand_id}/*.html`
- **Volume on disk**: 30 brand directories, 37,612 lines of real generated game HTML
- **Pipeline**: ELTM `brick → brand_injector → coder → PDCA → property_oracle` (in repo at `/Users/mozat/eltm/eltm/`)
- **Trigger**: `POST /api/v1/creative-gen/request` (router `app/routers/creative_gen.py`,
  292 lines) → enqueues job on Redis `creative_gen:queue` → ELTM HTTP at
  `localhost:8001` → generated HTML written back to `landing/games/{brand}/`
- **Game library**: 79 recipes seeded in `app/data/recipes_seed.json`
- **Sample brand**: `landing/games/brand-9c7223a6/` has 4 fully-generated games
  (coffee_shop, coffee-latte-art, match3, shop_manager — 5,441 LOC)

### 1.2 Recipes (the templates)
- **Output**: Recipe JSON objects (modules + rules composition)
- **Pipeline**: `POST /api/v1/recipe-generator/from-description` — NL → Recipe
  via `eltm.llm.call_llm`. Heuristic fallback if LLM unreachable.
- **Storage**: Redis `brand:{bid}:generated_recipes` HASH
- **Verified**: 79 seed recipes + on-demand NL generation

### 1.3 Brand-skinned assets (the polish layer)
- **Pipeline**: `app/services/wavef_brand_color.py` + `brand_translation_service.py`
- **Effect**: Each game gets merchant's logo + colour palette + voucher copy
  injected (via the brand_injector inside ELTM)

### 1.4 What's NOT generated yet
- **Landing pages** (this site): all 17 HTML files in `landing/*.html` are
  hand-edited. They should be generated from a per-brand template.
- **Per-merchant `/play.html?brand={X}` chrome**: hard-coded.
- **Per-merchant storefront listing card**: hard-coded MOCK_STORES array.
- **Per-merchant onboarding flow**: hand-coded welcome modal.

---

## 2. The machine's known gaps (where 修机器 actually happens)

### Gap A — Generation pipeline has no quality-gate after PDCA
- Current: brick → ELTM → coder → PDCA → property_oracle outputs HTML
- Missing: a "shop-owner verdict" pass — run the generated game through a
  persona sim (`scripts/sim_users_v2.py`) and reject if avg verdict < threshold
- **Fix**: add `eltm.brick.verdict_gate(game_html, persona_set) → keep/reject`
- **Why**: would catch low-quality games BEFORE they ship to merchants,
  same way we do landing-page verdict checks

### Gap B — No per-brand landing-page generator
- Current: every merchant lands on the same `/landing/portal.html`
- Missing: `POST /api/v1/landing-gen/request` that produces
  `landing/brands/{brand}/index.html` with merchant's name, photos, vouchers,
  case studies, all wired to their wallet
- **Fix**: build `app/services/landing_gen.py` mirroring `creative_gen.py` shape
- **Why**: enterprise pilots want their OWN landing page, not "yours powered by KiX"

### Gap C — The `play.html?demo=1` path doesn't dogfood the pipeline (FIXED THIS COMMIT)
- Previously: hand-coded SVG spin wheel, hand-coded "Step 1 of 3" cards.
- Now: loads a real generated game from `landing/games/{brand}/*.html` in an
  iframe. Visitor inspects the iframe → sees real KiX pipeline output.
- Each vertical (kopi/nasi/bubbletea/cafe/nail/gym) maps to a real generated
  brand sample on disk.

### Gap D — Recipe seed isn't periodically regenerated
- Current: `app/data/recipes_seed.json` (79 recipes) is hand-curated from
  Q4 2025
- Missing: a `scripts/refresh_recipe_seed.py` that calls
  `eltm.recipe_generator.batch_generate(N=50)` and writes the new variants
  to a staging directory for human review
- **Why**: 79 → 800 game library (deck claim) needs a generator, not manual writes

### Gap E — Generation is single-tenant per request
- Current: `creative_gen.request` is sync per-merchant
- Missing: nightly batch — for every active brand, regenerate ALL their games
  with the latest recipe versions + property-oracle improvements
- **Fix**: `app/workers/nightly_creative_refresh.py` (sibling of
  `wallet_reconciliation_worker.py`)
- **Why**: when we improve the pipeline (e.g. better brand-injection), every
  merchant should auto-get the upgrade without re-requesting

### Gap F — No "play this game against your own brand" preview before commit
- Current: merchants generate a game → see it in portal → either approve or reject
- Missing: a "swap your logo / voucher copy into this template right now"
  inline preview that doesn't require a backend generation job
- **Fix**: `app/services/brand_inject_preview.py` — client-side overlay via JS
  + CSS variable injection, no backend round-trip
- **Why**: cuts the iterate loop from 60s to <1s

---

## 3. What 'fix the machine' means concretely

Each gap = a piece of engineering work that improves EVERY merchant's
experience, not just one page. Priority order:

| # | Gap | Effort | Files to touch |
|---|-----|--------|----------------|
| 1 | C — dogfood `/play?demo=1` to load real generated games | DONE (this commit) | `landing/play.html` |
| 2 | A — verdict-gate the pipeline output | 1 day | `eltm/brick/verdict_gate.py` (new) |
| 3 | F — client-side brand-preview overlay | 2 days | `app/services/brand_inject_preview.py` (new) + JS module |
| 4 | E — nightly creative refresh worker | 3 days | `app/workers/nightly_creative_refresh.py` (new) |
| 5 | D — periodic recipe seed regeneration | 2 days | `scripts/refresh_recipe_seed.py` (new) |
| 6 | B — per-brand landing-page generator | 5 days | `app/services/landing_gen.py` (new) + ELTM integration |

Total: ~13 dev-days to convert the platform from "manual" to "fully
generative". Demo of #1 already in this commit.

---

## 4. What we should STOP doing

- ❌ Manually editing `landing/*.html` files for content tweaks. Anything
  that's content (copy / images / case-data) should be in JSON / YAML +
  rendered by a template engine.
- ❌ Hand-coding demo "games" in `play.html` (this commit reverses).
- ❌ Hand-curating the recipes seed when ELTM can batch-generate variants.
- ❌ Per-merchant landing pages that aren't generated from a single template.

## 5. What we should KEEP doing

- ✅ Verdict-driven iteration (persona sims) — the highest-leverage habit
- ✅ Honest disclaimers everywhere — the trust differentiator
- ✅ Self-serve / no-card-to-start — the SMB wedge
- ✅ Direct founder line — the alpha-cohort hook

---

## 6. Bible drift implications

After fixing Gaps A-F, Bible numbers need refresh:
- `recipes`: 79 → could grow weekly via D
- `services`: 47 → +2-3 per gap fixed (landing_gen, brand_inject_preview, etc)
- `workers`: 15 → +1 (nightly_creative_refresh)
- Add a §3.X entry per new generator

Auto-checked by `scripts/bible_check.py`. 0% drift policy maintained.

---

*This is the "machine" view. The user-facing landing pages are just one of
its outputs. Improving the landings without improving the machine is
"painting the assembly line" — wrong target. Per founder 2026-05-31.*
