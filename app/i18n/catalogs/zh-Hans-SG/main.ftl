### KiX Platform — Simplified Chinese (Singapore) catalog (Wave 2)
### Seeded from in-source translations (tutorials/conditions/welcome_kit)
### + curated extras for recipe_generator / modules / errors / common UI.

### KiX Platform — English (Singapore) catalog
### Source-of-truth locale for SG bilingual launch (Wave 2).
### See /Users/mozat/a-docs/i18n-trinity-strategy.md for strategy.
###
### Naming convention: <router>-<semantic-action>
### ICU MessageFormat for plurals/select; otherwise plain text.

## ─────────────────────────────────────────────────────────────────────────
## Smoke-test message (from Wave 1)
## ─────────────────────────────────────────────────────────────────────────
welcome-message = 欢迎 { $name }！
    .description = 您有 { $count } 条消息

## ─────────────────────────────────────────────────────────────────────────
## tutorials.py — module display names (MODULE_META)
## ─────────────────────────────────────────────────────────────────────────
tutorials-module-progression = 成长体系
tutorials-module-currency = 代币系统
tutorials-module-item = 道具系统
tutorials-module-achievement = 成就系统
tutorials-module-quest = 任务系统
tutorials-module-tier = 等级 (Tier)
tutorials-module-event = 活动事件
tutorials-module-roulette = 抽奖轮盘
tutorials-module-league = 联赛
tutorials-module-pass = 战令通行证
tutorials-module-smartquests = 智能任务
tutorials-module-storyquest = 剧情任务
tutorials-module-lives = 生命值
tutorials-module-tourney = 锦标赛
tutorials-module-collection = 收藏册
tutorials-module-badgewall = 勋章墙
tutorials-module-streak = 连续打卡
tutorials-module-voucher_builder = 优惠券模板
tutorials-module-voucher = 优惠券
tutorials-module-social_graph = 社交图谱
tutorials-module-social_feed = 社交动态
tutorials-module-auto_share = 自动分享
tutorials-module-share_to_win = 分享得奖
tutorials-module-energy_invite = 邀请送能量
tutorials-module-friend_challenge = 好友挑战
tutorials-module-ladder_climb = 天梯攀升
tutorials-module-streak_rescue = 续命挽救
tutorials-module-leaderboard = 排行榜
tutorials-module-network_effect = 网络效应
tutorials-module-score_to_coupon = 积分换券
tutorials-module-energy = 能量系统
tutorials-module-upsell = 增值推荐
tutorials-module-redemption_store = 兑换商店
tutorials-module-rate_limit = 频率限制
tutorials-module-group_actions = 团购助力
tutorials-module-groupbuy = 拼团
tutorials-module-atomic_group = 原子团
tutorials-module-pricecut = 砍一刀
tutorials-module-coop_quest = 合作任务
tutorials-module-raid = 副本
tutorials-module-squad = 战队
tutorials-module-territory = 领地战
tutorials-module-gift_sending = 送礼
tutorials-module-trading_post = 交易所
tutorials-module-group_reward = 团体奖励
tutorials-module-fcfs = 先到先得
tutorials-module-limited_drop = 限量发放
tutorials-module-triggers = 触发器

## tutorials.py — step instruction templates
tutorials-step-intro = 我们将引导你搭建「{ $recipe_name }」。包含 { $module_count } 个模块、{ $rule_count } 条规则。
tutorials-step-navigate-engagement = 点击侧边栏的 Engagement 进入模块市场
tutorials-step-navigate-vouchers = 进入侧边栏的 Vouchers 配置优惠券模板
tutorials-step-navigate-rules = 进入侧边栏的 Rules 配置事件规则
tutorials-step-enable-module = 启用 { $module_name } 模块
tutorials-step-configure-module = 配置 { $module_name }：{ $params_summary }
tutorials-step-create-voucher-template = 创建优惠券模板：{ $template_summary }
tutorials-step-create-rule = 创建规则：当 { $trigger_event } 触发时执行 { $actions_summary }
tutorials-step-test-action = 让我们模拟一次「{ $event_name }」来测试规则
tutorials-step-celebrate = 完成！你的「{ $recipe_name }」体系已经上线 🎉

## ─────────────────────────────────────────────────────────────────────────
## conditions.py — FIX_HINTS for eligibility blockers
## ─────────────────────────────────────────────────────────────────────────
conditions-blocker-supply_exhausted = 本期奖池已发完，请关注下一期活动
conditions-blocker-budget_exhausted = 本期预算已用完，请关注下一期活动
conditions-blocker-tier_required = 需要更高等级才能参与，去升级吧
conditions-blocker-first_time_only = 本活动仅限首次参与的用户
conditions-blocker-user_segment_excluded = 您当前不符合参与条件
conditions-blocker-user_segment_not_included = 您当前不符合参与条件
conditions-blocker-min_account_age_days = 账号注册时间不足，再过几天再来吧
conditions-blocker-user_attribute_filter = 您当前不符合参与条件
conditions-blocker-frequency_per_user_per_day = 今日已参与过本活动，明日再来
conditions-blocker-frequency_per_user_per_week = 本周已参与上限，下周再来
conditions-blocker-frequency_per_user_per_month = 本月已参与上限，下月再来
conditions-blocker-frequency_per_user_total = 您已达到该活动的累计参与上限
conditions-blocker-frequency_global_per_day = 今日参与人数已达上限，明日请早
conditions-blocker-time_not_yet_started = 活动尚未开始
conditions-blocker-time_already_ended = 活动已结束
conditions-blocker-time_invalid_day_of_week = 今天不是活动开放日
conditions-blocker-time_invalid_hour = 当前不在活动开放时段
conditions-blocker-action_prerequisites_unmet = 尚未完成参与活动所需的前置任务
conditions-blocker-campaign_not_found = 找不到该活动
conditions-blocker-reservation_not_found = 预约不存在或已过期
conditions-blocker-reservation_already_committed = 该预约已确认，无法重复操作
conditions-blocker-reservation_already_refunded = 该预约已退回
conditions-blocker-reservation_expired = 预约已过期，请重新发起
conditions-blocker-commit_contention = 系统繁忙，请稍后重试

## ─────────────────────────────────────────────────────────────────────────
## welcome_kit.py — printable collateral items
## ─────────────────────────────────────────────────────────────────────────
welcome_kit-item-table_stand-title = 桌牌 (A5 双面)
welcome_kit-item-table_stand-desc = A5 桌面立牌，正反面均印有扫码引导。
welcome_kit-item-counter_standing-title = 柜台立牌 (A4)
welcome_kit-item-counter_standing-desc = A4 立式陈列，适合柜台/前台位置。
welcome_kit-item-door_sticker-title = 门贴 (150mm 圆形)
welcome_kit-item-door_sticker-desc = 门口/橱窗静电贴，提示路过用户扫码。
welcome_kit-item-social_poster-title = 社交海报 (1080×1080)
welcome_kit-item-social_poster-desc = 可直接发到朋友圈/小红书/抖音 的方形海报。
welcome_kit-item-handover_kit-title = 完整 Handover 包
welcome_kit-item-handover_kit-desc = 上述所有素材打包 (HTML 索引)。
welcome_kit-default-tagline = 扫码玩游戏 拿奖励！

## ─────────────────────────────────────────────────────────────────────────
## recipe_generator.py — generator output labels
## ─────────────────────────────────────────────────────────────────────────
recipe_generator-match-found = 已从配方库匹配现成方案 '{ $recipe_name }'。
recipe_generator-match-score = 匹配分数 { $score }，原因：{ $reasons }。
recipe_generator-summary-untitled = 未命名
recipe_generator-summary-empty-modules = 无
recipe_generator-summary-recipe-includes = 配方 '{ $recipe_name }' 包含 { $module_count } 个模块：{ $module_list }，通过 { $rule_count } 条规则连接。
recipe_generator-heuristic-fallback = （启发式模板）根据关键词匹配选择了相关模块和默认规则。
recipe_generator-default-description = 邀请10位好友，解锁免费咖啡券

## ─────────────────────────────────────────────────────────────────────────
## modules.py — module marketplace labels (samples)
## ─────────────────────────────────────────────────────────────────────────
modules-status-active = 已启用
modules-status-inactive = 未启用
modules-status-coming_soon = 即将上线
modules-action-enable = 启用
modules-action-disable = 停用
modules-action-configure = 配置

## ─────────────────────────────────────────────────────────────────────────
## Generic API error codes (Stripe-style)
## ─────────────────────────────────────────────────────────────────────────
error-internal = 服务器内部错误，请稍后重试。
error-not_found = 找不到该资源。
error-unauthorized = 需要登录。
error-forbidden = 您没有权限执行该操作。
error-validation = 请求参数校验失败。
error-rate_limited = 请求过于频繁，请稍后再试。
error-conflict = 请求与当前资源状态冲突。

## ─────────────────────────────────────────────────────────────────────────
## Common UI labels (landing pages)
## ─────────────────────────────────────────────────────────────────────────
common-cta-login = 登录
common-cta-logout = 退出
common-cta-signup = 注册
common-cta-cancel = 取消
common-cta-save = 保存
common-cta-confirm = 确认
common-cta-back = 返回
common-cta-next = 下一步
common-cta-loading = 加载中…
common-nav-home = 首页
common-nav-portal = 管理端
common-nav-storefront = 店铺
common-nav-play = 玩
common-nav-connect = 连接
common-currency-sgd = 新元
common-currency-cny = 人民币
common-currency-usd = 美元
