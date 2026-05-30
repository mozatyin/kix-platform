### KiX Platform — Vietnamese (Vietnam) catalog
### Source-of-truth locale for SG bilingual launch (Wave 2).
### See /Users/mozat/a-docs/i18n-trinity-strategy.md for strategy.
###
### Naming convention: <router>-<semantic-action>
### ICU MessageFormat for plurals/select; otherwise plain text.


## ─────────────────────────────────────────────────────────────────────────
## Smoke-test message (from Wave 1)
## ─────────────────────────────────────────────────────────────────────────

welcome-message = [vi-VN] Welcome { $name }!
    .description =
        You have { $count ->
            [one] 1 message
           *[other] { $count } messages
        }

## ─────────────────────────────────────────────────────────────────────────
## tutorials.py — module display names (MODULE_META)
## ─────────────────────────────────────────────────────────────────────────

tutorials-module-progression = [vi-VN] Progression
tutorials-module-currency = [vi-VN] Currency
tutorials-module-item = [vi-VN] Item
tutorials-module-achievement = [vi-VN] Achievement
tutorials-module-quest = [vi-VN] Quest
tutorials-module-tier = [vi-VN] Tier
tutorials-module-event = [vi-VN] Event
tutorials-module-roulette = [vi-VN] Reward Roulette
tutorials-module-league = [vi-VN] League
tutorials-module-pass = [vi-VN] Battle Pass
tutorials-module-smartquests = [vi-VN] Smart Quests
tutorials-module-storyquest = [vi-VN] Story Quest
tutorials-module-lives = [vi-VN] Lives
tutorials-module-tourney = [vi-VN] Tournament
tutorials-module-collection = [vi-VN] Collection
tutorials-module-badgewall = [vi-VN] Badge Wall
tutorials-module-streak = [vi-VN] Streak
tutorials-module-voucher_builder = [vi-VN] Voucher Builder
tutorials-module-voucher = [vi-VN] Voucher
tutorials-module-social_graph = [vi-VN] Social Graph
tutorials-module-social_feed = [vi-VN] Social Feed
tutorials-module-auto_share = [vi-VN] Auto Share
tutorials-module-share_to_win = [vi-VN] Share to Win
tutorials-module-energy_invite = [vi-VN] Energy Invite
tutorials-module-friend_challenge = [vi-VN] Friend Challenge
tutorials-module-ladder_climb = [vi-VN] Ladder Climb
tutorials-module-streak_rescue = [vi-VN] Streak Rescue
tutorials-module-leaderboard = [vi-VN] Leaderboard
tutorials-module-network_effect = [vi-VN] Network Effect
tutorials-module-score_to_coupon = [vi-VN] Score → Coupon
tutorials-module-energy = [vi-VN] Energy
tutorials-module-upsell = [vi-VN] Upsell
tutorials-module-redemption_store = [vi-VN] Redemption Store
tutorials-module-rate_limit = [vi-VN] Rate Limit
tutorials-module-group_actions = [vi-VN] Group Actions
tutorials-module-groupbuy = [vi-VN] Group Buy
tutorials-module-atomic_group = [vi-VN] Atomic Group
tutorials-module-pricecut = [vi-VN] Price Cut
tutorials-module-coop_quest = [vi-VN] Coop Quest
tutorials-module-raid = [vi-VN] Raid
tutorials-module-squad = [vi-VN] Squad
tutorials-module-territory = [vi-VN] Territory
tutorials-module-gift_sending = [vi-VN] Gift Sending
tutorials-module-trading_post = [vi-VN] Trading Post
tutorials-module-group_reward = [vi-VN] Group Reward
tutorials-module-fcfs = [vi-VN] First-Come First-Served
tutorials-module-limited_drop = [vi-VN] Limited Drop
tutorials-module-triggers = [vi-VN] Triggers

## tutorials.py — step instruction templates

tutorials-step-intro =
    We'll walk you through setting up "{ $recipe_name }". { $module_count ->
        [one] 1 module
       *[other] { $module_count } modules
    } and { $rule_count ->
        [one] 1 rule
       *[other] { $rule_count } rules
    }.
tutorials-step-navigate-engagement = [vi-VN] Click Engagement in the sidebar to open the module marketplace
tutorials-step-navigate-vouchers = [vi-VN] Open Vouchers in the sidebar to configure voucher templates
tutorials-step-navigate-rules = [vi-VN] Open Rules in the sidebar to configure event rules
tutorials-step-enable-module = [vi-VN] Enable the { $module_name } module
tutorials-step-configure-module = [vi-VN] Configure { $module_name }: { $params_summary }
tutorials-step-create-voucher-template = [vi-VN] Create voucher template: { $template_summary }
tutorials-step-create-rule = [vi-VN] Create rule: when { $trigger_event } → { $actions_summary }
tutorials-step-test-action = [vi-VN] Let's simulate "{ $event_name }" to test the rules
tutorials-step-celebrate = [vi-VN] Done! Your "{ $recipe_name }" setup is live.

## ─────────────────────────────────────────────────────────────────────────
## conditions.py — FIX_HINTS for eligibility blockers
## ─────────────────────────────────────────────────────────────────────────

conditions-blocker-supply_exhausted = [vi-VN] This campaign's supply has been fully claimed.
conditions-blocker-budget_exhausted = [vi-VN] This campaign's budget has been fully spent.
conditions-blocker-tier_required = [vi-VN] A higher tier is required for this campaign.
conditions-blocker-first_time_only = [vi-VN] This campaign is for first-time participants only.
conditions-blocker-user_segment_excluded = [vi-VN] You are not in an eligible user segment.
conditions-blocker-user_segment_not_included = [vi-VN] You are not in an eligible user segment.
conditions-blocker-min_account_age_days = [vi-VN] Your account is too new to participate yet.
conditions-blocker-user_attribute_filter = [vi-VN] Your account does not match the required attributes.
conditions-blocker-frequency_per_user_per_day = [vi-VN] You have hit today's limit. Try again tomorrow.
conditions-blocker-frequency_per_user_per_week = [vi-VN] You have hit this week's limit.
conditions-blocker-frequency_per_user_per_month = [vi-VN] You have hit this month's limit.
conditions-blocker-frequency_per_user_total = [vi-VN] You have reached the total limit for this campaign.
conditions-blocker-frequency_global_per_day = [vi-VN] Today's global limit has been reached.
conditions-blocker-time_not_yet_started = [vi-VN] The campaign has not started yet.
conditions-blocker-time_already_ended = [vi-VN] The campaign has ended.
conditions-blocker-time_invalid_day_of_week = [vi-VN] The campaign is not open today.
conditions-blocker-time_invalid_hour = [vi-VN] The campaign is not open at this hour.
conditions-blocker-action_prerequisites_unmet = [vi-VN] Prerequisite actions have not been completed.
conditions-blocker-campaign_not_found = [vi-VN] Campaign not found.
conditions-blocker-reservation_not_found = [vi-VN] Reservation not found or expired.
conditions-blocker-reservation_already_committed = [vi-VN] Reservation has already been committed.
conditions-blocker-reservation_already_refunded = [vi-VN] Reservation has already been refunded.
conditions-blocker-reservation_expired = [vi-VN] Reservation has expired; please retry.
conditions-blocker-commit_contention = [vi-VN] High contention on commit; please retry.

## ─────────────────────────────────────────────────────────────────────────
## welcome_kit.py — printable collateral items
## ─────────────────────────────────────────────────────────────────────────

welcome_kit-item-table_stand-title = [vi-VN] Table Stand (A5, double-sided)
welcome_kit-item-table_stand-desc = [vi-VN] A5 desktop standee with QR call-to-action on both faces.
welcome_kit-item-counter_standing-title = [vi-VN] Counter Standee (A4)
welcome_kit-item-counter_standing-desc = [vi-VN] A4 upright display for the counter or reception area.
welcome_kit-item-door_sticker-title = [vi-VN] Door Sticker (150mm round)
welcome_kit-item-door_sticker-desc = [vi-VN] Static-cling door / window decal inviting passers-by to scan.
welcome_kit-item-social_poster-title = [vi-VN] Social Poster (1080×1080)
welcome_kit-item-social_poster-desc = [vi-VN] Square poster ready for Instagram, Facebook, TikTok.
welcome_kit-item-handover_kit-title = [vi-VN] Full Handover Pack
welcome_kit-item-handover_kit-desc = [vi-VN] All assets above bundled into a single HTML index.
welcome_kit-default-tagline = [vi-VN] Scan to play. Win rewards.

## ─────────────────────────────────────────────────────────────────────────
## recipe_generator.py — generator output labels
## ─────────────────────────────────────────────────────────────────────────

recipe_generator-match-found = [vi-VN] Matched recipe '{ $recipe_name }' from the library.
recipe_generator-match-score = [vi-VN] Match score { $score }; reasons: { $reasons }.
recipe_generator-summary-untitled = [vi-VN] Untitled
recipe_generator-summary-empty-modules = [vi-VN] none
recipe_generator-summary-recipe-includes =
    Recipe '{ $recipe_name }' includes { $module_count ->
        [one] 1 module
       *[other] { $module_count } modules
    }: { $module_list }, connected by { $rule_count ->
        [one] 1 rule
       *[other] { $rule_count } rules
    }.
recipe_generator-heuristic-fallback = [vi-VN] (Heuristic template) Matched related modules and default rules from keywords.
recipe_generator-default-description = [vi-VN] Invite 10 friends, unlock a free coffee voucher.

## ─────────────────────────────────────────────────────────────────────────
## modules.py — module marketplace labels (samples)
## ─────────────────────────────────────────────────────────────────────────

modules-status-active = [vi-VN] Active
modules-status-inactive = [vi-VN] Inactive
modules-status-coming_soon = [vi-VN] Coming soon
modules-action-enable = [vi-VN] Enable
modules-action-disable = [vi-VN] Disable
modules-action-configure = [vi-VN] Configure

## ─────────────────────────────────────────────────────────────────────────
## Generic API error codes (Stripe-style)
## ─────────────────────────────────────────────────────────────────────────

error-internal = [vi-VN] An internal error occurred. Please retry shortly.
error-not_found = [vi-VN] The requested resource was not found.
error-unauthorized = [vi-VN] Authentication is required.
error-forbidden = [vi-VN] You do not have permission to perform this action.
error-validation = [vi-VN] The request payload failed validation.
error-rate_limited = [vi-VN] You have exceeded the rate limit. Try again later.
error-conflict = [vi-VN] The request conflicts with the current resource state.

## ─────────────────────────────────────────────────────────────────────────
## Common UI labels (landing pages)
## ─────────────────────────────────────────────────────────────────────────

common-cta-login = [vi-VN] Login
common-cta-logout = [vi-VN] Logout
common-cta-signup = [vi-VN] Sign up
common-cta-cancel = [vi-VN] Cancel
common-cta-save = [vi-VN] Save
common-cta-confirm = [vi-VN] Confirm
common-cta-back = [vi-VN] Back
common-cta-next = [vi-VN] Next
common-cta-loading = [vi-VN] Loading…
common-nav-home = [vi-VN] Home
common-nav-portal = [vi-VN] Portal
common-nav-storefront = [vi-VN] Storefront
common-nav-play = [vi-VN] Play
common-nav-connect = [vi-VN] Connect
common-currency-sgd = [vi-VN] SGD
common-currency-cny = [vi-VN] CNY
common-currency-usd = [vi-VN] USD
