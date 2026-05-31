/* =============================================================================
 * KiX Enterprise UI Kit — vanilla JS helpers
 * Exposes window.KixUI with: Modal, Drawer, Toast, Table, Chart, DateRange,
 * CommandPalette, Theme. No deps. Designed for progressive enhancement.
 * ========================================================================== */
(function (global) {
  'use strict';

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const el = (tag, attrs, children) => {
    const node = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        if (k === 'class') node.className = attrs[k];
        else if (k === 'style' && typeof attrs[k] === 'object') Object.assign(node.style, attrs[k]);
        else if (k.startsWith('on') && typeof attrs[k] === 'function') node.addEventListener(k.slice(2), attrs[k]);
        else if (k === 'html') node.innerHTML = attrs[k];
        else if (attrs[k] !== undefined && attrs[k] !== null) node.setAttribute(k, attrs[k]);
      }
    }
    if (children) {
      (Array.isArray(children) ? children : [children]).forEach(c => {
        if (c == null) return;
        node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
      });
    }
    return node;
  };

  /* -------- Theme ------------------------------------------------------- */
  const Theme = {
    get() { return document.documentElement.getAttribute('data-theme') || 'light'; },
    set(t) { document.documentElement.setAttribute('data-theme', t); try { localStorage.setItem('kix-theme', t); } catch (_) {} },
    toggle() { this.set(this.get() === 'dark' ? 'light' : 'dark'); },
    init() {
      try {
        const saved = localStorage.getItem('kix-theme');
        if (saved) this.set(saved);
      } catch (_) {}
    }
  };

  /* -------- Modal ------------------------------------------------------- */
  const Modal = {
    open({ title, body, footer, onClose, size }) {
      const closeBtn = el('button', { class: 'kix-topbar__iconbtn', 'aria-label': 'Close' }, '✕');
      const modal = el('div', { class: 'kix-modal', role: 'dialog', 'aria-modal': 'true' }, [
        el('div', { class: 'kix-modal__header' }, [
          el('div', { class: 'kix-h3' }, title || ''),
          closeBtn
        ]),
        el('div', { class: 'kix-modal__body', html: typeof body === 'string' ? body : '' }),
        footer ? el('div', { class: 'kix-modal__footer', html: typeof footer === 'string' ? footer : '' }) : null
      ]);
      if (size === 'lg') modal.style.inlineSize = 'min(800px, calc(100vw - 32px))';
      if (body instanceof Node) { const bodyEl = modal.querySelector('.kix-modal__body'); bodyEl.innerHTML = ''; bodyEl.appendChild(body); }
      if (footer instanceof Node) { const f = modal.querySelector('.kix-modal__footer'); f.innerHTML = ''; f.appendChild(footer); }
      const backdrop = el('div', { class: 'kix-modal-backdrop' }, [modal]);
      const close = () => {
        backdrop.classList.remove('is-open');
        setTimeout(() => backdrop.remove(), 200);
        document.removeEventListener('keydown', esc);
        if (typeof onClose === 'function') onClose();
      };
      const esc = (e) => { if (e.key === 'Escape') close(); };
      closeBtn.addEventListener('click', close);
      backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(); });
      document.addEventListener('keydown', esc);
      document.body.appendChild(backdrop);
      requestAnimationFrame(() => backdrop.classList.add('is-open'));
      return { close, root: backdrop };
    }
  };

  /* -------- Drawer ------------------------------------------------------ */
  const Drawer = {
    open({ title, body, footer, onClose }) {
      const closeBtn = el('button', { class: 'kix-topbar__iconbtn', 'aria-label': 'Close' }, '✕');
      const drawer = el('aside', { class: 'kix-drawer', role: 'dialog' }, [
        el('div', { class: 'kix-drawer__header' }, [el('div', { class: 'kix-h3' }, title || ''), closeBtn]),
        el('div', { class: 'kix-drawer__body', html: typeof body === 'string' ? body : '' }),
        footer ? el('div', { class: 'kix-drawer__footer', html: typeof footer === 'string' ? footer : '' }) : null
      ]);
      if (body instanceof Node) { const b = drawer.querySelector('.kix-drawer__body'); b.innerHTML = ''; b.appendChild(body); }
      const backdrop = el('div', { class: 'kix-drawer-backdrop' });
      const close = () => {
        drawer.classList.remove('is-open'); backdrop.classList.remove('is-open');
        setTimeout(() => { drawer.remove(); backdrop.remove(); }, 250);
        document.removeEventListener('keydown', esc);
        if (typeof onClose === 'function') onClose();
      };
      const esc = (e) => { if (e.key === 'Escape') close(); };
      closeBtn.addEventListener('click', close);
      backdrop.addEventListener('click', close);
      document.addEventListener('keydown', esc);
      document.body.appendChild(backdrop);
      document.body.appendChild(drawer);
      requestAnimationFrame(() => { backdrop.classList.add('is-open'); drawer.classList.add('is-open'); });
      return { close, root: drawer };
    }
  };

  /* -------- Toast ------------------------------------------------------- */
  const Toast = {
    _stack: null,
    _ensureStack() {
      if (!this._stack || !document.body.contains(this._stack)) {
        this._stack = el('div', { class: 'kix-toast-stack', 'aria-live': 'polite' });
        document.body.appendChild(this._stack);
      }
      return this._stack;
    },
    show({ message, variant = 'info', duration = 4000 } = {}) {
      const stack = this._ensureStack();
      const toast = el('div', { class: `kix-toast kix-toast--${variant}`, role: 'status' }, message || '');
      stack.appendChild(toast);
      const t = setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 200); }, duration);
      toast.addEventListener('click', () => { clearTimeout(t); toast.remove(); });
      return toast;
    }
  };

  /* -------- Table binding ---------------------------------------------- */
  const Table = {
    bind(tableEl, opts = {}) {
      if (!tableEl) return;
      const { sortable = true, paginated = false, pageSize = 10, bulkActions = false, onBulkChange } = opts;
      // Sortable
      if (sortable) {
        $$('thead th[data-sortable]', tableEl).forEach((th, colIdx) => {
          th.classList.add('kix-sortable');
          th.setAttribute('aria-sort', 'none');
          th.addEventListener('click', () => {
            const current = th.getAttribute('aria-sort');
            const dir = current === 'ascending' ? 'descending' : 'ascending';
            $$('thead th[data-sortable]', tableEl).forEach(o => o.setAttribute('aria-sort', 'none'));
            th.setAttribute('aria-sort', dir);
            const tbody = tableEl.tBodies[0];
            const rows = Array.from(tbody.rows);
            const idx = th.cellIndex;
            rows.sort((a, b) => {
              const av = (a.cells[idx]?.dataset.value ?? a.cells[idx]?.textContent ?? '').trim();
              const bv = (b.cells[idx]?.dataset.value ?? b.cells[idx]?.textContent ?? '').trim();
              const an = parseFloat(av), bn = parseFloat(bv);
              const numeric = !isNaN(an) && !isNaN(bn);
              const cmp = numeric ? an - bn : av.localeCompare(bv);
              return dir === 'ascending' ? cmp : -cmp;
            });
            rows.forEach(r => tbody.appendChild(r));
          });
        });
      }
      // Bulk
      if (bulkActions) {
        const head = $('thead input[type="checkbox"][data-bulk]', tableEl);
        const rowChecks = () => $$('tbody input[type="checkbox"][data-bulk-row]', tableEl);
        if (head) {
          head.addEventListener('change', () => {
            rowChecks().forEach(c => { c.checked = head.checked; });
            if (onBulkChange) onBulkChange(rowChecks().filter(c => c.checked).length);
          });
        }
        rowChecks().forEach(c => c.addEventListener('change', () => {
          const checked = rowChecks().filter(x => x.checked).length;
          if (head) head.checked = checked === rowChecks().length && checked > 0;
          if (onBulkChange) onBulkChange(checked);
        }));
      }
      // Pagination (basic)
      if (paginated) {
        const tbody = tableEl.tBodies[0];
        const rows = Array.from(tbody.rows);
        const total = rows.length;
        const pages = Math.max(1, Math.ceil(total / pageSize));
        let page = 1;
        const render = () => {
          rows.forEach((r, i) => { r.style.display = (i >= (page - 1) * pageSize && i < page * pageSize) ? '' : 'none'; });
          pageInfo.textContent = `${page} / ${pages}`;
          prev.disabled = page <= 1; next.disabled = page >= pages;
        };
        const prev = el('button', { class: 'kix-pagination__btn', onclick: () => { if (page > 1) { page--; render(); } } }, '‹');
        const next = el('button', { class: 'kix-pagination__btn', onclick: () => { if (page < pages) { page++; render(); } } }, '›');
        const pageInfo = el('span', { class: 'kix-muted' }, '');
        const pager = el('div', { class: 'kix-pagination', style: { padding: '12px 16px', justifyContent: 'flex-end' } }, [prev, pageInfo, next]);
        tableEl.parentNode.insertBefore(pager, tableEl.nextSibling);
        render();
      }
    }
  };

  /* -------- Chart (lightweight SVG) ------------------------------------ */
  const palette = ['#1A73E8', '#00B341', '#F4A300', '#D72C0D', '#6F42C1', '#008080', '#FF6B35', '#C2185B'];
  function svg(tag, attrs, children) {
    const e = document.createElementNS('http://www.w3.org/2000/svg', tag);
    if (attrs) for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (children) (Array.isArray(children) ? children : [children]).forEach(c => c && e.appendChild(c));
    return e;
  }
  const Chart = {
    _container(host) {
      if (host instanceof HTMLCanvasElement) {
        const parent = host.parentNode;
        const ns = svg('svg', { xmlns: 'http://www.w3.org/2000/svg' });
        parent.replaceChild(ns, host);
        return ns;
      }
      while (host.firstChild) host.removeChild(host.firstChild);
      const ns = svg('svg', { xmlns: 'http://www.w3.org/2000/svg' });
      host.appendChild(ns);
      return ns;
    },
    line(host, data, opts = {}) {
      // data: [{label, values:[n,n,n]}], or [n,n,n]
      const root = this._container(host);
      const W = opts.width || host.clientWidth || 480;
      const H = opts.height || 200;
      const pad = { t: 16, r: 16, b: 24, l: 32 };
      root.setAttribute('viewBox', `0 0 ${W} ${H}`);
      root.setAttribute('width', '100%'); root.setAttribute('height', H);
      const series = Array.isArray(data[0]) || typeof data[0] === 'number'
        ? [{ label: 'A', values: Array.isArray(data[0]) ? data[0] : data }]
        : data;
      const flat = series.flatMap(s => s.values);
      const max = Math.max(...flat, 1), min = Math.min(...flat, 0);
      const n = series[0].values.length;
      const xStep = (W - pad.l - pad.r) / Math.max(1, n - 1);
      // axes
      root.appendChild(svg('line', { x1: pad.l, y1: H - pad.b, x2: W - pad.r, y2: H - pad.b, stroke: '#E1E3E5' }));
      root.appendChild(svg('line', { x1: pad.l, y1: pad.t, x2: pad.l, y2: H - pad.b, stroke: '#E1E3E5' }));
      series.forEach((s, si) => {
        const pts = s.values.map((v, i) => {
          const x = pad.l + i * xStep;
          const y = H - pad.b - ((v - min) / (max - min || 1)) * (H - pad.t - pad.b);
          return `${x},${y}`;
        }).join(' ');
        root.appendChild(svg('polyline', { fill: 'none', stroke: palette[si % palette.length], 'stroke-width': 2, points: pts }));
      });
      return root;
    },
    bar(host, data, opts = {}) {
      const root = this._container(host);
      const W = opts.width || host.clientWidth || 480;
      const H = opts.height || 200;
      const pad = { t: 16, r: 16, b: 32, l: 32 };
      root.setAttribute('viewBox', `0 0 ${W} ${H}`);
      root.setAttribute('width', '100%'); root.setAttribute('height', H);
      const values = (data || []).map(d => typeof d === 'number' ? d : d.value);
      const labels = (data || []).map((d, i) => typeof d === 'number' ? String(i + 1) : (d.label || ''));
      const max = Math.max(...values, 1);
      const bw = (W - pad.l - pad.r) / values.length * 0.7;
      const gap = (W - pad.l - pad.r) / values.length * 0.3;
      root.appendChild(svg('line', { x1: pad.l, y1: H - pad.b, x2: W - pad.r, y2: H - pad.b, stroke: '#E1E3E5' }));
      values.forEach((v, i) => {
        const h = ((v / max) * (H - pad.t - pad.b));
        const x = pad.l + i * (bw + gap) + gap / 2;
        const y = H - pad.b - h;
        root.appendChild(svg('rect', { x, y, width: bw, height: h, fill: palette[i % palette.length], rx: 2 }));
        const tx = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        tx.setAttribute('x', x + bw / 2); tx.setAttribute('y', H - pad.b + 14);
        tx.setAttribute('text-anchor', 'middle'); tx.setAttribute('font-size', '11');
        tx.setAttribute('fill', '#6B7280'); tx.textContent = labels[i];
        root.appendChild(tx);
      });
      return root;
    },
    donut(host, data, opts = {}) {
      const root = this._container(host);
      const W = opts.width || host.clientWidth || 200;
      const H = opts.height || 200;
      const cx = W / 2, cy = H / 2;
      const r = Math.min(W, H) / 2 - 10;
      const inner = r * 0.6;
      root.setAttribute('viewBox', `0 0 ${W} ${H}`);
      root.setAttribute('width', W); root.setAttribute('height', H);
      const values = (data || []).map(d => typeof d === 'number' ? d : d.value);
      const total = values.reduce((a, b) => a + b, 0) || 1;
      let acc = -Math.PI / 2;
      values.forEach((v, i) => {
        const ang = (v / total) * Math.PI * 2;
        const a1 = acc, a2 = acc + ang;
        const big = ang > Math.PI ? 1 : 0;
        const x1 = cx + Math.cos(a1) * r, y1 = cy + Math.sin(a1) * r;
        const x2 = cx + Math.cos(a2) * r, y2 = cy + Math.sin(a2) * r;
        const xi1 = cx + Math.cos(a2) * inner, yi1 = cy + Math.sin(a2) * inner;
        const xi2 = cx + Math.cos(a1) * inner, yi2 = cy + Math.sin(a1) * inner;
        const d = `M${x1},${y1} A${r},${r} 0 ${big} 1 ${x2},${y2} L${xi1},${yi1} A${inner},${inner} 0 ${big} 0 ${xi2},${yi2} Z`;
        root.appendChild(svg('path', { d, fill: palette[i % palette.length] }));
        acc = a2;
      });
      return root;
    },
    sparkline(host, values, opts = {}) {
      const root = this._container(host);
      const W = opts.width || host.clientWidth || 120;
      const H = opts.height || 32;
      root.setAttribute('viewBox', `0 0 ${W} ${H}`);
      root.setAttribute('width', '100%'); root.setAttribute('height', H);
      const max = Math.max(...values, 1), min = Math.min(...values, 0);
      const step = W / Math.max(1, values.length - 1);
      const pts = values.map((v, i) => `${i * step},${H - ((v - min) / (max - min || 1)) * H}`).join(' ');
      root.appendChild(svg('polyline', { fill: 'none', stroke: opts.color || palette[1], 'stroke-width': 1.5, points: pts }));
      return root;
    }
  };

  /* -------- DateRange (lightweight) ------------------------------------ */
  const DateRange = {
    bind(containerEl, { onChange, withComparison = true, presets } = {}) {
      if (!containerEl) return;
      const _presets = presets || [
        { label: 'Today',       days: 0 },
        { label: 'Last 7 days', days: 7 },
        { label: 'Last 30 days',days: 30 },
        { label: 'Last 90 days',days: 90 }
      ];
      const fmt = d => d.toISOString().slice(0, 10);
      let state = { preset: _presets[1], compare: withComparison };
      const label = el('span', {}, '');
      const cmp = withComparison ? el('span', { class: 'kix-daterange__compare' }, 'vs previous') : null;
      const trigger = el('button', { class: 'kix-daterange', type: 'button' }, [
        el('span', { html: '📅' }), label, cmp
      ].filter(Boolean));
      containerEl.appendChild(trigger);
      const update = () => {
        const to = new Date(); const from = new Date(); from.setDate(to.getDate() - state.preset.days);
        label.textContent = `${state.preset.label} (${fmt(from)} → ${fmt(to)})`;
        if (typeof onChange === 'function') onChange({ from: fmt(from), to: fmt(to), compare: state.compare, preset: state.preset.label });
      };
      trigger.addEventListener('click', () => {
        const menu = el('div', { class: 'kix-popover' });
        _presets.forEach(p => {
          const it = el('div', { class: 'kix-cmdk__item', onclick: () => { state.preset = p; update(); menu.remove(); } }, p.label);
          menu.appendChild(it);
        });
        if (withComparison) {
          menu.appendChild(el('hr', { class: 'kix-divider' }));
          const lbl = el('label', { class: 'kix-check', style: { padding: '8px 12px' } }, [
            (() => { const c = el('input', { type: 'checkbox' }); c.checked = state.compare; c.addEventListener('change', () => { state.compare = c.checked; update(); }); return c; })(),
            el('span', {}, 'Compare to previous')
          ]);
          menu.appendChild(lbl);
        }
        const rect = trigger.getBoundingClientRect();
        menu.style.position = 'fixed';
        menu.style.insetBlockStart = (rect.bottom + 4) + 'px';
        menu.style.insetInlineStart = rect.left + 'px';
        document.body.appendChild(menu);
        const dismiss = (e) => { if (!menu.contains(e.target) && e.target !== trigger) { menu.remove(); document.removeEventListener('click', dismiss, true); } };
        setTimeout(() => document.addEventListener('click', dismiss, true), 0);
      });
      update();
      return { setPreset: (p) => { state.preset = p; update(); } };
    }
  };

  /* -------- CommandPalette (⌘K) ---------------------------------------- */
  const CommandPalette = {
    _items: [],
    register(items) { this._items = (this._items || []).concat(items || []); },
    setItems(items) { this._items = items || []; },
    bind(triggerSelector) {
      const open = () => this.open();
      if (triggerSelector) $$(triggerSelector).forEach(t => t.addEventListener('click', open));
      document.addEventListener('keydown', (e) => {
        const isK = (e.key === 'k' || e.key === 'K');
        if (isK && (e.metaKey || e.ctrlKey)) { e.preventDefault(); open(); }
      });
    },
    open() {
      const input = el('input', { class: 'kix-cmdk__input', placeholder: 'Search pages, actions, settings…', autofocus: 'true' });
      const list = el('div', { class: 'kix-cmdk__list' });
      const cmdk = el('div', { class: 'kix-cmdk', role: 'dialog' }, [input, list]);
      const backdrop = el('div', { class: 'kix-cmdk-backdrop' }, [cmdk]);
      let focusIdx = 0;
      const render = (q) => {
        const ql = (q || '').toLowerCase();
        const matched = this._items.filter(it => !ql || (it.label || '').toLowerCase().includes(ql) || (it.keywords || '').toLowerCase().includes(ql));
        list.innerHTML = '';
        matched.slice(0, 50).forEach((it, i) => {
          const row = el('div', { class: 'kix-cmdk__item' + (i === focusIdx ? ' is-focused' : ''), onclick: () => { close(); if (typeof it.action === 'function') it.action(); else if (it.href) location.href = it.href; } }, [
            el('span', { html: it.icon || '🔎' }),
            el('span', { class: 'kix-grow' }, it.label || ''),
            it.shortcut ? el('span', { class: 'kix-muted kix-code' }, it.shortcut) : null
          ].filter(Boolean));
          list.appendChild(row);
        });
        if (!matched.length) list.appendChild(el('div', { class: 'kix-cmdk__item kix-muted' }, 'No results'));
      };
      const close = () => { backdrop.classList.remove('is-open'); setTimeout(() => backdrop.remove(), 200); document.removeEventListener('keydown', nav); };
      const nav = (e) => {
        if (e.key === 'Escape') return close();
        if (e.key === 'ArrowDown') { focusIdx++; render(input.value); }
        if (e.key === 'ArrowUp') { focusIdx = Math.max(0, focusIdx - 1); render(input.value); }
        if (e.key === 'Enter') { const target = list.querySelector('.kix-cmdk__item.is-focused'); if (target) target.click(); }
      };
      input.addEventListener('input', () => { focusIdx = 0; render(input.value); });
      backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(); });
      document.addEventListener('keydown', nav);
      document.body.appendChild(backdrop);
      requestAnimationFrame(() => { backdrop.classList.add('is-open'); input.focus(); });
      render('');
    }
  };

  /* -------- Sidebar toggle helper -------------------------------------- */
  const Sidebar = {
    bind({ sidebarSelector = '.kix-sidebar', shellSelector = '.kix-shell', toggleSelector = '[data-kix-sidebar-toggle]' } = {}) {
      const sidebar = $(sidebarSelector);
      const shell = $(shellSelector);
      $$(toggleSelector).forEach(t => t.addEventListener('click', () => {
        if (sidebar) sidebar.classList.toggle('is-collapsed');
        if (shell) shell.classList.toggle('is-collapsed');
      }));
    }
  };

  /* -------- Public API ------------------------------------------------- */
  const KixUI = { Theme, Modal, Drawer, Toast, Table, Chart, DateRange, CommandPalette, Sidebar, $, $$, el };
  global.KixUI = KixUI;

  // Auto-init theme from localStorage
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => Theme.init());
  } else { Theme.init(); }
})(window);
