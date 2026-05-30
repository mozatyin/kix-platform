### KiX Platform — Indonesian (Indonesia) catalog
### Source-of-truth locale for SG bilingual launch (Wave 2).
### See /Users/mozat/a-docs/i18n-trinity-strategy.md for strategy.
###
### Naming convention: <router>-<semantic-action>
### ICU MessageFormat for plurals/select; otherwise plain text.


## ─────────────────────────────────────────────────────────────────────────
## Smoke-test message (from Wave 1)
## ─────────────────────────────────────────────────────────────────────────

welcome-message = Welcome { $name }!
    .description =
        You have { $count ->
            [one] 1 message
           *[other] { $count } messages
        }

## ─────────────────────────────────────────────────────────────────────────
## tutorials.py — module display names (MODULE_META)
## ─────────────────────────────────────────────────────────────────────────

tutorials-module-progression = Progression
tutorials-module-currency = Currency
tutorials-module-item = Item
tutorials-module-achievement = Achievement
tutorials-module-quest = Quest
tutorials-module-tier = Tier
tutorials-module-event = Event
tutorials-module-roulette = Reward Roulette
tutorials-module-league = League
tutorials-module-pass = Battle Pass
tutorials-module-smartquests = Smart Quests
tutorials-module-storyquest = Story Quest
tutorials-module-lives = Lives
tutorials-module-tourney = Tournament
tutorials-module-collection = Collection
tutorials-module-badgewall = Badge Wall
tutorials-module-streak = Streak
tutorials-module-voucher_builder = Voucher Builder
tutorials-module-voucher = Voucher
tutorials-module-social_graph = Social Graph
tutorials-module-social_feed = Social Feed
tutorials-module-auto_share = Auto Share
tutorials-module-share_to_win = Share to Win
tutorials-module-energy_invite = Energy Invite
tutorials-module-friend_challenge = Friend Challenge
tutorials-module-ladder_climb = Ladder Climb
tutorials-module-streak_rescue = Streak Rescue
tutorials-module-leaderboard = Leaderboard
tutorials-module-network_effect = Network Effect
tutorials-module-score_to_coupon = Score → Coupon
tutorials-module-energy = Energy
tutorials-module-upsell = Upsell
tutorials-module-redemption_store = Redemption Store
tutorials-module-rate_limit = Rate Limit
tutorials-module-group_actions = Group Actions
tutorials-module-groupbuy = Group Buy
tutorials-module-atomic_group = Atomic Group
tutorials-module-pricecut = Price Cut
tutorials-module-coop_quest = Coop Quest
tutorials-module-raid = Raid
tutorials-module-squad = Squad
tutorials-module-territory = Territory
tutorials-module-gift_sending = Gift Sending
tutorials-module-trading_post = Trading Post
tutorials-module-group_reward = Group Reward
tutorials-module-fcfs = First-Come First-Served
tutorials-module-limited_drop = Limited Drop
tutorials-module-triggers = Triggers

## tutorials.py — step instruction templates

tutorials-step-intro =
    We'll walk you through setting up "{ $recipe_name }". { $module_count ->
        [one] 1 module
       *[other] { $module_count } modules
    } and { $rule_count ->
        [one] 1 rule
       *[other] { $rule_count } rules
    }.
tutorials-step-navigate-engagement = Click Engagement in the sidebar to open the module marketplace
tutorials-step-navigate-vouchers = Open Vouchers in the sidebar to configure voucher templates
tutorials-step-navigate-rules = Open Rules in the sidebar to configure event rules
tutorials-step-enable-module = Enable the { $module_name } module
tutorials-step-configure-module = Configure { $module_name }: { $params_summary }
tutorials-step-create-voucher-template = Create voucher template: { $template_summary }
tutorials-step-create-rule = Create rule: when { $trigger_event } → { $actions_summary }
tutorials-step-test-action = Let's simulate "{ $event_name }" to test the rules
tutorials-step-celebrate = Done! Your "{ $recipe_name }" setup is live.

## ─────────────────────────────────────────────────────────────────────────
## conditions.py — FIX_HINTS for eligibility blockers
## ─────────────────────────────────────────────────────────────────────────

conditions-blocker-supply_exhausted = This campaign's supply has been fully claimed.
conditions-blocker-budget_exhausted = This campaign's budget has been fully spent.
conditions-blocker-tier_required = A higher tier is required for this campaign.
conditions-blocker-first_time_only = This campaign is for first-time participants only.
conditions-blocker-user_segment_excluded = You are not in an eligible user segment.
conditions-blocker-user_segment_not_included = You are not in an eligible user segment.
conditions-blocker-min_account_age_days = Your account is too new to participate yet.
conditions-blocker-user_attribute_filter = Your account does not match the required attributes.
conditions-blocker-frequency_per_user_per_day = You have hit today's limit. Try again tomorrow.
conditions-blocker-frequency_per_user_per_week = You have hit this week's limit.
conditions-blocker-frequency_per_user_per_month = You have hit this month's limit.
conditions-blocker-frequency_per_user_total = You have reached the total limit for this campaign.
conditions-blocker-frequency_global_per_day = Today's global limit has been reached.
conditions-blocker-time_not_yet_started = The campaign has not started yet.
conditions-blocker-time_already_ended = The campaign has ended.
conditions-blocker-time_invalid_day_of_week = The campaign is not open today.
conditions-blocker-time_invalid_hour = The campaign is not open at this hour.
conditions-blocker-action_prerequisites_unmet = Prerequisite actions have not been completed.
conditions-blocker-campaign_not_found = Campaign not found.
conditions-blocker-reservation_not_found = Reservation not found or expired.
conditions-blocker-reservation_already_committed = Reservation has already been committed.
conditions-blocker-reservation_already_refunded = Reservation has already been refunded.
conditions-blocker-reservation_expired = Reservation has expired; please retry.
conditions-blocker-commit_contention = High contention on commit; please retry.

## ─────────────────────────────────────────────────────────────────────────
## welcome_kit.py — printable collateral items
## ─────────────────────────────────────────────────────────────────────────

welcome_kit-item-table_stand-title = Table Stand (A5, double-sided)
welcome_kit-item-table_stand-desc = A5 desktop standee with QR call-to-action on both faces.
welcome_kit-item-counter_standing-title = Counter Standee (A4)
welcome_kit-item-counter_standing-desc = A4 upright display for the counter or reception area.
welcome_kit-item-door_sticker-title = Door Sticker (150mm round)
welcome_kit-item-door_sticker-desc = Static-cling door / window decal inviting passers-by to scan.
welcome_kit-item-social_poster-title = Social Poster (1080×1080)
welcome_kit-item-social_poster-desc = Square poster ready for Instagram, Facebook, TikTok.
welcome_kit-item-handover_kit-title = Full Handover Pack
welcome_kit-item-handover_kit-desc = All assets above bundled into a single HTML index.
welcome_kit-default-tagline = Scan to play. Win rewards.

## ─────────────────────────────────────────────────────────────────────────
## recipe_generator.py — generator output labels
## ─────────────────────────────────────────────────────────────────────────

recipe_generator-match-found = Matched recipe '{ $recipe_name }' from the library.
recipe_generator-match-score = Match score { $score }; reasons: { $reasons }.
recipe_generator-summary-untitled = Untitled
recipe_generator-summary-empty-modules = none
recipe_generator-summary-recipe-includes =
    Recipe '{ $recipe_name }' includes { $module_count ->
        [one] 1 module
       *[other] { $module_count } modules
    }: { $module_list }, connected by { $rule_count ->
        [one] 1 rule
       *[other] { $rule_count } rules
    }.
recipe_generator-heuristic-fallback = (Heuristic template) Matched related modules and default rules from keywords.
recipe_generator-default-description = Invite 10 friends, unlock a free coffee voucher.

## ─────────────────────────────────────────────────────────────────────────
## modules.py — module marketplace labels (samples)
## ─────────────────────────────────────────────────────────────────────────

modules-status-active = Active
modules-status-inactive = Inactive
modules-status-coming_soon = Coming soon
modules-action-enable = Enable
modules-action-disable = Disable
modules-action-configure = Configure

## ─────────────────────────────────────────────────────────────────────────
## Generic API error codes (Stripe-style)
## ─────────────────────────────────────────────────────────────────────────

error-internal = An internal error occurred. Please retry shortly.
error-not_found = The requested resource was not found.
error-unauthorized = Authentication is required.
error-forbidden = You do not have permission to perform this action.
error-validation = The request payload failed validation.
error-rate_limited = You have exceeded the rate limit. Try again later.
error-conflict = The request conflicts with the current resource state.

## ─────────────────────────────────────────────────────────────────────────
## Common UI labels (landing pages)
## ─────────────────────────────────────────────────────────────────────────

common-cta-login = Login
common-cta-logout = Logout
common-cta-signup = Sign up
common-cta-cancel = Cancel
common-cta-save = Save
common-cta-confirm = Confirm
common-cta-back = Back
common-cta-next = Next
common-cta-loading = Loading…
common-nav-home = Home
common-nav-portal = Portal
common-nav-storefront = Storefront
common-nav-play = Play
common-nav-connect = Connect
common-currency-sgd = SGD
common-currency-cny = CNY
common-currency-usd = USD
