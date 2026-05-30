### KiX Platform — Indonesian (Indonesia) catalog
### Source-of-truth locale for SG bilingual launch (Wave 2).
### See /Users/mozat/a-docs/i18n-trinity-strategy.md for strategy.
###
### Naming convention: <router>-<semantic-action>
### ICU MessageFormat for plurals/select; otherwise plain text.


## ─────────────────────────────────────────────────────────────────────────
## Smoke-test message (from Wave 1)
## ─────────────────────────────────────────────────────────────────────────

welcome-message = [id-ID] Welcome { $name }!
    .description =
        You have { $count ->
            [one] 1 message
           *[other] { $count } messages
        }

## ─────────────────────────────────────────────────────────────────────────
## tutorials.py — module display names (MODULE_META)
## ─────────────────────────────────────────────────────────────────────────

tutorials-module-progression = [id-ID] Progression
tutorials-module-currency = [id-ID] Currency
tutorials-module-item = [id-ID] Item
tutorials-module-achievement = [id-ID] Achievement
tutorials-module-quest = [id-ID] Quest
tutorials-module-tier = [id-ID] Tier
tutorials-module-event = [id-ID] Event
tutorials-module-roulette = [id-ID] Reward Roulette
tutorials-module-league = [id-ID] League
tutorials-module-pass = [id-ID] Battle Pass
tutorials-module-smartquests = [id-ID] Smart Quests
tutorials-module-storyquest = [id-ID] Story Quest
tutorials-module-lives = [id-ID] Lives
tutorials-module-tourney = [id-ID] Tournament
tutorials-module-collection = [id-ID] Collection
tutorials-module-badgewall = [id-ID] Badge Wall
tutorials-module-streak = [id-ID] Streak
tutorials-module-voucher_builder = [id-ID] Voucher Builder
tutorials-module-voucher = [id-ID] Voucher
tutorials-module-social_graph = [id-ID] Social Graph
tutorials-module-social_feed = [id-ID] Social Feed
tutorials-module-auto_share = [id-ID] Auto Share
tutorials-module-share_to_win = [id-ID] Share to Win
tutorials-module-energy_invite = [id-ID] Energy Invite
tutorials-module-friend_challenge = [id-ID] Friend Challenge
tutorials-module-ladder_climb = [id-ID] Ladder Climb
tutorials-module-streak_rescue = [id-ID] Streak Rescue
tutorials-module-leaderboard = [id-ID] Leaderboard
tutorials-module-network_effect = [id-ID] Network Effect
tutorials-module-score_to_coupon = [id-ID] Score → Coupon
tutorials-module-energy = [id-ID] Energy
tutorials-module-upsell = [id-ID] Upsell
tutorials-module-redemption_store = [id-ID] Redemption Store
tutorials-module-rate_limit = [id-ID] Rate Limit
tutorials-module-group_actions = [id-ID] Group Actions
tutorials-module-groupbuy = [id-ID] Group Buy
tutorials-module-atomic_group = [id-ID] Atomic Group
tutorials-module-pricecut = [id-ID] Price Cut
tutorials-module-coop_quest = [id-ID] Coop Quest
tutorials-module-raid = [id-ID] Raid
tutorials-module-squad = [id-ID] Squad
tutorials-module-territory = [id-ID] Territory
tutorials-module-gift_sending = [id-ID] Gift Sending
tutorials-module-trading_post = [id-ID] Trading Post
tutorials-module-group_reward = [id-ID] Group Reward
tutorials-module-fcfs = [id-ID] First-Come First-Served
tutorials-module-limited_drop = [id-ID] Limited Drop
tutorials-module-triggers = [id-ID] Triggers

## tutorials.py — step instruction templates

tutorials-step-intro =
    We'll walk you through setting up "{ $recipe_name }". { $module_count ->
        [one] 1 module
       *[other] { $module_count } modules
    } and { $rule_count ->
        [one] 1 rule
       *[other] { $rule_count } rules
    }.
tutorials-step-navigate-engagement = [id-ID] Click Engagement in the sidebar to open the module marketplace
tutorials-step-navigate-vouchers = [id-ID] Open Vouchers in the sidebar to configure voucher templates
tutorials-step-navigate-rules = [id-ID] Open Rules in the sidebar to configure event rules
tutorials-step-enable-module = [id-ID] Enable the { $module_name } module
tutorials-step-configure-module = [id-ID] Configure { $module_name }: { $params_summary }
tutorials-step-create-voucher-template = [id-ID] Create voucher template: { $template_summary }
tutorials-step-create-rule = [id-ID] Create rule: when { $trigger_event } → { $actions_summary }
tutorials-step-test-action = [id-ID] Let's simulate "{ $event_name }" to test the rules
tutorials-step-celebrate = [id-ID] Done! Your "{ $recipe_name }" setup is live.

## ─────────────────────────────────────────────────────────────────────────
## conditions.py — FIX_HINTS for eligibility blockers
## ─────────────────────────────────────────────────────────────────────────

conditions-blocker-supply_exhausted = [id-ID] This campaign's supply has been fully claimed.
conditions-blocker-budget_exhausted = [id-ID] This campaign's budget has been fully spent.
conditions-blocker-tier_required = [id-ID] A higher tier is required for this campaign.
conditions-blocker-first_time_only = [id-ID] This campaign is for first-time participants only.
conditions-blocker-user_segment_excluded = [id-ID] You are not in an eligible user segment.
conditions-blocker-user_segment_not_included = [id-ID] You are not in an eligible user segment.
conditions-blocker-min_account_age_days = [id-ID] Your account is too new to participate yet.
conditions-blocker-user_attribute_filter = [id-ID] Your account does not match the required attributes.
conditions-blocker-frequency_per_user_per_day = [id-ID] You have hit today's limit. Try again tomorrow.
conditions-blocker-frequency_per_user_per_week = [id-ID] You have hit this week's limit.
conditions-blocker-frequency_per_user_per_month = [id-ID] You have hit this month's limit.
conditions-blocker-frequency_per_user_total = [id-ID] You have reached the total limit for this campaign.
conditions-blocker-frequency_global_per_day = [id-ID] Today's global limit has been reached.
conditions-blocker-time_not_yet_started = [id-ID] The campaign has not started yet.
conditions-blocker-time_already_ended = [id-ID] The campaign has ended.
conditions-blocker-time_invalid_day_of_week = [id-ID] The campaign is not open today.
conditions-blocker-time_invalid_hour = [id-ID] The campaign is not open at this hour.
conditions-blocker-action_prerequisites_unmet = [id-ID] Prerequisite actions have not been completed.
conditions-blocker-campaign_not_found = [id-ID] Campaign not found.
conditions-blocker-reservation_not_found = [id-ID] Reservation not found or expired.
conditions-blocker-reservation_already_committed = [id-ID] Reservation has already been committed.
conditions-blocker-reservation_already_refunded = [id-ID] Reservation has already been refunded.
conditions-blocker-reservation_expired = [id-ID] Reservation has expired; please retry.
conditions-blocker-commit_contention = [id-ID] High contention on commit; please retry.

## ─────────────────────────────────────────────────────────────────────────
## welcome_kit.py — printable collateral items
## ─────────────────────────────────────────────────────────────────────────

welcome_kit-item-table_stand-title = [id-ID] Table Stand (A5, double-sided)
welcome_kit-item-table_stand-desc = [id-ID] A5 desktop standee with QR call-to-action on both faces.
welcome_kit-item-counter_standing-title = [id-ID] Counter Standee (A4)
welcome_kit-item-counter_standing-desc = [id-ID] A4 upright display for the counter or reception area.
welcome_kit-item-door_sticker-title = [id-ID] Door Sticker (150mm round)
welcome_kit-item-door_sticker-desc = [id-ID] Static-cling door / window decal inviting passers-by to scan.
welcome_kit-item-social_poster-title = [id-ID] Social Poster (1080×1080)
welcome_kit-item-social_poster-desc = [id-ID] Square poster ready for Instagram, Facebook, TikTok.
welcome_kit-item-handover_kit-title = [id-ID] Full Handover Pack
welcome_kit-item-handover_kit-desc = [id-ID] All assets above bundled into a single HTML index.
welcome_kit-default-tagline = [id-ID] Scan to play. Win rewards.

## ─────────────────────────────────────────────────────────────────────────
## recipe_generator.py — generator output labels
## ─────────────────────────────────────────────────────────────────────────

recipe_generator-match-found = [id-ID] Matched recipe '{ $recipe_name }' from the library.
recipe_generator-match-score = [id-ID] Match score { $score }; reasons: { $reasons }.
recipe_generator-summary-untitled = [id-ID] Untitled
recipe_generator-summary-empty-modules = [id-ID] none
recipe_generator-summary-recipe-includes =
    Recipe '{ $recipe_name }' includes { $module_count ->
        [one] 1 module
       *[other] { $module_count } modules
    }: { $module_list }, connected by { $rule_count ->
        [one] 1 rule
       *[other] { $rule_count } rules
    }.
recipe_generator-heuristic-fallback = [id-ID] (Heuristic template) Matched related modules and default rules from keywords.
recipe_generator-default-description = [id-ID] Invite 10 friends, unlock a free coffee voucher.

## ─────────────────────────────────────────────────────────────────────────
## modules.py — module marketplace labels (samples)
## ─────────────────────────────────────────────────────────────────────────

modules-status-active = [id-ID] Active
modules-status-inactive = [id-ID] Inactive
modules-status-coming_soon = [id-ID] Coming soon
modules-action-enable = [id-ID] Enable
modules-action-disable = [id-ID] Disable
modules-action-configure = [id-ID] Configure

## ─────────────────────────────────────────────────────────────────────────
## Generic API error codes (Stripe-style)
## ─────────────────────────────────────────────────────────────────────────

error-internal = [id-ID] An internal error occurred. Please retry shortly.
error-not_found = [id-ID] The requested resource was not found.
error-unauthorized = [id-ID] Authentication is required.
error-forbidden = [id-ID] You do not have permission to perform this action.
error-validation = [id-ID] The request payload failed validation.
error-rate_limited = [id-ID] You have exceeded the rate limit. Try again later.
error-conflict = [id-ID] The request conflicts with the current resource state.

## ─────────────────────────────────────────────────────────────────────────
## Common UI labels (landing pages)
## ─────────────────────────────────────────────────────────────────────────

common-cta-login = [id-ID] Login
common-cta-logout = [id-ID] Logout
common-cta-signup = [id-ID] Sign up
common-cta-cancel = [id-ID] Cancel
common-cta-save = [id-ID] Save
common-cta-confirm = [id-ID] Confirm
common-cta-back = [id-ID] Back
common-cta-next = [id-ID] Next
common-cta-loading = [id-ID] Loading…
common-nav-home = [id-ID] Home
common-nav-portal = [id-ID] Portal
common-nav-storefront = [id-ID] Storefront
common-nav-play = [id-ID] Play
common-nav-connect = [id-ID] Connect
common-currency-sgd = [id-ID] SGD
common-currency-cny = [id-ID] CNY
common-currency-usd = [id-ID] USD
