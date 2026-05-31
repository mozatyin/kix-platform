/* ============================================================
 * KiX i18n — Locale Switcher widget
 * ------------------------------------------------------------
 * Floating top-right globe + dropdown. Vanilla JS, no framework.
 * Activates after `kix:i18n-ready` fires from i18next-runtime.js.
 * ============================================================ */
(function () {
  'use strict';

  const LOCALE_LABELS = {
    'en-SG':      { native: 'English (SG)',     short: 'EN-SG' },
    'zh-Hans-SG': { native: '简体中文 (新加坡)',  short: '中-SG' },
    'en-US':      { native: 'English (US)',     short: 'EN-US' },
    'zh-Hans-CN': { native: '简体中文 (中国)',    short: '中-CN' },
    // Phase 2 SEA
    'id-ID':      { native: 'Bahasa Indonesia',  short: 'ID' },
    'ms-MY':      { native: 'Bahasa Melayu',     short: 'MS' },
    'th-TH':      { native: 'ไทย',               short: 'TH' },
    'vi-VN':      { native: 'Tiếng Việt',        short: 'VI' },
    // Phase 3 RTL launch
    'ar-EG':      { native: 'العربية (مصر)',       short: 'AR-EG' },
    'ar-SA':      { native: 'العربية (السعودية)',  short: 'AR-SA' },
    'he-IL':      { native: 'עברית',              short: 'HE' },
  };

  const STYLE = `
  .kix-locale-switcher{
    /* Bottom-right widget pattern (like Intercom). Universal —
     * never overlaps nav, never affected by sticky-header height,
     * works on every viewport + zoom level. */
    position:fixed; bottom:20px; right:20px; z-index:99999;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    font-size:13px; user-select:none;
  }
  @media(max-width:640px){ .kix-locale-switcher{ bottom:16px; right:12px } }
  .kix-locale-switcher *{box-sizing:border-box}
  .kix-ls-button{
    display:inline-flex; align-items:center; gap:6px;
    background:rgba(0,0,0,.78); color:#fff; border:1px solid rgba(255,255,255,.18);
    border-radius:999px; padding:6px 12px; cursor:pointer;
    backdrop-filter:blur(8px); -webkit-backdrop-filter:blur(8px);
    transition:background .15s ease;
  }
  .kix-ls-button:hover{ background:rgba(0,0,0,.92) }
  .kix-ls-globe{ width:16px; height:16px; flex-shrink:0 }
  .kix-ls-caret{ width:10px; height:10px; opacity:.7 }
  .kix-ls-menu{
    /* Pop UPWARDS from button (button is at bottom-right of viewport) */
    position:absolute; bottom:calc(100% + 6px); right:0; top:auto;
    min-width:200px; background:#0b0b0b; color:#fff;
    border:1px solid rgba(255,255,255,.14); border-radius:10px;
    box-shadow:0 12px 32px rgba(0,0,0,.45);
    overflow:hidden; display:none;
    max-height:60vh; overflow-y:auto;
  }
  .kix-ls-menu[data-open="true"]{ display:block }
  .kix-ls-item{
    display:block; width:100%; text-align:start;
    padding:9px 14px; background:transparent; color:#fff;
    border:0; cursor:pointer; font:inherit;
  }
  .kix-ls-item:hover{ background:rgba(255,255,255,.08) }
  .kix-ls-item[aria-current="true"]{
    color:#00FC00; font-weight:600;
  }
  [dir="rtl"] .kix-locale-switcher{ right:auto; left:14px }
  [dir="rtl"] .kix-ls-menu{ right:auto; left:0 }
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
    wrap.className = 'kix-locale-switcher';
    wrap.setAttribute('role', 'group');
    wrap.setAttribute('aria-label', 'Language');

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'kix-ls-button';
    btn.setAttribute('aria-haspopup', 'true');
    btn.setAttribute('aria-expanded', 'false');
    btn.innerHTML = `
      <svg class="kix-ls-globe" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <circle cx="12" cy="12" r="9"></circle>
        <path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"></path>
      </svg>
      <span class="kix-ls-label">EN-SG</span>
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
      item.addEventListener('click', () => {
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
    if (window.KixI18n && window.KixI18n.changeLocale) {
      await window.KixI18n.changeLocale(locale);
    } else {
      try { localStorage.setItem('kix_locale', locale); } catch (_) {}
      const url = new URL(window.location.href);
      url.searchParams.set('lang', locale);
      window.location.href = url.toString();
    }
  }

  function mount() {
    if (document.querySelector('.kix-locale-switcher')) return;
    injectStyle();
    const node = buildSwitcher();
    document.body.appendChild(node);
    if (node.__refresh) node.__refresh();
  }

  document.addEventListener('kix:i18n-ready', mount);
  document.addEventListener('kix:locale-changed', () => {
    const node = document.querySelector('.kix-locale-switcher');
    if (node && node.__refresh) node.__refresh();
  });

  // Fallback: if runtime didn't dispatch ready (e.g., offline CDN), still mount UI.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(mount, 1500));
  } else {
    setTimeout(mount, 1500);
  }
})();
