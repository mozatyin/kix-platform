### KiX Platform — ar-EG catalog
### Source-of-truth locale for SG bilingual launch (Wave 2).
### See /Users/mozat/a-docs/i18n-trinity-strategy.md for strategy.
###
### Naming convention: <router>-<semantic-action>
### ICU MessageFormat for plurals/select; otherwise plain text.


## ─────────────────────────────────────────────────────────────────────────
## Smoke-test message (from Wave 1)
## ─────────────────────────────────────────────────────────────────────────

welcome-message = [ar-EG] Welcome { $name }!
    .description =
        You have { $count ->
            [one] 1 message
           *[other] { $count } messages
        }

## ─────────────────────────────────────────────────────────────────────────
## tutorials.py — module display names (MODULE_META)
## ─────────────────────────────────────────────────────────────────────────

tutorials-module-progression = [ar-EG] Progression
tutorials-module-currency = [ar-EG] Currency
tutorials-module-item = [ar-EG] Item
tutorials-module-achievement = [ar-EG] Achievement
tutorials-module-quest = [ar-EG] Quest
tutorials-module-tier = [ar-EG] Tier
tutorials-module-event = [ar-EG] Event
tutorials-module-roulette = [ar-EG] Reward Roulette
tutorials-module-league = [ar-EG] League
tutorials-module-pass = [ar-EG] Battle Pass
tutorials-module-smartquests = [ar-EG] Smart Quests
tutorials-module-storyquest = [ar-EG] Story Quest
tutorials-module-lives = [ar-EG] Lives
tutorials-module-tourney = [ar-EG] Tournament
tutorials-module-collection = [ar-EG] Collection
tutorials-module-badgewall = [ar-EG] Badge Wall
tutorials-module-streak = [ar-EG] Streak
tutorials-module-voucher_builder = [ar-EG] Voucher Builder
tutorials-module-voucher = [ar-EG] Voucher
tutorials-module-social_graph = [ar-EG] Social Graph
tutorials-module-social_feed = [ar-EG] Social Feed
tutorials-module-auto_share = [ar-EG] Auto Share
tutorials-module-share_to_win = [ar-EG] Share to Win
tutorials-module-energy_invite = [ar-EG] Energy Invite
tutorials-module-friend_challenge = [ar-EG] Friend Challenge
tutorials-module-ladder_climb = [ar-EG] Ladder Climb
tutorials-module-streak_rescue = [ar-EG] Streak Rescue
tutorials-module-leaderboard = [ar-EG] Leaderboard
tutorials-module-network_effect = [ar-EG] Network Effect
tutorials-module-score_to_coupon = [ar-EG] Score → Coupon
tutorials-module-energy = [ar-EG] Energy
tutorials-module-upsell = [ar-EG] Upsell
tutorials-module-redemption_store = [ar-EG] Redemption Store
tutorials-module-rate_limit = [ar-EG] Rate Limit
tutorials-module-group_actions = [ar-EG] Group Actions
tutorials-module-groupbuy = [ar-EG] Group Buy
tutorials-module-atomic_group = [ar-EG] Atomic Group
tutorials-module-pricecut = [ar-EG] Price Cut
tutorials-module-coop_quest = [ar-EG] Coop Quest
tutorials-module-raid = [ar-EG] Raid
tutorials-module-squad = [ar-EG] Squad
tutorials-module-territory = [ar-EG] Territory
tutorials-module-gift_sending = [ar-EG] Gift Sending
tutorials-module-trading_post = [ar-EG] Trading Post
tutorials-module-group_reward = [ar-EG] Group Reward
tutorials-module-fcfs = [ar-EG] First-Come First-Served
tutorials-module-limited_drop = [ar-EG] Limited Drop
tutorials-module-triggers = [ar-EG] Triggers

## tutorials.py — step instruction templates

tutorials-step-intro =
    We'll walk you through setting up "{ $recipe_name }". { $module_count ->
        [one] 1 module
       *[other] { $module_count } modules
    } and { $rule_count ->
        [one] 1 rule
       *[other] { $rule_count } rules
    }.
tutorials-step-navigate-engagement = [ar-EG] Click Engagement in the sidebar to open the module marketplace
tutorials-step-navigate-vouchers = [ar-EG] Open Vouchers in the sidebar to configure voucher templates
tutorials-step-navigate-rules = [ar-EG] Open Rules in the sidebar to configure event rules
tutorials-step-enable-module = [ar-EG] Enable the { $module_name } module
tutorials-step-configure-module = [ar-EG] Configure { $module_name }: { $params_summary }
tutorials-step-create-voucher-template = [ar-EG] Create voucher template: { $template_summary }
tutorials-step-create-rule = [ar-EG] Create rule: when { $trigger_event } → { $actions_summary }
tutorials-step-test-action = [ar-EG] Let's simulate "{ $event_name }" to test the rules
tutorials-step-celebrate = [ar-EG] Done! Your "{ $recipe_name }" setup is live.

## ─────────────────────────────────────────────────────────────────────────
## conditions.py — FIX_HINTS for eligibility blockers
## ─────────────────────────────────────────────────────────────────────────

conditions-blocker-supply_exhausted = [ar-EG] This campaign's supply has been fully claimed.
conditions-blocker-budget_exhausted = [ar-EG] This campaign's budget has been fully spent.
conditions-blocker-tier_required = [ar-EG] A higher tier is required for this campaign.
conditions-blocker-first_time_only = [ar-EG] This campaign is for first-time participants only.
conditions-blocker-user_segment_excluded = [ar-EG] You are not in an eligible user segment.
conditions-blocker-user_segment_not_included = [ar-EG] You are not in an eligible user segment.
conditions-blocker-min_account_age_days = [ar-EG] Your account is too new to participate yet.
conditions-blocker-user_attribute_filter = [ar-EG] Your account does not match the required attributes.
conditions-blocker-frequency_per_user_per_day = [ar-EG] You have hit today's limit. Try again tomorrow.
conditions-blocker-frequency_per_user_per_week = [ar-EG] You have hit this week's limit.
conditions-blocker-frequency_per_user_per_month = [ar-EG] You have hit this month's limit.
conditions-blocker-frequency_per_user_total = [ar-EG] You have reached the total limit for this campaign.
conditions-blocker-frequency_global_per_day = [ar-EG] Today's global limit has been reached.
conditions-blocker-time_not_yet_started = [ar-EG] The campaign has not started yet.
conditions-blocker-time_already_ended = [ar-EG] The campaign has ended.
conditions-blocker-time_invalid_day_of_week = [ar-EG] The campaign is not open today.
conditions-blocker-time_invalid_hour = [ar-EG] The campaign is not open at this hour.
conditions-blocker-action_prerequisites_unmet = [ar-EG] Prerequisite actions have not been completed.
conditions-blocker-campaign_not_found = [ar-EG] Campaign not found.
conditions-blocker-reservation_not_found = [ar-EG] Reservation not found or expired.
conditions-blocker-reservation_already_committed = [ar-EG] Reservation has already been committed.
conditions-blocker-reservation_already_refunded = [ar-EG] Reservation has already been refunded.
conditions-blocker-reservation_expired = [ar-EG] Reservation has expired; please retry.
conditions-blocker-commit_contention = [ar-EG] High contention on commit; please retry.

## ─────────────────────────────────────────────────────────────────────────
## welcome_kit.py — printable collateral items
## ─────────────────────────────────────────────────────────────────────────

welcome_kit-item-table_stand-title = [ar-EG] Table Stand (A5, double-sided)
welcome_kit-item-table_stand-desc = [ar-EG] A5 desktop standee with QR call-to-action on both faces.
welcome_kit-item-counter_standing-title = [ar-EG] Counter Standee (A4)
welcome_kit-item-counter_standing-desc = [ar-EG] A4 upright display for the counter or reception area.
welcome_kit-item-door_sticker-title = [ar-EG] Door Sticker (150mm round)
welcome_kit-item-door_sticker-desc = [ar-EG] Static-cling door / window decal inviting passers-by to scan.
welcome_kit-item-social_poster-title = [ar-EG] Social Poster (1080×1080)
welcome_kit-item-social_poster-desc = [ar-EG] Square poster ready for Instagram, Facebook, TikTok.
welcome_kit-item-handover_kit-title = [ar-EG] Full Handover Pack
welcome_kit-item-handover_kit-desc = [ar-EG] All assets above bundled into a single HTML index.
welcome_kit-default-tagline = [ar-EG] Scan to play. Win rewards.

## ─────────────────────────────────────────────────────────────────────────
## recipe_generator.py — generator output labels
## ─────────────────────────────────────────────────────────────────────────

recipe_generator-match-found = [ar-EG] Matched recipe '{ $recipe_name }' from the library.
recipe_generator-match-score = [ar-EG] Match score { $score }; reasons: { $reasons }.
recipe_generator-summary-untitled = [ar-EG] Untitled
recipe_generator-summary-empty-modules = [ar-EG] none
recipe_generator-summary-recipe-includes =
    Recipe '{ $recipe_name }' includes { $module_count ->
        [one] 1 module
       *[other] { $module_count } modules
    }: { $module_list }, connected by { $rule_count ->
        [one] 1 rule
       *[other] { $rule_count } rules
    }.
recipe_generator-heuristic-fallback = [ar-EG] (Heuristic template) Matched related modules and default rules from keywords.
recipe_generator-default-description = [ar-EG] Invite 10 friends, unlock a free coffee voucher.

## ─────────────────────────────────────────────────────────────────────────
## modules.py — module marketplace labels (samples)
## ─────────────────────────────────────────────────────────────────────────

modules-status-active = [ar-EG] Active
modules-status-inactive = [ar-EG] Inactive
modules-status-coming_soon = [ar-EG] Coming soon
modules-action-enable = [ar-EG] Enable
modules-action-disable = [ar-EG] Disable
modules-action-configure = [ar-EG] Configure

## ─────────────────────────────────────────────────────────────────────────
## Generic API error codes (Stripe-style)
## ─────────────────────────────────────────────────────────────────────────

error-internal = [ar-EG] An internal error occurred. Please retry shortly.
error-not_found = [ar-EG] The requested resource was not found.
error-unauthorized = [ar-EG] Authentication is required.
error-forbidden = [ar-EG] You do not have permission to perform this action.
error-validation = [ar-EG] The request payload failed validation.
error-rate_limited = [ar-EG] You have exceeded the rate limit. Try again later.
error-conflict = [ar-EG] The request conflicts with the current resource state.

## ─────────────────────────────────────────────────────────────────────────
## Common UI labels (landing pages)
## ─────────────────────────────────────────────────────────────────────────

common-cta-login = [ar-EG] Login
common-cta-logout = [ar-EG] Logout
common-cta-signup = [ar-EG] Sign up
common-cta-cancel = [ar-EG] Cancel
common-cta-save = [ar-EG] Save
common-cta-confirm = [ar-EG] Confirm
common-cta-back = [ar-EG] Back
common-cta-next = [ar-EG] Next
common-cta-loading = [ar-EG] Loading…
common-nav-home = [ar-EG] Home
common-nav-portal = [ar-EG] Portal
common-nav-storefront = [ar-EG] Storefront
common-nav-play = [ar-EG] Play
common-nav-connect = [ar-EG] Connect
common-currency-sgd = [ar-EG] SGD
common-currency-cny = [ar-EG] CNY
common-currency-usd = [ar-EG] USD
