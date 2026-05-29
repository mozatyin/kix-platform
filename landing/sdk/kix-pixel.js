/*!
 * KiX Conversion Pixel SDK v1.0.0
 * Google-Analytics-style pixel for merchant websites.
 * (c) 2026 KiX. MIT License.
 *
 * Embed:
 *   <script async src="https://api.kix.gg/sdk/kix-pixel.js" data-pixel="px_xxx"></script>
 *
 * Public API (window.kix):
 *   kix.identify(user_id)
 *   kix.track(eventType, params)
 *   kix.purchase(orderId, amountCents, currency?)   currency defaults to 'CNY'
 *   kix.signup(newUserId)
 *   kix.addToCart(productId, valueCents?)
 *   kix.fp()                                        returns device fingerprint
 *   kix.ready                                       true once pageview sent
 */
(function () {
  'use strict';

  // ── Resolve pixel id + endpoint ────────────────────────────────────────
  var SCRIPT_EL = document.currentScript ||
    (function () {
      var s = document.getElementsByTagName('script');
      return s[s.length - 1];
    })();

  var PIXEL_ID = (SCRIPT_EL && SCRIPT_EL.dataset && SCRIPT_EL.dataset.pixel) ||
    window.KIX_PIXEL_ID || null;

  // Allow override via global before script tag, else derive from script src,
  // else fall back to the public KiX endpoint.
  var EVENT_URL = window.KIX_PIXEL_BASE || (function () {
    try {
      var src = SCRIPT_EL && SCRIPT_EL.src;
      if (src) {
        var origin = new URL(src).origin;
        return origin + '/api/v1/pixel/event';
      }
    } catch (e) { /* ignore */ }
    return 'https://api.kix.gg/api/v1/pixel/event';
  })();

  if (!PIXEL_ID) {
    if (window.console) console.warn('[KiX Pixel] missing data-pixel attribute; SDK disabled.');
    return;
  }

  // ── Storage helpers (graceful when localStorage unavailable) ──────────
  var memStore = {};
  function lsGet(k) {
    try { return window.localStorage.getItem(k); }
    catch (e) { return memStore[k] || null; }
  }
  function lsSet(k, v) {
    try { window.localStorage.setItem(k, v); }
    catch (e) { memStore[k] = v; }
  }

  // ── Device fingerprint ────────────────────────────────────────────────
  function fingerprint() {
    var sig = [
      navigator.userAgent || '',
      (screen.width || 0) + 'x' + (screen.height || 0),
      (Intl.DateTimeFormat().resolvedOptions().timeZone) || '',
      navigator.language || '',
      navigator.hardwareConcurrency || 0,
      navigator.platform || ''
    ].join('|');
    var h = 0;
    for (var i = 0; i < sig.length; i++) {
      h = ((h << 5) - h) + sig.charCodeAt(i);
      h = h | 0;  // force int32
    }
    return 'fp_' + Math.abs(h).toString(36);
  }

  var FP = lsGet('_kix_fp');
  if (!FP) {
    FP = fingerprint();
    lsSet('_kix_fp', FP);
  }

  // ── User identity ─────────────────────────────────────────────────────
  function currentUserId() {
    return window.KIX_USER_ID || lsGet('_kix_uid') || null;
  }

  // ── Core send (fire-and-forget) ───────────────────────────────────────
  function send(eventType, params) {
    params = params || {};
    var body = {
      pixel_id: PIXEL_ID,
      event_type: eventType,
      user_id: currentUserId(),
      device_fingerprint: FP,
      origin: location.origin,
      referrer: document.referrer || null,
      url: location.href
    };
    for (var k in params) {
      if (Object.prototype.hasOwnProperty.call(params, k)) body[k] = params[k];
    }

    // Prefer fetch w/ keepalive (survives unload). Fall back to sendBeacon,
    // then to a sync XHR as last resort (legacy browsers).
    var json = JSON.stringify(body);
    try {
      if (window.fetch) {
        return fetch(EVENT_URL, {
          method: 'POST',
          mode: 'cors',
          credentials: 'omit',
          headers: { 'Content-Type': 'application/json' },
          body: json,
          keepalive: true
        }).catch(function () { /* swallow */ });
      }
    } catch (e) { /* fall through */ }
    try {
      if (navigator.sendBeacon) {
        var blob = new Blob([json], { type: 'application/json' });
        navigator.sendBeacon(EVENT_URL, blob);
        return;
      }
    } catch (e) { /* fall through */ }
    try {
      var xhr = new XMLHttpRequest();
      xhr.open('POST', EVENT_URL, true);
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.send(json);
    } catch (e) { /* swallow */ }
  }

  // ── Auto-fire pageview ────────────────────────────────────────────────
  // Wait until DOM ready so referrer/title are stable; harmless otherwise.
  function firePageview() {
    send('pageview');
    window.kix.ready = true;
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', firePageview, { once: true });
  } else {
    firePageview();
  }

  // ── Public API ────────────────────────────────────────────────────────
  // Queue model: if merchant calls window.kix.X before this script loads
  // (placed _kixq array on window), we'll drain it here.
  window.kix = {
    pixel_id: PIXEL_ID,
    ready: false,

    identify: function (uid) {
      if (!uid) return;
      lsSet('_kix_uid', String(uid));
      window.KIX_USER_ID = String(uid);
    },

    track: function (eventType, params) {
      if (!eventType) return;
      send(String(eventType), params || {});
    },

    purchase: function (orderId, amountCents, currency) {
      if (!orderId || amountCents == null) {
        if (window.console) console.warn('[KiX Pixel] purchase requires orderId + amountCents');
        return;
      }
      send('purchase', {
        order_id: String(orderId),
        amount_cents: parseInt(amountCents, 10) || 0,
        currency: currency || 'CNY'
      });
    },

    signup: function (newUserId) {
      if (newUserId) {
        lsSet('_kix_uid', String(newUserId));
        window.KIX_USER_ID = String(newUserId);
      }
      send('signup');
    },

    addToCart: function (productId, valueCents) {
      send('add_to_cart', {
        meta: {
          product_id: productId != null ? String(productId) : null,
          value_cents: valueCents != null ? (parseInt(valueCents, 10) || 0) : null
        }
      });
    },

    fp: function () { return FP; }
  };

  // Drain pre-load command queue (Segment/GTM style).
  try {
    var q = window._kixq;
    if (q && q.length) {
      for (var i = 0; i < q.length; i++) {
        var item = q[i];
        if (!item || !item[0]) continue;
        var method = item[0];
        var args = Array.prototype.slice.call(item, 1);
        if (typeof window.kix[method] === 'function') {
          try { window.kix[method].apply(window.kix, args); }
          catch (e) { /* swallow */ }
        }
      }
      window._kixq = [];
    }
  } catch (e) { /* ignore */ }
})();
