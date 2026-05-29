# KiX 平台总览 — 团队接入手册

> **🎯 一句话亮点**
>
> **「KiX 把全世界最强的 Gamification 全部免费送给商家，只靠跨商家的用户连接赚钱。」**
>
> 软件免费 · 网络收费 · AI 复刻的功能永远免费 · 真正的护城河 = 商家之间的用户流动

---

## 📌 给新接入成员（5 分钟读完版）

### 我们做什么
- **对商家**：1 张图就能描述清楚 ——「全免费的 Gamification SaaS 平台 + 拉新引流网络」
- **对用户**：在 KiX 上玩游戏 → 拿优惠 → 进店消费 → 跨品牌发现新商家
- **对我们**：当 A 商家的玩家被引导到 B 商家消费，我们抽佣

### 怎么赚钱（只有两条收入线）

```
模式 A · 跨店带新单提成 (CPS)
└── 用户在 A 商家玩游戏 → 跳到 B 商家消费 → KiX 抽订单 5-15%
    （远低于美团 12.6%，因为我们不送货）

模式 B · 纯拉新 CPA
└── 商家充值 ¥1000 → 出价 ¥20/新客 → KiX 算法引导 50 个新客
    （低于 Facebook ¥50-300，因为游戏引导用户质量高）
```

**其他一切：永久免费**（包括 SaaS / 模块 / 模板 / AI 生成 / 分析 / SDK / 白标 / 推送）

### 为什么会赢
1. AI 让所有软件功能 12 个月内被竞争对手复刻 → 卖软件 = 慢性自杀
2. 我们抢先把所有功能送出去 → 100 → 1000 → 10000 商家加入
3. 网络价值 = N² × 0 (零成本) → 指数级增长
4. 当 KiX 是唯一能连接 10000 商家用户的网络时，跨店带客的抽佣就是真生意

财务推算：100 商家 ¥960K/年 → 1000 商家 ¥38M/年 → 10000 商家 ¥1.2-2.4B/年

---

## 🏗 系统架构（5 层）

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 5 · 商业化   广告平台 / 拍卖 / 归因 / 钱包 / 结算       │
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

## 📦 全模块清单（27 个 routers / 417 routes）

### 🎮 Gamification 核心（Round 0 已有）

| 模块 | 端点 | 用途 |
|------|------|------|
| `progression.py` | 6 | XP / Level / Badge / Streak / Daily Check-in |
| `primitives.py` | 18 | Currency / Item / Achievement / Quest / Tier / Event |
| `modules.py` | 36 | 10 个组合式顶层模块 |
| `network_effect.py` | 11 | 6 种病毒触发器（拉新引擎）|
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

### 💰 商业化平台（今天新建的 ⭐）

#### 收入引擎 5 件套

| 模块 | 端点 | 用途 |
|------|------|------|
| `attribution.py` | 22 | 7 天 last-touch + 多触点 + 邀请码 + Take Rate Ladder + View-through + Incrementality |
| `wallet.py` | 10 | 商家充值 + 原子扣款 + 日预算 + 自动续费 + 退款 |
| `campaigns.py` | 21 | Campaign + AdGroup 层级 + Review Queue + Quality Score 透明 |
| `auction.py` | 8 | Quality-adjusted Vickrey GSP + 保留价 + Pacing + Smart Bidding |
| `geofence.py` | 8 | Redis GEO + 3 级反垃圾 + 进店归因 + LBS 推送 |

**4 种出价策略**：CPA / CPS / CPM / CPV

#### 商家广告平台标准件

| 模块 | 端点 | 用途 |
|------|------|------|
| `frequency_cap.py` | 6 | 用户曝光封顶 + Pacing 算法（防用户疲劳）|
| `consent.py` | 8 | GDPR/PIPL/PDP 用户授权（Article 15/17 数据导出/删除）|
| `pixel.py` | 6 | 商家网站 JS Pixel SDK（自动归因）|
| `disputes.py` | 8 | 商家投诉 + 退款工作流 + 归因回滚 |
| `audiences.py` | 10 | Custom Audience 上传 + Lookalike 算法 |
| `master_accounts.py` | 13 | 多店 master 账号 + RBAC + 邀请接受 + 预算级联 |
| `payouts.py` | 13 | 商家提现 + 银行账户 + 自动结算 + 发票生成 |
| `creative_gen.py` | 10 | ELTM AI 创意生成 + A/B 测试 |
| `storefront.py` | 9 | 公开品牌主页 + 关注 + 评价 + 跨品牌发现 |
| `vouchers.py` | 12 | 跨店发券 / 转赠 / 兑换（建造中）|

**子合计**: 10 routers / ~103 endpoints

---

### 📊 总规模
- **27 个 routers**
- **417+ routes**
- **3 个仓库联动**：kix-platform / eltm（AI 游戏生成）/ code-soul + pm-soul（编排）
- **2 个前端**：portal.html (商家 Portal) / storefront.html (用户公开品牌页)
- **2 个 SDK**：kix.js (gamification 嵌入) / kix-pixel.js (转化追踪)

---

## 🎯 核心商业机制详解

### 1. Quality-Adjusted Vickrey GSP 拍卖

```
rank = max_bid × quality_score × pacing_factor
winner = argmax(rank)
charge = min(ceil(runner_up_rank / winner_qs) + 1, max_bid)
```

- Vickrey 真理：出真实价值 = 最优策略，商家不需博弈
- Quality Score = 0.3 + min(CTR×8, 0.4) + min(CVR×6, 0.3)
- Pacing：每天 50% 时间应该花 50% 预算，超支降权 0.3，落后提权 1.0
- 保留价（reserve price）防止贱卖广告位

### 2. 7 天 Last-Touch 归因 + 多触点扩展

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

### 3. Take Rate Ladder（阶梯激励大商家）

```
GMV < ¥10K      → 商家抽 10% × KiX 拿 30% = KiX 净 3%
GMV ¥10K-100K   → 商家抽 8%  × KiX 拿 25% = KiX 净 2%
GMV ¥100K-1M    → 商家抽 6%  × KiX 拿 20% = KiX 净 1.2%
GMV > ¥1M       → 商家抽 5%  × KiX 拿 15% = KiX 净 0.75%
```

商家越大 take rate 越低 → 激励留存。

### 4. LBS 触发流程

```
1. 用户进入商家 500m 围栏（Redis GEOSEARCH）
2. 触发 push（频率封顶 + 时段限制 + cooldown）
3. 推送游戏链接（impression_token 跟踪）
4. 用户玩游戏 → 拿优惠券
5. 用户进店核销 → 归因订单
6. KiX 抽 CPS 提成
```

---

## 🔌 标准集成方式（给商家）

### 1. 自家网站埋码（5 行 HTML）

```html
<!-- 任何页面顶部 -->
<script src="https://api.kix.gg/sdk/kix-pixel.js" data-pixel="YOUR_PIXEL_ID"></script>

<!-- 注册成功时 -->
<script>kix.identify('user_123');</script>

<!-- 下单成功时 -->
<script>kix.purchase('order_123', 5000);</script>
```

完事。归因、转化、CPS 提成全自动。

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

---

## 🧪 验证战绩（老王 印尼奶茶 10 家店全程模拟）

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

## 📚 完整文档索引

### 战略层
- `MASTER_BLUEPRINT.md` — KiX 总蓝图：全球最强 Gamification → 可定制平台
- `MONETIZATION_V2.md` — **AI 时代货币化模式**（核心商业逻辑）⭐
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
- `/Users/mozat/a-docs/laoli-sim-findings.md` — 老李 广州读书会（运行中）
- `/Users/mozat/a-docs/laozhang-sim-findings.md` — 老张 北京高端餐厅（运行中）
- `/Users/mozat/a-docs/laohuang-sim-findings.md` — 老黄 杭州母婴电商（运行中）

### 历史与对比
- `MONETIZATION.md` — V1 货币化（已超越，留参考）
- `/Users/mozat/a-docs/kix-monetization-trinity.md` — V1 三体研究

---

## 🚀 快速启动

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
open http://localhost:8000/landing/portal.html
```

---

## 🎓 各团队接入要点

### 📈 商业 / 销售团队
**销售话术核心**：
> 「平台全免费。不收月费。不收订阅。功能比 Shopify + Smartico + Bunchball 加起来还多。
>
> 我们怎么赚钱？只有两种：
> 1. 如果我们通过游戏帮你带来一个新客，你愿意付多少？
> 2. 如果我们通过跨品牌网络给你导流一笔交易，我们抽多少？
>
> 全是 performance-based。你赚我们才赚。」

**关键 KPI**：商家上平台数 / 跨品牌订单 GMV / Take Rate / 商家提现率

### 💻 工程团队
**核心架构原则**：
- 三仓独立：kix-platform (业务) / eltm (AI 游戏) / code-soul (编排)
- Redis 是唯一状态存储（PG 仅 brand_config + 长期数据）
- 所有金额 integer cents（永不 float）
- 原子操作 WATCH/MULTI，幂等性必须
- 跨模块只调内部 helper（`*_internal` 函数），不走 HTTP

**关键文件**：
- `app/main.py` — 所有 router 注册入口
- `app/redis_client.py` — Redis 连接池
- `app/config.py` — 配置（含 ELTM_BASE_URL）
- `scripts/e2e_ads_platform.py` — 集成测试模板
- `scripts/sim_*.py` — 多行业商家模拟

### 🎨 产品 / 设计团队
**优先级**：
1. **跨品牌发现 UI**：用户在 A 玩游戏时怎么自然发现 B（最难）
2. **归因透明面板**：让商家看到每笔 KiX 带来的订单（建立信任）
3. **CAC 节省报告**：把抽佣框架成「省了多少传统获客成本」（情绪关键）
4. **商家 Onboarding 路径**：从注册到第一笔归因订单的旅程缩到 7 天内

### 🛡 法务 / 合规
**已实施**：
- `consent.py` 模块强制 GDPR Article 15 / 17（数据导出 / 删除）
- 跨品牌追踪需明确 `cross_brand_tracking` 授权
- 5 维反作弊评分
- 商家投诉 + 退款工作流

**待完善**：
- 跨境合规（印尼商家收 IDR / 中国商家收 CNY / 国际商家收 USD）
- 税务发票自动生成
- VAT / 增值税计算

---

## 🔄 持续迭代节奏（Trinity Protocol）

```
每轮 = ① Industry 业界对标
       ② Academic 第一性原理
       ③ Reality 实地模拟商家

发现 gap → 派并行 agents 修复 → 重新模拟 → 验证收敛
```

**今天的迭代成果**：
- Round 1: 老王 sim → 暴露 11 个 gap (P0=2 P1=7 P2=2)
- Round 2: 派 3 个修复 agent + 1 个根因诊断 → P0+P1 全清零 → 47 PASS

**下轮目标**（运行中）：
- 老李书友会（社区/订阅型）
- 老张高端餐厅（VIP/预订型）  
- 老黄母婴电商（纯线上/高量级）

每个会暴露不同行业的盲点，继续 Trinity 迭代。

---

## 一句话总结（给所有接入成员）

> **我们不卖软件，我们卖网络。AI 让软件功能不值钱，但 AI 永远造不出商家间的用户连接。我们就是那个连接。**

收入路径清晰：
1. **免费送一切** → 招 100 商家 → 验证 attribution
2. **跨品牌发现激活** → 1000 商家 → 月入 ¥38M
3. **网络效应起飞** → 10000 商家 → 年入 ¥1.2-2.4B

每一行代码都为这条路径服务。
