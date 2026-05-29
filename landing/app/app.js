/* ============================================================
   KiX App — Single-bundle SPA (Vanilla JS)
   - Onboarding: device fingerprint → KiX ID (kid)
   - Sessions, QR scan binding, push inbox, profile, rewards
   - The KiX ID + Push Engine APIs are being built in parallel —
     this client tolerates schema drift (e.g. {pushes: [...]} or
     bare array; multiple voucher fields; lookup endpoint missing).
   ============================================================ */
(function () {
  'use strict';

  // ── Config ──────────────────────────────────────────────────────────
  const API = window.KIX_API_BASE || (location.origin + '/api/v1');
  const KID_KEY = '_kix_kid';
  const SESSION_KEY = '_kix_session';
  const FP_KEY = '_kix_fp';

  // ── State ───────────────────────────────────────────────────────────
  const state = {
    kid: localStorage.getItem(KID_KEY) || null,
    session: localStorage.getItem(SESSION_KEY) || null,
    profile: null,
    inbox: [],
    rewards: { points: 0, vouchers: [], badges: [] },
    currentView: 'home',
  };

  // ── Device Fingerprint ──────────────────────────────────────────────
  function deviceFingerprint() {
    let fp = localStorage.getItem(FP_KEY);
    if (!fp) {
      const sig = [
        navigator.userAgent,
        screen.width + 'x' + screen.height,
        (Intl.DateTimeFormat().resolvedOptions().timeZone) || 'UTC',
        navigator.language || 'en',
        (navigator.hardwareConcurrency || 0),
      ].join('|');
      let h = 0;
      for (let i = 0; i < sig.length; i++) h = ((h << 5) - h) + sig.charCodeAt(i) | 0;
      fp = 'fp_' + Math.abs(h).toString(36) + '_' + Date.now().toString(36).slice(-4);
      localStorage.setItem(FP_KEY, fp);
    }
    return fp;
  }
  const FP = deviceFingerprint();

  // ── API Helpers ─────────────────────────────────────────────────────
  async function api(path, opts) {
    opts = opts || {};
    const res = await fetch(API + path, {
      method: opts.method || 'GET',
      headers: Object.assign(
        { 'Content-Type': 'application/json' },
        state.session ? { 'X-KiX-Session': state.session } : {},
        opts.headers || {}
      ),
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      const err = new Error(res.status + ' ' + (text || res.statusText));
      err.status = res.status;
      throw err;
    }
    // Some endpoints return 204
    if (res.status === 204) return null;
    return res.json().catch(() => ({}));
  }

  // Best-effort: never throw — return null on failure.
  async function apiSafe(path, opts) {
    try { return await api(path, opts); } catch (e) {
      console.warn('[kix] api soft-fail', path, e.message);
      return null;
    }
  }

  // ── UI Utilities ────────────────────────────────────────────────────
  function $(sel) { return document.querySelector(sel); }
  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function toast(msg, ms) {
    const el = $('#kix-toast');
    if (!el) { console.log('[toast]', msg); return; }
    el.textContent = msg;
    el.classList.remove('hidden');
    requestAnimationFrame(() => el.classList.add('show'));
    clearTimeout(toast._t);
    toast._t = setTimeout(() => {
      el.classList.remove('show');
      setTimeout(() => el.classList.add('hidden'), 250);
    }, ms || 2200);
  }
  function fmtDate(ts) {
    if (!ts) return '';
    const ms = ts > 1e12 ? ts : ts * 1000;
    return new Date(ms).toLocaleString();
  }

  // ── Onboarding ──────────────────────────────────────────────────────
  async function ensureKid() {
    if (state.kid) return state.kid;

    // Try lookup by fingerprint first (no-op if endpoint absent)
    const lookup = await apiSafe('/kix-id/lookup', {
      method: 'POST',
      body: { device_fingerprint: FP },
    });
    if (lookup && (lookup.found || lookup.kid)) {
      state.kid = lookup.kid;
      localStorage.setItem(KID_KEY, state.kid);
      return state.kid;
    }

    // Register
    const reg = await apiSafe('/kix-id/register', {
      method: 'POST',
      body: {
        device_fingerprint: FP,
        primary_language: navigator.language || 'zh-CN',
        country: 'CN',
      },
    });
    if (reg && reg.kid) {
      state.kid = reg.kid;
      localStorage.setItem(KID_KEY, state.kid);
      return state.kid;
    }

    // Fallback: generate local id so the app remains usable offline-ish
    state.kid = 'kid_local_' + FP.slice(3);
    localStorage.setItem(KID_KEY, state.kid);
    return state.kid;
  }

  // ── Session ─────────────────────────────────────────────────────────
  async function startSession() {
    const sess = await apiSafe('/kix-id/session/create', {
      method: 'POST',
      body: { kid: state.kid, device_fingerprint: FP, source: 'app_open' },
    });
    if (sess && (sess.session_token || sess.token)) {
      state.session = sess.session_token || sess.token;
      localStorage.setItem(SESSION_KEY, state.session);
    }
  }

  // ── QR Scan Handler ─────────────────────────────────────────────────
  async function handleQRScan(params) {
    await ensureKid();
    await startSession();

    const result = await apiSafe('/kix-id/qr-scan/bind', {
      method: 'POST',
      body: {
        qr_token: params.qr,
        kid: state.kid,
        device_fingerprint: FP,
        source_brand_id: params.b,
        source_store_id: params.s,
      },
    });
    showBrandWelcome(result || { brand_id: params.b, store_id: params.s });
  }

  function showBrandWelcome(result) {
    const brand = result.brand_name || result.brand_id || '商家';
    const c = $('#view-container');
    const hasGame = !!result.recipe_id || !!result.game_slug;
    c.innerHTML = `
      <div class="kix-brand-welcome">
        <h1>欢迎光临</h1>
        <div class="brand-tag">${escapeHtml(brand)}</div>
        <p>${escapeHtml(result.welcome_message || '完成一局游戏，赢取这家商家的奖励。')}</p>
        ${hasGame ? `
          <button class="kix-cta" onclick="window.playGame('${escapeHtml(result.recipe_id || result.game_slug)}', '${escapeHtml(result.brand_id || '')}')">
            开始游戏
          </button>` : `
          <button class="kix-cta" onclick="window.setView('play')">浏览游戏</button>`}
        <p style="margin-top:18px;font-size:12px;color:var(--kix-text-mute);">
          你的 KiX ID: <code>${escapeHtml(state.kid)}</code>
        </p>
      </div>
    `;
  }

  // ── Inbox ───────────────────────────────────────────────────────────
  async function loadInbox() {
    if (!state.kid) return [];
    const r = await apiSafe('/push/user/' + encodeURIComponent(state.kid) + '/inbox?limit=50');
    if (!r) { state.inbox = []; updateInboxBadge(); return []; }
    state.inbox = Array.isArray(r) ? r : (r.pushes || r.items || []);
    updateInboxBadge();
    return state.inbox;
  }

  function updateInboxBadge() {
    const unread = state.inbox.filter(p => (p.status || 'delivered') !== 'opened').length;
    const badge = $('#inbox-badge');
    if (!badge) return;
    if (unread > 0) {
      badge.textContent = unread > 99 ? '99+' : String(unread);
      badge.classList.remove('hidden');
    } else {
      badge.classList.add('hidden');
    }
  }

  async function openPush(push_id) {
    if (!state.kid) return;
    await apiSafe('/push/' + encodeURIComponent(push_id) + '/mark', {
      method: 'POST',
      body: { kid: state.kid, status: 'opened' },
    });
    const push = state.inbox.find(p => p.push_id === push_id || p.id === push_id);
    if (push) {
      push.status = 'opened';
      updateInboxBadge();
      if (push.deep_link) {
        window.location.href = push.deep_link;
      } else if (push.brand_id) {
        window.location.href = '/landing/storefront.html?b=' + encodeURIComponent(push.brand_id);
      }
    }
  }

  // ── Rewards ─────────────────────────────────────────────────────────
  async function loadRewards() {
    if (!state.kid) return;
    const r = await apiSafe('/vouchers/user/' + encodeURIComponent(state.kid) + '?status=issued&limit=50');
    const list = Array.isArray(r) ? r : (r ? (r.vouchers || r.items || []) : []);
    state.rewards.vouchers = list;
    // Points may live on profile or a separate balance endpoint
    const bal = await apiSafe('/progression/balance/' + encodeURIComponent(state.kid));
    if (bal) {
      state.rewards.points = bal.points || bal.xp || 0;
      state.rewards.badges = bal.badges || [];
    }
    const summary = $('#rewards-summary');
    if (summary) summary.textContent = (state.rewards.points || 0) + ' ⭐';
  }

  // ── Views ───────────────────────────────────────────────────────────
  function renderHome() {
    const c = $('#view-container');
    const name = (state.profile && state.profile.display_name) || '玩家';
    c.innerHTML = `
      <div class="kix-welcome">
        <h1>欢迎，${escapeHtml(name)}!</h1>
        <p>你的 KiX ID: <code>${escapeHtml(state.kid || '加载中...')}</code></p>
      </div>
      <div class="kix-cards">
        <div class="kix-card" data-go="play">
          <h3>🎮 玩游戏</h3>
          <p>玩游戏赢取积分和奖励</p>
        </div>
        <div class="kix-card" data-go="rewards">
          <h3>🎁 我的奖励</h3>
          <p>${state.rewards.points} 积分 · ${state.rewards.vouchers.length} 张优惠券</p>
        </div>
        <div class="kix-card" data-go="inbox">
          <h3>📬 收件箱</h3>
          <p>查看商家推送给你的活动</p>
        </div>
      </div>
      <div class="kix-discover">
        <h2>推荐发现</h2>
        <div id="discover-list"><div class="kix-loading"><div class="spinner"></div></div></div>
      </div>
    `;
    c.querySelectorAll('[data-go]').forEach(el =>
      el.addEventListener('click', () => setView(el.dataset.go))
    );
    loadDiscover();
  }

  async function loadDiscover() {
    const r = await apiSafe('/storefront/discover?limit=10&country=CN');
    const list = $('#discover-list');
    if (!list) return;
    const brands = !r ? [] : (Array.isArray(r) ? r : (r.brands || r.items || []));
    if (!brands.length) {
      list.innerHTML = '<p class="empty">暂无推荐</p>';
      return;
    }
    list.innerHTML = brands.map(b => `
      <div class="kix-discover-card" data-brand="${escapeHtml(b.brand_id)}">
        <h4>${escapeHtml(b.display_name || b.brand_id)}</h4>
        <p>${escapeHtml(b.bio || '')}</p>
        <small>${b.rating ? '⭐ ' + Number(b.rating).toFixed(1) : ''}${b.rating && b.followers ? ' · ' : ''}${b.followers ? b.followers + ' followers' : ''}</small>
      </div>
    `).join('');
    list.querySelectorAll('[data-brand]').forEach(el =>
      el.addEventListener('click', () => window.visitBrand(el.dataset.brand))
    );
  }

  function renderInbox() {
    const c = $('#view-container');
    c.innerHTML = `
      <h2>📬 收件箱</h2>
      <div id="inbox-list"><div class="kix-loading"><div class="spinner"></div></div></div>
    `;
    loadInbox().then(() => {
      const list = $('#inbox-list');
      if (!list) return;
      if (!state.inbox.length) {
        list.innerHTML = '<p class="empty">还没有消息</p>';
        return;
      }
      list.innerHTML = state.inbox.map(p => {
        const id = p.push_id || p.id || '';
        const status = p.status || 'delivered';
        return `
          <div class="kix-push-item ${status === 'opened' ? 'read' : 'unread'}" data-id="${escapeHtml(id)}">
            <h3>${escapeHtml(p.title || p.brand_name || '通知')}</h3>
            <p>${escapeHtml(p.body || p.message || '')}</p>
            <small>${fmtDate(p.created_at || p.timestamp)}</small>
          </div>
        `;
      }).join('');
      list.querySelectorAll('[data-id]').forEach(el =>
        el.addEventListener('click', () => openPush(el.dataset.id))
      );
    });
  }

  function renderPlay() {
    const c = $('#view-container');
    c.innerHTML = `
      <h2>🎮 游戏</h2>
      <p>选择一个商家的游戏开始玩</p>
      <div id="games-list"><div class="kix-loading"><div class="spinner"></div></div></div>
    `;
    apiSafe('/recipes/?limit=20').then(r => {
      const list = $('#games-list');
      if (!list) return;
      const recipes = !r ? [] : (Array.isArray(r) ? r : (r.recipes || r.items || []));
      if (!recipes.length) {
        list.innerHTML = '<p class="empty">暂无可玩游戏</p>';
        return;
      }
      list.innerHTML = recipes.map(rec => `
        <div class="kix-game-card" data-rec="${escapeHtml(rec.recipe_id || rec.id)}">
          <h3>${escapeHtml(rec.name || rec.title || rec.recipe_id)}</h3>
          <p>${escapeHtml(rec.description || '')}</p>
          <small>类型: ${escapeHtml(rec.industry || rec.category || '其他')}</small>
        </div>
      `).join('');
      list.querySelectorAll('[data-rec]').forEach(el =>
        el.addEventListener('click', () => window.playGame(el.dataset.rec))
      );
    });
  }

  function renderRewards() {
    const c = $('#view-container');
    c.innerHTML = `
      <h2>🎁 我的奖励</h2>
      <div class="kix-rewards-summary">
        <div>${state.rewards.points} 积分</div>
        <div>${state.rewards.vouchers.length} 张优惠券</div>
        <div>${state.rewards.badges.length} 枚徽章</div>
      </div>
      <div id="vouchers-list"><div class="kix-loading"><div class="spinner"></div></div></div>
    `;
    loadRewards().then(() => {
      const list = $('#vouchers-list');
      if (!list) return;
      if (!state.rewards.vouchers.length) {
        list.innerHTML = '<p class="empty">还没有优惠券</p>';
        return;
      }
      list.innerHTML = state.rewards.vouchers.map(v => `
        <div class="kix-voucher-card">
          <h3>${escapeHtml(v.template_id || v.name || v.title || '优惠券')}</h3>
          <p>¥${((v.value_cents || 0) / 100).toFixed(2)} · 来自 ${escapeHtml(v.issuer_brand_id || v.brand_id || '商家')}</p>
          <small>有效期至 ${fmtDate(v.expires_at || v.expiry)}</small>
        </div>
      `).join('');
      // refresh top-bar rewards summary
      const summary = $('#rewards-summary');
      if (summary) summary.textContent = (state.rewards.points || 0) + ' ⭐';
    });
  }

  function renderProfile() {
    const c = $('#view-container');
    const p = state.profile || {};
    c.innerHTML = `
      <h2>👤 我的资料</h2>
      <p>KiX ID: <code>${escapeHtml(state.kid || '-')}</code></p>
      <div class="kix-profile-section">
        <h3>设置</h3>
        <label>显示名
          <input id="profile-name" value="${escapeHtml(p.display_name || '')}" placeholder="昵称">
        </label>
        <label>语言
          <select id="profile-lang">
            <option value="zh-CN" ${p.primary_language === 'zh-CN' ? 'selected' : ''}>中文</option>
            <option value="en-US" ${p.primary_language === 'en-US' ? 'selected' : ''}>English</option>
            <option value="id-ID" ${p.primary_language === 'id-ID' ? 'selected' : ''}>Indonesian</option>
          </select>
        </label>
        <button id="profile-save">保存</button>
      </div>
      <div class="kix-profile-section">
        <h3>已连接的商家</h3>
        <div id="connected-list"><div class="kix-loading"><div class="spinner"></div></div></div>
      </div>
      <div class="kix-profile-section">
        <h3>隐私 / 授权</h3>
        <a href="#" data-act="consent">管理授权</a>
        <a href="#" data-act="export">导出我的数据</a>
        <a href="#" data-act="delete">注销账号</a>
      </div>
    `;
    $('#profile-save').addEventListener('click', window.saveProfile);
    c.querySelectorAll('[data-act]').forEach(el =>
      el.addEventListener('click', (e) => {
        e.preventDefault();
        ({ consent: window.manageConsent, export: window.exportData, delete: window.deleteAccount }[el.dataset.act] || (() => {}))();
      })
    );
    loadConnectedMerchants();
  }

  async function loadConnectedMerchants() {
    if (!state.kid) return;
    const r = await apiSafe('/kix-id/connect/grants/' + encodeURIComponent(state.kid));
    const grants = !r ? [] : (Array.isArray(r) ? r : (r.grants || r.items || []));
    const list = $('#connected-list');
    if (!list) return;
    if (!grants.length) {
      list.innerHTML = '<p class="empty">还没有连接任何商家</p>';
      return;
    }
    list.innerHTML = grants.map(g => `
      <div class="kix-grant-item">
        <span>${escapeHtml(g.brand_id)}</span>
        <span class="scopes">${escapeHtml((g.scopes_granted || g.scopes || []).join(', '))}</span>
        <button data-gid="${escapeHtml(g.grant_id || g.id)}">撤销</button>
      </div>
    `).join('');
    list.querySelectorAll('[data-gid]').forEach(el =>
      el.addEventListener('click', () => window.revokeGrant(el.dataset.gid))
    );
  }

  // ── Navigation ──────────────────────────────────────────────────────
  const VIEWS = {
    home: renderHome,
    inbox: renderInbox,
    play: renderPlay,
    rewards: renderRewards,
    profile: renderProfile,
  };

  function setView(view) {
    if (!VIEWS[view]) view = 'home';
    state.currentView = view;
    document.querySelectorAll('.kix-nav button').forEach(b =>
      b.classList.toggle('active', b.dataset.view === view)
    );
    VIEWS[view]();
    window.scrollTo({ top: 0, behavior: 'instant' });
  }

  // ── Welcome (new user) ──────────────────────────────────────────────
  async function showWelcome() {
    toast('欢迎来到 KiX!');
  }

  // ── Init ────────────────────────────────────────────────────────────
  async function init() {
    const params = new URLSearchParams(location.search);

    // QR scan branch
    if (params.get('qr') || params.get('b')) {
      try {
        await handleQRScan(Object.fromEntries(params));
      } catch (e) {
        console.error('QR scan failed', e);
        $('#view-container').innerHTML = '<p class="empty">QR 扫码处理失败，请重试</p>';
      }
      // Bind nav for after-welcome navigation
      bindNav();
      // background load
      loadInbox(); loadRewards();
      return;
    }

    // Normal open
    await ensureKid();
    await startSession();

    // Load profile (best-effort)
    const p = await apiSafe('/kix-id/' + encodeURIComponent(state.kid));
    if (p) {
      state.profile = p;
      const display = $('#kid-display');
      if (display) display.textContent = p.display_name || (state.kid || '').slice(0, 12);
    } else {
      const display = $('#kid-display');
      if (display) display.textContent = (state.kid || '').slice(0, 12);
    }

    bindNav();
    setView('home');

    // background loads
    loadInbox();
    loadRewards();
  }

  function bindNav() {
    document.querySelectorAll('.kix-nav button').forEach(b => {
      b.addEventListener('click', () => setView(b.dataset.view));
    });
  }

  // ── Global window handlers ─────────────────────────────────────────
  window.setView = setView;
  window.openPush = openPush;

  window.visitBrand = function (bid) {
    if (!bid) return;
    window.location.href = '/landing/storefront.html?b=' + encodeURIComponent(bid);
  };

  window.playGame = function (rid, brand) {
    if (!rid) return;
    let url = '/landing/play.html?recipe=' + encodeURIComponent(rid);
    if (brand) url += '&brand=' + encodeURIComponent(brand);
    window.location.href = url;
  };

  window.revokeGrant = async function (gid) {
    if (!gid) return;
    if (!confirm('确认撤销这个商家的授权？')) return;
    await apiSafe('/kix-id/connect/revoke', { method: 'POST', body: { grant_id: gid, by: 'user' } });
    toast('授权已撤销');
    loadConnectedMerchants();
  };

  window.saveProfile = async function () {
    const name = $('#profile-name').value.trim();
    const lang = $('#profile-lang').value;
    const r = await apiSafe('/kix-id/' + encodeURIComponent(state.kid) + '/update', {
      method: 'POST',
      body: { display_name: name, primary_language: lang },
    });
    if (r) {
      state.profile = Object.assign(state.profile || {}, r);
      toast('保存成功');
      const display = $('#kid-display');
      if (display && name) display.textContent = name;
    } else {
      toast('保存失败，请稍后重试');
    }
  };

  window.manageConsent = function () {
    window.location.href = '/landing/app/consent.html';
  };

  window.exportData = async function () {
    const r = await apiSafe('/kix-id/' + encodeURIComponent(state.kid) + '/export', { method: 'POST' });
    if (r && r.download_url) {
      window.location.href = r.download_url;
    } else {
      toast('导出请求已提交，稍后将邮件通知');
    }
  };

  window.deleteAccount = async function () {
    if (!confirm('注销账号将永久删除你的 KiX ID 和所有数据，确定吗？')) return;
    if (!confirm('再次确认：这个操作无法撤销！')) return;
    const r = await apiSafe('/kix-id/' + encodeURIComponent(state.kid) + '/delete', { method: 'POST' });
    if (r) {
      localStorage.removeItem(KID_KEY);
      localStorage.removeItem(SESSION_KEY);
      toast('账号已注销');
      setTimeout(() => location.reload(), 1500);
    } else {
      toast('注销失败');
    }
  };

  // ── Boot ────────────────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
