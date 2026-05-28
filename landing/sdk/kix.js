/*!
 * KiX SDK v1.0.0
 * Embeddable gamification widget for brand websites.
 * (c) 2026 KiX. MIT License.
 *
 * Usage:
 *   <script src="https://kix.app/sdk/kix.js" data-brand="brand-xxx"></script>
 *   <div id="kix-widget" data-mode="floating"></div>
 */
(function (global) {
  'use strict';

  // ------------------------------------------------------------------
  // Config & Defaults
  // ------------------------------------------------------------------
  var DEFAULTS = {
    brand_id: null,
    base_url: 'https://api.kix.app',
    mode: 'floating', // floating | inline | modal
    container_id: 'kix-widget',
    brand_color: '#6C5CE7',
    brand_name: 'KiX',
    debug: false
  };

  var STATE = {
    config: Object.assign({}, DEFAULTS),
    user_id: null,
    progression: { xp: 0, level: 1, to_next: 100, streak: 0, badges: [], energy: 100 },
    brand: null,
    listeners: {},
    ready: false,
    dom: { fab: null, panel: null, modal: null, inline: null, styles: null }
  };

  // ------------------------------------------------------------------
  // Utilities
  // ------------------------------------------------------------------
  function log() {
    if (STATE.config.debug && global.console) {
      var args = Array.prototype.slice.call(arguments);
      args.unshift('[KiX]');
      console.log.apply(console, args);
    }
  }
  function warn() {
    if (global.console) {
      var args = Array.prototype.slice.call(arguments);
      args.unshift('[KiX]');
      console.warn.apply(console, args);
    }
  }

  function uuid() {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0,
        v = c === 'x' ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  function hashStr(s) {
    var h = 5381,
      i = s.length;
    while (i) h = (h * 33) ^ s.charCodeAt(--i);
    return (h >>> 0).toString(16);
  }

  function deviceFingerprint() {
    try {
      var bits = [
        navigator.userAgent || '',
        navigator.language || '',
        screen.width + 'x' + screen.height + 'x' + screen.colorDepth,
        new Date().getTimezoneOffset(),
        navigator.hardwareConcurrency || '',
        navigator.platform || ''
      ];
      // canvas fp
      try {
        var c = document.createElement('canvas');
        var ctx = c.getContext('2d');
        ctx.textBaseline = 'top';
        ctx.font = '14px Arial';
        ctx.fillStyle = '#f60';
        ctx.fillRect(0, 0, 60, 20);
        ctx.fillStyle = '#069';
        ctx.fillText('kix-fp', 2, 2);
        bits.push(c.toDataURL());
      } catch (e) { /* ignore */ }
      return hashStr(bits.join('|'));
    } catch (e) {
      return uuid();
    }
  }

  function storageKey() {
    return 'kix_user_' + (STATE.config.brand_id || 'default');
  }

  function loadUserId() {
    try {
      var k = storageKey();
      var v = global.localStorage && localStorage.getItem(k);
      if (v) return v;
    } catch (e) { /* ignore */ }
    return null;
  }

  function saveUserId(uid) {
    try {
      localStorage.setItem(storageKey(), uid);
    } catch (e) { /* ignore */ }
  }

  function cacheProgression(p) {
    try {
      localStorage.setItem(storageKey() + '_prog', JSON.stringify(p));
    } catch (e) {}
  }
  function loadCachedProgression() {
    try {
      var v = localStorage.getItem(storageKey() + '_prog');
      if (v) return JSON.parse(v);
    } catch (e) {}
    return null;
  }

  function fetchJSON(url, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    if (opts.body && typeof opts.body !== 'string') opts.body = JSON.stringify(opts.body);
    return fetch(url, opts).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  // ------------------------------------------------------------------
  // DOM helpers (no innerHTML)
  // ------------------------------------------------------------------
  function el(tag, props, children) {
    var n = document.createElement(tag);
    if (props) {
      for (var k in props) {
        if (!Object.prototype.hasOwnProperty.call(props, k)) continue;
        if (k === 'style' && typeof props[k] === 'object') {
          for (var s in props[k]) n.style[s] = props[k][s];
        } else if (k === 'class') {
          n.className = props[k];
        } else if (k === 'on' && typeof props[k] === 'object') {
          for (var ev in props[k]) n.addEventListener(ev, props[k][ev]);
        } else if (k === 'text') {
          n.textContent = props[k];
        } else if (k === 'attrs') {
          for (var a in props[k]) n.setAttribute(a, props[k][a]);
        } else {
          n[k] = props[k];
        }
      }
    }
    if (children) {
      for (var i = 0; i < children.length; i++) {
        var c = children[i];
        if (c == null) continue;
        if (typeof c === 'string') n.appendChild(document.createTextNode(c));
        else n.appendChild(c);
      }
    }
    return n;
  }

  function $(sel, root) { return (root || document).querySelector(sel); }

  // ------------------------------------------------------------------
  // Styles
  // ------------------------------------------------------------------
  function injectStyles() {
    if (STATE.dom.styles) return;
    var color = STATE.config.brand_color;
    var css = [
      '.kix-sdk-fab{position:fixed;right:20px;bottom:20px;width:60px;height:60px;border-radius:50%;background:' + color + ';color:#fff;display:flex;align-items:center;justify-content:center;font:600 22px system-ui,sans-serif;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.25);z-index:2147483600;border:none;transition:transform .2s ease,box-shadow .2s ease;user-select:none}',
      '.kix-sdk-fab:hover{transform:scale(1.06);box-shadow:0 10px 26px rgba(0,0,0,.3)}',
      '.kix-sdk-fab .kix-sdk-dot{position:absolute;top:6px;right:6px;width:12px;height:12px;border-radius:50%;background:#ff4757;border:2px solid #fff}',
      '.kix-sdk-panel{position:fixed;right:20px;bottom:90px;width:350px;height:500px;background:#fff;border-radius:16px;box-shadow:0 16px 40px rgba(0,0,0,.25);z-index:2147483601;display:flex;flex-direction:column;overflow:hidden;font:14px/1.4 system-ui,-apple-system,sans-serif;color:#222}',
      '.kix-sdk-header{padding:16px;background:linear-gradient(135deg,' + color + ',' + shadeColor(color, -20) + ');color:#fff;display:flex;justify-content:space-between;align-items:center}',
      '.kix-sdk-header h3{margin:0;font-size:16px;font-weight:600}',
      '.kix-sdk-close{background:transparent;border:none;color:#fff;font-size:22px;cursor:pointer;line-height:1;padding:0 4px}',
      '.kix-sdk-body{flex:1;overflow-y:auto;padding:16px}',
      '.kix-sdk-stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}',
      '.kix-sdk-stat{background:#f5f5f9;border-radius:10px;padding:10px;text-align:center}',
      '.kix-sdk-stat-label{font-size:11px;color:#777;text-transform:uppercase;letter-spacing:.5px}',
      '.kix-sdk-stat-value{font-size:20px;font-weight:700;color:' + color + ';margin-top:2px}',
      '.kix-sdk-progress{margin-bottom:16px}',
      '.kix-sdk-progress-label{display:flex;justify-content:space-between;font-size:12px;color:#666;margin-bottom:4px}',
      '.kix-sdk-progress-bar{height:8px;background:#eee;border-radius:4px;overflow:hidden}',
      '.kix-sdk-progress-fill{height:100%;background:linear-gradient(90deg,' + color + ',' + shadeColor(color, 20) + ');transition:width .4s ease}',
      '.kix-sdk-cta{display:block;width:100%;padding:12px;background:' + color + ';color:#fff;border:none;border-radius:10px;font-weight:600;font-size:14px;cursor:pointer;margin-bottom:10px;transition:opacity .2s}',
      '.kix-sdk-cta:hover{opacity:.9}',
      '.kix-sdk-cta-secondary{background:#fff;color:' + color + ';border:1px solid ' + color + '}',
      '.kix-sdk-section{margin-top:14px;font-size:12px;font-weight:600;color:#999;text-transform:uppercase;letter-spacing:.5px}',
      '.kix-sdk-quest{padding:10px;background:#fafafd;border-radius:8px;margin-top:8px;border-left:3px solid ' + color + '}',
      '.kix-sdk-quest-title{font-weight:600;font-size:13px}',
      '.kix-sdk-quest-meta{font-size:11px;color:#888;margin-top:2px}',
      '.kix-sdk-badge-row{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}',
      '.kix-sdk-badge{width:32px;height:32px;border-radius:50%;background:' + color + ';color:#fff;display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:600}',
      '.kix-sdk-modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:2147483700;display:flex;align-items:center;justify-content:center;padding:20px}',
      '.kix-sdk-modal{background:#fff;border-radius:16px;width:100%;max-width:900px;height:90vh;max-height:700px;display:flex;flex-direction:column;overflow:hidden;position:relative}',
      '.kix-sdk-modal-close{position:absolute;top:10px;right:14px;background:rgba(0,0,0,.5);color:#fff;border:none;width:32px;height:32px;border-radius:50%;cursor:pointer;font-size:18px;z-index:2}',
      '.kix-sdk-modal iframe{flex:1;border:none;width:100%;height:100%}',
      '.kix-sdk-inline{background:#fff;border-radius:14px;padding:16px;box-shadow:0 4px 14px rgba(0,0,0,.08);font:14px/1.4 system-ui,sans-serif;color:#222;border:1px solid #eee}',
      '.kix-sdk-inline-head{display:flex;align-items:center;gap:10px;margin-bottom:12px}',
      '.kix-sdk-inline-avatar{width:40px;height:40px;border-radius:50%;background:' + color + ';color:#fff;display:flex;align-items:center;justify-content:center;font-weight:600}',
      '.kix-sdk-toast{position:fixed;bottom:100px;right:20px;background:' + color + ';color:#fff;padding:12px 16px;border-radius:10px;font:14px system-ui,sans-serif;box-shadow:0 6px 20px rgba(0,0,0,.25);z-index:2147483800;animation:kix-fade .3s ease}',
      '@keyframes kix-fade{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}',
      '@media(max-width:480px){.kix-sdk-panel{right:0;bottom:0;width:100%;height:100%;border-radius:0}.kix-sdk-fab{right:14px;bottom:14px}}'
    ].join('\n');
    var style = el('style', { type: 'text/css' });
    style.appendChild(document.createTextNode(css));
    document.head.appendChild(style);
    STATE.dom.styles = style;
  }

  function shadeColor(hex, percent) {
    var c = hex.replace('#', '');
    if (c.length === 3) c = c.split('').map(function (x) { return x + x; }).join('');
    var n = parseInt(c, 16);
    var r = Math.max(0, Math.min(255, (n >> 16) + percent));
    var g = Math.max(0, Math.min(255, ((n >> 8) & 0xff) + percent));
    var b = Math.max(0, Math.min(255, (n & 0xff) + percent));
    return '#' + ((r << 16) | (g << 8) | b).toString(16).padStart(6, '0');
  }

  // ------------------------------------------------------------------
  // Event bus
  // ------------------------------------------------------------------
  function on(event, cb) {
    (STATE.listeners[event] = STATE.listeners[event] || []).push(cb);
  }
  function off(event, cb) {
    var list = STATE.listeners[event];
    if (!list) return;
    STATE.listeners[event] = list.filter(function (f) { return f !== cb; });
  }
  function emit(event, data) {
    log('emit', event, data);
    var list = STATE.listeners[event] || [];
    list.forEach(function (cb) {
      try { cb(data); } catch (e) { warn('listener error', e); }
    });
  }

  // ------------------------------------------------------------------
  // API client
  // ------------------------------------------------------------------
  var API = {
    getBrand: function () {
      return fetchJSON(STATE.config.base_url + '/api/v1/brands/' + STATE.config.brand_id)
        .catch(function (e) { warn('brand fetch failed', e.message); return null; });
    },
    shadowAuth: function (fingerprint) {
      return fetchJSON(STATE.config.base_url + '/api/v1/auth/shadow', {
        method: 'POST',
        body: { brand_id: STATE.config.brand_id, fingerprint: fingerprint }
      }).catch(function (e) {
        warn('shadow auth failed, generating local id', e.message);
        return { user_id: 'anon-' + fingerprint };
      });
    },
    getProgression: function (user_id) {
      return fetchJSON(
        STATE.config.base_url + '/api/v1/progression/user/' + user_id +
        '/progression?brand_id=' + encodeURIComponent(STATE.config.brand_id)
      ).catch(function (e) { warn('progression fetch failed', e.message); return null; });
    },
    awardXP: function (user_id, amount, reason) {
      return fetchJSON(STATE.config.base_url + '/api/v1/progression/award/xp', {
        method: 'POST',
        body: {
          user_id: user_id, brand_id: STATE.config.brand_id,
          amount: amount, reason: reason || 'manual'
        }
      }).catch(function (e) { warn('award xp failed', e.message); return null; });
    },
    checkin: function (user_id) {
      return fetchJSON(STATE.config.base_url + '/api/v1/progression/checkin', {
        method: 'POST',
        body: { user_id: user_id, brand_id: STATE.config.brand_id }
      }).catch(function (e) { warn('checkin failed', e.message); return null; });
    },
    shareToWin: function (user_id, score) {
      return fetchJSON(STATE.config.base_url + '/api/v1/network/share-to-win', {
        method: 'POST',
        body: { user_id: user_id, brand_id: STATE.config.brand_id, score: score }
      }).catch(function (e) { warn('share failed', e.message); return null; });
    },
    startGame: function (user_id, slug) {
      return fetchJSON(STATE.config.base_url + '/api/v1/game/start', {
        method: 'POST',
        body: { user_id: user_id, brand_id: STATE.config.brand_id, slug: slug || null }
      }).catch(function (e) { warn('start game failed', e.message); return null; });
    }
  };

  // ------------------------------------------------------------------
  // Widgets
  // ------------------------------------------------------------------
  function buildFAB() {
    var fab = el('button', {
      class: 'kix-sdk-fab',
      attrs: { 'aria-label': 'Open ' + STATE.config.brand_name + ' rewards' },
      on: { click: togglePanel }
    }, [document.createTextNode('★')]);
    var dot = el('span', { class: 'kix-sdk-dot' });
    fab.appendChild(dot);
    document.body.appendChild(fab);
    STATE.dom.fab = fab;
  }

  function togglePanel() {
    if (STATE.dom.panel) { closePanel(); } else { openPanel(); }
  }

  function openPanel() {
    if (STATE.dom.panel) return;
    var p = STATE.progression;
    var pct = Math.min(100, Math.round((p.xp / Math.max(1, p.xp + p.to_next)) * 100));

    var stats = el('div', { class: 'kix-sdk-stats' }, [
      statBlock('XP', p.xp),
      statBlock('Level', p.level),
      statBlock('Streak', (p.streak || 0) + 'd'),
      statBlock('Energy', p.energy || 0)
    ]);

    var progress = el('div', { class: 'kix-sdk-progress' }, [
      el('div', { class: 'kix-sdk-progress-label' }, [
        el('span', { text: 'Level ' + p.level }),
        el('span', { text: p.to_next + ' XP to next' })
      ]),
      el('div', { class: 'kix-sdk-progress-bar' }, [
        el('div', { class: 'kix-sdk-progress-fill', style: { width: pct + '%' } })
      ])
    ]);

    var playBtn = el('button', {
      class: 'kix-sdk-cta',
      text: 'Play Games',
      on: { click: function () { KiX.game.launch(); } }
    });
    var checkinBtn = el('button', {
      class: 'kix-sdk-cta kix-sdk-cta-secondary',
      text: 'Daily Check-in',
      on: { click: function () { KiX.streak.checkin(); } }
    });
    var shareBtn = el('button', {
      class: 'kix-sdk-cta kix-sdk-cta-secondary',
      text: 'Invite Friends',
      on: { click: function () { KiX.share.inviteFriend('Join me on ' + STATE.config.brand_name + '!'); } }
    });

    var questsLbl = el('div', { class: 'kix-sdk-section', text: 'Active Quests' });
    var quests = el('div', {}, [
      questCard('Play 3 games today', '1/3'),
      questCard('Earn 100 XP', Math.min(100, p.xp) + '/100')
    ]);

    var badgesLbl = el('div', { class: 'kix-sdk-section', text: 'Badges' });
    var badgeRow = el('div', { class: 'kix-sdk-badge-row' },
      (p.badges && p.badges.length
        ? p.badges.slice(0, 6).map(function (b) {
            return el('div', { class: 'kix-sdk-badge', text: (b.icon || (b.name || '?').charAt(0)) });
          })
        : [el('span', { style: { color: '#999', fontSize: '12px' }, text: 'No badges yet — play to earn!' })])
    );

    var panel = el('div', { class: 'kix-sdk-panel' }, [
      el('div', { class: 'kix-sdk-header' }, [
        el('h3', { text: STATE.config.brand_name + ' Rewards' }),
        el('button', { class: 'kix-sdk-close', text: '×', on: { click: closePanel } })
      ]),
      el('div', { class: 'kix-sdk-body' }, [
        stats, progress, playBtn, checkinBtn, shareBtn,
        questsLbl, quests,
        badgesLbl, badgeRow
      ])
    ]);

    document.body.appendChild(panel);
    STATE.dom.panel = panel;
    emit('panel_open', {});
  }

  function closePanel() {
    if (STATE.dom.panel) {
      document.body.removeChild(STATE.dom.panel);
      STATE.dom.panel = null;
      emit('panel_close', {});
    }
  }

  function statBlock(label, value) {
    return el('div', { class: 'kix-sdk-stat' }, [
      el('div', { class: 'kix-sdk-stat-label', text: label }),
      el('div', { class: 'kix-sdk-stat-value', text: String(value) })
    ]);
  }
  function questCard(title, progress) {
    return el('div', { class: 'kix-sdk-quest' }, [
      el('div', { class: 'kix-sdk-quest-title', text: title }),
      el('div', { class: 'kix-sdk-quest-meta', text: 'Progress: ' + progress })
    ]);
  }

  function buildInline(target) {
    var p = STATE.progression;
    var initial = (STATE.config.brand_name || 'K').charAt(0).toUpperCase();
    var node = el('div', { class: 'kix-sdk-inline' }, [
      el('div', { class: 'kix-sdk-inline-head' }, [
        el('div', { class: 'kix-sdk-inline-avatar', text: initial }),
        el('div', {}, [
          el('div', { style: { fontWeight: '600' }, text: STATE.config.brand_name + ' Rewards' }),
          el('div', { style: { fontSize: '12px', color: '#777' }, text: 'Level ' + p.level + ' • ' + p.xp + ' XP' })
        ])
      ]),
      el('div', { class: 'kix-sdk-stats' }, [
        statBlock('XP', p.xp),
        statBlock('Streak', (p.streak || 0) + 'd'),
        statBlock('Badges', (p.badges || []).length),
        statBlock('Energy', p.energy || 0)
      ]),
      el('button', {
        class: 'kix-sdk-cta',
        text: 'Play & Earn',
        on: { click: function () { KiX.game.launch(); } }
      })
    ]);
    while (target.firstChild) target.removeChild(target.firstChild);
    target.appendChild(node);
    STATE.dom.inline = node;
  }

  // ------------------------------------------------------------------
  // Modal / Game launcher
  // ------------------------------------------------------------------
  function openModal(url) {
    if (STATE.dom.modal) return;
    var iframe = el('iframe', {
      attrs: { src: url, allow: 'autoplay; fullscreen; gamepad' }
    });
    var closeBtn = el('button', {
      class: 'kix-sdk-modal-close', text: '×',
      on: { click: closeModal }
    });
    var modal = el('div', { class: 'kix-sdk-modal' }, [closeBtn, iframe]);
    var bg = el('div', {
      class: 'kix-sdk-modal-bg',
      on: { click: function (e) { if (e.target === bg) closeModal(); } }
    }, [modal]);
    document.body.appendChild(bg);
    STATE.dom.modal = bg;

    // Listen for messages from the game iframe
    if (!STATE._msgBound) {
      window.addEventListener('message', onGameMessage);
      STATE._msgBound = true;
    }
    emit('game_open', { url: url });
  }

  function closeModal() {
    if (STATE.dom.modal) {
      document.body.removeChild(STATE.dom.modal);
      STATE.dom.modal = null;
      emit('game_close', {});
    }
  }

  function onGameMessage(ev) {
    var data = ev.data;
    if (!data || typeof data !== 'object') return;
    if (data.kix_event) {
      if (data.kix_event === 'score_update') emit('score_update', data.payload || {});
      else if (data.kix_event === 'game_end') {
        emit('game_end', data.payload || {});
        if (data.payload && typeof data.payload.score === 'number') {
          KiX.xp.award(Math.max(1, Math.round(data.payload.score / 10)), 'game_end');
        }
      }
    }
  }

  function showToast(text) {
    var t = el('div', { class: 'kix-sdk-toast', text: text });
    document.body.appendChild(t);
    setTimeout(function () {
      if (t.parentNode) t.parentNode.removeChild(t);
    }, 2400);
  }

  // ------------------------------------------------------------------
  // Public API
  // ------------------------------------------------------------------
  var KiX = {
    version: '1.0.0',
    _state: STATE,

    init: function (opts) {
      opts = opts || {};
      Object.assign(STATE.config, opts);
      if (!STATE.config.brand_id) {
        warn('init called without brand_id; SDK will not start.');
        return Promise.resolve(false);
      }
      log('init', STATE.config);

      // 1. Identity
      var existing = loadUserId();
      var fp = deviceFingerprint();
      var idPromise;
      if (existing) {
        STATE.user_id = existing;
        idPromise = Promise.resolve({ user_id: existing });
      } else {
        idPromise = API.shadowAuth(fp).then(function (r) {
          var uid = (r && r.user_id) ? r.user_id : 'anon-' + fp;
          STATE.user_id = uid;
          saveUserId(uid);
          return r;
        });
      }

      return idPromise
        .then(function () { return API.getBrand(); })
        .then(function (brand) {
          if (brand) {
            STATE.brand = brand;
            if (brand.color) STATE.config.brand_color = brand.color;
            if (brand.name) STATE.config.brand_name = brand.name;
          }
          injectStyles();
          return API.getProgression(STATE.user_id);
        })
        .then(function (prog) {
          if (prog) {
            STATE.progression = Object.assign(STATE.progression, prog);
            cacheProgression(STATE.progression);
          } else {
            var cached = loadCachedProgression();
            if (cached) STATE.progression = cached;
          }
          mountWidget();
          STATE.ready = true;
          emit('ready', { user_id: STATE.user_id, brand: STATE.brand });
          return true;
        })
        .catch(function (e) {
          warn('init failed', e);
          injectStyles();
          mountWidget();
          return false;
        });
    },

    user: {
      get: function () {
        if (!STATE.user_id) {
          var fp = deviceFingerprint();
          STATE.user_id = 'anon-' + fp;
          saveUserId(STATE.user_id);
        }
        return STATE.user_id;
      },
      upgrade: function (provider, token) {
        return fetchJSON(STATE.config.base_url + '/api/v1/auth/upgrade', {
          method: 'POST',
          body: {
            anon_user_id: STATE.user_id,
            brand_id: STATE.config.brand_id,
            provider: provider, token: token
          }
        }).then(function (r) {
          if (r && r.user_id) {
            STATE.user_id = r.user_id;
            saveUserId(STATE.user_id);
            emit('user_upgraded', r);
          }
          return r;
        }).catch(function (e) {
          warn('upgrade failed', e.message);
          return null;
        });
      }
    },

    game: {
      launch: function (slug) {
        var uid = KiX.user.get();
        return API.startGame(uid, slug).then(function (session) {
          var url = (session && session.url)
            ? session.url
            : (STATE.config.base_url.replace(/\/api.*$/, '') +
               '/play.html?brand=' + encodeURIComponent(STATE.config.brand_id) +
               (slug ? '&slug=' + encodeURIComponent(slug) : '') +
               '&user=' + encodeURIComponent(uid));
          openModal(url);
          return session;
        }).catch(function () {
          // graceful fallback
          openModal('/play.html?brand=' + encodeURIComponent(STATE.config.brand_id || ''));
        });
      },
      close: closeModal,
      on: function (ev, cb) { on(ev, cb); }
    },

    xp: {
      award: function (amount, reason) {
        var uid = KiX.user.get();
        return API.awardXP(uid, amount, reason).then(function (r) {
          if (r) {
            STATE.progression.xp = r.xp != null ? r.xp : STATE.progression.xp + amount;
            if (r.level != null) STATE.progression.level = r.level;
            if (r.to_next != null) STATE.progression.to_next = r.to_next;
          } else {
            STATE.progression.xp += amount;
          }
          cacheProgression(STATE.progression);
          showToast('+' + amount + ' XP' + (reason ? ' • ' + reason : ''));
          emit('xp_award', { amount: amount, reason: reason, total: STATE.progression.xp });
          refreshUI();
          return STATE.progression;
        });
      },
      get: function () {
        return {
          xp: STATE.progression.xp,
          level: STATE.progression.level,
          to_next: STATE.progression.to_next
        };
      }
    },

    badge: {
      award: function (badge_id) {
        return fetchJSON(STATE.config.base_url + '/api/v1/progression/award/badge', {
          method: 'POST',
          body: { user_id: KiX.user.get(), brand_id: STATE.config.brand_id, badge_id: badge_id }
        }).then(function (r) {
          if (r && r.badge) {
            STATE.progression.badges = STATE.progression.badges || [];
            STATE.progression.badges.push(r.badge);
            cacheProgression(STATE.progression);
            showToast('Badge unlocked: ' + (r.badge.name || badge_id));
            emit('badge_award', r.badge);
            refreshUI();
          }
          return r;
        }).catch(function (e) { warn('badge award failed', e.message); return null; });
      },
      list: function () { return (STATE.progression.badges || []).slice(); }
    },

    streak: {
      checkin: function () {
        return API.checkin(KiX.user.get()).then(function (r) {
          if (r) {
            STATE.progression.streak = r.current != null ? r.current : (STATE.progression.streak || 0) + 1;
            cacheProgression(STATE.progression);
            showToast('Day ' + STATE.progression.streak + ' check-in!');
            emit('streak_checkin', STATE.progression);
            refreshUI();
          }
          return r;
        });
      },
      get: function () {
        return {
          current: STATE.progression.streak || 0,
          last_date: STATE.progression.last_checkin || null
        };
      }
    },

    energy: {
      get: function () { return STATE.progression.energy || 0; },
      spend: function (amount) {
        STATE.progression.energy = Math.max(0, (STATE.progression.energy || 0) - amount);
        cacheProgression(STATE.progression);
        emit('energy_change', { balance: STATE.progression.energy });
        refreshUI();
        return STATE.progression.energy;
      }
    },

    share: {
      toWin: function (score) {
        return API.shareToWin(KiX.user.get(), score).then(function (r) {
          var url = (r && r.share_url) ||
            (location.origin + '/share?brand=' + encodeURIComponent(STATE.config.brand_id) +
              '&u=' + encodeURIComponent(KiX.user.get()) + '&s=' + encodeURIComponent(score));
          emit('share_link_ready', { url: url });
          return url;
        });
      },
      inviteFriend: function (message) {
        var url = location.origin + '/?ref=' + encodeURIComponent(KiX.user.get()) +
                  '&brand=' + encodeURIComponent(STATE.config.brand_id || '');
        message = message || 'Join me!';
        if (navigator.share) {
          return navigator.share({ title: STATE.config.brand_name, text: message, url: url })
            .then(function () { emit('invite_sent', { url: url }); return url; })
            .catch(function () { return url; });
        }
        // Fallback: copy to clipboard
        try {
          navigator.clipboard.writeText(message + ' ' + url);
          showToast('Invite link copied!');
        } catch (e) {
          showToast(url);
        }
        emit('invite_sent', { url: url });
        return Promise.resolve(url);
      }
    },

    on: on,
    off: off,
    emit: emit
  };

  function refreshUI() {
    if (STATE.dom.panel) { closePanel(); openPanel(); }
    if (STATE.dom.inline) {
      var parent = STATE.dom.inline.parentNode;
      if (parent) buildInline(parent);
    }
  }

  function mountWidget() {
    var container = document.getElementById(STATE.config.container_id);
    var mode = STATE.config.mode;
    if (container && container.getAttribute('data-mode')) {
      mode = container.getAttribute('data-mode');
    }
    log('mountWidget mode=' + mode);
    if (mode === 'inline' && container) {
      buildInline(container);
    } else if (mode === 'modal') {
      // modal mode = no FAB, brand triggers KiX.game.launch() manually
    } else {
      // floating (default)
      if (!STATE.dom.fab) buildFAB();
    }
  }

  // ------------------------------------------------------------------
  // Auto-init from <script data-brand="...">
  // ------------------------------------------------------------------
  function autoInit() {
    var scripts = document.getElementsByTagName('script');
    var self = null;
    for (var i = scripts.length - 1; i >= 0; i--) {
      var s = scripts[i];
      if (s.src && /kix\.js(\?|$)/.test(s.src) && s.getAttribute('data-brand')) {
        self = s;
        break;
      }
    }
    if (!self) {
      // also scan for any script tag with data-brand attribute pointing to us
      for (var j = 0; j < scripts.length; j++) {
        if (scripts[j].getAttribute('data-brand') && /kix/i.test(scripts[j].src || '')) {
          self = scripts[j];
          break;
        }
      }
    }
    if (self) {
      var brand_id = self.getAttribute('data-brand');
      var base_url = self.getAttribute('data-base-url') || DEFAULTS.base_url;
      var mode = self.getAttribute('data-mode') || DEFAULTS.mode;
      var debug = self.getAttribute('data-debug') === 'true';
      KiX.init({ brand_id: brand_id, base_url: base_url, mode: mode, debug: debug });
    }
  }

  global.KiX = KiX;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', autoInit);
  } else {
    autoInit();
  }
})(typeof window !== 'undefined' ? window : this);
