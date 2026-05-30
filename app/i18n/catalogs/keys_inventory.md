# i18n Keys Inventory — Wave 2

Generated 2026-05-30. Master registry of all translation keys shipped in
`app/i18n/catalogs/{en-SG,zh-Hans-SG}/main.ftl`.

**Naming convention:** `<router-or-page>-<semantic-action>`

**Fallback chain:** `zh-Hans-SG → zh-Hans → zh-Hans-CN → en-US`,
`en-SG → en → en-US`.

## Coverage summary

| Bucket | Keys | en-SG | zh-Hans-SG | Source |
|---|--:|--:|--:|---|
| smoke (welcome-message) | 1 | yes | yes | Wave 1 scaffold |
| tutorials.MODULE_META | 47 | yes | yes | `app/routers/tutorials.py` |
| tutorials.INSTRUCTION_TEMPLATES | 10 | yes | yes | `app/routers/tutorials.py` |
| conditions.FIX_HINTS | 24 | yes | yes | `app/routers/conditions.py` |
| welcome_kit._ITEMS | 11 | yes | yes | `app/routers/welcome_kit.py` |
| recipe_generator | 7 | yes | yes | new |
| modules (status/action labels) | 6 | yes | yes | new |
| API error codes (Stripe-style) | 7 | yes | yes | new |
| common UI (CTA, nav, currency) | 17 | yes | yes | new |
| **Total** | **130** | 130 | 130 | — |

## Key tables

### tutorials-module-* (47 keys)
`tutorials-module-<module_id>` for each entry in `MODULE_META`. Map:
`progression, currency, item, achievement, quest, tier, event, roulette,
league, pass, smartquests, storyquest, lives, tourney, collection,
badgewall, streak, voucher_builder, voucher, social_graph, social_feed,
auto_share, share_to_win, energy_invite, friend_challenge, ladder_climb,
streak_rescue, leaderboard, network_effect, score_to_coupon, energy,
upsell, redemption_store, rate_limit, group_actions, groupbuy,
atomic_group, pricecut, coop_quest, raid, squad, territory, gift_sending,
trading_post, group_reward, fcfs, limited_drop, triggers`.

### tutorials-step-* (10 keys)
Step instruction templates. All accept ICU variables:
- `tutorials-step-intro` — `$recipe_name, $module_count, $rule_count`
- `tutorials-step-navigate-engagement|vouchers|rules` — none
- `tutorials-step-enable-module` — `$module_name`
- `tutorials-step-configure-module` — `$module_name, $params_summary`
- `tutorials-step-create-voucher-template` — `$template_summary`
- `tutorials-step-create-rule` — `$trigger_event, $actions_summary`
- `tutorials-step-test-action` — `$event_name`
- `tutorials-step-celebrate` — `$recipe_name`

### conditions-blocker-* (24 keys)
One per `FIX_HINTS` blocker code. Codes are the keys from
`app/routers/conditions.py::FIX_HINTS`. Used as Stripe-style error codes
so the client owns localisation. Example:
`conditions-blocker-supply_exhausted`.

### welcome_kit-item-* (10 keys) + 1 tagline
For each item in `_ITEMS` (`table_stand`, `counter_standing`,
`door_sticker`, `social_poster`, `handover_kit`):
- `welcome_kit-item-<key>-title`
- `welcome_kit-item-<key>-desc`

Plus `welcome_kit-default-tagline` = default scan-to-play tagline.

### recipe_generator-* (7 keys)
- `recipe_generator-match-found` — recipe library hit
- `recipe_generator-match-score` — match reasons
- `recipe_generator-summary-untitled` — placeholder
- `recipe_generator-summary-empty-modules` — empty list
- `recipe_generator-summary-recipe-includes` — full summary template
- `recipe_generator-heuristic-fallback` — fallback notice
- `recipe_generator-default-description` — sample seed copy

### error-* (7 keys)
Stripe-style API error codes: `internal, not_found, unauthorized,
forbidden, validation, rate_limited, conflict`.

### common-* (17 keys)
- `common-cta-*` — login, logout, signup, cancel, save, confirm, back, next, loading
- `common-nav-*` — home, portal, storefront, play, connect
- `common-currency-*` — sgd, cny, usd

## Resolving duplicates

The extraction CSV had several near-duplicates; we resolved by keeping
the most specific key and marking it canonical here:

| Source string | Canonical key | Notes |
|---|---|---|
| "您当前不符合参与条件" | `conditions-blocker-user_segment_excluded` | also reused for `user_segment_not_included` and `user_attribute_filter` |
| "扫码玩游戏 拿奖励！" | `welcome_kit-default-tagline` | brand-overridable |

## Stage 3 deferred (out of scope for Wave 2)

- `compliance.py` — 84 Chinese strings: **CN-jurisdiction regulatory
  data**. Keeps CN-only per Trinity strategy doc §3.2 footnote.
- `recipe_generator.py` keyword lists — these are NLU dictionaries, not
  UI strings; kept as code constants.
- `landing/games/**` — generated game content has its own per-locale
  generation pipeline (Phase 2 work).
- `landing/sdk/portal-views/*.js` — 273 admin-UI strings; deferred to
  Wave 3 (heaviest JS module bundle).
