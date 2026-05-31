/* ============================================================
 * KiX i18n — Locale Switcher widget (v2: inline-in-nav primary)
 * ------------------------------------------------------------
 * Activates on `kix:i18n-ready` from i18next-runtime.js.
 *
 * MOUNT MODES (auto-detected):
 *   1. Inline-in-nav (PREFERRED): pages with <div class="kix-lang-slot">
 *      somewhere in their nav get an inline button mounted there.
 *      Never floats. Never overlaps content. The proper-design path.
 *   2. Fallback floating bottom-right: ONLY if no .kix-lang-slot found.
 *      Last-resort safety net so the user can always change language.
 *
 * Inline mounted button matches the host page's nav color scheme by
 * default (light bg, dark text). Override with .kix-lang-slot[data-theme="dark"].
 * ============================================================ */
(function () {
  'use strict';

  const LOCALE_LABELS = {
    'en-SG':      { native: 'English (SG)',     short: 'EN' },
    'zh-Hans-SG': { native: '简体中文 (新加坡)',  short: '中' },
    'en-US':      { native: 'English (US)',     short: 'EN-US' },
    'zh-Hans-CN': { native: '简体中文 (中国)',    short: '中-CN' },
    'id-ID':      { native: 'Bahasa Indonesia',  short: 'ID' },
    'ms-MY':      { native: 'Bahasa Melayu',     short: 'BM' },
    'th-TH':      { native: 'ไทย',               short: 'TH' },
    'vi-VN':      { native: 'Tiếng Việt',        short: 'VI' },
    'ar-EG':      { native: 'العربية (مصر)',       short: 'AR' },
    'ar-SA':      { native: 'العربية (السعودية)',  short: 'AR-SA' },
    'he-IL':      { native: 'עברית',              short: 'HE' },
  };

  const STYLE = `
  /* Inline-in-nav button — matches typical light-themed landing nav */
  .kix-ls-inline{
    display:inline-flex; align-items:center; gap:6px;
    background:transparent; color:inherit;
    border:1px solid #E2E8F0; border-radius:8px;
    padding:6px 12px; font:inherit; font-size:13px; font-weight:500;
    cursor:pointer; transition:background .15s, border-color .15s;
    position:relative;
  }
  .kix-ls-inline:hover{ background:#F1F5F9; border-color:#CBD5E1 }
  .kix-ls-inline:focus-visible{ outline:2px solid #00B341; outline-offset:2px }
  .kix-ls-inline .kix-ls-globe{ width:14px; height:14px; flex-shrink:0; opacity:.7 }
  .kix-ls-inline .kix-ls-caret{ width:10px; height:10px; opacity:.6 }
  .kix-ls-inline .kix-ls-label{ font-variant-numeric:tabular-nums }

  /* Dark theme variant — opt in via data-theme="dark" on .kix-lang-slot */
  .kix-lang-slot[data-theme="dark"] .kix-ls-inline{
    background:rgba(255,255,255,.08); color:#fff;
    border-color:rgba(255,255,255,.18);
  }
  .kix-lang-slot[data-theme="dark"] .kix-ls-inline:hover{
    background:rgba(255,255,255,.15);
  }

  /* Dropdown menu — pops downward from inline button */
  .kix-ls-menu{
    position:absolute; top:calc(100% + 6px); right:0;
    min-width:200px; background:#0F172A; color:#fff;
    border:1px solid rgba(255,255,255,.14); border-radius:10px;
    box-shadow:0 12px 32px rgba(15,23,42,.25);
    overflow:hidden; display:none;
    max-height:60vh; overflow-y:auto;
    z-index:99999;
  }
  .kix-ls-menu[data-open="true"]{ display:block }
  .kix-ls-item{
    display:block; width:100%; text-align:start;
    padding:9px 14px; background:transparent; color:#fff;
    border:0; cursor:pointer; font:inherit; font-size:13.5px;
  }
  .kix-ls-item:hover{ background:rgba(255,255,255,.08) }
  .kix-ls-item[aria-current="true"]{ color:#34D399; font-weight:600 }

  /* RTL */
  [dir="rtl"] .kix-ls-menu{ right:auto; left:0 }

  /* FALLBACK: floating bottom-right, only if no .kix-lang-slot in page */
  .kix-ls-fallback{
    position:fixed; bottom:20px; right:20px; z-index:99999;
    font-family:-apple-system,BlinkMacSystemFont,sans-serif;
  }
  .kix-ls-fallback .kix-ls-inline{
    background:rgba(15,23,42,.92); color:#fff; border-color:rgba(255,255,255,.18);
    box-shadow:0 4px 12px rgba(15,23,42,.25);
  }
  .kix-ls-fallback .kix-ls-inline:hover{ background:rgba(15,23,42,1) }
  .kix-ls-fallback .kix-ls-menu{ bottom:calc(100% + 6px); top:auto }
  @media(max-width:640px){ .kix-ls-fallback{ bottom:16px; right:12px } }
  `;

  function injectStyle() {
    if (document.getElementById('kix-ls-style')) return;
    const s = document.createElement('style');
    s.id = 'kix-ls-style';
    s.textContent = STYLE;
    document.head.appendChild(s);
  }

  function buildSwitcher() {
    const wrap = document.createElement('div');
    wrap.style.position = 'relative';
    wrap.style.display = 'inline-block';

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'kix-ls-inline';
    btn.setAttribute('aria-haspopup', 'true');
    btn.setAttribute('aria-expanded', 'false');
    btn.setAttribute('aria-label', 'Change language');
    btn.innerHTML = `
      <svg class="kix-ls-globe" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <circle cx="12" cy="12" r="9"></circle>
        <path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"></path>
      </svg>
      <span class="kix-ls-label">EN</span>
      <svg class="kix-ls-caret" viewBox="0 0 12 12" aria-hidden="true">
        <path d="M2 4l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>`;

    const menu = document.createElement('div');
    menu.className = 'kix-ls-menu';
    menu.setAttribute('role', 'listbox');

    const list = (window.KixI18n && window.KixI18n.SUPPORTED) || Object.keys(LOCALE_LABELS);
    list.forEach(loc => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'kix-ls-item';
      item.setAttribute('role', 'option');
      item.dataset.locale = loc;
      const lbl = LOCALE_LABELS[loc] || { native: loc, short: loc };
      item.textContent = lbl.native;
      item.addEventListener('click', e => {
        e.stopPropagation();
        chooseLocale(loc);
        closeMenu();
      });
      menu.appendChild(item);
    });

    btn.addEventListener('click', e => {
      e.stopPropagation();
      const open = menu.getAttribute('data-open') === 'true';
      if (open) closeMenu(); else openMenu();
    });
    document.addEventListener('click', closeMenu);
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') closeMenu();
    });

    wrap.appendChild(btn);
    wrap.appendChild(menu);

    function openMenu() {
      menu.setAttribute('data-open', 'true');
      btn.setAttribute('aria-expanded', 'true');
      refreshActive();
    }
    function closeMenu() {
      menu.setAttribute('data-open', 'false');
      btn.setAttribute('aria-expanded', 'false');
    }
    function refreshActive() {
      const cur = (window.KixI18n && window.KixI18n.getLocale()) || '';
      menu.querySelectorAll('.kix-ls-item').forEach(it => {
        it.setAttribute('aria-current', it.dataset.locale === cur ? 'true' : 'false');
      });
      const lbl = LOCALE_LABELS[cur];
      if (lbl) btn.querySelector('.kix-ls-label').textContent = lbl.short;
    }

    wrap.__refresh = refreshActive;
    return wrap;
  }

  async function chooseLocale(locale) {
    // Persist BEFORE redirecting so cross-page navigation keeps the locale.
    try { localStorage.setItem('kix_locale', locale); } catch (_) {}
    if (window.KixI18n && window.KixI18n.changeLocale) {
      await window.KixI18n.changeLocale(locale);
    } else {
      const url = new URL(window.location.href);
      url.searchParams.set('lang', locale);
      window.location.href = url.toString();
    }
  }

  function mount() {
    if (document.querySelector('.kix-ls-inline')) return;  // already mounted
    injectStyle();
    const node = buildSwitcher();

    // PREFERRED: mount inline into all .kix-lang-slot placeholders
    const slots = document.querySelectorAll('.kix-lang-slot');
    if (slots.length > 0) {
      slots.forEach((slot, idx) => {
        const widget = idx === 0 ? node : buildSwitcher();
        slot.appendChild(widget);
        if (widget.__refresh) widget.__refresh();
      });
      return;
    }

    // FALLBACK: floating bottom-right (only if no slot anywhere)
    const fb = document.createElement('div');
    fb.className = 'kix-ls-fallback';
    fb.appendChild(node);
    document.body.appendChild(fb);
    if (node.__refresh) node.__refresh();
  }

  document.addEventListener('kix:i18n-ready', mount);
  document.addEventListener('kix:locale-changed', () => {
    document.querySelectorAll('.kix-ls-inline').forEach(btn => {
      const wrap = btn.parentElement;
      if (wrap && wrap.__refresh) wrap.__refresh();
    });
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(mount, 1500));
  } else {
    setTimeout(mount, 1500);
  }
})();
