### KiX Platform — Arabic (Saudi Arabia) catalog
### Source-of-truth locale for SG bilingual launch (Wave 2).
### See /Users/mozat/a-docs/i18n-trinity-strategy.md for strategy.
###
### Naming convention: <router>-<semantic-action>
### ICU MessageFormat for plurals/select; otherwise plain text.


## ─────────────────────────────────────────────────────────────────────────
## Smoke-test message (from Wave 1)
## ─────────────────────────────────────────────────────────────────────────

welcome-message = [ar-SA] Welcome { $name }!
    .description =
        You have { $count ->
            [one] 1 message
           *[other] { $count } messages
        }

## ─────────────────────────────────────────────────────────────────────────
## tutorials.py — module display names (MODULE_META)
## ─────────────────────────────────────────────────────────────────────────

tutorials-module-progression = [ar-SA] Progression
tutorials-module-currency = [ar-SA] Currency
tutorials-module-item = [ar-SA] Item
tutorials-module-achievement = [ar-SA] Achievement
tutorials-module-quest = [ar-SA] Quest
tutorials-module-tier = [ar-SA] Tier
tutorials-module-event = [ar-SA] Event
tutorials-module-roulette = [ar-SA] Reward Roulette
tutorials-module-league = [ar-SA] League
tutorials-module-pass = [ar-SA] Battle Pass
tutorials-module-smartquests = [ar-SA] Smart Quests
tutorials-module-storyquest = [ar-SA] Story Quest
tutorials-module-lives = [ar-SA] Lives
tutorials-module-tourney = [ar-SA] Tournament
tutorials-module-collection = [ar-SA] Collection
tutorials-module-badgewall = [ar-SA] Badge Wall
tutorials-module-streak = [ar-SA] Streak
tutorials-module-voucher_builder = [ar-SA] Voucher Builder
tutorials-module-voucher = [ar-SA] Voucher
tutorials-module-social_graph = [ar-SA] Social Graph
tutorials-module-social_feed = [ar-SA] Social Feed
tutorials-module-auto_share = [ar-SA] Auto Share
tutorials-module-share_to_win = [ar-SA] Share to Win
tutorials-module-energy_invite = [ar-SA] Energy Invite
tutorials-module-friend_challenge = [ar-SA] Friend Challenge
tutorials-module-ladder_climb = [ar-SA] Ladder Climb
tutorials-module-streak_rescue = [ar-SA] Streak Rescue
tutorials-module-leaderboard = [ar-SA] Leaderboard
tutorials-module-network_effect = [ar-SA] Network Effect
tutorials-module-score_to_coupon = [ar-SA] Score → Coupon
tutorials-module-energy = [ar-SA] Energy
tutorials-module-upsell = [ar-SA] Upsell
tutorials-module-redemption_store = [ar-SA] Redemption Store
tutorials-module-rate_limit = [ar-SA] Rate Limit
tutorials-module-group_actions = [ar-SA] Group Actions
tutorials-module-groupbuy = [ar-SA] Group Buy
tutorials-module-atomic_group = [ar-SA] Atomic Group
tutorials-module-pricecut = [ar-SA] Price Cut
tutorials-module-coop_quest = [ar-SA] Coop Quest
tutorials-module-raid = [ar-SA] Raid
tutorials-module-squad = [ar-SA] Squad
tutorials-module-territory = [ar-SA] Territory
tutorials-module-gift_sending = [ar-SA] Gift Sending
tutorials-module-trading_post = [ar-SA] Trading Post
tutorials-module-group_reward = [ar-SA] Group Reward
tutorials-module-fcfs = [ar-SA] First-Come First-Served
tutorials-module-limited_drop = [ar-SA] Limited Drop
tutorials-module-triggers = [ar-SA] Triggers

## tutorials.py — step instruction templates

tutorials-step-intro =
    We'll walk you through setting up "{ $recipe_name }". { $module_count ->
        [one] 1 module
       *[other] { $module_count } modules
    } and { $rule_count ->
        [one] 1 rule
       *[other] { $rule_count } rules
    }.
tutorials-step-navigate-engagement = [ar-SA] Click Engagement in the sidebar to open the module marketplace
tutorials-step-navigate-vouchers = [ar-SA] Open Vouchers in the sidebar to configure voucher templates
tutorials-step-navigate-rules = [ar-SA] Open Rules in the sidebar to configure event rules
tutorials-step-enable-module = [ar-SA] Enable the { $module_name } module
tutorials-step-configure-module = [ar-SA] Configure { $module_name }: { $params_summary }
tutorials-step-create-voucher-template = [ar-SA] Create voucher template: { $template_summary }
tutorials-step-create-rule = [ar-SA] Create rule: when { $trigger_event } → { $actions_summary }
tutorials-step-test-action = [ar-SA] Let's simulate "{ $event_name }" to test the rules
tutorials-step-celebrate = [ar-SA] Done! Your "{ $recipe_name }" setup is live.

## ─────────────────────────────────────────────────────────────────────────
## conditions.py — FIX_HINTS for eligibility blockers
## ─────────────────────────────────────────────────────────────────────────

conditions-blocker-supply_exhausted = [ar-SA] This campaign's supply has been fully claimed.
conditions-blocker-budget_exhausted = [ar-SA] This campaign's budget has been fully spent.
conditions-blocker-tier_required = [ar-SA] A higher tier is required for this campaign.
conditions-blocker-first_time_only = [ar-SA] This campaign is for first-time participants only.
conditions-blocker-user_segment_excluded = [ar-SA] You are not in an eligible user segment.
conditions-blocker-user_segment_not_included = [ar-SA] You are not in an eligible user segment.
conditions-blocker-min_account_age_days = [ar-SA] Your account is too new to participate yet.
conditions-blocker-user_attribute_filter = [ar-SA] Your account does not match the required attributes.
conditions-blocker-frequency_per_user_per_day = [ar-SA] You have hit today's limit. Try again tomorrow.
conditions-blocker-frequency_per_user_per_week = [ar-SA] You have hit this week's limit.
conditions-blocker-frequency_per_user_per_month = [ar-SA] You have hit this month's limit.
conditions-blocker-frequency_per_user_total = [ar-SA] You have reached the total limit for this campaign.
conditions-blocker-frequency_global_per_day = [ar-SA] Today's global limit has been reached.
conditions-blocker-time_not_yet_started = [ar-SA] The campaign has not started yet.
conditions-blocker-time_already_ended = [ar-SA] The campaign has ended.
conditions-blocker-time_invalid_day_of_week = [ar-SA] The campaign is not open today.
conditions-blocker-time_invalid_hour = [ar-SA] The campaign is not open at this hour.
conditions-blocker-action_prerequisites_unmet = [ar-SA] Prerequisite actions have not been completed.
conditions-blocker-campaign_not_found = [ar-SA] Campaign not found.
conditions-blocker-reservation_not_found = [ar-SA] Reservation not found or expired.
conditions-blocker-reservation_already_committed = [ar-SA] Reservation has already been committed.
conditions-blocker-reservation_already_refunded = [ar-SA] Reservation has already been refunded.
conditions-blocker-reservation_expired = [ar-SA] Reservation has expired; please retry.
conditions-blocker-commit_contention = [ar-SA] High contention on commit; please retry.

## ─────────────────────────────────────────────────────────────────────────
## welcome_kit.py — printable collateral items
## ─────────────────────────────────────────────────────────────────────────

welcome_kit-item-table_stand-title = [ar-SA] Table Stand (A5, double-sided)
welcome_kit-item-table_stand-desc = [ar-SA] A5 desktop standee with QR call-to-action on both faces.
welcome_kit-item-counter_standing-title = [ar-SA] Counter Standee (A4)
welcome_kit-item-counter_standing-desc = [ar-SA] A4 upright display for the counter or reception area.
welcome_kit-item-door_sticker-title = [ar-SA] Door Sticker (150mm round)
welcome_kit-item-door_sticker-desc = [ar-SA] Static-cling door / window decal inviting passers-by to scan.
welcome_kit-item-social_poster-title = [ar-SA] Social Poster (1080×1080)
welcome_kit-item-social_poster-desc = [ar-SA] Square poster ready for Instagram, Facebook, TikTok.
welcome_kit-item-handover_kit-title = [ar-SA] Full Handover Pack
welcome_kit-item-handover_kit-desc = [ar-SA] All assets above bundled into a single HTML index.
welcome_kit-default-tagline = [ar-SA] Scan to play. Win rewards.

## ─────────────────────────────────────────────────────────────────────────
## recipe_generator.py — generator output labels
## ─────────────────────────────────────────────────────────────────────────

recipe_generator-match-found = [ar-SA] Matched recipe '{ $recipe_name }' from the library.
recipe_generator-match-score = [ar-SA] Match score { $score }; reasons: { $reasons }.
recipe_generator-summary-untitled = [ar-SA] Untitled
recipe_generator-summary-empty-modules = [ar-SA] none
recipe_generator-summary-recipe-includes =
    Recipe '{ $recipe_name }' includes { $module_count ->
        [one] 1 module
       *[other] { $module_count } modules
    }: { $module_list }, connected by { $rule_count ->
        [one] 1 rule
       *[other] { $rule_count } rules
    }.
recipe_generator-heuristic-fallback = [ar-SA] (Heuristic template) Matched related modules and default rules from keywords.
recipe_generator-default-description = [ar-SA] Invite 10 friends, unlock a free coffee voucher.

## ─────────────────────────────────────────────────────────────────────────
## modules.py — module marketplace labels (samples)
## ─────────────────────────────────────────────────────────────────────────

modules-status-active = [ar-SA] Active
modules-status-inactive = [ar-SA] Inactive
modules-status-coming_soon = [ar-SA] Coming soon
modules-action-enable = [ar-SA] Enable
modules-action-disable = [ar-SA] Disable
modules-action-configure = [ar-SA] Configure

## ─────────────────────────────────────────────────────────────────────────
## Generic API error codes (Stripe-style)
## ─────────────────────────────────────────────────────────────────────────

error-internal = [ar-SA] An internal error occurred. Please retry shortly.
error-not_found = [ar-SA] The requested resource was not found.
error-unauthorized = [ar-SA] Authentication is required.
error-forbidden = [ar-SA] You do not have permission to perform this action.
error-validation = [ar-SA] The request payload failed validation.
error-rate_limited = [ar-SA] You have exceeded the rate limit. Try again later.
error-conflict = [ar-SA] The request conflicts with the current resource state.

## ─────────────────────────────────────────────────────────────────────────
## Common UI labels (landing pages)
## ─────────────────────────────────────────────────────────────────────────

common-cta-login = [ar-SA] Login
common-cta-logout = [ar-SA] Logout
common-cta-signup = [ar-SA] Sign up
common-cta-cancel = [ar-SA] Cancel
common-cta-save = [ar-SA] Save
common-cta-confirm = [ar-SA] Confirm
common-cta-back = [ar-SA] Back
common-cta-next = [ar-SA] Next
common-cta-loading = [ar-SA] Loading…
common-nav-home = [ar-SA] Home
common-nav-portal = [ar-SA] Portal
common-nav-storefront = [ar-SA] Storefront
common-nav-play = [ar-SA] Play
common-nav-connect = [ar-SA] Connect
common-currency-sgd = [ar-SA] SGD
common-currency-cny = [ar-SA] CNY
common-currency-usd = [ar-SA] USD
