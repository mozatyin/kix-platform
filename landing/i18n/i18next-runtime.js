/* ============================================================
 * KiX i18n runtime — vanilla JS i18next bootstrap
 * ------------------------------------------------------------
 * Scope (Phase 1 — Infrastructure only):
 *   - Loads i18next + plugins from CDN (with optional self-host fallback in landing/vendor/).
 *   - Configures BCP 47 locales: en-SG, zh-Hans-SG, en-US, zh-Hans-CN.
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
  const SUPPORTED      = ['en-SG', 'zh-Hans-SG', 'en-US', 'zh-Hans-CN'];
  const NAMESPACES     = ['common', 'portal', 'storefront', 'play'];
  const RTL_LANGS      = ['ar', 'he', 'fa', 'ur']; // future Phase 3

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
    html.setAttribute('dir', isRTL(locale) ? 'rtl' : 'ltr');
    // Lazy-load rtl.css when needed
    if (isRTL(locale) && !document.getElementById('kix-rtl-css')) {
      const link = document.createElement('link');
      link.id = 'kix-rtl-css';
      link.rel = 'stylesheet';
      link.href = scriptBaseUrl() + 'rtl.css';
      document.head.appendChild(link);
    }
  }

  // ---------- data-i18n attribute helper ----------
  function applyI18n(root) {
    root = root || document;
    if (!global.i18next || !global.i18next.t) return;
    root.querySelectorAll('[data-i18n]').forEach(el => {
      const key = el.dataset.i18n;
      if (!key) return;
      const text = global.i18next.t(key, getDataI18nOptions(el));
      if (text !== key) el.textContent = text;
    });
    root.querySelectorAll('[data-i18n-attr]').forEach(el => {
      // Format: data-i18n-attr="title:tooltip.help; placeholder:form.email"
      const spec = el.dataset.i18nAttr;
      spec.split(';').forEach(pair => {
        const [attr, key] = pair.split(':').map(s => (s || '').trim());
        if (!attr || !key) return;
        const text = global.i18next.t(key, getDataI18nOptions(el));
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
        default:      ['en-SG'],
      },
      supportedLngs: SUPPORTED,
      nonExplicitSupportedLngs: true,
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
