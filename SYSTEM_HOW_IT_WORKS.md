# KiX 系统是怎么工作的（端到端讲清楚）

> 给所有团队成员的「3 分钟读完」版系统机制说明
> 配套阅读：[PLATFORM_OVERVIEW.md](PLATFORM_OVERVIEW.md) / [MONETIZATION_V2.md](MONETIZATION_V2.md)

---

## 🎯 三类角色

```
┌─────────────────────────────────────────────────────────┐
│  普通用户（你和我）                                       │
│  · 在 KiX App 里玩游戏 / 拿优惠 / 跨品牌发现              │
│  · 唯一身份：kid_xxxxxxx                                 │
└─────────────────────────────────────────────────────────┘
                              ↑↓
┌─────────────────────────────────────────────────────────┐
│  KiX 平台（中间运营方）                                   │
│  · 32+ routers / 600+ endpoints                         │
│  · 全免费工具 + 拍卖撮合 + 网络效应                       │
└─────────────────────────────────────────────────────────┘
                              ↑↓
┌─────────────────────────────────────────────────────────┐
│  商家 (Brand)                                            │
│  · 老王奶茶 / 老张餐厅 / 老黄电商 / ...                   │
│  · 永远只跟 KiX 沟通，从不跟其他商家见面                  │
└─────────────────────────────────────────────────────────┘
```

---

## 🚪 用户旅程（5 步）

### 第 1 步 · QR 扫码进入
```
用户在老王奶茶店扫描 QR
  ↓
浏览器打开 KiX App (partner.letskix.com/app/)
  ↓
后台 POST /kix-id/qr-scan/bind
  ↓
新人：自动注册 kid_xxxxxxx
老人：识别 device_fingerprint，复用现有 kid
  ↓
游戏自动加载（老王为这次扫码配置的游戏）
```

### 第 2 步 · 玩游戏 → 拿优惠
```
用户玩"消消乐"30 秒
  ↓
触发拍卖：/auction/run {kid, context: {time, lat, lng, source_brand: 老王}}
  ↓
老王的 campaign 默认 target_audience=new_users_only
  ↓
如果用户不是老王的现有客户：老王赢拍卖
  ↓
用户获得"免费小杯优惠券"（store in voucher:{vid}）
  ↓
老王扣 ¥20 CPA 给 KiX（CPS 5% 给 KiX 也可以）
```

### 第 3 步 · 跨品牌算法推送
```
用户离开老王店
  ↓
KiX App 后台运行：/push/now {kid, context}
  ↓
Push Engine 评估候选 campaign：
  relevance_score = 0.4×category_match + 0.3×geo + 0.15×time + 0.15×freshness
  ↓
老张餐厅（不是用户现有客户）出价 ¥15/新客 → 赢拍卖
  ↓
用户手机收到推送："你附近的老张餐厅有免费开胃菜"
  ↓
老张扣 ¥15 给 KiX（push 送达即扣）
```

### 第 4 步 · 用户到老张消费
```
用户点开推送 → 跳进 KiX App → 看到老张的游戏 + 优惠券
  ↓
用户进店扫码核销 (geofence enter)
  ↓
老张系统 POST /attribution/track/conversion
  {kid, brand=老张, amount=200, source_brand=老王(自动归因)}
  ↓
7 天 last-touch 归因找到老王 → 老王是引流方
  ↓
CPS 5% 提成自动分账：
  老张付 ¥10 → KiX 拿 ¥3 + 老王拿 ¥7
  (通过 payouts.inter-brand-transfer 双账分录)
```

### 第 5 步 · 用户跨多家品牌
```
KiX 累计该用户在 5 个商家的互动
  ↓
形成画像
  ↓
下次出现 → 算法精准匹配 → 推送更高价值商家
  ↓
网络效应：商家越多 → 用户画像越准 → 匹配效率越高
```

---

## 🏪 商家旅程（4 步）

### 第 1 步 · 注册即免费
```
商家访问 partner.letskix.com → 注册
  ↓
立刻拿到全部免费工具：
  · 50+ Gamification 模块（XP/badge/streak/quest/tier）
  · 79 Recipe 模板（按行业筛）
  · NL → Recipe AI 生成器（说一句话生成游戏）
  · Pixel SDK (5 行 HTML 埋码)
  · A/B testing / 受众管理 / 分析仪表盘
  · 不收月费，不收订阅费
```

### 第 2 步 · 充值钱包 + 设广告活动
```
想要拉新客 → 充值 ¥10000 → /wallet/{bid}/topup
  ↓
创建 Campaign：
  /campaigns/create {
    objective: "acquire",
    bid_strategy: "cpa",
    max_bid_cents: 2000,    // ¥20/新客
    target_audience: "new_users_only",  // 默认不重复买自家客户
    targeting: {
      geo: {country: "ID", city: "Jakarta", radius_km: 30},
      demographics: {age_min: 18, age_max: 45}
    },
    creative: {recipe_id: "starbucks_loyalty"}
  }
  ↓
默认自动审批通过 → 立刻进入拍卖池
```

### 第 3 步 · 等 KiX 算法交付
```
商家什么都不用做 → KiX 算法自动：
  · 实时拍卖（每次用户出现 → 谁出价最高 + quality_score 最优 → 赢）
  · Pacing（每小时均匀消耗预算，不一口气烧完）
  · 排除自家现有客户（不浪费钱买回老客）
  · 频率封顶（同一用户每天不超过 3 次曝光）
  · 反作弊（设备指纹速度 >3次/60秒 自动 429）
```

### 第 4 步 · 看效果 + 自动扣费
```
Dashboard 实时显示：
  · 今日交付了多少新用户
  · 这些用户转化了多少（CPS bid_percent_bps）
  · CAC / ROAS / 同行业平均对比
  · 跨店访问报表（多店商家）
  · 节省了多少 CAC（vs 排除前）

只在交付成功时扣费：
  CPA: 每新注册 ¥20
  CPS: 每笔订单的 5%
  CPM: 每千次曝光 ¥30
  CPV: 每次到店 ¥3
  CPE: 每次游戏完成 ¥1
```

---

## ⚙️ KiX 平台内部（核心机制）

### 拍卖引擎 (Quality-adjusted Vickrey GSP)
```
rank = max_bid × quality_score × pacing_factor
winner = argmax(rank)
charge = min(ceil(runner_up_rank / winner_qs) + 1, max_bid)
```

**为什么这样设计**：
- **Vickrey 二价**：出真实价值 = 最优策略（商家不需博弈）
- **Quality Score**：CTR/CVR 反馈，质量越高越省钱
- **Pacing**：每天平均消耗，防止一小时烧完

### 归因系统 (7 天 last-touch + 多触点)
```
用户旅程: [事件1, 事件2, ..., 事件N]
对每个 conversion：从最新往回找，
找第一个 source_brand != target_brand 的事件
→ 那就是引流方 → 自动佣金分账
```

可配置窗口：1-365 天（医疗 / 旅游 / 房产 / 教育 等长周期场景）

### 3 个独立钱包
- **老王**（引流方）：自动收到佣金分成
- **老张**（接收方）：付了佣金但获得真实客户
- **KiX**：作为撮合方收平台费

### 数据流（一笔交易完整链路）
```
1. 用户在老王店玩游戏
2. POST /attribution/track/event {kid, brand=老王, stage=engagement}
   → user:{kid}:journey LIST 增加一条
3. 用户去老张消费
4. POST /pixel/event {pixel_id=老张, type=purchase, amount=200}
   → 自动调用 /attribution/track/conversion
5. 归因引擎查 journey → 找到老王
6. 自动调用 wallet.charge(老张, 10) 
   → wallet.refund(老张:佣金回到老张) 
   → payouts.inter-brand-transfer(老张→老王, ¥7)
7. KiX 自动留 ¥3 作平台费
```

---

## 🔐 安全 + 合规层

| 层 | 解决 | 实现 |
|----|------|------|
| **Consent** | GDPR/PIPL/PDP | 15 个 scope，7 个 regulated scope 需 OTP/签名/视频 |
| **PII 审计** | 个保法 §51 | 敏感字段每次访问留痕，1h 异常检测窗口 |
| **Compliance Scanner** | 广告法 §7 §25 | 70 条 banned phrase 自动扫描 + 强制注入风险提示 |
| **Frequency Cap** | 防用户疲劳 | 全局 10/天 + 单品牌 3/天 + tier override + priority bypass |
| **Fraud** | 反作弊 | 设备指纹速度 / self-attribution / token replay / trust_score |
| **Document Consent** | 双录 / 医疗同意书 / 买卖合同 | OTP / signature / video_recording 强证据 |
| **Media Registry** | 敏感图片/视频 | medical_sensitive / before_after / biometric 必需 consent_grant_id |
| **Legal Hold** | 司法保全 | 防止医疗 / 法务证据被删 |

---

## 🌐 完整模块清单（32+ routers）

```
┌────────────────────────────────────────────────────────────────────┐
│ Layer 5 · 商业化         attribution + auction + wallet + campaigns │
│                          payouts + fx + compliance + disputes        │
│                          audiences + frequency_cap + partnerships    │
│                          creative_gen + storefront + reservations    │
│                          transactions + fraud                        │
├────────────────────────────────────────────────────────────────────┤
│ Layer 4 · 商家入口       portal.html (Ads Manager) +                │
│                          storefront.html (公开品牌页) +              │
│                          api-docs/ (Swagger UI + 21 tags)            │
├────────────────────────────────────────────────────────────────────┤
│ Layer 3 · 网络层         push_engine + master_accounts +            │
│                          kix_id + listings + media                  │
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

---

## 💰 商业模式 = 1 句话

**「软件免费送，网络收费。」**

- 软件价值：AI 12 个月内会被复刻 → 永远免费
- 网络价值：跨商家用户连接 → 唯一不可复刻 → 唯一收费

每个商家 → 只跟 KiX 签合同 → 全免费用工具 → 想买别人家用户就出价 → KiX 算法撮合 → 交付成功扣费

### 5 种出价策略（同一拍卖引擎）

| 出价 | 何时扣费 | 用途 | 参考价 |
|------|---------|------|--------|
| **CPA** | 转化时（新注册） | 拉新主流 | ¥10-100 |
| **CPS** | 转化时（订单比例） | 跨店带单 | 5-15% |
| **CPM** | 每千次曝光 | 知名度 | ¥10-50 |
| **CPV** | 每次到店 | LBS 引流 | ¥1-10 |
| **CPE** | 每次游戏完成 | 互动深度 | ¥0.5-5 |

---

## 🎬 当前平台状态

| 维度 | 数字 |
|------|------|
| Platform routes | **600+** |
| Routers | **32+** |
| Recipe 库 | **79**（26 行业）|
| 商家行业完整模拟 | **18+** |
| Trinity 迭代轮次 | **9 轮** |
| E2E 测试 | **24/24 PASS** |
| 总 passes (16 核心 sim) | **730+** |

### 已模拟覆盖的 16+ 行业
- 印尼奶茶（10 店）/ 北京高端餐厅 / 广州书友会 / 杭州母婴电商
- 上海健身（5 店）/ 深圳 K12 / 上海医院 / 杭州旅游
- 上海医美 / 北京金融 / 深圳房产 / 杭州美发
- 广州物流 / 二手 C2C / 成都宠物 / 共享单车
- + 直播带货 + B2B SaaS

---

## 📚 配套文档

- [PLATFORM_OVERVIEW.md](PLATFORM_OVERVIEW.md) — 完整平台总览（推荐先看）
- [MONETIZATION_V2.md](MONETIZATION_V2.md) — TikTok/Google 模型商业逻辑
- [README.md](README.md) — 工程启动指南
- [ENGINEERING.md](ENGINEERING.md) — 工程移交手册
- [GAMIFICATION_AUDIT.md](GAMIFICATION_AUDIT.md) — 全球 100 案例审计

---

## 💡 给新接入成员的 5 个关键认知

1. **KiX 是平台，不是服务商**：不卖软件，卖网络。商家越多，价值越高。
2. **商家从不见面**：单边合约。算法是唯一的撮合方。
3. **用户身份是 kid，不是手机号**：跨品牌追踪靠 KiX ID + Connect 协议（类似 Facebook Connect）。
4. **拍卖驱动 vs 抽佣驱动**：商家自主出价，按真实交付付费 — 与美团 12-26% 强制抽佣模型完全不同。
5. **Gamification 是载体，网络是商品**：游戏让用户停留，停留产生数据，数据训练算法，算法精准匹配，匹配产生收入。
