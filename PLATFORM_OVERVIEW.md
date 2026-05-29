# KiX 平台总览 — 团队接入手册

> **一句话亮点**
>
> **「KiX = TikTok Ads for Gamification。商家管理自己用户全免费；想要别人的用户就来出价，KiX 算法撮合。」**
>
> 软件免费 · 拍卖收费 · 单边合约（商家只与 KiX 签）· 商家之间永远互不可见

## 官方域名

| 端 | 域名 | 用途 |
|------|------|------|
| **商家入口** | https://partner.letskix.com/ | Merchant Portal — 注册 / 钱包 / 活动 / 数据 |
| **品牌主页** | https://partner.letskix.com/s/{brand_id} | Storefront — 用户公开访问的品牌页 |
| **API 接口** | https://api.letskix.com/ | REST API 根 |
| **Pixel SDK** | https://partner.letskix.com/sdk/kix-pixel.js | 商家网站埋码用 |
| **Gamification SDK** | https://partner.letskix.com/sdk/kix.js | 嵌入游戏组件用 |

---

## 给新接入成员（5 分钟读完版）

### 网络结构 — 单边合约，不是 N×N

```
       ┌──────────┐
       │   KiX    │  ← 唯一合同对手方
       └──────────┘
        ↑↑↑↑↑↑↑↑↑
        │││││││││
   ┌────┘│││││││└────┐
   │     │││││││     │
 商家A 商家B 商家C ... 商家N
   ▲     ▲     ▲       ▲
   │     │     │       │
   └──── 商家彼此永远互不可见 ────┘
         （没有 brand ↔ brand 边）
```

每个商家只有一条线 → 通向 KiX。
KiX 拍卖算法在中心决策谁的 offer 被展给哪个用户。
**没有任何两个商家之间的合约、知情或感知。**

### 商家的两种模式

```
模式 FREE — 管理自己现有用户（永久免费、无上限）
└── 50+ Gamification 模块 + 100 Recipes + AI 游戏生成
    + Analytics + Push/SMS + A/B + White Label + SDK
    用于：留存、激活、复购、社群

模式 PAID — 通过拍卖获取新用户（按 KiX 真实交付付费）
└── 在 Ads Manager 创建 Campaign
    默认 target_audience = new_users_only
    选出价策略：CPA / CPS / CPM / CPV / CPE
    KiX 拍卖（Quality-adjusted Vickrey GSP）撮合
    商家永远不知道、不关心其他商家是谁
```

### 怎么赚钱（拍卖驱动，多策略并行）

| 出价策略 | 含义 | 参考价位 | 对标 |
|---------|------|---------|------|
| **CPA** | 每个新注册用户 | ¥10-100 | Facebook ¥50-300 / 抖音 ¥30-200 |
| **CPS** | 新客成交订单分成 | 5-15% | 美团 12-26%（KiX 远低且非强制） |
| **CPM** | 每千次曝光 | ¥10-50 | 标准品牌曝光 |
| **CPV** | 每次进店访问（LBS） | ¥1-10 | 线下流量 |
| **CPE** | 每次游戏完成（互动） | ¥0.5-5 | Gamification 原生（KiX unique） |

**其他一切：永久免费**（SaaS / 模块 / 模板 / AI 生成 / 分析 / SDK / 白标 / 推送）

### 为什么会赢

1. **AI 让所有软件功能 12 个月内被复刻** → 卖软件 = 慢性自杀 → 全免费送出抢占商家
2. **单边合约**（N）而非 bilateral（N²）→ 像 Google Ads / TikTok Ads 可以无限扩展；Plenti（双方签约）3 年就死
3. **拍卖代替固定抽佣** → 商家自主定价 → 不会像美团一样引发反弹
4. **Quality Score 数据复利** → 越多拍卖数据，匹配越精准，护城河越深
5. **Gamification 原生载体** → CPE 比传统 banner/feed 互动深度高一个量级

财务推算：1000 商家 ¥18M/年 → 10000 商家 ¥420M/年 → 100000 商家年化十亿级（对标 TikTok Ads $200B 全球）

---

## Why TikTok / Google 模型胜出（核心理由）

| 维度 | Bilateral 模型（Plenti） | KiX / TikTok / Google 单边模型 |
|------|------------------------|------------------------------|
| **可扩展性** | N² 合同，加一个品牌指数增长协调成本 | N 合同，加一个商家线性 |
| **隐私** | 品牌看到对方策略、用户、价格 | 商家完全互不可见，KiX 仅做撮合 |
| **速度** | 月度谈判 → 法务 → 集成 | 注册即用，出价即跑，分钟级 |
| **网络效应** | 任一品牌退出 = 多米诺骨牌 | 任一商家退出 = 其他商家拍卖照常 |
| **决策机制** | 联盟集体决策（无人有权） | KiX 算法中心化决策 |
| **退出成本** | 高（合约违约金 + 数据纠缠） | 零（关 Campaign，提钱包余额） |
| **历史结果** | 60% 联盟 10 年内死亡 | TikTok/Google Ads 持续高速增长 |

**Trinity Protocol 三体验证**:
- **Industry**: TikTok Ads / Google Ads / Meta Ads 全部单边拍卖结构，均高速增长；Plenti 双方签约 3 年死亡，Catalina 类似
- **Academic**: 合同复杂度 N² vs N，梅特卡夫定律网络效应只在中心化撮合时成立
- **Reality**: KiX 实际跑老王/老李/老张/老黄 sim，商家从注册到第一次拍卖中标全程 < 7 天，无任何商家间交互

---

## 系统架构（5 层）

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 5 · 商业化   广告拍卖 / 归因 / 钱包 / 结算            │
├─────────────────────────────────────────────────────────────┤
│  Layer 4 · 商家入口  Portal SPA / Ads Manager / Storefront    │
├─────────────────────────────────────────────────────────────┤
│  Layer 3 · 引擎层    Network Effect / Commerce Loop / Rules    │
├─────────────────────────────────────────────────────────────┤
│  Layer 2 · 模块层    50+ Gamification Modules + 12 Recipes   │
├─────────────────────────────────────────────────────────────┤
│  Layer 1 · 原语层    11 Universal Primitives + Event Bus       │
└─────────────────────────────────────────────────────────────┘
                          ↓
        FastAPI + Redis + PostgreSQL + ELTM AI 游戏生成器
```

---

## 全模块清单（27 个 routers / 417 routes）

### Gamification 核心（FREE 模式 — 永久免费）

| 模块 | 端点 | 用途 |
|------|------|------|
| `progression.py` | 6 | XP / Level / Badge / Streak / Daily Check-in |
| `primitives.py` | 18 | Currency / Item / Achievement / Quest / Tier / Event |
| `modules.py` | 36 | 10 个组合式顶层模块 |
| `network_effect.py` | 11 | 6 种病毒触发器（FREE 模式拉动自己用户分享）|
| `commerce_loop.py` | 14 | 5 个商业模块（盈利引擎）|
| `triggers.py` | 12 | UserAttr / RateLimit / LimitedDrop / Perk / FCFS |
| `multiplayer.py` | 17 | CoopQuest / Raid / Squad / Territory |
| `social.py` | 16 | Friends / Following / Feed / Kudos |
| `p2p.py` | 11 | Gift / Trade |
| `group_actions.py` | 13 | GroupBuy / Atomic / PriceCut |
| `voucher_builder.py` | 7 | Conditional voucher templates |
| `rule_engine.py` | 11 | WHEN-THEN composition |
| `brand_modules.py` | 4 | Merchant 模块开关 |
| `conditions.py` | 9 | 通用条件引擎 (check/reserve/commit/refund) |
| `recipes.py` | 10 | 12 个预置 Recipe 配方 |
| `recipe_generator.py` | 6 | NL → Recipe AI 转换 |
| `tutorials.py` | 8 | Recipe → Tutorial 引导 |

**子合计**: 17 routers / 209 endpoints

---

### 广告平台（PAID 模式 — 拍卖驱动）

#### 拍卖引擎 5 件套（主路径）

| 模块 | 端点 | 用途 |
|------|------|------|
| `attribution.py` | 22 | 7 天 last-touch + 多触点 + impression_token + 5 维反作弊 |
| `wallet.py` | 10 | 商家充值 + 原子扣款 + 日预算 + 自动续费 + 退款 |
| `campaigns.py` | 21 | Campaign + AdGroup 层级 + Review Queue + Quality Score 透明 |
| `auction.py` | 8 | Quality-adjusted Vickrey GSP + 保留价 + Pacing + Smart Bidding |
| `geofence.py` | 8 | Redis GEO + 3 级反垃圾 + 进店归因 + LBS 推送 |

**5 种出价策略**：CPA / CPS / CPM / CPV / CPE（同一拍卖引擎复用）

#### 标准广告平台辅件（TikTok / Google 同结构）

| 模块 | 端点 | 用途 |
|------|------|------|
| `frequency_cap.py` | 6 | 用户曝光封顶 + Pacing（防用户疲劳）|
| `consent.py` | 8 | GDPR/PIPL/PDP 授权（Article 15/17 数据导出/删除）|
| `pixel.py` | 6 | 商家网站 JS Pixel SDK（自动归因）|
| `disputes.py` | 8 | 商家投诉 + 退款工作流 + 归因回滚 |
| `audiences.py` | 10 | Custom Audience 上传 + Lookalike 算法 |
| `master_accounts.py` | 13 | 多店 master 账号 + RBAC + 邀请接受 + 预算级联 |
| `payouts.py` | 13 | 商家提现 + 银行账户 + 自动结算 + 发票生成 |
| `creative_gen.py` | 10 | ELTM AI 创意生成 + A/B 测试 |
| `storefront.py` | 9 | 公开品牌主页 + 关注 + 评价 |
| `vouchers.py` | 12 | 跨店发券 / 转赠 / 兑换 |

**子合计**: 10 routers / ~103 endpoints

#### OPTIONAL（高级功能，不在主路径）

| 模块 | 状态 | 说明 |
|------|------|------|
| `partnerships.py` | **OPTIONAL (joint campaigns only, advanced)** | 仅用于两个商家显式双向同意的联合活动。主路径不需要，99% 商家用不到。**默认禁用**。保留是为了极少数 bilateral 合作场景（如银行 × 航司联名卡），不是 KiX 商业模式的核心。 |

---

### 总规模
- **27 个 routers**
- **417+ routes**
- **3 个仓库联动**：kix-platform / eltm（AI 游戏生成）/ code-soul + pm-soul（编排）
- **2 个前端**：portal.html (商家 Portal) / storefront.html (用户公开品牌页)
- **2 个 SDK**：kix.js (gamification 嵌入) / kix-pixel.js (转化追踪)

---

## 核心商业机制详解

### 1. Quality-Adjusted Vickrey GSP 拍卖（auction.py）

```
rank = max_bid × quality_score × pacing_factor
winner = argmax(rank)
charge = min(ceil(runner_up_rank / winner_qs) + 1, max_bid)
```

- **Vickrey 真理**：出真实价值 = 最优策略，商家不需博弈
- **Quality Score** = 0.3 + min(CTR×8, 0.4) + min(CVR×6, 0.3)
- **Pacing**：每天 50% 时间应花 50% 预算，超支降权 0.3，落后提权 1.0
- **保留价**（reserve price）防止贱卖广告位

与 Google Ads / TikTok Ads 同结构。**商家只与拍卖引擎交互，看不到其他商家**。

### 2. 7 天 Last-Touch 归因 + 多触点扩展（attribution.py）

```python
# 标准 last-touch
journey = user_journey[user_id][:7d]
attribute_to = first(e for e in reversed(journey) if e.source_brand)

# 可选多触点模型
- linear: 每个触点 1/N
- time_decay: half-life = 48h
- position_based: 40/40/20 三段法
- data_driven: Shapley-ish + GMV 加权
```

5 维反作弊评分（0-100）：rate_limit / self_attribution / blacklist / token_replay / geo_anomaly。score > 70 拒绝。

**关键**：归因结果只反馈给 KiX 自己（用于扣款），商家只看到「KiX 给我交付了 N 个新用户」，看不到这些用户从哪个其他商家来。

### 3. Take Rate Ladder（大商家激励）

```
GMV < ¥10K      → KiX 拍卖净 ~3%
GMV ¥10K-100K   → KiX 拍卖净 ~2%
GMV ¥100K-1M    → KiX 拍卖净 ~1.2%
GMV > ¥1M       → KiX 拍卖净 ~0.75%
```

注意：这是商家 **自愿出价的等效净率**，不是 KiX 强加的抽佣率。商家自己出价多少，KiX 就收多少。Ladder 是商家可选择的预设档位。

### 4. LBS 拍卖流程（geofence.py + auction.py）

```
1. 用户进入某区域（Redis GEOSEARCH 多商家 500m 围栏）
2. KiX 拍卖在该区域出价的所有商家 → 选中标者
3. 触发 push（频率封顶 + 时段限制 + cooldown）
4. 推送游戏链接（impression_token 跟踪）
5. 用户玩游戏 → 拿优惠券
6. 用户进店核销 → 归因订单
7. KiX 按中标商家的出价策略扣款（CPV / CPS / 其他）
```

中标商家不知道其他参与拍卖的商家是谁，也不知道用户上一站去过哪里。

---

## 标准集成方式（给商家）

### 1. 自家网站埋码（5 行 HTML）

```html
<!-- 任何页面顶部 -->
<script src="https://partner.letskix.com/sdk/kix-pixel.js" data-pixel="YOUR_PIXEL_ID"></script>

<!-- 注册成功时 -->
<script>kix.identify('user_123');</script>

<!-- 下单成功时 -->
<script>kix.purchase('order_123', 5000);</script>
```

完事。归因、转化、按出价策略自动扣款。

### 2. 自家 App 接入

```javascript
// 任何 RESTful HTTP 客户端
POST /api/v1/attribution/track/conversion
{user_id, target_brand: YOUR_BRAND, order_id, amount_cents}
```

### 3. 完全离线门店

```
POST /api/v1/geofence/stores/register {lat, lng, radius_meters}
POST /api/v1/geofence/visit {user_id, store_id, evidence: "qr_scan"}
```

QR 码扫码或人工录入即可。

### 4. Ads Manager 创建 Campaign（PAID 模式）

```javascript
POST /api/v1/wallet/topup {brand_id, amount_cents: 100000}
POST /api/v1/campaigns {
  brand_id,
  bid_strategy: "CPA",          // or CPS / CPM / CPV / CPE
  max_bid_cents: 2000,          // ¥20
  target_audience: "new_users_only",   // 默认值，永远不向自己已有用户收费
  daily_budget_cents: 10000,
  creative_id: "ai_generated_xxx"
}
// 之后 KiX 拍卖在背后自动跑，商家不需要做任何其他事
```

---

## 验证战绩（老王 印尼奶茶 10 家店全程模拟）

| 指标 | 修复前 | 修复后 |
|------|-------|-------|
| Pass | 43 | **47** |
| P0 阻断 | 2 | **0** |
| P1 摩擦 | 7 | **0** |
| 拍卖胜率 | 0% (0/119) | **91% (130/143)** |
| 真实点击 | 0 | **41** |
| 真实转化 | 0 | **7** |
| 每日预算执行 | 失效 | **生效（第13单 402）**|

「边跑边修」工作流：模拟商家完整旅程 → 发现 gap → Trinity 三体迭代修复 → 重新验证。

---

## 完整文档索引

### 战略层
- `MASTER_BLUEPRINT.md` — KiX 总蓝图
- `MONETIZATION_V2.md` — **拍卖驱动商业化模型**（核心商业逻辑）
- `SIGNIFICANCE.md` — 销售/技术/产品三团队意义
- `TRINITY_ANALYSIS.md` — 三体迭代方法论

### 工程层
- `ENGINEERING.md` — 工程移交手册
- `BUILD_HISTORY.md` — 累计构建历史
- `BUILD_PROCESS.md` — 构建方法论
- `README.md` — 启动指南
- `GAME_LIBRARY.md` — 游戏库
- `GAMIFICATION_AUDIT.md` — 全球 100 案例审计
- `TEAM_TRAINING.md` — 团队培训

### 模拟与验证
- `/Users/mozat/a-docs/kix-ads-platform-trinity.md` — 广告平台三体设计
- `/Users/mozat/a-docs/laowang-sim-findings.md` — 老王 印尼奶茶店全程
- `/Users/mozat/a-docs/laoli-sim-findings.md` — 老李 广州读书会
- `/Users/mozat/a-docs/laozhang-sim-findings.md` — 老张 北京高端餐厅
- `/Users/mozat/a-docs/laohuang-sim-findings.md` — 老黄 杭州母婴电商

### 历史与对比
- `MONETIZATION.md` — V1 货币化（已超越，留参考）
- `/Users/mozat/a-docs/kix-monetization-trinity.md` — V1 三体研究

---

## 快速启动

```bash
# 1. 克隆三仓
git clone https://github.com/mozatyin/kix-platform.git
git clone https://github.com/mozatyin/eltm.git
git clone https://github.com/mozatyin/code-soul.git

# 2. 启动 Redis + PG
docker-compose up -d  # (在 kix-platform 目录)

# 3. 启动 FastAPI
cd kix-platform
.venv/bin/pip install -e .
.venv/bin/uvicorn app.main:app --reload --port 8000

# 4. 启动 ELTM（AI 游戏生成器）
cd ../eltm
.venv/bin/python -m eltm.kix_api  # port 8001

# 5. 跑全流程 E2E 测试
cd ../kix-platform
.venv/bin/python scripts/e2e_ads_platform.py
.venv/bin/python scripts/sim_laowang.py

# 6. 打开商家 Portal
open http://localhost:8000/landing/portal.html  # 本地开发
# 生产环境：https://partner.letskix.com/
```

---

## 各团队接入要点

### 商业 / 销售团队
**销售话术核心**：
> 「这个平台像 TikTok Ads / Google Ads，但更好。
>
> 管理你自己的用户 — 全套 Gamification 永久免费、无上限。
>
> 想要别人家的用户？进 Ads Manager 出价：CPA / CPS / CPM / CPV / CPE 五种策略任选。KiX 拍卖给你撮合，你只在我们真的交付时付钱。
>
> 你不需要和任何其他商家打交道。你的对手方只有 KiX。」

**关键 KPI**：商家注册数 / FREE→PAID 转化率 / PAID 商家月预算 / Quality Score 分布 / 商家提现率

### 工程团队
**核心架构原则**：
- 三仓独立：kix-platform (业务) / eltm (AI 游戏) / code-soul (编排)
- Redis 是唯一状态存储（PG 仅 brand_config + 长期数据）
- 所有金额 integer cents（永不 float）
- 原子操作 WATCH/MULTI，幂等性必须
- 跨模块只调内部 helper（`*_internal` 函数），不走 HTTP
- **拍卖路径单向**：商家 → KiX → 算法决策 → 展示，永远不暴露商家间信息

**关键文件**：
- `app/main.py` — 所有 router 注册入口
- `app/redis_client.py` — Redis 连接池
- `app/config.py` — 配置（含 ELTM_BASE_URL）
- `scripts/e2e_ads_platform.py` — 集成测试模板
- `scripts/sim_*.py` — 多行业商家模拟
- `app/routers/auction.py` — 拍卖核心，永远不返回 losing bidders 信息
- `app/routers/attribution.py` — 归因结果只返回 KiX 内部，对商家只暴露聚合指标

### 产品 / 设计团队
**优先级**：
1. **Ads Manager UI**：参考 TikTok Ads Manager / Google Ads UI，让商家自助创建 Campaign
2. **Quality Score 透明面板**：让商家看到自己排名为什么，但**不暴露其他商家的出价或身份**
3. **归因透明面板**：每笔扣款追溯到 impression_token + click + conversion
4. **CAC 节省报告**：把出价框架成「投入 ROI」而非「被抽佣」
5. **商家 Onboarding 路径**：注册到第一笔归因订单 < 7 天

### 法务 / 合规
**已实施**：
- `consent.py` 模块强制 GDPR Article 15 / 17（数据导出 / 删除）
- 用户级 `cross_brand_tracking` 授权
- 5 维反作弊评分
- 商家投诉 + 退款工作流
- **单边合约结构**：商家 ToS 与 KiX 签，KiX 与所有商家签，**商家之间无任何合约关系** → 法律上规避 Plenti 类多米诺骨牌风险

**待完善**：
- 跨境合规（印尼商家收 IDR / 中国 CNY / 国际 USD）
- 税务发票自动生成
- VAT / 增值税计算

---

## 持续迭代节奏（Trinity Protocol）

```
每轮 = ① Industry 业界对标（TikTok / Google / Meta Ads 是基准）
       ② Academic 第一性原理（N vs N² 合约、拍卖理论、网络效应）
       ③ Reality 实地模拟商家

发现 gap → 派并行 agents 修复 → 重新模拟 → 验证收敛
```

**已完成迭代**：
- Round 1: 老王 sim → 暴露 11 个 gap (P0=2 P1=7 P2=2)
- Round 2: 派 3 个修复 agent + 1 个根因诊断 → P0+P1 全清零 → 47 PASS

**下轮目标**（运行中）：
- 老李书友会（社区/订阅型）
- 老张高端餐厅（VIP/预订型）
- 老黄母婴电商（纯线上/高量级）

---

## 一句话总结（给所有接入成员）

> **KiX 是 TikTok Ads / Google Ads 的同结构平台，多了一层永久免费的 Gamification SaaS。**
>
> 商家只与 KiX 签合同（N 份，而非 N²）。商家之间永远互不可见。拍卖算法在中心做所有匹配决策。
>
> 卖软件 = AI 时代慢性自杀 → 全免费。
> 卖拍卖 = TikTok/Google 已验证的护城河 → 这是 KiX 真正的生意。

收入路径清晰：
1. **FREE 模式招商家** → 100 → 1000 → 10000
2. **PAID 模式拍卖收钱** → 商家自主出价，KiX 算法撮合
3. **Quality Score 数据复利 + 网络效应** → 年化十亿级（对标 TikTok Ads $200B 全球）

每一行代码都为这条路径服务。
