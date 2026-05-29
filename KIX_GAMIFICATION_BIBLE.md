# 📖 KIX GAMIFICATION BIBLE
## 唯一权威文档 · Single Source of Truth

> 「软件免费送，网络收费。」 — KiX 唯一商业模式
>
> 团队任何争议以此文档为准。其他文档为细化补充。

**最后更新**: 2026-05-29 · **平台规模**: 726 routes / 38 routers / 79 Recipe / 18 商家行业模拟 / 11 轮 Trinity 迭代

---

# 目录

- [第一卷 · 为什么 (Why)](#第一卷--为什么-why)
- [第二卷 · 系统怎么跑 (How)](#第二卷--系统怎么跑-how)
- [第三卷 · 平台是什么 (What)](#第三卷--平台是什么-what)
- [第四卷 · 合规与安全](#第四卷--合规与安全)
- [第五卷 · 接入指南](#第五卷--接入指南)
- [第六卷 · 战绩](#第六卷--战绩)
- [第七卷 · 路线图](#第七卷--路线图)
- [附录](#附录)

---

# 第一卷 · 为什么 (Why)

## 1.1 一句话亮点

> **KiX = TikTok Ads for Gamification。商家管理自己用户全免费，想要别人的用户就来出价，KiX 算法撮合。**

软件免费 · 拍卖收费 · 单边合约 · 商家之间永远互不可见

## 1.2 AI 让软件不值钱的时代

```
软件 marginal cost (AI 时代) → 0  →  价格趋零
网络价值 = N² × 0  →  指数级
```

**结论**：卖软件 = 慢性自杀。卖网络 = 唯一护城河。

### 业界对比

| 类型 | 代表 | AI 时代展望 |
|------|------|------------|
| 卖软件 SaaS | Salesforce, HubSpot | ⚠️ AI 复刻威胁严重 |
| 卖订阅 | Shopify, Notion | ⚠️ 商家自建 AI 工具替代 |
| **卖连接** | VISA, 美团, 抖音 | ✅ AI 无法复刻网络效应 |
| **卖广告** | Google, Meta, TikTok | ✅ AI 优化精度更高 |

KiX 选 **卖连接 + 卖广告** 复合模式。

## 1.3 商业模式 (TikTok Ads + 订阅 双护城河)

### 单边合约结构

```
       ┌──────────┐
       │   KiX    │  ← 唯一合同对手方
       └──────────┘
        ↑↑↑↑↑↑↑↑↑
   ┌────┘│││││││└────┐
   │     │││││││     │
 商家A 商家B 商家C ... 商家N
   ▲     ▲     ▲       ▲
   └──── 商家彼此永远互不可见 ────┘
```

每个商家只有一条线 → 通向 KiX。商家之间**没有任何合约、知情或感知**。

### 双重收入护城河

```
收入 1 · 广告（拍卖驱动）
  ├── CPA  每个新注册用户 ¥10-100
  ├── CPS  每笔订单分成 5-15%
  ├── CPM  每千次曝光 ¥10-50
  ├── CPV  每次到店访问 ¥1-10
  └── CPE  每次游戏完成 ¥0.5-5
  
  → KiX 抽 take_rate 30-70% per dollar

收入 2 · Premium 订阅（ARR）
  ├── FREE        ¥0    1 game
  ├── STARTER     ¥199/月  3 games + A/B 测试
  ├── GROWTH      ¥999/月  10 games + 自定义品牌
  └── ENTERPRISE  ¥5000/月 无限 + 白标 + SLA
  
  → 前 3 个月免费（Apple Music 策略）
```

### 财务推算

| 阶段 | 商家数 | 年化营收 |
|------|--------|---------|
| 启动期 | 100 | ¥960K |
| 成长期 | 1,000 | ¥38M |
| 网络效应起飞 | 10,000 | ¥1.2-2.4B |

### 单商家 LTV

```
Premium ARR ¥999 × 70% 续费率 × 3 年 = ¥2,100
广告支出 ¥2000/月 × 12 × take_rate 30% = ¥7,200
总 LTV ≈ ¥9,300 / 商家

CAC（销售+物流+客服）≈ ¥200-500
ROI ≈ 18-46x
```

---

# 第二卷 · 系统怎么跑 (How)

## 2.1 三角色架构

```
┌─────────────────────────────────────────────────────────┐
│  普通用户（玩家）                                         │
│  · 在 KiX App 里玩游戏 / 拿优惠 / 跨品牌发现              │
│  · 唯一身份：kid_xxxxxxx                                 │
└─────────────────────────────────────────────────────────┘
                              ↑↓
┌─────────────────────────────────────────────────────────┐
│  KiX 平台（中间运营方）                                   │
│  · 38+ routers / 726+ endpoints                         │
│  · 全免费工具 + 拍卖撮合 + 网络效应                       │
└─────────────────────────────────────────────────────────┘
                              ↑↓
┌─────────────────────────────────────────────────────────┐
│  商家 (Brand)                                            │
│  · 老王奶茶 / 老张餐厅 / 老黄电商 / ...                   │
│  · 永远只跟 KiX 沟通，从不跟其他商家见面                  │
└─────────────────────────────────────────────────────────┘
```

## 2.2 用户旅程（懒-到-上钩三步漏斗）

### ❌ 错误流程
```
扫码 → 注册 KiX → 下载 App → 玩游戏 → 拿奖
        ↑
   80% 在这里离开
```

### ✅ 正确流程

**第 1 步 · 玩（零摩擦）**
```
扫 QR → 直接打开 H5 游戏 → 后台用 device_fingerprint 创建匿名 kid
游戏立刻开始 → 不需要任何用户信息
```

**第 2 步 · 上钩（赢了才动心）**
```
赢了 → 弹窗：
  🎁 你赢得了 "南洋茶饮 中杯免费券" 价值 ¥18
  [立即收藏 →]   [先看看]
```

**第 3 步 · 收藏（必须注册）**
```
点收藏 → "请输入手机号保存到 KiX 钱包"
匿名 kid 升级为实名 kid（journey/voucher 全部继承）
建议下载 KiX App 永久保存
```

### 漏斗数据预期

| 步骤 | 漏斗 | 累计 |
|------|------|------|
| 看到 QR | 100% | 100% |
| 扫码进入 | 60% | 60% |
| 完成一局游戏 | 70% | 42% |
| 赢取奖励 | 50% | 21% |
| 点击「收藏」 | 70% | 14.7% |
| 输入手机号注册 | 80% | 11.8% |
| 下载 KiX App | 30% | 3.5% |

## 2.3 商家旅程（7 步 + 3 个月试用）

```
1. 销售触达 / 自然发现
   ↓
2. 注册（认证）+ 留信用卡（第一年免费但必须有）
   ↓
3. 创建游戏（FREE 1 个 / Premium N 个）
   ↓
4. 收到欢迎礼包（实体 QR 桌牌寄到门店）
   ↓
5. 看到每日扫码 + 转化（爱上 KiX 多巴胺）
   ↓
6. 升级到 Premium（前 3 个月免费 / Apple Music 策略）
   ↓
7. 启动广告活动（拉新 / 跨店带客 / 高峰拉量）
```

### 3 个月试用为什么是黄金窗口

```
1 个月  → 商家还没看到趋势 → 投诉
3 个月  → 90 天 dashboard 数据 + QR 物料已发完 + 团队习惯
       → 切换成本 > 继续付费成本 → 90% 自然续费
6 个月  → 商家警觉性下降，但 KiX 现金流晚
1 年    → 商家把 KiX 当年度费用，期满 40% 流失
```

### 商家心理钩子

- **欢迎礼包**：实体 QR 桌牌 = 「这东西是真的」
- **每日 dashboard**：「今天又有 12 人扫了你的码」= 多巴胺
- **CAC saved**：「已替你省了 ¥2,340」= 数字胜利
- **第 2 个游戏触发升级**：自然产生付费意愿

## 2.4 KiX 平台内部（核心机制）

### Quality-Adjusted Vickrey GSP 拍卖

```
rank = max_bid × quality_score × pacing_factor
winner = argmax(rank)
charge = min(ceil(runner_up_rank / winner_qs) + 1, max_bid)
```

- **Vickrey 二价**：出真实价值 = 最优策略（商家不需博弈）
- **Quality Score** = 0.3 + min(CTR×8, 0.4) + min(CVR×6, 0.3)
- **Pacing**：每天 50% 时间应花 50% 预算，超支降权 0.3

### 归因系统（7 天 last-touch，可配 1-365 天）

```
用户旅程: [事件1, 事件2, ..., 事件N]
对每个 conversion：从最新往回找，
找第一个 source_brand != target_brand 的事件
→ 那就是引流方 → 自动佣金分账
```

可配窗口：医疗 365 天 / 旅游 210 天 / 房产 180 天 / 餐饮 7 天 / 闪购 1 天

### 数据流（一笔交易完整链路）

```
1. 用户在老王店玩游戏
2. POST /attribution/track/event → user:{kid}:journey 累积
3. 用户去老张消费
4. POST /pixel/event {type=purchase, amount=200}
5. 自动归因引擎找到老王（last-touch within 7d）
6. wallet.charge(老张, 10) → KiX ¥3 + payouts.inter-brand-transfer(老张→老王 ¥7)
7. Trust score / Push history / Audience 全自动更新
```

## 2.5 5 种出价策略（同一拍卖引擎）

| 出价 | 何时扣费 | 用途 | 参考价 |
|------|---------|------|--------|
| **CPA** | 转化时（新注册） | 拉新主流 | ¥10-100 |
| **CPS** | 转化时（订单比例） | 跨店带单 | 5-15% |
| **CPM** | 每千次曝光 | 知名度 | ¥10-50 |
| **CPV** | 每次到店 | LBS 引流 | ¥1-10 |
| **CPE** | 每次游戏完成 | 互动深度 | ¥0.5-5 |

---

# 第三卷 · 平台是什么 (What)

## 3.1 5 层架构图

```
┌────────────────────────────────────────────────────────────────────┐
│ Layer 5 · 商业化         attribution + auction + wallet + campaigns │
│                          payouts + fx + compliance + disputes        │
│                          audiences + frequency_cap + partnerships    │
│                          creative_gen + storefront + reservations    │
│                          transactions + fraud + brand_subscriptions  │
│                          payment_methods + dashboards + welcome_kit  │
├────────────────────────────────────────────────────────────────────┤
│ Layer 4 · 商家入口       portal.html (Ads Manager) +                │
│                          storefront.html (公开品牌页) +              │
│                          api-docs/ (Swagger UI + 21 tags)            │
├────────────────────────────────────────────────────────────────────┤
│ Layer 3 · 网络层         push_engine + master_accounts +            │
│                          kix_id + listings + media + accounts +     │
│                          subscriptions + user_wallet + deposits +   │
│                          pricing                                     │
├────────────────────────────────────────────────────────────────────┤
│ Layer 2 · Gamification   progression + primitives + modules +       │
│                          network_effect + commerce_loop +           │
│                          multiplayer + social + p2p +               │
│                          group_actions + conditions +               │
│                          voucher_builder + rule_engine +            │
│                          brand_modules + recipes +                  │
│                          recipe_generator + tutorials +             │
│                          vouchers + entities                        │
├────────────────────────────────────────────────────────────────────┤
│ Layer 1 · 基础设施        FastAPI + Redis (50+ key 范式) +           │
│                          PostgreSQL + Redis Streams (事件总线) +    │
│                          ELTM AI 游戏生成器                          │
└────────────────────────────────────────────────────────────────────┘
```

## 3.2 38 routers / 726 endpoints 清单

### Gamification 核心（FREE 模式 — 永久免费）

| 模块 | 端点 | 用途 |
|------|------|------|
| `progression.py` | 6 | XP / Level / Badge / Streak / Daily Check-in |
| `primitives.py` | 60+ | Currency / Item / Achievement / Quest / Tier / Event / 时序属性 / 关系 / 实体 / 身份合并 |
| `modules.py` | 36 | 10 个组合式顶层模块 |
| `network_effect.py` | 11 | 6 种病毒触发器 |
| `commerce_loop.py` | 14 | 5 个商业模块 |
| `triggers.py` | 18 | UserAttr / RateLimit / LimitedDrop / Perk / FCFS + 事件 trigger |
| `multiplayer.py` | 17 | CoopQuest / Raid / Squad / Territory |
| `social.py` | 16 | Friends / Following / Feed / Kudos |
| `p2p.py` | 11 | Gift / Trade |
| `group_actions.py` | 13 | GroupBuy / Atomic / PriceCut |
| `voucher_builder.py` | 7 | Conditional voucher templates |
| `rule_engine.py` | 15+ | WHEN-THEN + v2 attr-watch + recipient indirection |
| `brand_modules.py` | 4 | Merchant 模块开关 |
| `conditions.py` | 9 | 通用条件引擎 |
| `recipes.py` | 11 | **79 个 Recipe** / 26 行业 |
| `recipe_generator.py` | 7 | NL → Recipe AI 转换 |
| `tutorials.py` | 8 | Recipe → Tutorial 引导 |
| `vouchers.py` | 16 | 发券/转赠/兑换/批量/关系条件 |
| `entities.py`（在 primitives）| 6 | 非人类实体（pet/property/vehicle）|

### 广告平台（PAID 模式 — 拍卖驱动）

| 模块 | 端点 | 用途 |
|------|------|------|
| `attribution.py` | 30+ | 7天 last-touch + 多触点 + take rate ladder + view-through + cohort + 共同归因 |
| `wallet.py` | 18 | 商家充值 + 原子扣款 + 日预算 + FX + take-rate + 反向佣金 |
| `campaigns.py` | 21 | Campaign + AdGroup + Review + Quality Score + target_audience |
| `auction.py` | 12 | GSP Vickrey + 保留价 + Pacing + Smart Bidding + 排除现有客户 |
| `geofence.py` | 9 | Redis GEO + LBS push + 11 占位符插值 |

### 标准广告平台辅件（TikTok / Google 同结构）

| 模块 | 端点 | 用途 |
|------|------|------|
| `frequency_cap.py` | 8 | 用户曝光封顶 + tier override + priority bypass |
| `consent.py` | 14 | GDPR/PIPL + 15 scope + 双录 + 文档签名 |
| `pixel.py` | 7 | JS SDK + 批量事件 + WeChat 兼容 + refund |
| `disputes.py` | 8 | 商家投诉 + 退款 + 归因回滚 |
| `audiences.py` | 12 | Custom + Lookalike + 时序/生命周期/属性过滤 |
| `master_accounts.py` | 30+ | 多店 master + RBAC + 跨店报表 + tier portability |
| `payouts.py` | 16 | 商家提现 + 银行账户 + 自动结算 + 双账分录 + 跨品牌 transfer |
| `creative_gen.py` | 10 | ELTM AI 创意生成 + A/B 测试 |
| `storefront.py` | 9 | 公开品牌主页 + 关注 + 评价 |
| `reservations.py` | 13 | 预订 + 周期序列 + travelers manifest + fulfiller |
| `transactions.py` | 7 | 通用账本 + refund + 反向佣金 |
| `fraud.py` | 13 | trust_score + AML + incident + velocity |
| `fx.py` | 6 | 多币种 + 转换 + 历史 |
| `compliance.py` | 13 | 广告法扫描 + PII 审计 + GDPR + 文档保留 |
| `media.py` | 8 | Sensitive 媒体注册表 + legal hold |
| `partnerships.py` | 8 | 联合营销（OPTIONAL，不在主路径） |

### KiX 超级 App（用户网络层）

| 模块 | 端点 | 用途 |
|------|------|------|
| `kix_id.py` | 17 | Universal kid + OAuth Connect + 跨商户画像 + 设备反作弊 |
| `push_engine.py` | 16 | 智能推送 + 拍卖集成 + 频率管控 + 多 placeholder |
| `listings.py` | 11 | C2C marketplace + offer chain |
| `accounts.py` | 11 | 企业 Account 实体 + 组织架构 + 买方委员会 |
| `subscriptions.py` | 12 | SaaS subscription + NDR/GRR + seat-based |
| `user_wallet.py` | 8 | 消费者钱包 + freeze/release |
| `deposits.py` | 5 | 押金生命周期 |
| `pricing.py` | 4 | 动态定价 + 高峰/库存触发器 |

### 商业化新增（R11）

| 模块 | 端点 | 用途 |
|------|------|------|
| `brand_subscriptions.py` | 9 | 4 级 Brand Tier + 3 个月试用 + Quota |
| `payment_methods.py` | 9 | 信用卡 on-file + 防卡复用 + Stripe stub |
| `dashboards.py` | 4 | Today + 累计 + 排行榜 + Insights |
| `welcome_kit.py` | 4 | 自动生成桌牌 / 立牌 / 门贴 / 海报 |

## 3.3 79 Recipe 库（26 行业）

```
F&B:        coffee / bubble_tea / food / restaurant / luxury_dining
Retail:     retail / ecommerce / luxury_retail / fashion
Health:     fitness / beauty / wellness / healthcare / medical / medical_aesthetics
Family:     baby_products / kids_education / parenting
Community:  community / book_club / education / co_working
Hospitality: hotel / travel / airline
Entertainment: gaming / music / events / cinema
Services:   automotive / real_estate / financial_services / telecom
Marketplace: marketplace / sharing_economy / logistics
其他:        pet / other
```

任意行业 NL 描述 → AI 自动选 Recipe → 5 分钟出可玩游戏。

---

# 第四卷 · 合规与安全

| 层 | 解决 | 实现 |
|----|------|------|
| **Consent** | GDPR/PIPL/PDP | 15 个 scope，7 个 regulated scope 需 OTP/签名/视频 |
| **PII 审计** | 个保法 §51 | 敏感字段每次访问留痕，1h 异常检测窗口 |
| **Compliance Scanner** | 广告法 §7 §25 | 70 条 banned phrase 扫描 + 强制注入风险提示 |
| **Frequency Cap** | 防用户疲劳 | 全局 10/天 + 单品牌 3/天 + tier override + priority bypass |
| **Fraud** | 反作弊 + AML | trust_score + 设备指纹速度 + token replay + AML SAR |
| **Document Consent** | 双录 / 医疗 / 买卖 | OTP / signature / video_recording 强证据 |
| **Media Registry** | 敏感图片/视频 | medical_sensitive / before_after / biometric 必需 consent_grant_id |
| **Legal Hold** | 司法保全 | 防止医疗 / 法务证据被删 |
| **GDPR Article 15** | 数据导出 | 24 个 user 数据模式打包 JSON/CSV，TTL 7 天 |
| **GDPR Article 17** | 数据删除 | 保留类强制兜底（医疗 5y / 金融 7y）|
| **支付防刷** | 信用卡复用 | 同卡哈希 → 拒绝多商家注册 |

---

# 第五卷 · 接入指南

## 5.1 商家接入（5 行 HTML Pixel）

```html
<!-- 任何页面顶部 -->
<script src="https://partner.letskix.com/sdk/kix-pixel.js" data-pixel="YOUR_PIXEL_ID"></script>

<!-- 注册成功时 -->
<script>kix.identify('user_123');</script>

<!-- 下单成功时 -->
<script>kix.purchase('order_123', 5000);</script>
```

完事。归因、转化、CPS 提成全自动。

WeChat Mini-Program 也支持（`wx<appid>` 协议）。

## 5.2 开发者接入（KiX ID OAuth）

```
1. POST /api/v1/kix-id/connect/authorize
   {brand_id, scopes: [profile, history, location, marketing], redirect_uri}
   → returns {grant_id, code}

2. POST /api/v1/kix-id/connect/token
   {grant_id, code, client_secret}
   → returns {access_token, kid, scopes}

3. GET /api/v1/kix-id/{kid}/profile-for-merchant/{brand_id}
   Authorization: Bearer <access_token>
   → returns kid's profile filtered by granted scopes
```

类 Facebook Connect / 微信开放平台同模式。

## 5.3 销售话术

**核心**:
> 「平台全免费。不收月费不收订阅。功能比 Shopify + Smartico + Bunchball 加起来还多。
>
> 我们怎么赚钱？只有两种：
> 1. 如果我们通过游戏帮你带来一个新客，你愿意付多少？
> 2. 如果我们通过跨品牌网络给你导流一笔交易，我们抽多少？
>
> 全是 performance-based。你赚我们才赚。」

**收信用卡**:
> 「填一下信用卡，前 3 个月完全免费体验。这是给你保留账号优先权 + 防止恶意刷号。
> 3 个月后如果你觉得效果不好，提前一周取消就行。但**数据会告诉你答案** — 90% 的商家
> 看到 dashboard 数字后都自然续费了。」

**让用户下载 KiX App**:
> 「通过你店里的 QR 注册的每个用户，平台永久标记是**你的客户**。他下次来你店附近，
> KiX 自动推你的活动给他。他去隔壁竞品消费，算法可以把他拉回你这。**你不装这个 SaaS，
> 等于把客户白送给装了的竞品。**」

---

# 第六卷 · 战绩

## 6.1 18 商家行业模拟

| 商家 | 行业 | 总 passes | P0 |
|------|------|----------|-----|
| 老王 | 印尼奶茶 10 店 | 53 | 0 ✨ |
| 老张 | 北京高端餐厅 | 44 | 3 |
| 老李 | 广州书友会 | 37 | 2 |
| 老黄 | 杭州母婴电商 | 40 | 1 |
| 老周 | 上海健身 5 馆 | 74 | 3 |
| 老吴 | 深圳 K12 教育 | 50 | 4 |
| 老蔡 | 上海私立医院 | 60 | 5 |
| 老梁 | 杭州旅游 | 53 | 10 |
| 老沈 | 上海医美 | 42 | 6 |
| 老郑 | 北京金融 | 40 | 10 |
| 老陆 | 深圳房产 | 43 | 5 |
| 老钱 | 杭州美发 | 50 | 5 |
| 老贾 | 广州物流 | 39 | 10 |
| 老胡 | 二手 C2C | 41 | 5 |
| 老韩 | 成都宠物 | 49 | 6 |
| 老田 | 共享单车 | 24 | 8 |
| 老柯 | 直播带货 | 38 | 6 |
| 老石 | B2B SaaS | 25 | 8 |

总计：**809 passes**（11 轮迭代下来从 0 起步）

## 6.2 11 轮 Trinity 迭代

```
Round 0:  209 routes  · 初始 Gamification 核心
Round 1:  412 routes  · 商家广告平台 + 第一次老王 sim
Round 2:  451 routes  · 5 跨行业 P0 修复
Round 3:  464 routes  · Reservation + 22 recipes + 老周/老吴
Round 4:  494 routes  · 时序属性 + 关系 + 多触点归因
Round 5:  525 routes  · TikTok 模型校正 + KiX 超级 app + KiX ID + Push Engine
Round 6:  572 routes  · 11 跨行业根因 (FX/合规/resource_id/实体...)
Round 7:  603 routes  · Regulated-data + 4-scope tier + 49 recipes
Round 8:  603 routes  · 9 schema alias + 79 recipes + 老柯/老石
Round 9:  644 routes  · Fraud + AML + GDPR + Transactions + 跨模块桥接
Round 10: 700 routes  · Master rollup + Financial primitives + B2B accounts
Round 11: 726 routes  · Brand subscription tier + Payment methods + Dashboards
                      · 3 个月试用 (Apple Music 策略)
```

## 6.3 E2E 测试 24/24 PASS

完整 TikTok/Google 模型验证：
1. ✅ Brand A 充值钱包 + 自动审批 campaign
2. ✅ User U 扫码 → 自动创建 kid
3. ✅ 拍卖 → 曝光 → 点击 → 转化 → wallet 扣 ¥10
4. ✅ Brand B 进入网络 + 出价
5. ✅ KiX 算法跨品牌推送 → Brand B 赢 → 立即扣费
6. ✅ U 在 Brand B 消费 → 归因 Brand A → 自动佣金分账
7. ✅ Brand A 不能买回自己客户（自动 exclude）
8. ✅ /admin/savings 显示节省的 CAC

## 6.4 P0 收敛趋势

```
Round 1 老王 sim 首跑:  P0=2
Round 1 修复后:        P0=0  (老王完美)
Round 5 模型校正:      P0 全局减少
Round 7 合规层:        16 → 12 P0
Round 9 Fraud:         115 → 111
Round 10 三大支柱:     111 → 102 (-9)
Round 11 商家漏斗:     预计降 ~5 P0 (验证中)
```

---

# 第七卷 · 路线图

## 7.1 已完成 (Done)

| 类别 | 完成项 |
|------|--------|
| 商业模式 | TikTok/Google 单边合约 + 双重收入护城河 |
| 广告平台 | 5 出价策略 + Quality Score + Pacing + 反作弊 |
| 用户身份 | KiX ID + OAuth Connect + 跨商户画像 |
| 多触点归因 | 7 天 last-touch + 配置 1-365 天 + 共同归因 |
| 合规 | GDPR / PIPL / 广告法 / PII 审计 |
| 三 P0 漏斗 | 用户旅程 + 商家旅程 + 网络效应 |
| 商家订阅 | 4 级 tier + 3 个月试用 + 自动续费 |
| 支付 | PCI-safe + Stripe-ready + 防卡复用 |
| Dashboard | Today + 累计 + Insights + 排行 |
| 欢迎礼包 | 自动生成 4 种印刷物料 |

## 7.2 进行中 (R11 收尾)

- [ ] check_quota 接入 campaigns/recipes/audiences/creative_gen
- [ ] Sim probe wave 3（6 sim 接 R10 端点）
- [ ] 短信 OTP 网关接入（外部）

## 7.3 待办 (Round 12+)

### P0
- [ ] Stripe 实际 API 接入（payment_methods 现在是 stub）
- [ ] 印刷物料 PDF 渲染（现在是 HTML，需 reportlab/Pillow）
- [ ] 物流接口（welcome_kit 寄送）
- [ ] 销售 CRM 集成（lead 跟踪）

### P1
- [ ] 100 商家试运营计划
- [ ] 多端 App（iOS / Android / WeChat MP / Mini-Program）
- [ ] 国际化（印尼语 / 英文 / 泰文）
- [ ] 销售/商户 onboarding 视频教程

### P2
- [ ] 中后台运营工具（admin dashboard）
- [ ] 第三方 API marketplace（让开发者扩展 KiX）
- [ ] 内容审核 AI（自动扫 banned content）

---

# 附录

## A. 所有文档索引

### 战略层
- [`KIX_GAMIFICATION_BIBLE.md`](KIX_GAMIFICATION_BIBLE.md) — **本文档**（唯一权威源）
- [`PLATFORM_OVERVIEW.md`](PLATFORM_OVERVIEW.md) — 平台总览（团队接入手册）
- [`MONETIZATION_V2.md`](MONETIZATION_V2.md) — TikTok/Google 模型详解
- [`MASTER_BLUEPRINT.md`](MASTER_BLUEPRINT.md) — 全球 100 Gamification → KiX 化
- [`SIGNIFICANCE.md`](SIGNIFICANCE.md) — 销售/技术/产品三团队意义
- [`TRINITY_ANALYSIS.md`](TRINITY_ANALYSIS.md) — 三体迭代方法论

### 操作层
- [`SYSTEM_HOW_IT_WORKS.md`](SYSTEM_HOW_IT_WORKS.md) — 系统机制端到端
- [`USER_FLOW_TRUTH.md`](USER_FLOW_TRUTH.md) — 用户漏斗真相
- [`MERCHANT_FLOW_TRUTH.md`](MERCHANT_FLOW_TRUTH.md) — 商家漏斗真相
- [`TEAM_TRAINING.md`](TEAM_TRAINING.md) — 团队培训

### 工程层
- [`README.md`](README.md) — 启动指南
- [`ENGINEERING.md`](ENGINEERING.md) — 工程移交
- [`BUILD_HISTORY.md`](BUILD_HISTORY.md) — 构建历史
- [`BUILD_PROCESS.md`](BUILD_PROCESS.md) — 构建方法论
- [`GAMIFICATION_AUDIT.md`](GAMIFICATION_AUDIT.md) — 全球 100 案例
- [`GAME_LIBRARY.md`](GAME_LIBRARY.md) — 游戏库

### 验证文档
- `/Users/mozat/a-docs/lao*-sim-findings.md` — 18 商家模拟完整 findings
- `/Users/mozat/a-docs/round*-verification.md` — 11 轮迭代验证报告

## B. 关键决策记录 (ADR)

| ADR # | 决策 | 时间 | Rationale |
|-------|------|------|-----------|
| 1 | TikTok/Google 单边模型，**不是** Plenti 双边联盟 | R5 | Plenti $100M 3 年死，60% 联盟 10 年内死 |
| 2 | 3 个月试用，**不是** 1 年试用 | R11 | Apple Music 策略 — 短促累积价值，切换成本 > 续费成本 |
| 3 | 信用卡留底强制 | R11 | 一卡一账号防刷 + 第 91 天自动扣 |
| 4 | 拍卖默认 `target_audience=new_users_only` | R5 | TikTok/Google 标准 — 商家不浪费钱买回自己客户 |
| 5 | KiX 是用户唯一前端（KiX App）| R5 | 像 Facebook Connect — KiX 拥有用户关系 |
| 6 | 7 天归因默认，1-365 天可配 | R6 | 餐饮 7 天，医疗 365 天，房产 180 天 |
| 7 | 79 Recipe / 26 行业 | R8 | 商家不要"自己设计"，要"按行业选" |
| 8 | LLM 仅用于创造（游戏/文案），决策全确定性 | 通则 | LLM 不确定性 → 钱算不清 / 合规无法证明 |

## C. 词汇表

| 术语 | 意思 |
|------|------|
| **kid** | KiX ID — 用户唯一身份 (`kid_xxxxxxx`) |
| **brand** | 商家/品牌 — 在 KiX 上的客户 |
| **master** | 多店商家的总公司账号（如老王的 10 家奶茶店在一个 master 下） |
| **eid** | Entity ID — 非人类实体（pet / property / vehicle） |
| **aid** | Account ID — B2B 公司实体（不同于 master） |
| **GSP** | Generalized Second-Price 拍卖 — Google Ads 同款 |
| **CPA/CPS/CPM/CPV/CPE** | 5 种出价策略 |
| **target_audience** | new_users_only / retargeting_only / all |
| **Quality Score** | 0-1 浮点，影响拍卖排名 |
| **Pacing** | 预算均匀消耗策略 |
| **Take Rate** | KiX 从佣金里抽的比例（30-70%） |
| **NDR/GRR** | Net Dollar Retention / Gross Revenue Retention（SaaS 关键指标） |

## D. 销售话术合集

见第 5.3 节 + [`MERCHANT_FLOW_TRUTH.md`](MERCHANT_FLOW_TRUTH.md)。

## E. 开发者 API 速查

完整 OpenAPI 文档：`/docs`（Swagger UI）/ `/redoc`（ReDoc）
公开 API 参考：`/api-docs`

关键端点：
- 注册：`POST /brands/register`
- 创建游戏：`POST /games/create`
- 充值：`POST /wallet/{bid}/topup`
- 创建广告：`POST /campaigns/create`
- 跟踪转化：`POST /attribution/track/conversion`
- 推送：`POST /push/now`

---

## 一句话总结

> **KiX 不卖软件，KiX 卖用户。**
>
> 全免费工具喂养商家 → 3 个月试用上钩 Premium → 充钱开广告买别家用户 → 跨品牌网络自我催化 → 永远不会被 AI 复刻。

---

📖 *KIX GAMIFICATION BIBLE · 唯一权威 · 持续更新*
