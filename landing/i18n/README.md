# KiX Landing i18n — Frontend Infrastructure

**Phase 1 status:** scaffolding only. No visible Chinese strings have been replaced yet — that work happens in a later phase. This directory contains the runtime, catalogs, switcher widget, and RTL preparation that all future translation work plugs into.

## Files

| Path | Purpose |
|---|---|
| `i18next-runtime.js` | Vanilla JS bootstrap: loads i18next + plugins (CDN with `vendor/` fallback), configures detection / backend / namespaces, exposes `window.KixI18n`. |
| `locale-switcher.js` | Floating top-right globe + dropdown. Auto-mounts after `kix:i18n-ready`. |
| `rtl.css` | CSS logical-properties helpers for the future Arabic / Hebrew milestone. Loaded only when `<html dir="rtl">`. |
| `locales/{lng}/{ns}.json` | Translation catalogs. 4 locales × 4 namespaces = 16 files. ICU MessageFormat. |
| `test_i18n.html` | Visual smoke test. Open in a browser; switch locales to confirm wiring. |

## Supported locales

- `en-SG` — Singapore English (default)
- `zh-Hans-SG` — Singapore Simplified Chinese
- `en-US` — US English
- `zh-Hans-CN` — China Simplified Chinese

Detection order: `?lang=` query string → `localStorage["kix_locale"]` → `navigator.language` → `en-SG`.

## Namespaces

- `common` — login, nav, errors, generic UI
- `portal` — brand portal (admin)
- `storefront` — merchant storefront
- `play` — player experience

## How to add a new translatable string — 5 steps

1. **Pick a namespace + key.** Use dot-namespacing: `portal.dashboard.title`, `play.cta.start`. Keys must be stable; do **not** translate keys.
2. **Add the source string to `locales/en-SG/{ns}.json`.** This is the canonical source of truth.
   ```json
   { "portal.dashboard.title": "Dashboard" }
   ```
3. **Mirror the key into the other 3 locale JSONs.** Use placeholder `"TODO"` if you don't have the translation yet — i18next will fall back per the `fallbackLng` map until filled.
4. **Mark the DOM node.** Two ways:
   - Text content: `<h1 data-i18n="portal.dashboard.title">Dashboard</h1>`
   - Attribute: `<input data-i18n-attr="placeholder:form.email.placeholder; aria-label:form.email.label">`
   - Interpolation: `<span data-i18n="welcome.message" data-i18n-opt-name="Alice"></span>`
5. **Verify.** Open the page, switch locales via the top-right widget, and confirm the string flips. For dynamically inserted DOM, call `KixI18n.applyI18n(rootElement)` after insertion.

## ICU MessageFormat

Use ICU for plurals / gender / select — *never* string concatenation.

```json
{
  "messages.count": "{count, plural, =0 {No messages} one {1 message} other {# messages}}",
  "greeting":       "{gender, select, female {欢迎她} male {欢迎他} other {欢迎}}"
}
```

```js
i18next.t('messages.count', { count: 5 });   // "5 messages"
```

## JavaScript API (window.KixI18n)

```js
KixI18n.getLocale()             // current BCP 47 locale
KixI18n.changeLocale('zh-Hans-SG')
KixI18n.applyI18n(rootElement)  // re-translate after DOM injection
KixI18n.SUPPORTED               // ["en-SG", "zh-Hans-SG", "en-US", "zh-Hans-CN"]
KixI18n.NAMESPACES              // ["common", "portal", "storefront", "play"]
```

### Events

- `kix:i18n-ready` — fires once after initial bootstrap completes.
- `kix:locale-changed` — fires on every `changeLocale()` call.

## RTL (Phase 3 — Arabic / Hebrew / Farsi / Urdu)

`rtl.css` is loaded automatically when an RTL locale is active. To prepare new components:

- Use **logical properties**: `margin-inline-start`, `padding-block-end`, `text-align: start`.
- Use the `.kix-icon-directional` class on arrows / chevrons that should mirror.
- Wrap embedded English IDs / numbers in Arabic prose with `<bdi>…</bdi>`.

## Don'ts

- Don't hard-code strings inside JS. Use `i18next.t()` or `data-i18n=`.
- Don't translate keys. Keys are identifiers, not text.
- Don't use `+` to splice translations. Use ICU's `{var}` interpolation.
- Don't replace `"Login / 登录"` mock-ups yet — that's a deliberate Phase 2 task.
