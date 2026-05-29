/*!
 * KiX Conversion Pixel SDK v1.2.0
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
 *   kix.identifyEnhanced({email, phone, first_name, last_name, address})
 *                                                  hashes PII client-side and
 *                                                  caches it for subsequent
 *                                                  `track()` calls so the
 *                                                  server can attribute even
 *                                                  without cookies (Enhanced
 *                                                  Conversions).
 *   kix.track(eventType, params)
 *   kix.purchase(orderId, amountCents, currency?)   currency defaults to 'CNY'
 *   kix.signup(newUserId)
 *   kix.addToCart(productId, valueCents?)
 *   kix.refund(orderId, reason?)                    reverse a paid commission
 *   kix.return(orderId, refundCents?)               same, with refunded amount
 *   kix.batch([{event_type, ...}, ...])             up to 100 events per call
 *   kix.fp()                                        returns device fingerprint
 *   kix.eventId()                                   mint a dedup id matching CAPI
 *   kix.ready                                       true once pageview sent
 *
 *   // 30+ pixel events — thin wrappers over kix.track():
 *   kix.viewContent(contentId)             kix.startTrial(planId)
 *   kix.search(query)                      kix.subscribe(planId, amountCents)
 *   kix.viewVideo(videoId)                 kix.upgrade(planId)
 *   kix.videoComplete(videoId)             kix.downgrade(planId)
 *   kix.clickButton(name)                  kix.cancelSubscription(planId)
 *   kix.clickLink(href)                    kix.renewalSuccess(planId, amountCents)
 *   kix.viewItem(itemId, valueCents?)      kix.renewalFail(planId, reason?)
 *   kix.viewListing(listingId)             kix.leadFormView(formId)
 *   kix.addToWishlist(itemId)              kix.leadFormSubmit(formId)
 *   kix.removeFromCart(productId)          kix.scheduleDemo(datetime)
 *   kix.initiateCheckout(orderId, amt)     kix.contact(method)
 *   kix.addPaymentInfo(method)             kix.share(target, channel)
 *   kix.applyCoupon(code, valueCents?)     kix.comment(targetId)
 *   kix.purchaseSuccess(orderId, amt)      kix.like(targetId)
 *   kix.purchaseFail(orderId, reason?)     kix.follow(targetId)
 *   kix.completeRegistration(uid?)         kix.unlockAchievement(name)
 *   kix.levelUp(level)                     kix.tutorialStart(name)
 *   kix.tutorialComplete(name)             kix.gameStart(slug)
 *   kix.gameEnd(slug, score?)              kix.gameWin(slug, reward?)
 *   kix.gameLose(slug, reason?)            kix.voucherClaim(code)
 *   kix.voucherRedeem(code)                kix.donate(amountCents, cause?)
 *   kix.scheduleAppointment(datetime)      kix.checkin(locationId)
 *   kix.rate(targetId, rating)             kix.install(source?)
 *   kix.uninstall(reason?)                 kix.orderCancelled(orderId, reason?)
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

  // ── Enhanced Conversions (hashed-PII cache) ───────────────────────────
  // Merchants call kix.identifyEnhanced({email,phone,...}) once at login
  // or checkout; we hash client-side and cache. Every subsequent event
  // automatically attaches the cached hashes so the server can attribute
  // even when cookies/fingerprint are blocked.
  function cachedEnhanced() {
    var raw = lsGet('_kix_enh');
    if (!raw) return null;
    try { return JSON.parse(raw); } catch (e) { return null; }
  }
  function setEnhanced(obj) {
    try { lsSet('_kix_enh', JSON.stringify(obj || {})); }
    catch (e) { /* ignore */ }
  }
  // SHA-256 helper using SubtleCrypto when available, with a small JS
  // fallback for legacy environments. Lowercases + trims input first so
  // hashes line up with what merchants compute server-side.
  async function sha256Hex(str) {
    var s = String(str == null ? '' : str).trim().toLowerCase();
    try {
      if (window.crypto && window.crypto.subtle && window.TextEncoder) {
        var buf = await window.crypto.subtle.digest(
          'SHA-256', new TextEncoder().encode(s)
        );
        var bytes = new Uint8Array(buf);
        var hex = '';
        for (var i = 0; i < bytes.length; i++) {
          var h = bytes[i].toString(16);
          if (h.length < 2) h = '0' + h;
          hex += h;
        }
        return hex;
      }
    } catch (e) { /* fall through */ }
    // Bail rather than ship a soft-hash that desyncs from the server.
    if (window.console) console.warn(
      '[KiX Pixel] SubtleCrypto unavailable; identifyEnhanced disabled.'
    );
    return null;
  }

  // ── Unique event-id (for CAPI dedup) ──────────────────────────────────
  // Browser pixel + server CAPI for the same conversion MUST share an
  // event_id so the server can collapse them and book commission once.
  // Merchants are encouraged to pass their own; we mint one if absent.
  function mintEventId() {
    try {
      if (window.crypto && window.crypto.randomUUID) {
        return 'ev_' + window.crypto.randomUUID().replace(/-/g, '');
      }
    } catch (e) { /* ignore */ }
    return 'ev_' + Date.now().toString(36) + '_' +
      Math.random().toString(36).slice(2, 10);
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
    // Auto-attach cached Enhanced-Conversions hashes (set by
    // kix.identifyEnhanced). Per-call params win so merchants can override.
    var enh = cachedEnhanced();
    if (enh && Object.keys(enh).length) body.enhanced_data = enh;
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

    fp: function () { return FP; },

    // Mint (or echo) a dedup-friendly event_id for paired pixel/CAPI calls.
    eventId: function () { return mintEventId(); },

    // ── Enhanced Conversions (hashed PII) ──────────────────────────────
    // Async — returns the cached object. Safe to call multiple times; each
    // call merges into the cache so a login followed by a checkout can
    // accumulate (email→phone→address) without losing earlier hashes.
    identifyEnhanced: async function (userData) {
      if (!userData) return null;
      var current = cachedEnhanced() || {};
      var pairs = [
        ['email',      'email_sha256'],
        ['phone',      'phone_sha256'],
        ['first_name', 'first_name_sha256'],
        ['last_name',  'last_name_sha256']
      ];
      for (var i = 0; i < pairs.length; i++) {
        var src = pairs[i][0], dst = pairs[i][1];
        if (userData[src]) {
          var h = await sha256Hex(userData[src]);
          if (h) current[dst] = h;
        }
      }
      if (userData.address) {
        var ah = await sha256Hex(userData.address);
        if (ah) current.address_hash = ah;
      }
      if (userData.external_id) {
        current.external_id = String(userData.external_id);
      }
      setEnhanced(current);
      return current;
    },

    // Clear cached hashes (e.g. user signs out).
    clearEnhanced: function () {
      setEnhanced({});
    },

    // ── 30+ engagement / commerce / SaaS / lead / social / game events ─
    viewContent: function (contentId) {
      send('view_content', { meta: { content_id: contentId != null ? String(contentId) : null } });
    },
    search: function (query) {
      send('search', { meta: { query: query != null ? String(query) : null } });
    },
    scroll: function (depthPct) {
      send('scroll', { meta: { depth_pct: depthPct != null ? Number(depthPct) : null } });
    },
    viewVideo: function (videoId) {
      send('view_video', { meta: { video_id: videoId != null ? String(videoId) : null } });
    },
    videoComplete: function (videoId) {
      send('video_complete', { meta: { video_id: videoId != null ? String(videoId) : null } });
    },
    clickButton: function (name) {
      send('click_button', { meta: { name: name != null ? String(name) : null } });
    },
    clickLink: function (href) {
      send('click_link', { meta: { href: href != null ? String(href) : null } });
    },

    viewItem: function (itemId, valueCents) {
      send('view_item', {
        meta: {
          item_id: itemId != null ? String(itemId) : null,
          value_cents: valueCents != null ? (parseInt(valueCents, 10) || 0) : null
        }
      });
    },
    viewListing: function (listingId) {
      send('view_listing', { meta: { listing_id: listingId != null ? String(listingId) : null } });
    },
    addToWishlist: function (itemId) {
      send('add_to_wishlist', { meta: { item_id: itemId != null ? String(itemId) : null } });
    },
    removeFromCart: function (productId) {
      send('remove_from_cart', { meta: { product_id: productId != null ? String(productId) : null } });
    },
    initiateCheckout: function (orderId, amountCents) {
      send('initiate_checkout', {
        order_id: orderId != null ? String(orderId) : null,
        amount_cents: amountCents != null ? (parseInt(amountCents, 10) || 0) : null
      });
    },
    addPaymentInfo: function (method) {
      send('add_payment_info', { meta: { method: method != null ? String(method) : null } });
    },
    completeRegistration: function (uid) {
      if (uid) {
        lsSet('_kix_uid', String(uid));
        window.KIX_USER_ID = String(uid);
      }
      send('complete_registration');
    },
    applyCoupon: function (code, valueCents) {
      send('apply_coupon', {
        meta: {
          code: code != null ? String(code) : null,
          value_cents: valueCents != null ? (parseInt(valueCents, 10) || 0) : null
        }
      });
    },
    purchaseSuccess: function (orderId, amountCents, currency) {
      send('purchase_success', {
        order_id: String(orderId || ''),
        amount_cents: amountCents != null ? (parseInt(amountCents, 10) || 0) : null,
        currency: currency || 'CNY'
      });
    },
    purchaseFail: function (orderId, reason) {
      send('purchase_fail', {
        order_id: String(orderId || ''),
        meta: { reason: reason != null ? String(reason) : null }
      });
    },
    orderCancelled: function (orderId, reason) {
      send('order_cancelled', {
        order_id: String(orderId || ''),
        meta: { reason: reason != null ? String(reason) : null }
      });
    },

    // SaaS / subscription
    startTrial: function (planId) {
      send('start_trial', { meta: { plan_id: planId != null ? String(planId) : null } });
    },
    subscribe: function (planId, amountCents, currency) {
      send('subscribe', {
        amount_cents: amountCents != null ? (parseInt(amountCents, 10) || 0) : null,
        currency: currency || 'CNY',
        meta: { plan_id: planId != null ? String(planId) : null }
      });
    },
    upgrade: function (planId) {
      send('upgrade', { meta: { plan_id: planId != null ? String(planId) : null } });
    },
    downgrade: function (planId) {
      send('downgrade', { meta: { plan_id: planId != null ? String(planId) : null } });
    },
    cancelSubscription: function (planId) {
      send('cancel_subscription', { meta: { plan_id: planId != null ? String(planId) : null } });
    },
    trialEnd: function (planId) {
      send('trial_end', { meta: { plan_id: planId != null ? String(planId) : null } });
    },
    renewalSuccess: function (planId, amountCents, currency) {
      send('renewal_success', {
        amount_cents: amountCents != null ? (parseInt(amountCents, 10) || 0) : null,
        currency: currency || 'CNY',
        meta: { plan_id: planId != null ? String(planId) : null }
      });
    },
    renewalFail: function (planId, reason) {
      send('renewal_fail', { meta: {
        plan_id: planId != null ? String(planId) : null,
        reason: reason != null ? String(reason) : null
      } });
    },

    // Lead gen
    leadFormView: function (formId) {
      send('lead_form_view', { meta: { form_id: formId != null ? String(formId) : null } });
    },
    leadFormSubmit: function (formId) {
      send('lead_form_submit', { meta: { form_id: formId != null ? String(formId) : null } });
    },
    scheduleDemo: function (datetime) {
      send('schedule_demo', { meta: { datetime: datetime != null ? String(datetime) : null } });
    },
    contact: function (method) {
      send('contact', { meta: { method: method != null ? String(method) : null } });
    },

    // Social / engagement
    share: function (target, channel) {
      send('share', { meta: { target: target != null ? String(target) : null,
                              channel: channel != null ? String(channel) : null } });
    },
    comment: function (targetId) {
      send('comment', { meta: { target_id: targetId != null ? String(targetId) : null } });
    },
    like: function (targetId) {
      send('like', { meta: { target_id: targetId != null ? String(targetId) : null } });
    },
    follow: function (targetId) {
      send('follow', { meta: { target_id: targetId != null ? String(targetId) : null } });
    },
    unlockAchievement: function (name) {
      send('achievement_unlocked', { meta: { achievement_name: name != null ? String(name) : null } });
    },
    levelUp: function (level) {
      send('level_up', { meta: { level: level != null ? Number(level) : null } });
    },
    tutorialStart: function (name) {
      send('tutorial_start', { meta: { name: name != null ? String(name) : null } });
    },
    tutorialComplete: function (name) {
      send('tutorial_complete', { meta: { name: name != null ? String(name) : null } });
    },

    // Game-specific
    gameStart: function (slug) {
      send('game_start', { meta: { slug: slug != null ? String(slug) : null } });
    },
    gameEnd: function (slug, score) {
      send('game_end', { meta: {
        slug: slug != null ? String(slug) : null,
        score: score != null ? Number(score) : null
      } });
    },
    gameWin: function (slug, reward) {
      send('game_win', { meta: {
        slug: slug != null ? String(slug) : null,
        reward: reward != null ? String(reward) : null
      } });
    },
    gameLose: function (slug, reason) {
      send('game_lose', { meta: {
        slug: slug != null ? String(slug) : null,
        reason: reason != null ? String(reason) : null
      } });
    },
    voucherClaim: function (code) {
      send('voucher_claim', { meta: { code: code != null ? String(code) : null } });
    },
    voucherRedeem: function (code) {
      send('voucher_redeem', { meta: { code: code != null ? String(code) : null } });
    },

    // Misc
    donate: function (amountCents, cause, currency) {
      send('donate', {
        amount_cents: amountCents != null ? (parseInt(amountCents, 10) || 0) : null,
        currency: currency || 'CNY',
        meta: { cause: cause != null ? String(cause) : null }
      });
    },
    scheduleAppointment: function (datetime) {
      send('schedule_appointment', { meta: { datetime: datetime != null ? String(datetime) : null } });
    },
    checkin: function (locationId) {
      send('checkin', { meta: { location_id: locationId != null ? String(locationId) : null } });
    },
    rate: function (targetId, rating) {
      send('rate', { meta: {
        target_id: targetId != null ? String(targetId) : null,
        rating: rating != null ? Number(rating) : null
      } });
    },
    install: function (source) {
      send('install', { meta: { source: source != null ? String(source) : null } });
    },
    uninstall: function (reason) {
      send('uninstall', { meta: { reason: reason != null ? String(reason) : null } });
    }
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
