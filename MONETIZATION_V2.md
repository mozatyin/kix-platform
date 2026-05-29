# KiX 平台商业化 v2 — 广告平台模式（TikTok Ads / Google Ads for Gamification）

> **核心论断**: KiX 是广告平台。像 TikTok Ads / Google Ads / Meta Ads。
> **商家之间永远看不到对方。** KiX 是每个商家唯一的合同对手方。
> 软件功能 12 个月内被 AI 复刻 → 全部免费送出。
> 真正护城河 = 拍卖算法 + 跨商家用户池 + 单向网络合约。

---

## ① 三体迭代：当软件不值钱

### Industry — 业界三类商业模式的真实命运

| 类型 | 代表 | 当前状况 | AI 时代展望 |
|------|------|---------|------------|
| **卖软件 SaaS** | Salesforce, HubSpot | 巨人但增长放缓 | AI 复刻威胁严重 |
| **卖订阅** | Shopify, Notion | 稳定但增长压力大 | 商家自建 AI 工具替代 |
| **卖功能模块** | Bunchball, Smartico | 中等规模 | AI Recipe 直接复刻 |
| **卖广告（拍卖网络）** | Google Ads, TikTok Ads, Meta Ads | 主导 + 高速增长 | AI 优化精度更高，护城河更深 |
| **卖连接（清算网络）** | VISA, Mastercard | 持续增长 | AI 无法复刻网络效应 |

### Academic — 第一性原理

```
软件 marginal cost (AI 时代) → ε（趋近零）
拍卖网络 marginal cost → 0 但 network value ∝ N²
广告主出价竞争 → KiX 不需协调任何 bilateral 关系
```

**梅特卡夫定律**: 网络价值 = 节点数² × 连接成本(0) = 指数级增长
**单边网络合约**: 每个商家只与 KiX 签约（N 份合同），而非彼此互签（N² 份合同）→ 可扩展性根本差异

**结论**: 软件做免费的赠品。**拍卖算法 + 单边合约 + 用户池**才是真正的资产。

### Reality — KiX 的位置

| KiX 资产 | 是否能被 AI 复刻？ | 价值 |
|---------|-------------------|------|
| 100 Recipe 模板 | 容易复刻（3 个月内） | 短期护城河 |
| 30+ Gamification 模块 | AI 几周可重建 | 工具，免费 |
| Tutorial / NL Generator | AI 易复刻 | 工具，免费 |
| AI 游戏生成 | ELTM 难复刻但可被超越 | 中期护城河 |
| **拍卖算法 + Quality Score** | 难复刻（需历史数据） | 中长期护城河 |
| **跨商家用户池（单边合约）** | 无法复刻（除非挖走商家） | **真正护城河** |

---

## ② VISA 模型的启示：拍卖驱动，而非 interchange fee

来源：[Card Network Economics - Spark](https://www.spark.money/research/card-network-economics-visa-mastercard)

**KiX 借鉴 VISA 的两点**：
- VISA 自己不发卡、不开店；KiX 自己不开店、不持有用户
- VISA 是 two-sided network：一边补贴（持卡人免费），另一边变现（商家付）

**KiX 与 VISA 的关键不同**：
- VISA 收 **interchange fee**（每笔固定百分比）
- KiX 收 **拍卖费**（商家自己出价决定）→ 更接近 Google Ads / TikTok Ads 模型
- VISA 不区分用户来源；KiX 严格区分「我的用户」vs「别人的用户」，只对后者收费

**Why 拍卖 > interchange**：
- interchange 是固定成本 → 商家觉得「被抽税」（参考美团 12.6% 引发的反弹）
- 拍卖是商家自主定价 → 商家觉得「我在投资 ROI」（参考 Google Ads / TikTok 没有大规模商家叛逃）

### 美团的对照 — 反面教材

来源：[Meituan 抽佣 - SCMP](https://www.scmp.com/tech/article/3133065/)

美团对餐厅抽佣 16-26%，被业界投诉「吸血虫」→ 抖音/快手撬墙脚。

**教训**: 固定抽佣率太高 → 商家逃离。**拍卖让商家自决定花多少**，根本消除这个问题。

### Plenti 的失败 — 「双方签约」死刑判决

来源：[Plenti 失败分析 - Manish Grover](https://www.manishgrover.com/failure-plenti-back-stone-ages-loyalty/)

Amex 投 $1 亿做跨品牌联盟 Plenti（2015-2018）：
- 合作品牌：Macy's, AT&T, Netflix, Exxon, RiteAid, Chilli's — **品牌互相之间签约**
- **3 年关闭**
- 死因：Macy's 退出 → 多米诺骨牌 → 全员退出
- **60% 联盟忠诚度项目 10 年内死亡**

**为什么死？根因 = bilateral 合约结构**：
- 商家想要的是自家用户数据，不是被迫与竞争对手共享
- 合约方退出 = 整个网络瓦解
- 「谁说了算」没人有决策权
- 大品牌发现自建标准忠诚度更好

**KiX 已显式规避 Plenti 陷阱**：

| Plenti 做错的 | KiX 怎么做 |
|--------------|-----------|
| 品牌互相签约 | **品牌只与 KiX 签约**（单边合约） |
| 共享积分（混淆品牌识别） | 每个品牌的积分/Tier/Voucher 完全独立 |
| 联盟集体决策 | KiX 拍卖算法决策（中心化、确定性） |
| 品牌互相看到对方策略 | **商家永远看不到其他商家** |
| 任一品牌退出 = 网络瓦解 | 任一商家退出 = 网络稳定（其他商家继续拍卖） |

> **partnerships.py 模块**: 标记为 OPTIONAL / 高级功能（joint campaigns only，需要商家明确双向同意），**不在主路径上**。99% 的商家永远不会用到它。
> 主路径 = 单边合约 + 拍卖 + 算法撮合。

---

## ③ 商家的两种模式（每个商家两种角色之一或并存）

### 模式 FREE — 管理自己的现有用户

**全功能 Gamification SaaS，永久免费、无上限。**

✅ 全部 50+ Gamification 模块
✅ 全部 100 Recipe 模板
✅ AI 游戏生成（ELTM）
✅ Analytics / Push / SMS / A/B / White Label
✅ 无月费 / 无订阅 / 无 SaaS 费 / 无功能上限

**用于**: 已有用户的留存、激活、复购、社群运营。商家自己的用户、自己玩 Gamification。

### 模式 PAID — 通过拍卖买别人的用户（新客获取）

**只对「new_users_only」拍卖。商家永远不会为自己已有用户付费。**

商家进入 Ads Manager（partner.letskix.com/ads），像 TikTok Ads / Google Ads 一样：

```
1. 充值钱包
2. 创建 Campaign，默认 target_audience=new_users_only
3. 选择出价策略（CPA / CPS / CPM / CPV / CPE）
4. KiX 拍卖算法在背后撮合，商家不需要知道任何其他商家的存在
5. 只在 KiX 真实交付一个新用户/订单时扣款
```

---

## ④ KiX 的明确收入线（拍卖驱动，多策略并行）

### Revenue Line 1: **CPS** — 新客成交订单分成

**机制**:
```
商家 B 在 Ads Manager 出价: 「新客成交订单我愿付 8% 给 KiX」
KiX 拍卖在合适时机把 B 的 offer 展给 B 没有的用户 U
U 在 B 完成首次成交 → KiX 抽 8%
B 不知道这个用户从哪个商家过来，KiX 也不告诉 U 这是「跨品牌」
```

**关键设计**:
- 只对 new_users_only 收费（U 必须之前不是 B 的用户）
- 拍卖决定哪些商家的 offer 被展给 U
- 商家自定出价（5-15% 区间，远低于美团 12.6%）

### Revenue Line 2: **CPA** — 每个新注册用户固定单价

**机制**:
```
商家 X 在 Ads Manager: 「我要新用户，¥20 / 个」
KiX 拍卖 + Quality Score 匹配最相关的 KiX 用户
每个新注册 + 完成首次互动 → KiX 收 ¥20
```

**对标**:
- Facebook Ads CPA: ¥50-300
- 抖音电商 CPA: ¥30-200
- 美团广告 CPA: ¥40-150
- **KiX CPA: ¥10-100**（基于游戏引导，质量分加权）

### Revenue Line 3-5: **CPM / CPV / CPE**（TikTok parity）

| 策略 | 收费触发 | 适用场景 |
|------|---------|---------|
| **CPM** | 每千次曝光 | 品牌曝光、新店启动 |
| **CPV** | 每次进店访问 | 线下流量（geofence + QR） |
| **CPE** | 每次游戏完成 | 互动深度（Gamification 原生） |

商家在 Ads Manager 自由组合。**所有策略共享同一个拍卖引擎**（`auction.py`：Quality-adjusted Vickrey GSP）。

---

## ⑤ How a Merchant Joins（商家加入流程）

```
1. 在 partner.letskix.com 注册
   → 立即获得免费 SaaS（FREE 模式）：完整 Gamification 管理自己用户

2. 想要获取新用户（不是自己现有的）？
   → 进入 Ads Manager
   → 充值钱包（wallet.py）
   → 创建 Campaign（campaigns.py）
       默认 target_audience = new_users_only
       选出价策略 = CPA / CPS / CPM / CPV / CPE
       设出价上限
   → KiX 拍卖（auction.py）+ Quality Score 在背后运作

3. 商家永远不需要知道：
   ✗ 其他商家是谁
   ✗ 其他商家在出价什么
   ✗ 用户上一个 touch 是哪个商家
   ✗ 任何 bilateral 合约

4. KiX 交付新用户/订单 → 扣钱包余额
   不交付 → 不扣
```

**KiX 是每个商家唯一的合同对手方。** 商家与 KiX 签 ToS，KiX 与所有商家签 ToS，**商家之间永远不签任何东西**。

---

## ⑥ 对标 TikTok Ads / Google Ads / Meta Ads

| Feature | TikTok Ads | Google Ads | Meta Ads | **KiX** |
|---------|-----------|-----------|----------|---------|
| Auction-based bidding | ✅ | ✅ | ✅ | ✅ |
| Brand-to-brand contracts | ❌ | ❌ | ❌ | ❌ |
| Existing-customer exclusion (default) | ✅ | ✅ | ✅ | ✅ |
| Quality Score | ✅ | ✅ | ✅ | ✅ |
| Smart bidding (auto-optimize) | ✅ | ✅ | ✅ | ✅ (`auction.py` Smart Bidding) |
| Custom Audiences + Lookalike | ✅ | ✅ | ✅ | ✅ (`audiences.py`) |
| Frequency cap | ✅ | ✅ | ✅ | ✅ (`frequency_cap.py`) |
| Conversion pixel | ✅ | ✅ | ✅ | ✅ (`pixel.py`) |
| **Free SaaS for managing own users** | ❌ | ❌ | ❌ | ✅ **(KiX unique)** |
| **Gamification-native ad units** | ❌ | ❌ | ❌ | ✅ **(KiX unique)** |

**KiX 在标准广告平台之上多了两件事**：
1. 商家管理自己用户的部分（FREE 模式）= 全功能 SaaS 永久免费
2. 广告位载体 = 游戏（CPE 互动深度远高于 banner/feed）

---

## 凡是软件能做的，全免费

**完整免费功能清单（永久）**:

- 平台注册、登录、Dashboard、API
- 游戏定制、AI 生成（ELTM）
- 50+ Gamification 模块全部
- 100 Recipe 模板全部
- NL Generator AI 配方
- Tutorial Engine 教程
- Conditions Engine 条件
- RuleEngine 规则
- Analytics 数据分析
- SDK / Embed Widget
- Push / SMS 通道（成本价转给商家，不加价）
- A/B 测试引擎
- 多语言翻译
- White Label（去 logo）
- 客服工具 / 培训认证 / 客户成功 1对1（前 100 商家）

**为什么这些全免费**：
**因为 AI 让所有这些 12 个月内会被竞争对手免费送出。** 我们先发，先送，把 100 → 1000 → 10000 商家锁进网络。

---

## 拍卖系统的关键设计

### 设计 1: 归因（attribution.py）

只有可归因才能收费。每次拍卖产生一个 `impression_token`，pixel/SDK 回传后形成归因链。

```python
# 用户被拍卖结果展示
GET /api/v1/auction/serve?user_id=U&context=...
  → 返回中标商家 B 的 offer + impression_token

# 用户访问 B
POST /api/v1/attribution/track/click
  Body: {impression_token, user_id}

# B 商家用户消费（pixel 自动回传）
POST /api/v1/attribution/track/conversion
  Body: {user_id, target_brand=B, amount_cents, order_id}
  → 7 天 last-touch attribution window
  → 若 U 是 B 的 new user → 按出价策略扣 B 钱包
```

商家 B 永远只与 KiX 交互。看不到 U 之前在哪里、看不到其他商家。

### 设计 2: 商家对价表（透明）

| 服务 | KiX 收费 | 商家可控 |
|------|---------|---------|
| FREE 模式 — 管理自己用户的全部 SaaS | ¥0 | 完全自主 |
| PAID — CPA（新用户） | 商家出价（参考 ¥10-100/人） | 自己设上限 |
| PAID — CPS（新客成交分成） | 商家出价（参考 5-15%） | 自己设上限 |
| PAID — CPM / CPV / CPE | 商家出价 | 自己设上限 |

只有「出价就收，不出就不收」一条铁律。

### 设计 3: 商家退出零成本

- 关闭所有 PAID Campaigns → 不再扣钱包 → 钱包余额可提现（payouts.py）
- FREE 模式永久保留 → 自家用户的 Gamification 继续跑
- 没有沉没成本，没有锁定

**但**：退出 = 失去 KiX 用户池的新客获取通道。这是网络效应的真正锁定，不是合同锁定。

---

## 财务模型测算（基于拍卖驱动）

### 假设

- 1000 商家加入（500 仅 FREE，500 同时跑 PAID）
- PAID 商家平均月预算 ¥3,000
- KiX take（拍卖均价 + Quality adjust）: ~80% spend efficiency
- KiX 月收入 ≈ 500 × ¥3,000 = ¥1.5M
- 年化 ≈ ¥18M

### 10,000 商家（成熟期）

- 7000 跑 PAID，平均月预算 ¥5,000（网络效应抬升 ROI）
- KiX 月收入 ≈ 7000 × ¥5,000 = ¥35M
- 年化 ≈ **¥420M**

### 100,000 商家（梦想）

- 网络效应 + Quality Score 数据复利 → 商家 ROI 上升 → 平均预算 ¥10,000
- KiX 年化 → **¥几十亿级**

对比：TikTok Ads 2023 年广告收入 ~$200B 全球，Google Ads ~$240B。**拍卖驱动的天花板就是这个量级**。

---

## 关键启动顺序

```
阶段 0 — 现在：免费招商家（FREE 模式）
  目标：100 商家上平台，全部用 FREE
  收入：¥0
  动作：免费送一切 SaaS，找种子商家
  时间：3 个月

阶段 1 — 建拍卖系统（已完成 ✓）
  ✓ campaigns.py (21 endpoints)
  ✓ auction.py (8 endpoints, Quality-adjusted Vickrey GSP)
  ✓ wallet.py (10 endpoints)
  ✓ attribution.py (22 endpoints)
  ✓ pixel.py / frequency_cap.py / audiences.py / disputes.py

阶段 2 — 启动 PAID（CPA 优先）
  目标：30 商家试 CPA，验证 ROI
  收入：实验性 ¥30-100K / 月
  时间：1-2 个月

阶段 3 — 全策略上线（CPS + CPM + CPV + CPE）
  目标：商家自主配置组合策略
  收入：¥500K-2M / 月
  时间：3-6 个月

阶段 4 — 规模化 + 网络效应
  目标：拍卖数据复利、Quality Score 收敛
  收入：¥10M+ / 月
  时间：6 个月后
```

---

## 必须避开的 3 个陷阱

### 陷阱 1: Plenti / 双方签约 — **已显式规避**
**严格禁止任何 bilateral 合约出现在主路径。** 商家之间永远互不可见。partnerships.py 仅 OPTIONAL 高级功能（joint campaigns，需双向显式同意），不在主路径。

### 陷阱 2: 美团固定抽佣过高
**KiX 不固定抽佣。** 商家自己出价，KiX 不强加百分比。这从根本上消除「被抽税」感。

### 陷阱 3: 跨品牌冷启动鸡和蛋
**需要前 1000 商家 + 100 万用户才有拍卖深度。** 在那之前 PAID 模式只是 demo。前 6 个月主推 FREE 模式价值，让商家先把自己用户运营好。

---

## 与现有平台的差异化

| 平台 | 本质 | 收费方式 | KiX 差异 |
|------|------|---------|---------|
| Shopify | 卖建站 SaaS | 月费 + 支付 | KiX 功能更多 + 完全免费 |
| Smartico | 卖 Gamification SaaS | 月费 | KiX 也提供，且免费 |
| Google Ads / TikTok Ads | 拍卖广告网络 | 商家出价 | KiX 同结构 + 多了 FREE SaaS 层 |
| 美团 | 固定抽佣外卖网络 | 12-26% 抽佣 | KiX 不固定抽佣，商家出价 |
| Plenti（已倒闭） | 双方签约联盟 | 失败模型 | KiX 单边合约，根本规避 |
| VISA | 清算网络 | 0.1-0.2% | KiX 用拍卖代替固定费率 |

**KiX 独特组合：免费 SaaS（FREE）+ 拍卖广告网络（PAID）+ Gamification 原生载体**

---

## 与产品/技术/商业团队的对齐

### 商业团队的销售话术

> 「这个平台像 TikTok Ads / Google Ads，但更好。
>
> 你管理自己用户的所有 Gamification 功能 — **永久免费**。
>
> 想要别人家的用户？进 Ads Manager，出价 CPA / CPS / CPM / CPV / CPE，KiX 拍卖给你撮合。你只在我们真的交付新用户时付钱。
>
> 你不需要和任何其他商家打交道。你的对手方只有 KiX。」

### 技术团队的实施重点

**P0（已完成）**：
- ✓ 拍卖引擎（auction.py，Quality-adjusted Vickrey GSP）
- ✓ 归因系统（attribution.py，7 天 last-touch + 多触点）
- ✓ 钱包 + 自动扣款（wallet.py）
- ✓ Campaign 层级 + Review Queue（campaigns.py）

**P1**：
- Smart Bidding 优化（auction.py 已有骨架）
- Lookalike 算法精度提升（audiences.py）
- Quality Score 数据复利

**OPTIONAL（不在主路径）**：
- partnerships.py — joint campaigns，需要双向显式同意。大部分商家用不到。

### 产品团队的方向

- Ads Manager UI（参考 TikTok Ads Manager / Google Ads UI）
- Quality Score 透明化（让商家看到为什么自己排名第 N）
- 归因透明面板（每笔扣款都能追溯到 impression）
- 「我节省了多少传统获客 CAC」报告（情绪关键）

---

## 参考来源

- [Card Network Economics - Spark](https://www.spark.money/research/card-network-economics-visa-mastercard)
- [Google Ads Auction Mechanics](https://support.google.com/google-ads/answer/142918)
- [TikTok Ads Bidding Methods](https://ads.tiktok.com/help/article/bidding-methods)
- [Meta Ads Auction](https://www.facebook.com/business/help/430291176997542)
- [Meituan 抽佣率 - SCMP](https://www.scmp.com/tech/article/3133065/)
- [Plenti 失败分析](https://www.manishgrover.com/failure-plenti-back-stone-ages-loyalty/)
- [60% Coalition 项目 10 年内死亡](https://ascendantloyalty.com/coalition-loyalty-programs-successes-and-failures/)

---

## 一句话总结

**KiX = TikTok Ads for Gamification。**

商家管理自己用户全免费；想要别人的用户就来出价，KiX 拍卖算法撮合。商家永远不与其他商家打交道，KiX 是唯一合同对手方。

软件免费，拍卖收费，单边合约 N 而非 N²，网络效应才是真正护城河。
