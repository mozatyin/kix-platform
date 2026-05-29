# KiX Landing/Portal — RTL Readiness Audit & Migration Plan

**Date:** 2026-05-30
**Owner:** i18n Phase 3 (RTL milestone, months 6–9 per `i18n-trinity-strategy.md` §4.3)
**Scope:** `landing/**/*.{html,css,js}` (~8k LoC of inline-CSS + 5 HTML pages + portal-views JS).
**Audit tool:** `scripts/audit_rtl.py` (deterministic regex, no LLM, idempotent on already-logical CSS).
**Target locales:** `ar-EG`, `ar-SA`, `he-IL`, `fa-IR`, `ur-PK`.

This document is **Phase 2 (audit + plan)**. It deliberately does **not** rewrite any CSS — that work is Phase 3.

---

## 1. Why this matters

Arabic + Hebrew + Persian + Urdu cover ~6% of world population and ~30% of MENA. The strategy doc commits to landing them in months 6–9 as part of the Tier-1 expansion. The painful part of RTL is **not** translation — it is the chrome (margins, positioning, floats, flex-direction, icon orientation, border-radius corners, transforms) that was authored against an implicit LTR assumption. The fix at the language level is `dir="rtl"` on `<html>`; everything else is CSS plumbing.

If we keep authoring with directional properties (`margin-left`, `padding-right`, `left:`, `right:`), each new page adds debt. If we **migrate to logical properties** (`margin-inline-start`, `padding-inline-end`, `inset-inline-start`, `inset-inline-end`), every new page is RTL-correct for free, and the runtime cost is zero because logical properties are native CSS (>95% browser support since 2023).

---

## 2. Audit summary — current state

Audit was run with `python3 -m scripts.audit_rtl --csv docs/i18n/rtl-audit-report.csv`.

**Total: 714 findings across `landing/`** — 116 P0 + 41 P1 + 557 P2.

| Scope | Files audited | Total | P0 (breaks) | P1 (suboptimal) | P2 (cosmetic) |
|---|---:|---:|---:|---:|---:|
| **All `landing/`** (incl. generated games **and** new `rtl-base.css` / `rtl-test.html` from this PR) | ~120 | 714 | 116 | 41 | 557 |
| **Excluding `landing/games/`** (LLM-generated content, out of scope per strategy §3.1) | 32 | 491 | 71 | 41 | 379 |
| **Excluding games/ AND new RTL infrastructure files** (the true legacy-debt count) | 30 | 427 | 62 | 37 | 328 |

> Note: `i18n/rtl-base.css` itself surfaces 39 audit findings because its job is to **author explicit `right:auto;left:Npx`** RTL-override pairs — those are exactly the patterns the audit looks for. They are intentional. The audit CLI can be filtered with `grep -v "^i18n/rtl-"` for the "true legacy debt" view.

### 2.1 Severity breakdown

- **P0 (breaks layout) — 107 total / 62 outside generated games.** `float: left|right`, absolute `left:` / `right:` positioning, `flex-direction: row` with directional intent, `translateX(...)` with positive value, `.style.left = ...` / `.style.right = ...` JS assignments. These cause the wrong corner to anchor, sidebars to land on the wrong edge, and toast popups to fly in from the wrong direction.
- **P1 (suboptimal) — 37 total.** Directional spacing (`margin-left`, `padding-right`, `border-left`) and `text-align: left|right`. RTL "works" without these fixes but text sits on the wrong edge.
- **P2 (cosmetic) — 506 total / 328 outside games.** ~395 ASCII box-drawing characters (`─│┌└` etc — Chinese comment dividers in the CSS) + ~111 directional arrow glyphs + a handful of `direction: ltr` hardcodes. ASCII art dominates this bucket but most of it is in comments, **not** semantic content; we will filter these in PR review.

### 2.2 Top files (worst-first)

| File | P0 | P1 | P2 | Notes |
|---|---:|---:|---:|---|
| `portal.html` | 21 | 20 | 134 | Highest-density file (4,977 LoC). Float-free; bulk of P0 is `position: fixed; top:0; left:0; right:0` (header/footer bars) and toast `translateX(120%)`. P2 noise is mostly comment dividers. |
| `play.html` | 12 | 7 | 16 | Game host page. Several `position:absolute; right:8px` for close buttons. |
| `app/app.css` | 8 | 1 | 14 | First-class CSS — easiest to migrate. |
| `sdk/kix.js` | 7 | 1 | 0 | SDK code does `.style.left =` for floating widgets — needs to switch to class-toggle. |
| `storefront.html` | 4 | 1 | 19 | Lower density; surgical fixes feasible. |
| `i18n/locale-switcher.js` | 4 | 0 | 0 | False-positive-prone — switcher already special-cases RTL via separate CSS; verify in review. |
| `index.html` | 3 | 2 | 36 | Marketing page; low-effort migration. |
| `i18n/rtl.css` | 2 | 0 | 2 | The two P0 findings are **intentional** `right: auto; left: 14px` swaps for the `.kix-top-right` widget — explicit RTL override patterns. Audit tool will continue to surface; ack as known-good. |
| `connect.html` | 0 | 1 | 0 | Near-clean. |
| `landing/games/**/*.html` (generated) | 45 | 0 | 178 | **Out of scope.** These are LLM-generated brand games; the generator pipeline (Code-Soul / ELTM) owns their migration. Tracked separately. |

Full per-finding output: [`docs/i18n/rtl-audit-report.csv`](./rtl-audit-report.csv).

---

## 3. Migration plan

### 3.1 Recommended tooling

| Layer | Pick | Why |
|---|---|---|
| **CSS auto-migration** | [`postcss-logical`](https://github.com/csstools/postcss-logical) v7+ | Mature, well-tested, transforms `margin-left → margin-inline-start` etc. Idempotent on already-logical input. ~3kB build-time, zero runtime cost. |
| **Stylelint guardrail** | [`stylelint-use-logical`](https://github.com/csstools/stylelint-use-logical) | CI rule blocks new directional properties from landing. |
| **PR check** | Custom: `scripts/audit_rtl.py --csv ... && diff` against baseline | Fails when delta in **P0** > 0 vs main. See §7. |
| **Runtime RTL toggle** | Existing `landing/i18n/i18next-runtime.js` (Agent 2 owns) sets `<html dir="rtl">` based on locale. | No new runtime. |
| **Base stylesheet** | New `landing/i18n/rtl-base.css` (this PR) | Author-side shims + utilities. Loaded by `i18next-runtime.js` only when `dir="rtl"`. |

### 3.2 Conversion order (by density × priority)

1. **`portal.html`** (4,977 LoC, 21 P0 + 20 P1) — highest blast radius. Convert first; do a tracer-bullet RTL render after to validate `postcss-logical` + manual P0 fixes.
2. **`play.html`** (1,367 LoC, 12 P0) — game host chrome; medium urgency (Arabic-speaking players matter for monetisation).
3. **`storefront.html`** (713 LoC, 4 P0) — merchant storefront; low complexity but customer-facing.
4. **`app/app.css`** (8 P0) — first-class CSS, easiest tool target.
5. **`sdk/kix.js`** (7 P0) — JS-set positioning needs hand-conversion to class toggling. **Not** auto-migrate-able.
6. **`index.html` / `connect.html`** — low density, mop-up.
7. **`landing/games/**`** — defer; owner = game-generation pipeline.

### 3.3 Effort estimate

| Workstream | Effort |
|---|---:|
| PostCSS pipeline setup + CI wiring (npm build target, stylelint config) | 0.5 PW |
| `portal.html` migration (auto + manual P0 review + visual QA) | 1.0 PW |
| `play.html` + `storefront.html` + `index.html` + `connect.html` migration | 0.75 PW |
| `app/app.css` + `sdk/kix.js` JS positioning rewrite | 0.5 PW |
| `rtl-base.css` finalisation (per-component overrides, icon mirroring) | 0.25 PW |
| QA — visual diff in pseudoloc + 1 native Arabic reviewer pass | 0.5 PW |
| Buffer (10%) | 0.5 PW |
| **Total** | **~4 person-weeks** |

This slots into the strategy doc's month-6–9 RTL milestone with comfortable buffer.

---

## 4. CSS logical-properties cheat sheet

| Directional (legacy)             | Logical (target)                | Notes |
|---|---|---|
| `margin-left`                    | `margin-inline-start`           | |
| `margin-right`                   | `margin-inline-end`             | |
| `margin: 0 16px 0 24px`          | `margin-block: 0; margin-inline: 24px 16px` | shorthand swaps |
| `padding-left`                   | `padding-inline-start`          | |
| `padding-right`                  | `padding-inline-end`            | |
| `border-left`                    | `border-inline-start`           | width/style/color sub-properties exist (`border-inline-start-width` etc.) |
| `border-right`                   | `border-inline-end`             | |
| `border-top-left-radius`         | `border-start-start-radius`     | corner radii use *two-axis* logical names |
| `border-top-right-radius`        | `border-start-end-radius`       | |
| `border-bottom-left-radius`      | `border-end-start-radius`       | |
| `border-bottom-right-radius`     | `border-end-end-radius`         | |
| `text-align: left`               | `text-align: start`             | |
| `text-align: right`              | `text-align: end`               | |
| `float: left`                    | `float: inline-start`           | **but prefer flexbox** for new code |
| `float: right`                   | `float: inline-end`             | |
| `left: 0`                        | `inset-inline-start: 0`         | |
| `right: 0`                       | `inset-inline-end: 0`           | |
| `top: 0; right: 0`               | `inset-block-start: 0; inset-inline-end: 0` | |
| `top: 0; left: 0; right: 0`      | `inset-block-start: 0; inset-inline: 0`     | the LTR-symmetric case stays clean |
| `transform: translateX(100%)`    | `transform: translate(var(--rtl-flip, 1) * 100%, 0)` | requires CSS var set on `<html>` for RTL |
| `flex-direction: row`            | unchanged — flexbox is already direction-aware via `dir` on ancestor | `row-reverse` still maps to *visual* reversal regardless of `dir` |
| `clear: left`                    | `clear: inline-start`           | |

**Compound shorthands to watch:**
- `margin: <top> <right> <bottom> <left>` — *do not* mechanically swap; use `margin-block` + `margin-inline` two-value form which is dir-aware.
- `border-radius: <tl> <tr> <br> <bl>` — same caveat; consider per-corner logical assignments.

### 4.1 Browser support (as of 2026-05)

| Feature | Chrome | Safari | Firefox | Edge |
|---|---|---|---|---|
| `margin-inline-*`, `padding-inline-*` | ✅ 87+ (2020) | ✅ 14.1+ (2021) | ✅ 66+ (2019) | ✅ 87+ |
| `inset-inline-*`, `inset-block-*` | ✅ 87+ | ✅ 14.1+ | ✅ 63+ | ✅ 87+ |
| `border-start-start-radius` (logical corners) | ✅ 89+ (2021) | ✅ 15+ (2021) | ✅ 66+ | ✅ 89+ |
| `float: inline-start` | ✅ 118+ (2023) | ✅ 17.4+ (2024) | ✅ 53+ | ✅ 118+ |
| `text-align: start`/`end` | ✅ 1+ | ✅ 3.1+ | ✅ 1+ | ✅ 12+ |
| `direction-aware logical media queries` | partial | partial | partial | partial |

**Coverage:** >97% of global users on caniuse as of 2026-05. **No polyfill needed** for KiX's target markets (Tier 1 = mobile-first modern browsers).

### 4.2 Manual review list — things that must NOT auto-mirror

| Element | Why |
|---|---|
| Logos (KiX wordmark, brand glyphs) | Brand identity — never mirrored. |
| Photographs and product images | Visual content is not chrome. |
| Charts/graphs with directional X-axis (e.g. "time → ") | Time should still flow LTR even in RTL UI (Arabic readers report this preference). |
| Code snippets / monospace blocks | Code is LTR universally. |
| Phone numbers, email addresses, URLs | LTR atoms inside RTL paragraphs — wrap in `<bdi>` or `dir="auto"`. |
| Currency: keep symbol+number adjacency | `Intl.NumberFormat` handles this; do not manually pad. |
| Game boards (chess, mahjong, etc. in `landing/games/`) | Game logic depends on coordinates; do not flip the board, only the UI chrome around it. |
| `→`, `←`, `▶`, `◀` glyphs used as **directional indicators** (Next button, slider) | These SHOULD mirror (use `.kix-icon-directional` class — already wired in `rtl.css`). |
| `→`, `←` glyphs used as **non-directional decoration** (e.g. logo, arrow emoji in tagline) | These should NOT mirror — author must omit the class. |

### 4.3 ASCII art / box-drawing

The audit flagged 395 ASCII-box-drawing matches. Spot-check sample: all are in CSS comment dividers like `/* ── Hero ───── */`. They don't affect rendered output. **Resolution:** keep as-is; the audit reports them so reviewers can sanity-check, but `--report` summary can be filtered with a future `--ignore-comments` flag.

---

## 5. `rtl-base.css` stub strategy

The new `landing/i18n/rtl-base.css` complements (does **not** duplicate) the existing `landing/i18n/rtl.css`:

- **Existing `rtl.css`** = author-side utility classes (`.kix-ms-md`, `.kix-text-start`, etc.) + the existing icon-mirror rules. Loaded **always**, encourages logical authoring for new code.
- **New `rtl-base.css`** = patches for *already-written* directional code. Loaded **conditionally** by `i18next-runtime.js` when `<html dir="rtl">`. Contains:
  - `text-align` re-resets per common landmark (`header`, `nav`, `main`, `aside`, `footer`).
  - `flex-direction: row-reverse` overrides for known layout containers that authored `flex-direction: row` with implicit LTR intent (nav bar, button rows, card grids).
  - Position swaps for common `position: fixed; top: 0; left: 0; right: 0;` pattern → no swap needed (symmetric) but verify per file.
  - **Pre-Phase-3 patches only** for portal.html and play.html specific selectors — to be removed as those files migrate to logical properties.

This is **additive new infra**, not a rewrite of existing CSS.

---

## 6. Test plan

### 6.1 Visual QA page

`landing/i18n/rtl-test.html` provides 12 common UI patterns side-by-side with `dir="ltr"` and `dir="rtl"` toggles. QA engineers + Arabic locale testers will:

1. Open with `?dir=rtl` query param.
2. Verify each pattern flips correctly:
   - Header: logo on **start** edge, nav on **end** edge.
   - Sidebar: snaps to **start** edge.
   - Card grid: reads start-to-end.
   - Form: labels lead, inputs follow (RTL: labels on right, inputs on left).
   - Button group: primary CTA still gets visual priority (RTL: rightmost since reading starts there).
   - Modal: close-X button on **end** edge.
   - Dropdown: opens flush to the parent's **start** edge.
   - Tab bar: tab order matches reading direction.
   - Pagination: "next" arrow points in the reading direction (RTL: ← means next).
   - Currency: `Intl.NumberFormat("ar-EG", {style:"currency", currency:"EGP"})` renders `جنيه ١٢٫٣٤` or similar.
   - Date: `Intl.DateTimeFormat("ar-EG").format(new Date())` renders Hijri or Gregorian per locale conventions.
   - Icon button: chevron mirrors (`.kix-icon-directional`).

3. Mark Expected vs Actual on the visual checklist in the page.

### 6.2 Pseudoloc gate

In CI: render `landing/portal.html` with pseudo-Arabic strings (text expansion 30% + RTL wrapping) and screenshot-diff against baseline. Fail PR if visual diff > 2% in any landmark region.

### 6.3 Real-language smoke

Per-locale smoke tests:
- `ar-EG`: known Arabic Wikipedia sample (Cairo article first paragraph).
- `he-IL`: known Hebrew sample.
- `fa-IR`: known Persian sample (note: Persian uses Arabic script but has its own digits + punctuation).
- `ur-PK`: known Urdu sample (Nastaliq font preference — verify `font-family` fallback).

Sample fixtures live in `landing/i18n/rtl-test.html` as embedded `<bdi>` blocks (added in Phase 3 after Agent 2 lands the i18next integration; this PR just sets up the markup with placeholder text).

### 6.4 Automated audit tests

`tests/test_rtl_audit.py` — 8 tests covering: pattern detection per severity, idempotency on logical CSS, CSS-in-HTML handling (`<style>` blocks + `style=""` attrs), JS `style.left` detection, CSV schema, and a live audit smoke against `landing/`. All passing as of this PR.

---

## 7. CI integration spec (future)

**Not wired yet** — specification only. To be implemented when `postcss-logical` lands.

```yaml
# .github/workflows/rtl-audit.yml (proposal)
name: RTL audit
on: [pull_request]
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - name: Baseline (main)
        run: |
          git checkout origin/main -- landing/
          python3 -m scripts.audit_rtl --csv /tmp/main.csv
          git checkout HEAD -- landing/
      - name: Head
        run: python3 -m scripts.audit_rtl --csv /tmp/head.csv
      - name: Diff P0 only
        run: |
          main_p0=$(awk -F, '$4=="P0"{c++} END{print c+0}' /tmp/main.csv)
          head_p0=$(awk -F, '$4=="P0"{c++} END{print c+0}' /tmp/head.csv)
          if [ "$head_p0" -gt "$main_p0" ]; then
            echo "::error::RTL P0 regression: $main_p0 → $head_p0"
            diff /tmp/main.csv /tmp/head.csv | head -50
            exit 1
          fi
```

**Rule:** PR fails only on **net new P0** vs main. P1 / P2 deltas are non-blocking but reported. This is intentionally conservative — we don't want to block work on the 506-strong P2 backlog while we still author-against-LTR.

---

## 8. Phase 3 implementation checklist

When Phase 3 starts:

- [ ] Install `postcss`, `postcss-cli`, `postcss-logical`, `stylelint`, `stylelint-use-logical` in `landing/package.json`.
- [ ] Add `npm run css:logical` script that runs PostCSS over inline `<style>` blocks (via custom plugin or extract-inline-then-rewrite).
- [ ] Run on `portal.html` first — preview the diff manually before commit.
- [ ] Hand-fix the 21 P0 findings in `portal.html` that PostCSS doesn't auto-migrate (positioning, transforms, JS-set styles).
- [ ] Re-run `python3 -m scripts.audit_rtl --csv ...` and confirm P0 count drops by ≥20.
- [ ] Open `landing/i18n/rtl-test.html?dir=rtl` and walk through the 12 patterns.
- [ ] Repeat for `play.html`, `storefront.html`, `index.html`, `connect.html`.
- [ ] Convert `sdk/kix.js` `.style.left = ...` calls to class-toggle (`el.classList.add('kix-anchor-end')`).
- [ ] Wire `landing/i18n/rtl-base.css` load via `i18next-runtime.js` (coordinate with Agent 2).
- [ ] Add the CI workflow from §7.
- [ ] Native-Arabic reviewer pass on portal + storefront.

---

## 9. Open questions / non-decisions

1. **Persian (`fa-IR`) digits.** Persian conventionally uses Eastern Arabic-Indic digits (`۰۱۲۳۴۵۶۷۸۹`), Arabic uses Western or Arabic-Indic depending on locale. `Intl.NumberFormat` handles this if we set `numberingSystem` — out of scope for this audit, owned by formatter layer.
2. **Urdu font.** Urdu speakers strongly prefer Nastaliq script (`Noto Nastaliq Urdu`) over Naskh. Decide at Phase 3: do we ship the font as a self-hosted webfont, or accept system fallback?
3. **Game board orientation.** `landing/games/` is out of scope, but it's worth declaring policy: **boards stay LTR-coordinate** (chess `a1` is bottom-start, mahjong tile draw order stays); only the surrounding UI chrome mirrors.
4. **PostCSS scope: inline `<style>` blocks.** `postcss-logical` operates on `.css` files. We have ~95% of styles inline in HTML. Options: (a) extract inline blocks → `.css` files → migrate → re-inline (preferred), (b) custom AST plugin to operate in-place. Decide at Phase 3.
