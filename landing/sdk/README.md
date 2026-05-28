# KiX JavaScript SDK

Embeddable gamification widget for brand websites. Drop in 2 lines of HTML
and your customers get XP, levels, streaks, badges, daily quests, games,
and viral share — all branded as yours.

- Vanilla JS. No dependencies. No build step.
- ~25 KB unminified, ~10 KB after minify+gzip.
- Anonymous device fingerprint identity, upgradable to full login.
- Three display modes: `floating`, `inline`, `modal`.
- Pure DOM API (no `innerHTML`), scoped class names (`kix-sdk-*`).
- Brand color and name fetched from your KiX brand config on init.

---

## Install

Drop these two lines on any page where you want the widget:

```html
<script src="https://kix.app/sdk/kix.js" data-brand="brand-9c7223a6"></script>
<div id="kix-widget" data-mode="floating"></div>
```

That's it. The SDK:
1. Auto-creates an anonymous user (device fingerprint) on first visit.
2. Pulls your brand color + name.
3. Mounts a floating button in the bottom-right corner.
4. Opens a side panel on click showing the user's XP, level, streak, energy,
   active quests, and badges.

---

## Configuration (script tag data attributes)

| Attribute        | Required | Default                  | Description                                      |
|------------------|----------|--------------------------|--------------------------------------------------|
| `data-brand`     | yes      | —                        | Your KiX brand ID, e.g. `brand-9c7223a6`         |
| `data-base-url`  | no       | `https://api.kix.app`    | KiX API base URL                                 |
| `data-mode`      | no       | `floating`               | `floating` / `inline` / `modal`                  |
| `data-debug`     | no       | `false`                  | Set `"true"` to log SDK activity to console      |

### Programmatic init (instead of data attributes)

```js
KiX.init({
  brand_id: 'brand-9c7223a6',
  base_url: 'https://api.kix.app',
  mode: 'inline',
  container_id: 'my-rewards-box'
});
```

---

## Display modes

### `floating`
A 60×60 brand-colored button anchored bottom-right. Click expands a 350×500
side panel (full-screen on mobile). No layout impact on host page.

### `inline`
Mounts a compact dashboard into the target `<div>`. Use this on a "Rewards"
page or in a sidebar.

```html
<div id="kix-widget" data-mode="inline"></div>
```

### `modal`
No persistent UI. Use this when you want full control — call
`KiX.game.launch()` from your own button.

```html
<button onclick="KiX.game.launch()">Play to Earn Rewards</button>
```

---

## Public API

### `KiX.init(opts)`

Initialize the SDK. Returns a Promise that resolves when ready.

### `KiX.user`

```js
KiX.user.get();                          // → 'anon-7f3a...' or full user id
KiX.user.upgrade('google', oauthToken);  // promote anon → full user
```

User id is stored in `localStorage` under `kix_user_{brand_id}`.

### `KiX.game`

```js
KiX.game.launch();           // launches default game in modal iframe
KiX.game.launch('match3');   // launches specific game by slug
KiX.game.close();
KiX.game.on('score_update', cb);
KiX.game.on('game_end',     cb);
```

The iframe communicates back with `postMessage`:

```js
parent.postMessage({ kix_event: 'score_update', payload: { score: 120 } }, '*');
parent.postMessage({ kix_event: 'game_end',     payload: { score: 420 } }, '*');
```

`game_end` automatically awards XP proportional to score.

### `KiX.xp`

```js
KiX.xp.award(25, 'order:latte');   // → Promise<progression>
KiX.xp.get();                      // → { xp, level, to_next }
```

### `KiX.badge`

```js
KiX.badge.award('coffee_lover');
KiX.badge.list();                  // → user's badges
```

### `KiX.streak`

```js
KiX.streak.checkin();              // daily check-in (idempotent per day)
KiX.streak.get();                  // → { current, last_date }
```

### `KiX.energy`

```js
KiX.energy.get();                  // → number
KiX.energy.spend(10);              // → new balance
```

### `KiX.share`

```js
KiX.share.toWin(420).then(url => { /* share URL with score */ });
KiX.share.inviteFriend('Come play with me!');  // uses native share sheet
```

### Event bus

```js
KiX.on('ready',          info => console.log('SDK ready', info));
KiX.on('xp_award',       d => {});
KiX.on('streak_checkin', d => {});
KiX.on('badge_award',    d => {});
KiX.on('game_open',      d => {});
KiX.on('game_end',       d => {});
KiX.on('score_update',   d => {});
KiX.on('invite_sent',    d => {});
KiX.on('user_upgraded',  d => {});

KiX.off(event, cb);
KiX.emit('custom_event', { hello: 'world' });
```

---

## 5 common examples

### 1. Award XP when a customer completes an order

```js
checkout.on('purchase', function (order) {
  KiX.xp.award(order.total_cents / 10, 'purchase');
});
```

### 2. Daily check-in CTA on the home page

```html
<button onclick="KiX.streak.checkin()">Claim today's reward</button>
```

### 3. Inline mini-dashboard in a sidebar

```html
<aside>
  <h3>Your Brew Rewards</h3>
  <div id="rewards-mount" data-mode="inline"></div>
</aside>

<script src="https://kix.app/sdk/kix.js" data-brand="brand-9c7223a6"></script>
<script>
  KiX.init({
    brand_id: 'brand-9c7223a6',
    container_id: 'rewards-mount',
    mode: 'inline'
  });
</script>
```

### 4. Award a badge when user finishes onboarding

```js
form.addEventListener('submit', () => KiX.badge.award('founding_member'));
```

### 5. Share-to-win at end of a game session

```js
KiX.on('game_end', async function (e) {
  const url = await KiX.share.toWin(e.score);
  alert('You scored ' + e.score + '! Share to double XP: ' + url);
});
```

---

## Theme customization

The SDK pulls `color` and `name` from your KiX brand config
(`GET /api/v1/brands/{brand_id}`). Override locally if you want:

```js
KiX.init({
  brand_id: 'brand-9c7223a6',
  brand_color: '#6F4E37',
  brand_name:  'Brew Haven'
});
```

For deeper styling, every element uses `kix-sdk-*` class names — write CSS
with higher specificity in your own stylesheet:

```css
.kix-sdk-fab { width: 70px !important; height: 70px !important; }
.kix-sdk-panel { font-family: 'Inter', sans-serif !important; }
```

---

## API endpoints (the SDK calls)

| Method | URL                                                                          |
|--------|------------------------------------------------------------------------------|
| GET    | `/api/v1/brands/{brand_id}`                                                  |
| POST   | `/api/v1/auth/shadow`                                                        |
| POST   | `/api/v1/auth/upgrade`                                                       |
| GET    | `/api/v1/progression/user/{user_id}/progression?brand_id={brand_id}`         |
| POST   | `/api/v1/progression/award/xp`                                               |
| POST   | `/api/v1/progression/award/badge`                                            |
| POST   | `/api/v1/progression/checkin`                                                |
| POST   | `/api/v1/network/share-to-win`                                               |
| POST   | `/api/v1/game/start`                                                         |

All endpoints must accept CORS from your brand domain. The SDK handles
network failures gracefully — UI degrades to cached `localStorage` state
and console warnings (no thrown exceptions to host page).

---

## Identity model

On first visit:
1. SDK computes a stable device fingerprint (canvas + screen + lang + tz hash).
2. Calls `POST /api/v1/auth/shadow` to create an anonymous user.
3. Stores the returned `user_id` in `localStorage` under `kix_user_{brand_id}`.

Later, when the user logs in (via your existing auth or KiX hosted login):
```js
await KiX.user.upgrade('google', googleToken);
```
The anonymous progression is merged into the upgraded user account.

---

## Browser support

Chrome / Edge / Firefox / Safari — last 2 major versions. iOS 13+, Android 8+.

No polyfills required. Uses: `fetch`, `localStorage`, `addEventListener`,
ES5 + a sprinkle of ES2017 (`Object.assign`).

---

## License

MIT © 2026 KiX
