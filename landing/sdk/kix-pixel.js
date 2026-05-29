/*!
 * KiX Conversion Pixel SDK v1.1.0
 * Google-Analytics-style pixel for merchant websites + mobile apps.
 * (c) 2026 KiX. MIT License.
 *
 * Embed (web):
 *   <script async src="https://api.kix.gg/sdk/kix-pixel.js" data-pixel="px_xxx"></script>
 *
 * Embed (WeChat Mini-Program): bundle this file into your subpackage; auto-
 * detected via `typeof wx !== 'undefined'`. Exposes window.kix._mp for the
 * mini-program-native code path (wx.request, no fetch/localStorage).
 *
 * Public API (window.kix):
 *   kix.identify(user_id)
 *   kix.track(eventType, params)
 *   kix.purchase(orderId, amountCents, currency?)   currency defaults to 'CNY'
 *   kix.signup(newUserId)
 *   kix.addToCart(productId, valueCents?)
 *   kix.refund(orderId, reason?)                    reverse a paid commission
 *   kix.return(orderId, refundCents?)               same, with refunded amount
 *   kix.batch([{event_type, ...}, ...])             up to 100 events per call
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
  // Batch endpoint lives alongside the single-event endpoint.
  var BATCH_URL = EVENT_URL.replace(/\/event$/, '/events/batch');

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

    // Refund / return — server uses these to reverse commission on a
    // previously attributed purchase (looked up by order_id).
    refund: function (orderId, reason) {
      if (!orderId) {
        if (window.console) console.warn('[KiX Pixel] refund requires orderId');
        return;
      }
      return send('refund', {
        order_id: String(orderId),
        meta: { reason: reason != null ? String(reason) : null }
      });
    },

    'return': function (orderId, refundCents) {
      if (!orderId) {
        if (window.console) console.warn('[KiX Pixel] return requires orderId');
        return;
      }
      return send('return', {
        order_id: String(orderId),
        amount_cents: refundCents != null ? (parseInt(refundCents, 10) || 0) : null
      });
    },

    // Batch up to 100 events into one HTTP round-trip (mobile / high-volume).
    // Server enforces max 100 and per-event validation; we just package +
    // attach defaults so merchants don't have to repeat boilerplate.
    batch: function (events) {
      if (!events || !events.length) return;
      var uid = currentUserId();
      var enriched = [];
      for (var i = 0; i < events.length && i < 100; i++) {
        var e = events[i] || {};
        var packed = {
          event_type: e.event_type || e.type || 'custom',
          user_id: e.user_id != null ? e.user_id : uid,
          device_fingerprint: e.device_fingerprint || FP,
          origin: e.origin || location.origin,
          referrer: e.referrer != null ? e.referrer : (document.referrer || null),
          url: e.url != null ? e.url : location.href
        };
        // Copy through optional event fields verbatim.
        var passthrough = ['order_id', 'amount_cents', 'currency', 'meta'];
        for (var p = 0; p < passthrough.length; p++) {
          var key = passthrough[p];
          if (e[key] !== undefined) packed[key] = e[key];
        }
        enriched.push(packed);
      }
      var body = {
        pixel_id: PIXEL_ID,
        origin: location.origin,
        events: enriched
      };
      var json = JSON.stringify(body);
      try {
        if (window.fetch) {
          return fetch(BATCH_URL, {
            method: 'POST',
            mode: 'cors',
            credentials: 'omit',
            headers: { 'Content-Type': 'application/json' },
            body: json,
            keepalive: true
          }).catch(function () { /* swallow */ });
        }
      } catch (e2) { /* fall through */ }
      try {
        if (navigator.sendBeacon) {
          var blob = new Blob([json], { type: 'application/json' });
          navigator.sendBeacon(BATCH_URL, blob);
          return;
        }
      } catch (e3) { /* fall through */ }
      try {
        var xhr = new XMLHttpRequest();
        xhr.open('POST', BATCH_URL, true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.send(json);
      } catch (e4) { /* swallow */ }
    },

    fp: function () { return FP; }
  };

  // ── WeChat Mini-Program adapter ────────────────────────────────────────
  // Different code path: no fetch, no localStorage, no document.location.
  // Mini-programs call `wx.request` and identify themselves with their
  // App-ID via `wx<appid>` origin format. Exposed as window.kix._mp for
  // sites that bundle this file into a hybrid wrapper; pure mini-program
  // builds typically inline a trimmed copy.
  if (typeof wx !== 'undefined' && wx.request) {
    var MP_APPID = (typeof __wxConfig !== 'undefined' && __wxConfig.appId)
      ? __wxConfig.appId
      : 'unknown';
    var MP_ORIGIN = 'wx' + MP_APPID;
    window.kix._mp = {
      origin: MP_ORIGIN,
      send: function (eventType, params) {
        var data = {
          pixel_id: PIXEL_ID,
          event_type: eventType,
          device_fingerprint: FP,
          origin: MP_ORIGIN
        };
        if (params) {
          for (var k in params) {
            if (Object.prototype.hasOwnProperty.call(params, k)) data[k] = params[k];
          }
        }
        wx.request({
          url: EVENT_URL,
          method: 'POST',
          header: { 'content-type': 'application/json' },
          data: data
        });
      },
      batch: function (events) {
        if (!events || !events.length) return;
        var enriched = [];
        for (var i = 0; i < events.length && i < 100; i++) {
          var e = events[i] || {};
          enriched.push({
            event_type: e.event_type || e.type || 'custom',
            user_id: e.user_id != null ? e.user_id : null,
            device_fingerprint: e.device_fingerprint || FP,
            order_id: e.order_id,
            amount_cents: e.amount_cents,
            currency: e.currency,
            meta: e.meta,
            origin: MP_ORIGIN
          });
        }
        wx.request({
          url: BATCH_URL,
          method: 'POST',
          header: { 'content-type': 'application/json' },
          data: { pixel_id: PIXEL_ID, origin: MP_ORIGIN, events: enriched }
        });
      }
    };
  }

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
