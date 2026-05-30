### KiX Platform — he-IL catalog
### Source-of-truth locale for SG bilingual launch (Wave 2).
### See /Users/mozat/a-docs/i18n-trinity-strategy.md for strategy.
###
### Naming convention: <router>-<semantic-action>
### ICU MessageFormat for plurals/select; otherwise plain text.


## ─────────────────────────────────────────────────────────────────────────
## Smoke-test message (from Wave 1)
## ─────────────────────────────────────────────────────────────────────────

welcome-message = [he-IL] Welcome { $name }!
    .description =
        You have { $count ->
            [one] 1 message
           *[other] { $count } messages
        }

## ─────────────────────────────────────────────────────────────────────────
## tutorials.py — module display names (MODULE_META)
## ─────────────────────────────────────────────────────────────────────────

tutorials-module-progression = [he-IL] Progression
tutorials-module-currency = [he-IL] Currency
tutorials-module-item = [he-IL] Item
tutorials-module-achievement = [he-IL] Achievement
tutorials-module-quest = [he-IL] Quest
tutorials-module-tier = [he-IL] Tier
tutorials-module-event = [he-IL] Event
tutorials-module-roulette = [he-IL] Reward Roulette
tutorials-module-league = [he-IL] League
tutorials-module-pass = [he-IL] Battle Pass
tutorials-module-smartquests = [he-IL] Smart Quests
tutorials-module-storyquest = [he-IL] Story Quest
tutorials-module-lives = [he-IL] Lives
tutorials-module-tourney = [he-IL] Tournament
tutorials-module-collection = [he-IL] Collection
tutorials-module-badgewall = [he-IL] Badge Wall
tutorials-module-streak = [he-IL] Streak
tutorials-module-voucher_builder = [he-IL] Voucher Builder
tutorials-module-voucher = [he-IL] Voucher
tutorials-module-social_graph = [he-IL] Social Graph
tutorials-module-social_feed = [he-IL] Social Feed
tutorials-module-auto_share = [he-IL] Auto Share
tutorials-module-share_to_win = [he-IL] Share to Win
tutorials-module-energy_invite = [he-IL] Energy Invite
tutorials-module-friend_challenge = [he-IL] Friend Challenge
tutorials-module-ladder_climb = [he-IL] Ladder Climb
tutorials-module-streak_rescue = [he-IL] Streak Rescue
tutorials-module-leaderboard = [he-IL] Leaderboard
tutorials-module-network_effect = [he-IL] Network Effect
tutorials-module-score_to_coupon = [he-IL] Score → Coupon
tutorials-module-energy = [he-IL] Energy
tutorials-module-upsell = [he-IL] Upsell
tutorials-module-redemption_store = [he-IL] Redemption Store
tutorials-module-rate_limit = [he-IL] Rate Limit
tutorials-module-group_actions = [he-IL] Group Actions
tutorials-module-groupbuy = [he-IL] Group Buy
tutorials-module-atomic_group = [he-IL] Atomic Group
tutorials-module-pricecut = [he-IL] Price Cut
tutorials-module-coop_quest = [he-IL] Coop Quest
tutorials-module-raid = [he-IL] Raid
tutorials-module-squad = [he-IL] Squad
tutorials-module-territory = [he-IL] Territory
tutorials-module-gift_sending = [he-IL] Gift Sending
tutorials-module-trading_post = [he-IL] Trading Post
tutorials-module-group_reward = [he-IL] Group Reward
tutorials-module-fcfs = [he-IL] First-Come First-Served
tutorials-module-limited_drop = [he-IL] Limited Drop
tutorials-module-triggers = [he-IL] Triggers

## tutorials.py — step instruction templates

tutorials-step-intro =
    We'll walk you through setting up "{ $recipe_name }". { $module_count ->
        [one] 1 module
       *[other] { $module_count } modules
    } and { $rule_count ->
        [one] 1 rule
       *[other] { $rule_count } rules
    }.
tutorials-step-navigate-engagement = [he-IL] Click Engagement in the sidebar to open the module marketplace
tutorials-step-navigate-vouchers = [he-IL] Open Vouchers in the sidebar to configure voucher templates
tutorials-step-navigate-rules = [he-IL] Open Rules in the sidebar to configure event rules
tutorials-step-enable-module = [he-IL] Enable the { $module_name } module
tutorials-step-configure-module = [he-IL] Configure { $module_name }: { $params_summary }
tutorials-step-create-voucher-template = [he-IL] Create voucher template: { $template_summary }
tutorials-step-create-rule = [he-IL] Create rule: when { $trigger_event } → { $actions_summary }
tutorials-step-test-action = [he-IL] Let's simulate "{ $event_name }" to test the rules
tutorials-step-celebrate = [he-IL] Done! Your "{ $recipe_name }" setup is live.

## ─────────────────────────────────────────────────────────────────────────
## conditions.py — FIX_HINTS for eligibility blockers
## ─────────────────────────────────────────────────────────────────────────

conditions-blocker-supply_exhausted = [he-IL] This campaign's supply has been fully claimed.
conditions-blocker-budget_exhausted = [he-IL] This campaign's budget has been fully spent.
conditions-blocker-tier_required = [he-IL] A higher tier is required for this campaign.
conditions-blocker-first_time_only = [he-IL] This campaign is for first-time participants only.
conditions-blocker-user_segment_excluded = [he-IL] You are not in an eligible user segment.
conditions-blocker-user_segment_not_included = [he-IL] You are not in an eligible user segment.
conditions-blocker-min_account_age_days = [he-IL] Your account is too new to participate yet.
conditions-blocker-user_attribute_filter = [he-IL] Your account does not match the required attributes.
conditions-blocker-frequency_per_user_per_day = [he-IL] You have hit today's limit. Try again tomorrow.
conditions-blocker-frequency_per_user_per_week = [he-IL] You have hit this week's limit.
conditions-blocker-frequency_per_user_per_month = [he-IL] You have hit this month's limit.
conditions-blocker-frequency_per_user_total = [he-IL] You have reached the total limit for this campaign.
conditions-blocker-frequency_global_per_day = [he-IL] Today's global limit has been reached.
conditions-blocker-time_not_yet_started = [he-IL] The campaign has not started yet.
conditions-blocker-time_already_ended = [he-IL] The campaign has ended.
conditions-blocker-time_invalid_day_of_week = [he-IL] The campaign is not open today.
conditions-blocker-time_invalid_hour = [he-IL] The campaign is not open at this hour.
conditions-blocker-action_prerequisites_unmet = [he-IL] Prerequisite actions have not been completed.
conditions-blocker-campaign_not_found = [he-IL] Campaign not found.
conditions-blocker-reservation_not_found = [he-IL] Reservation not found or expired.
conditions-blocker-reservation_already_committed = [he-IL] Reservation has already been committed.
conditions-blocker-reservation_already_refunded = [he-IL] Reservation has already been refunded.
conditions-blocker-reservation_expired = [he-IL] Reservation has expired; please retry.
conditions-blocker-commit_contention = [he-IL] High contention on commit; please retry.

## ─────────────────────────────────────────────────────────────────────────
## welcome_kit.py — printable collateral items
## ─────────────────────────────────────────────────────────────────────────

welcome_kit-item-table_stand-title = [he-IL] Table Stand (A5, double-sided)
welcome_kit-item-table_stand-desc = [he-IL] A5 desktop standee with QR call-to-action on both faces.
welcome_kit-item-counter_standing-title = [he-IL] Counter Standee (A4)
welcome_kit-item-counter_standing-desc = [he-IL] A4 upright display for the counter or reception area.
welcome_kit-item-door_sticker-title = [he-IL] Door Sticker (150mm round)
welcome_kit-item-door_sticker-desc = [he-IL] Static-cling door / window decal inviting passers-by to scan.
welcome_kit-item-social_poster-title = [he-IL] Social Poster (1080×1080)
welcome_kit-item-social_poster-desc = [he-IL] Square poster ready for Instagram, Facebook, TikTok.
welcome_kit-item-handover_kit-title = [he-IL] Full Handover Pack
welcome_kit-item-handover_kit-desc = [he-IL] All assets above bundled into a single HTML index.
welcome_kit-default-tagline = [he-IL] Scan to play. Win rewards.

## ─────────────────────────────────────────────────────────────────────────
## recipe_generator.py — generator output labels
## ─────────────────────────────────────────────────────────────────────────

recipe_generator-match-found = [he-IL] Matched recipe '{ $recipe_name }' from the library.
recipe_generator-match-score = [he-IL] Match score { $score }; reasons: { $reasons }.
recipe_generator-summary-untitled = [he-IL] Untitled
recipe_generator-summary-empty-modules = [he-IL] none
recipe_generator-summary-recipe-includes =
    Recipe '{ $recipe_name }' includes { $module_count ->
        [one] 1 module
       *[other] { $module_count } modules
    }: { $module_list }, connected by { $rule_count ->
        [one] 1 rule
       *[other] { $rule_count } rules
    }.
recipe_generator-heuristic-fallback = [he-IL] (Heuristic template) Matched related modules and default rules from keywords.
recipe_generator-default-description = [he-IL] Invite 10 friends, unlock a free coffee voucher.

## ─────────────────────────────────────────────────────────────────────────
## modules.py — module marketplace labels (samples)
## ─────────────────────────────────────────────────────────────────────────

modules-status-active = [he-IL] Active
modules-status-inactive = [he-IL] Inactive
modules-status-coming_soon = [he-IL] Coming soon
modules-action-enable = [he-IL] Enable
modules-action-disable = [he-IL] Disable
modules-action-configure = [he-IL] Configure

## ─────────────────────────────────────────────────────────────────────────
## Generic API error codes (Stripe-style)
## ─────────────────────────────────────────────────────────────────────────

error-internal = [he-IL] An internal error occurred. Please retry shortly.
error-not_found = [he-IL] The requested resource was not found.
error-unauthorized = [he-IL] Authentication is required.
error-forbidden = [he-IL] You do not have permission to perform this action.
error-validation = [he-IL] The request payload failed validation.
error-rate_limited = [he-IL] You have exceeded the rate limit. Try again later.
error-conflict = [he-IL] The request conflicts with the current resource state.

## ─────────────────────────────────────────────────────────────────────────
## Common UI labels (landing pages)
## ─────────────────────────────────────────────────────────────────────────

common-cta-login = [he-IL] Login
common-cta-logout = [he-IL] Logout
common-cta-signup = [he-IL] Sign up
common-cta-cancel = [he-IL] Cancel
common-cta-save = [he-IL] Save
common-cta-confirm = [he-IL] Confirm
common-cta-back = [he-IL] Back
common-cta-next = [he-IL] Next
common-cta-loading = [he-IL] Loading…
common-nav-home = [he-IL] Home
common-nav-portal = [he-IL] Portal
common-nav-storefront = [he-IL] Storefront
common-nav-play = [he-IL] Play
common-nav-connect = [he-IL] Connect
common-currency-sgd = [he-IL] SGD
common-currency-cny = [he-IL] CNY
common-currency-usd = [he-IL] USD
