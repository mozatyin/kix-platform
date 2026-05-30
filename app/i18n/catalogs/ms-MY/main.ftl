### KiX Platform — Malay (Malaysia) catalog
### Source-of-truth locale for SG bilingual launch (Wave 2).
### See /Users/mozat/a-docs/i18n-trinity-strategy.md for strategy.
###
### Naming convention: <router>-<semantic-action>
### ICU MessageFormat for plurals/select; otherwise plain text.


## ─────────────────────────────────────────────────────────────────────────
## Smoke-test message (from Wave 1)
## ─────────────────────────────────────────────────────────────────────────

welcome-message = [ms-MY] Welcome { $name }!
    .description =
        You have { $count ->
            [one] 1 message
           *[other] { $count } messages
        }

## ─────────────────────────────────────────────────────────────────────────
## tutorials.py — module display names (MODULE_META)
## ─────────────────────────────────────────────────────────────────────────

tutorials-module-progression = [ms-MY] Progression
tutorials-module-currency = [ms-MY] Currency
tutorials-module-item = [ms-MY] Item
tutorials-module-achievement = [ms-MY] Achievement
tutorials-module-quest = [ms-MY] Quest
tutorials-module-tier = [ms-MY] Tier
tutorials-module-event = [ms-MY] Event
tutorials-module-roulette = [ms-MY] Reward Roulette
tutorials-module-league = [ms-MY] League
tutorials-module-pass = [ms-MY] Battle Pass
tutorials-module-smartquests = [ms-MY] Smart Quests
tutorials-module-storyquest = [ms-MY] Story Quest
tutorials-module-lives = [ms-MY] Lives
tutorials-module-tourney = [ms-MY] Tournament
tutorials-module-collection = [ms-MY] Collection
tutorials-module-badgewall = [ms-MY] Badge Wall
tutorials-module-streak = [ms-MY] Streak
tutorials-module-voucher_builder = [ms-MY] Voucher Builder
tutorials-module-voucher = [ms-MY] Voucher
tutorials-module-social_graph = [ms-MY] Social Graph
tutorials-module-social_feed = [ms-MY] Social Feed
tutorials-module-auto_share = [ms-MY] Auto Share
tutorials-module-share_to_win = [ms-MY] Share to Win
tutorials-module-energy_invite = [ms-MY] Energy Invite
tutorials-module-friend_challenge = [ms-MY] Friend Challenge
tutorials-module-ladder_climb = [ms-MY] Ladder Climb
tutorials-module-streak_rescue = [ms-MY] Streak Rescue
tutorials-module-leaderboard = [ms-MY] Leaderboard
tutorials-module-network_effect = [ms-MY] Network Effect
tutorials-module-score_to_coupon = [ms-MY] Score → Coupon
tutorials-module-energy = [ms-MY] Energy
tutorials-module-upsell = [ms-MY] Upsell
tutorials-module-redemption_store = [ms-MY] Redemption Store
tutorials-module-rate_limit = [ms-MY] Rate Limit
tutorials-module-group_actions = [ms-MY] Group Actions
tutorials-module-groupbuy = [ms-MY] Group Buy
tutorials-module-atomic_group = [ms-MY] Atomic Group
tutorials-module-pricecut = [ms-MY] Price Cut
tutorials-module-coop_quest = [ms-MY] Coop Quest
tutorials-module-raid = [ms-MY] Raid
tutorials-module-squad = [ms-MY] Squad
tutorials-module-territory = [ms-MY] Territory
tutorials-module-gift_sending = [ms-MY] Gift Sending
tutorials-module-trading_post = [ms-MY] Trading Post
tutorials-module-group_reward = [ms-MY] Group Reward
tutorials-module-fcfs = [ms-MY] First-Come First-Served
tutorials-module-limited_drop = [ms-MY] Limited Drop
tutorials-module-triggers = [ms-MY] Triggers

## tutorials.py — step instruction templates

tutorials-step-intro =
    We'll walk you through setting up "{ $recipe_name }". { $module_count ->
        [one] 1 module
       *[other] { $module_count } modules
    } and { $rule_count ->
        [one] 1 rule
       *[other] { $rule_count } rules
    }.
tutorials-step-navigate-engagement = [ms-MY] Click Engagement in the sidebar to open the module marketplace
tutorials-step-navigate-vouchers = [ms-MY] Open Vouchers in the sidebar to configure voucher templates
tutorials-step-navigate-rules = [ms-MY] Open Rules in the sidebar to configure event rules
tutorials-step-enable-module = [ms-MY] Enable the { $module_name } module
tutorials-step-configure-module = [ms-MY] Configure { $module_name }: { $params_summary }
tutorials-step-create-voucher-template = [ms-MY] Create voucher template: { $template_summary }
tutorials-step-create-rule = [ms-MY] Create rule: when { $trigger_event } → { $actions_summary }
tutorials-step-test-action = [ms-MY] Let's simulate "{ $event_name }" to test the rules
tutorials-step-celebrate = [ms-MY] Done! Your "{ $recipe_name }" setup is live.

## ─────────────────────────────────────────────────────────────────────────
## conditions.py — FIX_HINTS for eligibility blockers
## ─────────────────────────────────────────────────────────────────────────

conditions-blocker-supply_exhausted = [ms-MY] This campaign's supply has been fully claimed.
conditions-blocker-budget_exhausted = [ms-MY] This campaign's budget has been fully spent.
conditions-blocker-tier_required = [ms-MY] A higher tier is required for this campaign.
conditions-blocker-first_time_only = [ms-MY] This campaign is for first-time participants only.
conditions-blocker-user_segment_excluded = [ms-MY] You are not in an eligible user segment.
conditions-blocker-user_segment_not_included = [ms-MY] You are not in an eligible user segment.
conditions-blocker-min_account_age_days = [ms-MY] Your account is too new to participate yet.
conditions-blocker-user_attribute_filter = [ms-MY] Your account does not match the required attributes.
conditions-blocker-frequency_per_user_per_day = [ms-MY] You have hit today's limit. Try again tomorrow.
conditions-blocker-frequency_per_user_per_week = [ms-MY] You have hit this week's limit.
conditions-blocker-frequency_per_user_per_month = [ms-MY] You have hit this month's limit.
conditions-blocker-frequency_per_user_total = [ms-MY] You have reached the total limit for this campaign.
conditions-blocker-frequency_global_per_day = [ms-MY] Today's global limit has been reached.
conditions-blocker-time_not_yet_started = [ms-MY] The campaign has not started yet.
conditions-blocker-time_already_ended = [ms-MY] The campaign has ended.
conditions-blocker-time_invalid_day_of_week = [ms-MY] The campaign is not open today.
conditions-blocker-time_invalid_hour = [ms-MY] The campaign is not open at this hour.
conditions-blocker-action_prerequisites_unmet = [ms-MY] Prerequisite actions have not been completed.
conditions-blocker-campaign_not_found = [ms-MY] Campaign not found.
conditions-blocker-reservation_not_found = [ms-MY] Reservation not found or expired.
conditions-blocker-reservation_already_committed = [ms-MY] Reservation has already been committed.
conditions-blocker-reservation_already_refunded = [ms-MY] Reservation has already been refunded.
conditions-blocker-reservation_expired = [ms-MY] Reservation has expired; please retry.
conditions-blocker-commit_contention = [ms-MY] High contention on commit; please retry.

## ─────────────────────────────────────────────────────────────────────────
## welcome_kit.py — printable collateral items
## ─────────────────────────────────────────────────────────────────────────

welcome_kit-item-table_stand-title = [ms-MY] Table Stand (A5, double-sided)
welcome_kit-item-table_stand-desc = [ms-MY] A5 desktop standee with QR call-to-action on both faces.
welcome_kit-item-counter_standing-title = [ms-MY] Counter Standee (A4)
welcome_kit-item-counter_standing-desc = [ms-MY] A4 upright display for the counter or reception area.
welcome_kit-item-door_sticker-title = [ms-MY] Door Sticker (150mm round)
welcome_kit-item-door_sticker-desc = [ms-MY] Static-cling door / window decal inviting passers-by to scan.
welcome_kit-item-social_poster-title = [ms-MY] Social Poster (1080×1080)
welcome_kit-item-social_poster-desc = [ms-MY] Square poster ready for Instagram, Facebook, TikTok.
welcome_kit-item-handover_kit-title = [ms-MY] Full Handover Pack
welcome_kit-item-handover_kit-desc = [ms-MY] All assets above bundled into a single HTML index.
welcome_kit-default-tagline = [ms-MY] Scan to play. Win rewards.

## ─────────────────────────────────────────────────────────────────────────
## recipe_generator.py — generator output labels
## ─────────────────────────────────────────────────────────────────────────

recipe_generator-match-found = [ms-MY] Matched recipe '{ $recipe_name }' from the library.
recipe_generator-match-score = [ms-MY] Match score { $score }; reasons: { $reasons }.
recipe_generator-summary-untitled = [ms-MY] Untitled
recipe_generator-summary-empty-modules = [ms-MY] none
recipe_generator-summary-recipe-includes =
    Recipe '{ $recipe_name }' includes { $module_count ->
        [one] 1 module
       *[other] { $module_count } modules
    }: { $module_list }, connected by { $rule_count ->
        [one] 1 rule
       *[other] { $rule_count } rules
    }.
recipe_generator-heuristic-fallback = [ms-MY] (Heuristic template) Matched related modules and default rules from keywords.
recipe_generator-default-description = [ms-MY] Invite 10 friends, unlock a free coffee voucher.

## ─────────────────────────────────────────────────────────────────────────
## modules.py — module marketplace labels (samples)
## ─────────────────────────────────────────────────────────────────────────

modules-status-active = [ms-MY] Active
modules-status-inactive = [ms-MY] Inactive
modules-status-coming_soon = [ms-MY] Coming soon
modules-action-enable = [ms-MY] Enable
modules-action-disable = [ms-MY] Disable
modules-action-configure = [ms-MY] Configure

## ─────────────────────────────────────────────────────────────────────────
## Generic API error codes (Stripe-style)
## ─────────────────────────────────────────────────────────────────────────

error-internal = [ms-MY] An internal error occurred. Please retry shortly.
error-not_found = [ms-MY] The requested resource was not found.
error-unauthorized = [ms-MY] Authentication is required.
error-forbidden = [ms-MY] You do not have permission to perform this action.
error-validation = [ms-MY] The request payload failed validation.
error-rate_limited = [ms-MY] You have exceeded the rate limit. Try again later.
error-conflict = [ms-MY] The request conflicts with the current resource state.

## ─────────────────────────────────────────────────────────────────────────
## Common UI labels (landing pages)
## ─────────────────────────────────────────────────────────────────────────

common-cta-login = [ms-MY] Login
common-cta-logout = [ms-MY] Logout
common-cta-signup = [ms-MY] Sign up
common-cta-cancel = [ms-MY] Cancel
common-cta-save = [ms-MY] Save
common-cta-confirm = [ms-MY] Confirm
common-cta-back = [ms-MY] Back
common-cta-next = [ms-MY] Next
common-cta-loading = [ms-MY] Loading…
common-nav-home = [ms-MY] Home
common-nav-portal = [ms-MY] Portal
common-nav-storefront = [ms-MY] Storefront
common-nav-play = [ms-MY] Play
common-nav-connect = [ms-MY] Connect
common-currency-sgd = [ms-MY] SGD
common-currency-cny = [ms-MY] CNY
common-currency-usd = [ms-MY] USD
