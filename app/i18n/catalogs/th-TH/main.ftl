### KiX Platform — Thai (Thailand) catalog
### Source-of-truth locale for SG bilingual launch (Wave 2).
### See /Users/mozat/a-docs/i18n-trinity-strategy.md for strategy.
###
### Naming convention: <router>-<semantic-action>
### ICU MessageFormat for plurals/select; otherwise plain text.


## ─────────────────────────────────────────────────────────────────────────
## Smoke-test message (from Wave 1)
## ─────────────────────────────────────────────────────────────────────────

welcome-message = [th-TH] Welcome { $name }!
    .description =
        You have { $count ->
            [one] 1 message
           *[other] { $count } messages
        }

## ─────────────────────────────────────────────────────────────────────────
## tutorials.py — module display names (MODULE_META)
## ─────────────────────────────────────────────────────────────────────────

tutorials-module-progression = [th-TH] Progression
tutorials-module-currency = [th-TH] Currency
tutorials-module-item = [th-TH] Item
tutorials-module-achievement = [th-TH] Achievement
tutorials-module-quest = [th-TH] Quest
tutorials-module-tier = [th-TH] Tier
tutorials-module-event = [th-TH] Event
tutorials-module-roulette = [th-TH] Reward Roulette
tutorials-module-league = [th-TH] League
tutorials-module-pass = [th-TH] Battle Pass
tutorials-module-smartquests = [th-TH] Smart Quests
tutorials-module-storyquest = [th-TH] Story Quest
tutorials-module-lives = [th-TH] Lives
tutorials-module-tourney = [th-TH] Tournament
tutorials-module-collection = [th-TH] Collection
tutorials-module-badgewall = [th-TH] Badge Wall
tutorials-module-streak = [th-TH] Streak
tutorials-module-voucher_builder = [th-TH] Voucher Builder
tutorials-module-voucher = [th-TH] Voucher
tutorials-module-social_graph = [th-TH] Social Graph
tutorials-module-social_feed = [th-TH] Social Feed
tutorials-module-auto_share = [th-TH] Auto Share
tutorials-module-share_to_win = [th-TH] Share to Win
tutorials-module-energy_invite = [th-TH] Energy Invite
tutorials-module-friend_challenge = [th-TH] Friend Challenge
tutorials-module-ladder_climb = [th-TH] Ladder Climb
tutorials-module-streak_rescue = [th-TH] Streak Rescue
tutorials-module-leaderboard = [th-TH] Leaderboard
tutorials-module-network_effect = [th-TH] Network Effect
tutorials-module-score_to_coupon = [th-TH] Score → Coupon
tutorials-module-energy = [th-TH] Energy
tutorials-module-upsell = [th-TH] Upsell
tutorials-module-redemption_store = [th-TH] Redemption Store
tutorials-module-rate_limit = [th-TH] Rate Limit
tutorials-module-group_actions = [th-TH] Group Actions
tutorials-module-groupbuy = [th-TH] Group Buy
tutorials-module-atomic_group = [th-TH] Atomic Group
tutorials-module-pricecut = [th-TH] Price Cut
tutorials-module-coop_quest = [th-TH] Coop Quest
tutorials-module-raid = [th-TH] Raid
tutorials-module-squad = [th-TH] Squad
tutorials-module-territory = [th-TH] Territory
tutorials-module-gift_sending = [th-TH] Gift Sending
tutorials-module-trading_post = [th-TH] Trading Post
tutorials-module-group_reward = [th-TH] Group Reward
tutorials-module-fcfs = [th-TH] First-Come First-Served
tutorials-module-limited_drop = [th-TH] Limited Drop
tutorials-module-triggers = [th-TH] Triggers

## tutorials.py — step instruction templates

tutorials-step-intro =
    We'll walk you through setting up "{ $recipe_name }". { $module_count ->
        [one] 1 module
       *[other] { $module_count } modules
    } and { $rule_count ->
        [one] 1 rule
       *[other] { $rule_count } rules
    }.
tutorials-step-navigate-engagement = [th-TH] Click Engagement in the sidebar to open the module marketplace
tutorials-step-navigate-vouchers = [th-TH] Open Vouchers in the sidebar to configure voucher templates
tutorials-step-navigate-rules = [th-TH] Open Rules in the sidebar to configure event rules
tutorials-step-enable-module = [th-TH] Enable the { $module_name } module
tutorials-step-configure-module = [th-TH] Configure { $module_name }: { $params_summary }
tutorials-step-create-voucher-template = [th-TH] Create voucher template: { $template_summary }
tutorials-step-create-rule = [th-TH] Create rule: when { $trigger_event } → { $actions_summary }
tutorials-step-test-action = [th-TH] Let's simulate "{ $event_name }" to test the rules
tutorials-step-celebrate = [th-TH] Done! Your "{ $recipe_name }" setup is live.

## ─────────────────────────────────────────────────────────────────────────
## conditions.py — FIX_HINTS for eligibility blockers
## ─────────────────────────────────────────────────────────────────────────

conditions-blocker-supply_exhausted = [th-TH] This campaign's supply has been fully claimed.
conditions-blocker-budget_exhausted = [th-TH] This campaign's budget has been fully spent.
conditions-blocker-tier_required = [th-TH] A higher tier is required for this campaign.
conditions-blocker-first_time_only = [th-TH] This campaign is for first-time participants only.
conditions-blocker-user_segment_excluded = [th-TH] You are not in an eligible user segment.
conditions-blocker-user_segment_not_included = [th-TH] You are not in an eligible user segment.
conditions-blocker-min_account_age_days = [th-TH] Your account is too new to participate yet.
conditions-blocker-user_attribute_filter = [th-TH] Your account does not match the required attributes.
conditions-blocker-frequency_per_user_per_day = [th-TH] You have hit today's limit. Try again tomorrow.
conditions-blocker-frequency_per_user_per_week = [th-TH] You have hit this week's limit.
conditions-blocker-frequency_per_user_per_month = [th-TH] You have hit this month's limit.
conditions-blocker-frequency_per_user_total = [th-TH] You have reached the total limit for this campaign.
conditions-blocker-frequency_global_per_day = [th-TH] Today's global limit has been reached.
conditions-blocker-time_not_yet_started = [th-TH] The campaign has not started yet.
conditions-blocker-time_already_ended = [th-TH] The campaign has ended.
conditions-blocker-time_invalid_day_of_week = [th-TH] The campaign is not open today.
conditions-blocker-time_invalid_hour = [th-TH] The campaign is not open at this hour.
conditions-blocker-action_prerequisites_unmet = [th-TH] Prerequisite actions have not been completed.
conditions-blocker-campaign_not_found = [th-TH] Campaign not found.
conditions-blocker-reservation_not_found = [th-TH] Reservation not found or expired.
conditions-blocker-reservation_already_committed = [th-TH] Reservation has already been committed.
conditions-blocker-reservation_already_refunded = [th-TH] Reservation has already been refunded.
conditions-blocker-reservation_expired = [th-TH] Reservation has expired; please retry.
conditions-blocker-commit_contention = [th-TH] High contention on commit; please retry.

## ─────────────────────────────────────────────────────────────────────────
## welcome_kit.py — printable collateral items
## ─────────────────────────────────────────────────────────────────────────

welcome_kit-item-table_stand-title = [th-TH] Table Stand (A5, double-sided)
welcome_kit-item-table_stand-desc = [th-TH] A5 desktop standee with QR call-to-action on both faces.
welcome_kit-item-counter_standing-title = [th-TH] Counter Standee (A4)
welcome_kit-item-counter_standing-desc = [th-TH] A4 upright display for the counter or reception area.
welcome_kit-item-door_sticker-title = [th-TH] Door Sticker (150mm round)
welcome_kit-item-door_sticker-desc = [th-TH] Static-cling door / window decal inviting passers-by to scan.
welcome_kit-item-social_poster-title = [th-TH] Social Poster (1080×1080)
welcome_kit-item-social_poster-desc = [th-TH] Square poster ready for Instagram, Facebook, TikTok.
welcome_kit-item-handover_kit-title = [th-TH] Full Handover Pack
welcome_kit-item-handover_kit-desc = [th-TH] All assets above bundled into a single HTML index.
welcome_kit-default-tagline = [th-TH] Scan to play. Win rewards.

## ─────────────────────────────────────────────────────────────────────────
## recipe_generator.py — generator output labels
## ─────────────────────────────────────────────────────────────────────────

recipe_generator-match-found = [th-TH] Matched recipe '{ $recipe_name }' from the library.
recipe_generator-match-score = [th-TH] Match score { $score }; reasons: { $reasons }.
recipe_generator-summary-untitled = [th-TH] Untitled
recipe_generator-summary-empty-modules = [th-TH] none
recipe_generator-summary-recipe-includes =
    Recipe '{ $recipe_name }' includes { $module_count ->
        [one] 1 module
       *[other] { $module_count } modules
    }: { $module_list }, connected by { $rule_count ->
        [one] 1 rule
       *[other] { $rule_count } rules
    }.
recipe_generator-heuristic-fallback = [th-TH] (Heuristic template) Matched related modules and default rules from keywords.
recipe_generator-default-description = [th-TH] Invite 10 friends, unlock a free coffee voucher.

## ─────────────────────────────────────────────────────────────────────────
## modules.py — module marketplace labels (samples)
## ─────────────────────────────────────────────────────────────────────────

modules-status-active = [th-TH] Active
modules-status-inactive = [th-TH] Inactive
modules-status-coming_soon = [th-TH] Coming soon
modules-action-enable = [th-TH] Enable
modules-action-disable = [th-TH] Disable
modules-action-configure = [th-TH] Configure

## ─────────────────────────────────────────────────────────────────────────
## Generic API error codes (Stripe-style)
## ─────────────────────────────────────────────────────────────────────────

error-internal = [th-TH] An internal error occurred. Please retry shortly.
error-not_found = [th-TH] The requested resource was not found.
error-unauthorized = [th-TH] Authentication is required.
error-forbidden = [th-TH] You do not have permission to perform this action.
error-validation = [th-TH] The request payload failed validation.
error-rate_limited = [th-TH] You have exceeded the rate limit. Try again later.
error-conflict = [th-TH] The request conflicts with the current resource state.

## ─────────────────────────────────────────────────────────────────────────
## Common UI labels (landing pages)
## ─────────────────────────────────────────────────────────────────────────

common-cta-login = [th-TH] Login
common-cta-logout = [th-TH] Logout
common-cta-signup = [th-TH] Sign up
common-cta-cancel = [th-TH] Cancel
common-cta-save = [th-TH] Save
common-cta-confirm = [th-TH] Confirm
common-cta-back = [th-TH] Back
common-cta-next = [th-TH] Next
common-cta-loading = [th-TH] Loading…
common-nav-home = [th-TH] Home
common-nav-portal = [th-TH] Portal
common-nav-storefront = [th-TH] Storefront
common-nav-play = [th-TH] Play
common-nav-connect = [th-TH] Connect
common-currency-sgd = [th-TH] SGD
common-currency-cny = [th-TH] CNY
common-currency-usd = [th-TH] USD
