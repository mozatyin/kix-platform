/* ============================================================
 * KiX i18n runtime — vanilla JS i18next bootstrap
 * ------------------------------------------------------------
 * Scope (Phase 1 — Infrastructure only):
 *   - Loads i18next + plugins from CDN (with optional self-host fallback in landing/vendor/).
 *   - Configures BCP 47 locales: en-SG, zh-Hans-SG, en-US, zh-Hans-CN,
 *     id-ID, ms-MY, th-TH, vi-VN (Phase 2 SEA expansion).
 *   - ICU MessageFormat via i18next-icu plugin.
 *   - Detection: ?lang= → localStorage("kix_locale") → navigator.language → default.
 *   - HTTP backend loads landing/i18n/locales/{lng}/{ns}.json.
 *   - Namespaces: common, portal, storefront, play.
 *
 * IMPORTANT: This file ONLY wires the infrastructure. No visible
 * Chinese strings are touched — that's Agent 6's job.
 * ============================================================ */
(function (global) {
  'use strict';

  // ---------- Constants ----------
  const STORAGE_KEY    = 'kix_locale';
  const QUERY_PARAM    = 'lang';
  const DEFAULT_LOCALE = 'en-SG';
  const SUPPORTED      = [
    'en-SG', 'zh-Hans-SG', 'en-US', 'zh-Hans-CN',
    // Phase 2 SEA additions:
    'id-ID', 'ms-MY', 'th-TH', 'vi-VN',
    // Phase 3 RTL launch (Wave 4) — Arabic + Hebrew.
    'ar-EG', 'ar-SA', 'he-IL',
    // R29 Phase 4 — global expansion (East Asia + LatAm + Europe).
    'ja-JP', 'ko-KR', 'es-ES', 'pt-BR', 'fr-FR', 'de-DE',
  ];
  // Public alias — tests + consumers expect SUPPORTED_LOCALES.
  const SUPPORTED_LOCALES = SUPPORTED;
  const NAMESPACES     = ['common', 'portal', 'storefront', 'play', 'portal-sdk', 'index', 'connect', 'pricing', 'mycases', 'legal', 'landing'];
  const RTL_LANGS      = ['ar', 'he', 'fa', 'ur']; // Arabic + Hebrew live in Phase 3; fa/ur are Phase 4.

  // ---------- CDN sources (with self-host fallback discovery) ----------
  const CDN = {
    core:    'https://cdn.jsdelivr.net/npm/i18next@23.11.5/dist/umd/i18next.min.js',
    backend: 'https://cdn.jsdelivr.net/npm/i18next-http-backend@2.5.2/i18nextHttpBackend.min.js',
    detect:  'https://cdn.jsdelivr.net/npm/i18next-browser-languagedetector@7.2.1/i18nextBrowserLanguageDetector.min.js',
    icu:     'https://cdn.jsdelivr.net/npm/i18next-icu@2.3.0/i18nextICU.min.js',
  };
  const LOCAL_FALLBACK = {
    core:    'vendor/i18next.min.js',
    backend: 'vendor/i18next-http-backend.min.js',
    detect:  'vendor/i18next-browser-languagedetector.min.js',
    icu:     'vendor/i18next-icu.min.js',
  };

  // ---------- Helpers ----------
  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = src;
      s.async = false; // preserve order
      s.onload = () => resolve(src);
      s.onerror = () => reject(new Error('Failed to load ' + src));
      document.head.appendChild(s);
    });
  }

  // Try CDN first; on failure, try landing/vendor/ self-host fallback.
  async function loadWithFallback(key) {
    try {
      await loadScript(CDN[key]);
    } catch (_) {
      // Compute path relative to this script's directory.
      const here = scriptBaseUrl();
      const localUrl = here.replace(/i18n\/?$/, '') + LOCAL_FALLBACK[key];
      await loadScript(localUrl);
    }
  }

  // Find the directory this script was served from.
  function scriptBaseUrl() {
    const scripts = document.getElementsByTagName('script');
    for (let i = 0; i < scripts.length; i++) {
      const src = scripts[i].src || '';
      if (src.indexOf('i18next-runtime.js') !== -1) {
        return src.replace(/i18next-runtime\.js.*$/, '');
      }
    }
    return 'i18n/';
  }

  function detectInitialLocale() {
    // 1) ?lang= query param
    const params = new URLSearchParams(window.location.search);
    const qp = params.get(QUERY_PARAM);
    if (qp && SUPPORTED.indexOf(qp) !== -1) return qp;

    // 2) localStorage
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored && SUPPORTED.indexOf(stored) !== -1) return stored;
    } catch (_) {}

    // 3) navigator.language(s) — best-effort match against supported list
    const navLangs = (navigator.languages || [navigator.language || '']).map(String);
    for (const nl of navLangs) {
      const exact = SUPPORTED.find(s => s.toLowerCase() === nl.toLowerCase());
      if (exact) return exact;
      // Loose match by primary subtag (e.g., zh → zh-Hans-SG)
      const primary = nl.split('-')[0].toLowerCase();
      if (primary === 'zh') return 'zh-Hans-SG';
      if (primary === 'en') return 'en-SG';
      if (primary === 'id') return 'id-ID';
      if (primary === 'ms') return 'ms-MY';
      if (primary === 'th') return 'th-TH';
      if (primary === 'vi') return 'vi-VN';
      // Phase 3 RTL: route bare ar/he to the regional default.
      if (primary === 'ar') return 'ar-EG';
      if (primary === 'he') return 'he-IL';
    }

    // 4) default
    return DEFAULT_LOCALE;
  }

  function isRTL(locale) {
    const primary = (locale || '').split('-')[0].toLowerCase();
    return RTL_LANGS.indexOf(primary) !== -1;
  }

  function applyHtmlAttrs(locale) {
    const html = document.documentElement;
    html.setAttribute('lang', locale);
    const rtl = isRTL(locale);
    html.setAttribute('dir', rtl ? 'rtl' : 'ltr');
    // Lazy-load RTL stylesheet stack when entering RTL for the first time.
    // We ship three sheets: rtl.css (Phase 1 utilities, always-on patterns),
    // rtl-base.css (Phase 2 page-level patches) and rtl-overrides.css
    // (Phase 3 P0 audit fixes — float→flex, position swaps, icon mirror).
    if (rtl) {
      ensureRtlSheet('kix-rtl-css',           'rtl.css');
      ensureRtlSheet('kix-rtl-base-css',      'rtl-base.css');
      ensureRtlSheet('kix-rtl-overrides-css', 'rtl-overrides.css');
    }
  }

  function ensureRtlSheet(id, file) {
    if (document.getElementById(id)) return;
    const link = document.createElement('link');
    link.id = id;
    link.rel = 'stylesheet';
    link.href = scriptBaseUrl() + file;
    document.head.appendChild(link);
  }

  // ---------- data-i18n attribute helper ----------
  // Resolve a key against the right namespace. Two legacy shapes exist:
  //   (a) "ns:rest"           — explicit colon (modern landing pages)
  //   (b) "ns.rest..."        — implicit, key stored in NS bundle as full
  //                              string (portal.json keys are LITERALLY
  //                              "portal.register.country", redundant prefix).
  //   (c) "rest..."           — defaults to 'common' namespace.
  // Fix shipped 2026-05-31 — portal.html + storefront.html use (b) shape.
  function resolveI18n(rawKey, opts) {
    if (!rawKey) return rawKey;
    // (a) explicit colon — let i18next handle as-is
    if (rawKey.indexOf(':') !== -1) {
      return global.i18next.t(rawKey, opts);
    }
    // (b) implicit ns-prefix — try lookup in that namespace with full key
    const firstDot = rawKey.indexOf('.');
    if (firstDot !== -1) {
      const ns = rawKey.slice(0, firstDot);
      if (NAMESPACES.indexOf(ns) !== -1) {
        // Pass the FULL raw key (incl. prefix) + explicit ns option
        const merged = Object.assign({}, opts || {}, { ns: ns });
        const t = global.i18next.t(rawKey, merged);
        if (t !== rawKey) return t;
        // Fallback: try with prefix stripped (in case JSON shape changes later)
        const t2 = global.i18next.t(rawKey.slice(firstDot + 1), merged);
        if (t2 !== rawKey.slice(firstDot + 1)) return t2;
        return rawKey;
      }
    }
    // (c) default namespace
    return global.i18next.t(rawKey, opts);
  }

  function applyI18n(root) {
    root = root || document;
    if (!global.i18next || !global.i18next.t) return;
    root.querySelectorAll('[data-i18n]').forEach(el => {
      const rawKey = el.dataset.i18n;
      if (!rawKey) return;
      const text = resolveI18n(rawKey, getDataI18nOptions(el));
      if (text === rawKey) return;
      // Honour data-i18n-html="true" so translations can carry inline markup
      // (e.g. <em>, <strong>). Translators must keep markup minimal.
      if (el.dataset.i18nHtml === 'true') {
        el.innerHTML = text;
      } else {
        el.textContent = text;
      }
    });
    root.querySelectorAll('[data-i18n-attr]').forEach(el => {
      // Format: data-i18n-attr="title:tooltip.help; placeholder:form.email"
      // Supports namespaced keys: data-i18n-attr="content:index:meta.description"
      // (i.e. split only on the first colon to preserve the namespace separator).
      const spec = el.dataset.i18nAttr;
      spec.split(';').forEach(pair => {
        const trimmed = (pair || '').trim();
        const idx = trimmed.indexOf(':');
        if (idx === -1) return;
        const attr = trimmed.slice(0, idx).trim();
        const key  = trimmed.slice(idx + 1).trim();
        if (!attr || !key) return;
        const text = resolveI18n(key, getDataI18nOptions(el));
        if (text !== key) el.setAttribute(attr, text);
      });
    });
  }

  function getDataI18nOptions(el) {
    // Pass any data-i18n-opt-* as interpolation values.
    const opts = {};
    for (const name of Object.keys(el.dataset)) {
      const m = name.match(/^i18nOpt(.+)$/);
      if (m) {
        const k = m[1].charAt(0).toLowerCase() + m[1].slice(1);
        opts[k] = el.dataset[name];
      }
    }
    return opts;
  }

  // ---------- Public API ----------
  async function changeLocale(locale) {
    if (SUPPORTED.indexOf(locale) === -1) return;
    try { localStorage.setItem(STORAGE_KEY, locale); } catch (_) {}
    if (!global.i18next) return;
    await global.i18next.changeLanguage(locale);
    applyHtmlAttrs(locale);
    applyI18n(document);
    document.dispatchEvent(new CustomEvent('kix:locale-changed', { detail: { locale } }));
  }

  function getLocale() {
    return (global.i18next && global.i18next.language) || detectInitialLocale();
  }

  // ---------- Bootstrap ----------
  async function bootstrap() {
    const initial = detectInitialLocale();
    applyHtmlAttrs(initial); // apply early to avoid flicker

    // Sequentially load core libs (order matters).
    await loadWithFallback('core');
    await loadWithFallback('backend');
    await loadWithFallback('detect');
    try { await loadWithFallback('icu'); } catch (_) { /* ICU optional */ }

    const i18nextLib = global.i18next;
    const HttpBackend = global.i18nextHttpBackend || global.i18nextHttpBackend?.default;
    const LanguageDetector = global.i18nextBrowserLanguageDetector || global.i18nextBrowserLanguageDetector?.default;
    const ICU = global.i18nextICU || global.i18nextIcu;

    let chain = i18nextLib;
    if (HttpBackend)      chain = chain.use(HttpBackend.default || HttpBackend);
    if (LanguageDetector) chain = chain.use(LanguageDetector.default || LanguageDetector);
    if (ICU)              chain = chain.use(new (ICU.default || ICU)());

    await chain.init({
      lng: initial,
      fallbackLng: {
        'zh-Hans-SG': ['zh-Hans', 'zh-Hans-CN', 'en-SG'],
        'zh-Hans-CN': ['zh-Hans', 'en-SG'],
        'en-SG':      ['en', 'en-US'],
        'en-US':      ['en', 'en-SG'],
        // Phase 2 SEA — each new locale falls back to en-SG then en-US.
        'id-ID':      ['id', 'en-SG', 'en-US'],
        'ms-MY':      ['ms', 'en-SG', 'en-US'],
        'th-TH':      ['th', 'en-SG', 'en-US'],
        'vi-VN':      ['vi', 'en-SG', 'en-US'],
        // Phase 3 RTL — ar-SA falls back to ar-EG first (shared MSA base),
        // then English. he-IL has no regional sibling — straight to en-SG.
        'ar-EG':      ['ar', 'en-SG', 'en-US'],
        'ar-SA':      ['ar-EG', 'ar', 'en-SG', 'en-US'],
        'he-IL':      ['he', 'en-SG', 'en-US'],
        default:      ['en-SG'],
      },
      supportedLngs: false,
      load: 'currentOnly',
      ns: NAMESPACES,
      defaultNS: 'common',
      fallbackNS: ['common'],
      backend: {
        loadPath: scriptBaseUrl() + 'locales/{{lng}}/{{ns}}.json',
      },
      detection: {
        order: ['querystring', 'localStorage', 'navigator', 'htmlTag'],
        lookupQuerystring: QUERY_PARAM,
        lookupLocalStorage: STORAGE_KEY,
        caches: ['localStorage'],
      },
      interpolation: { escapeValue: false },
      returnEmptyString: false,
      // Catalog keys are FLAT strings with literal dots (e.g.,
      // "portal.register.country" stored as one key, not nested).
      // Without this, i18next treats dot as nested-path separator and lookup fails.
      keySeparator: false,
      nsSeparator: ':',
      debug: false,
    });

    // Persist whichever locale i18next settled on.
    try { localStorage.setItem(STORAGE_KEY, i18nextLib.language); } catch (_) {}
    applyHtmlAttrs(i18nextLib.language);
    applyI18n(document);

    document.dispatchEvent(new CustomEvent('kix:i18n-ready', { detail: { locale: i18nextLib.language } }));
  }

  // ---------- Expose ----------
  global.KixI18n = {
    SUPPORTED,
    SUPPORTED_LOCALES,
    NAMESPACES,
    DEFAULT_LOCALE,
    STORAGE_KEY,
    bootstrap,
    changeLocale,
    getLocale,
    applyI18n,
    isRTL,
  };

  // Auto-bootstrap on DOM ready (unless caller opts out).
  if (!global.__KIX_I18N_MANUAL__) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', bootstrap);
    } else {
      bootstrap();
    }
  }
})(window);
