# KiX Enterprise Design System

Production design system for the KiX merchant portal redesign. Distills the
ad-manager grammar of Google Ads / TikTok Ads / Meta Business Manager / Stripe
Dashboard / Shopify Admin into a single token + component layer that other
agents and pages can adopt without rewriting.

Source research:
- `/Users/mozat/a-docs/ux-trinity-portal-redesign.md` (UX Trinity playbook, § 2 / 4 / 6 / 9)
- `/Users/mozat/a-docs/xiao-wang-baseline-audit.md` (102 baseline complaints)

## Files

| File | Purpose |
|---|---|
| `tokens.css` | Color / type / space / radius / shadow / motion tokens (CSS custom properties). Light + `[data-theme="dark"]` overrides. |
| `components.css` | 25 component classes (buttons, inputs, table, modal, drawer, toast, sidebar, topbar, …). All RTL-ready via CSS logical properties. |
| `ui-kit.js` | Vanilla-JS helpers under `window.KixUI` — modal, drawer, toast, table binder, lightweight SVG chart, date range, command palette (⌘K), theme, sidebar. |
| `icons.svg` | 40 outline icons (Lucide/Heroicons-inspired) as a sprite. |
| `demo.html` | Single-page showcase of every component in the new shell. Reference page for other agents. |

## Quick start

```html
<!doctype html>
<html lang="en" data-theme="light" dir="ltr">
<head>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/landing/design-system/tokens.css">
  <link rel="stylesheet" href="/landing/design-system/components.css">
</head>
<body class="kix-app">
  <!-- your page here -->
  <script src="/landing/design-system/ui-kit.js"></script>
</body>
</html>
```

All classes are namespaced with `kix-*`. The body needs `class="kix-app"` for
typography defaults to take effect — this is intentional so existing
`landing/*.html` pages are not affected.

## Design tokens — cheat sheet

### Color
```
--kix-brand-primary       #00B341   (KiX green, WCAG AA on white)
--kix-brand-secondary     #1A73E8   (enterprise blue, links/info)
--kix-surface-canvas      #FFFFFF
--kix-surface-subdued     #F6F6F7   (panels, alt rows, sidebar bg light)
--kix-sidebar-bg          #1F2129   (dark slate per Google Ads)
--kix-text-strong/default/subdued/disabled
--kix-success / warning / critical / info  (+ matching --bg)
--kix-chart-1…8           8-color color-blind-safe palette
```

### Type
Inter base, JetBrains Mono for codes/IDs. Sizes range from `--kix-fs-caption`
(12 / 16) up through body (14 / 20), h3 (16), h2 (20), h1 (28), display-md (24),
display-lg (30). Headings = 600, body = 400, labels = 500.

### Space (4-pt scale)
`--kix-space-{050|100|150|200|300|400|500|600|800|1000|1200|1600}` → 2 / 4 / 6 / 8 / 12 / 16 / 20 / 24 / 32 / 40 / 48 / 64 px.

### Radius
`--kix-radius-{100|200|300|400|full}` → 2 / 4 / 6 / 8 / 999 px (table cells / buttons / cards / modals / pills).

### Shadow / motion
`--kix-shadow-{sm|md|lg|focus}`, `--kix-motion-{fast|default|slow}` (150ms ease-out is default).

## Component anatomy

### Button
```html
<button class="kix-btn kix-btn--primary">Save</button>
<button class="kix-btn kix-btn--secondary kix-btn--sm">Cancel</button>
<button class="kix-btn kix-btn--tertiary">Learn more</button>
<button class="kix-btn kix-btn--destructive">Delete</button>
```
Variants: `--primary | --secondary | --tertiary | --destructive | --icon`. Sizes: `--sm | (default) | --lg`.

### Input
```html
<div class="kix-field">
  <label class="kix-label">Email</label>
  <input class="kix-input" type="email">
  <div class="kix-helper">We never spam.</div>
</div>
```
Add `.kix-field--error` and `.kix-helper--error` for error states.

### Checkbox / Radio / Toggle
```html
<label class="kix-check"><input type="checkbox"> Enable</label>
<label class="kix-radio"><input type="radio" name="x"> Option A</label>
<label class="kix-toggle"><input type="checkbox"><span class="kix-toggle__slider"></span></label>
```

### Table (with helpers)
```html
<div class="kix-table-wrap">
  <table class="kix-table kix-tabular" id="t">
    <thead>
      <tr>
        <th><input type="checkbox" data-bulk></th>
        <th class="kix-sticky-col" data-sortable>Name</th>
        <th data-sortable>Spend</th>
      </tr>
    </thead>
    <tbody>
      <tr><td><input type="checkbox" data-bulk-row></td><td class="kix-sticky-col">A</td><td data-value="120">120</td></tr>
    </tbody>
  </table>
</div>
<script>
  KixUI.Table.bind(document.getElementById('t'), { sortable: true, paginated: true, bulkActions: true });
</script>
```

### Modal / Drawer / Toast
```js
KixUI.Modal.open({ title: 'Confirm', body: '<p>Sure?</p>', footer: '…' });
KixUI.Drawer.open({ title: 'Edit', body: 'html or DOM node' });
KixUI.Toast.show({ message: 'Saved', variant: 'success' });
```

### Chart
```js
KixUI.Chart.line(el, [{label:'A', values:[1,2,3,4]}]);
KixUI.Chart.bar(el, [{label:'Q1', value: 42}, ...]);
KixUI.Chart.donut(el, [40, 25, 20, 15]);
KixUI.Chart.sparkline(el, [1,2,3,2,5]);
```

### Date range
```js
KixUI.DateRange.bind(containerEl, {
  withComparison: true,
  onChange: ({from, to, compare, preset}) => {}
});
```

### Command palette (⌘K)
```js
KixUI.CommandPalette.setItems([
  { label: 'New campaign', icon: '＋', action: () => {} },
  { label: 'Go to Reports', href: '/reports' }
]);
KixUI.CommandPalette.bind('#cmdk-trigger');   // ⌘K / Ctrl-K also opens it
```

### Shell (sidebar + topbar)
```html
<div class="kix-shell">
  <aside class="kix-sidebar">…</aside>
  <header class="kix-topbar">…</header>
  <main class="kix-shell__main">
    <div class="kix-shell__content">…</div>
  </main>
</div>
```
Add `data-kix-sidebar-toggle` on a button and call `KixUI.Sidebar.bind()` to wire the collapse animation (240px ↔ 72px).

## Theming

```html
<html data-theme="light">  <!-- or "dark" -->
```
`KixUI.Theme.toggle()` flips it and persists to `localStorage['kix-theme']`. Dark mode inverts surfaces and borders; brand colors are preserved.

## RTL support

All component CSS uses CSS logical properties (`margin-inline-*`, `padding-inline-*`, `inset-inline-*`, `border-inline-*`, `text-align: start | end`). Setting `dir="rtl"` on `<html>` or a container flips layout automatically. The toggle component's slider transform is RTL-aware.

## Component cheat sheet (25)

| # | Component | Class / API |
|---|---|---|
| 1  | Button                | `.kix-btn .kix-btn--{primary\|secondary\|tertiary\|destructive\|icon}` `.kix-btn--{sm\|lg}` |
| 2  | Input                 | `.kix-field > .kix-label .kix-input .kix-helper` (+ `.kix-field--error`) |
| 3  | Select                | `.kix-select` |
| 4  | Checkbox              | `.kix-check` |
| 5  | Radio                 | `.kix-radio` |
| 6  | Toggle                | `.kix-toggle > input + .kix-toggle__slider` |
| 7  | Slider                | `.kix-slider` (input type=range) |
| 8  | DateRangePicker       | `KixUI.DateRange.bind(el, {withComparison})` |
| 9  | Table                 | `.kix-table-wrap > .kix-table` + `KixUI.Table.bind` |
| 10 | Tabs (top + side)     | `.kix-tabs[.kix-tabs--side] > .kix-tabs__item` |
| 11 | Card                  | `.kix-card > .kix-card__header / .kix-card__title` |
| 12 | Modal                 | `KixUI.Modal.open({title, body, footer})` |
| 13 | Drawer                | `KixUI.Drawer.open({title, body, footer})` |
| 14 | Toast                 | `KixUI.Toast.show({message, variant})` |
| 15 | Alert                 | `.kix-alert.kix-alert--{info\|success\|warning\|critical}` |
| 16 | Badge                 | `.kix-badge.kix-badge--{brand\|success\|warning\|critical\|info}` |
| 17 | Tag                   | `.kix-tag > .kix-tag__close` |
| 18 | Tooltip               | `.kix-tooltip > .kix-tooltip__content` |
| 19 | Popover               | `.kix-popover` (position via JS / inline style) |
| 20 | Breadcrumb            | `.kix-breadcrumb > a + .kix-breadcrumb__sep` |
| 21 | Pagination            | `.kix-pagination > .kix-pagination__btn` |
| 22 | MetricCard            | `.kix-metric > .kix-metric__{label\|value\|delta\|spark}` |
| 23 | Chart container       | `.kix-chart > .kix-chart__title + .kix-chart__canvas` + `KixUI.Chart.*` |
| 24 | EmptyState            | `.kix-empty > .kix-empty__{art\|title\|body}` |
| 25 | LoadingSkeleton       | `.kix-skeleton.kix-skeleton--{text\|title\|block}` |

Plus chrome: **Sidebar** (`.kix-sidebar`), **TopBar** (`.kix-topbar`), **PageHeader** (`.kix-pageheader`), **CommandPalette** (`KixUI.CommandPalette`).

## Constraints honoured

- Vanilla JS only, no React/Vue.
- Logical properties throughout → RTL-ready day 1.
- Inter via Google Fonts + system fallback.
- Total payload < 150 KB uncompressed.
- Does **not** touch any existing `landing/*.html` file or `landing/i18n/`.

## Demo

Open `landing/design-system/demo.html` in a browser. This is the reference
showcase other agents should match for visual consistency.
