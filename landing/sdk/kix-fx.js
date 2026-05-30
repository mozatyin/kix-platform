/* KiXFx — Wave F spec 09 prize-reveal animation library.
 *
 * Public API (each returns a Promise resolved after duration_ms):
 *   KiXFx.confetti(el, {duration_ms, colors})
 *   KiXFx.sparkle(el, {density, duration_ms})
 *   KiXFx.jackpot(el, {duration_ms})
 *   KiXFx.slotRoll(el, {final_label, duration_ms, steps})
 *
 * - CSS lives in /sdk/animations/*.css (auto-loaded lazily).
 * - Respects prefers-reduced-motion (animations no-op + resolve immediately).
 * - Brand-colour palette (Spec 03) can be passed via {colors}.
 */
(function (global) {
  'use strict';

  var CSS_BASE = '/sdk/animations/';
  var _loaded = {};
  function _ensureCss(name) {
    if (_loaded[name]) return;
    _loaded[name] = true;
    var link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = CSS_BASE + name + '.css';
    document.head.appendChild(link);
  }

  function _reducedMotion() {
    try {
      return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    } catch (e) { return false; }
  }

  function _delay(ms) {
    return new Promise(function (r) { setTimeout(r, Math.max(0, ms)); });
  }

  // ── confetti (canvas) ──────────────────────────────────────────────
  function confetti(el, opts) {
    opts = opts || {};
    var duration = opts.duration_ms || 2400;
    var colors = opts.colors || ['#ff6b6b', '#ffd93d', '#6bcf7f', '#4dabf7'];
    _ensureCss('confetti');
    if (_reducedMotion() || !el) return _delay(0);
    var canvas = document.createElement('canvas');
    canvas.className = 'kixfx-confetti-canvas';
    var rect = el.getBoundingClientRect();
    canvas.width = rect.width;
    canvas.height = rect.height;
    el.style.position = el.style.position || 'relative';
    el.appendChild(canvas);
    var ctx = canvas.getContext('2d');
    var parts = [];
    for (var i = 0; i < 80; i++) {
      parts.push({
        x: canvas.width / 2,
        y: canvas.height / 2,
        vx: (Math.random() - 0.5) * 8,
        vy: (Math.random() - 1.2) * 8,
        c: colors[i % colors.length],
        s: 4 + Math.random() * 4
      });
    }
    var start = performance.now();
    function tick(now) {
      var t = now - start;
      if (t > duration) {
        canvas.classList.add('kixfx-fading');
        setTimeout(function () { canvas.remove(); }, 600);
        return;
      }
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      parts.forEach(function (p) {
        p.x += p.vx; p.y += p.vy; p.vy += 0.18;
        ctx.fillStyle = p.c;
        ctx.fillRect(p.x, p.y, p.s, p.s);
      });
      requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
    return _delay(duration);
  }

  // ── sparkle (DOM dots) ─────────────────────────────────────────────
  function sparkle(el, opts) {
    opts = opts || {};
    var duration = opts.duration_ms || 1800;
    var density = Math.max(4, Math.min(40, opts.density || 16));
    _ensureCss('sparkle');
    if (_reducedMotion() || !el) return _delay(0);
    el.style.position = el.style.position || 'relative';
    var dots = [];
    for (var i = 0; i < density; i++) {
      var d = document.createElement('span');
      d.className = 'kixfx-sparkle kixfx-d' + (1 + (i % 3));
      d.style.left = (Math.random() * 100) + '%';
      d.style.top = (Math.random() * 100) + '%';
      d.style.animationDelay = (Math.random() * 0.8) + 's';
      el.appendChild(d);
      dots.push(d);
    }
    setTimeout(function () {
      dots.forEach(function (d) { d.remove(); });
    }, duration);
    return _delay(duration);
  }

  // ── jackpot (zoom+glow on element) ─────────────────────────────────
  function jackpot(el, opts) {
    opts = opts || {};
    var duration = opts.duration_ms || 1500;
    _ensureCss('jackpot');
    if (_reducedMotion() || !el) return _delay(0);
    el.classList.add('kixfx-jackpot');
    setTimeout(function () { el.classList.remove('kixfx-jackpot'); }, duration);
    return _delay(duration);
  }

  // ── slotRoll ───────────────────────────────────────────────────────
  function slotRoll(el, opts) {
    opts = opts || {};
    var duration = opts.duration_ms || 2000;
    var finalLabel = opts.final_label || '★';
    _ensureCss('slot-roll');
    if (_reducedMotion() || !el) {
      if (el) el.textContent = finalLabel;
      return _delay(0);
    }
    el.classList.add('kixfx-slot');
    el.innerHTML =
      '<div class="kixfx-slot-reel">' +
      '<span>A</span><span>B</span><span>C</span><span>D</span>' +
      '<span class="kixfx-slot-final"></span>' +
      '</div>';
    var finalSpan = el.querySelector('.kixfx-slot-final');
    if (finalSpan) finalSpan.textContent = finalLabel;
    setTimeout(function () { el.classList.add('kixfx-stopping'); },
               duration - 600);
    setTimeout(function () {
      el.classList.add('kixfx-done');
      el.classList.remove('kixfx-stopping');
    }, duration);
    return _delay(duration);
  }

  var KiXFx = {
    confetti: confetti,
    sparkle: sparkle,
    jackpot: jackpot,
    slotRoll: slotRoll,
    _reducedMotion: _reducedMotion,
    VERSION: '0.1.0'
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = KiXFx;
  } else {
    global.KiXFx = KiXFx;
  }
})(typeof window !== 'undefined' ? window : globalThis);
