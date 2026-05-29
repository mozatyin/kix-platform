# 商家旅程的真相 — 从注册到广告主的完整漏斗

> **关键洞察**：商家不是一夜变金主。先免费上钩 → 看到效果 → 主动升级。
> 同时**双重护城河**：信用卡防刷 + 高级版订阅 = KiX 的第二收入线。

---

## 🎯 完整 7 步商家漏斗

```
1. 销售触达 / 自然发现
   ↓
2. 注册（认证）+ 留信用卡
   ↓
3. 创建游戏（免费 1 个）
   ↓
4. 收到欢迎礼包（实体 QR 桌牌）
   ↓
5. 看到每日扫码 + 转化（爱上 KiX）
   ↓
6. 升级到 Premium（多游戏 + 功能）
   ↓
7. 启动广告活动（拉新 / 跨店带客）
```

---

## 第 1 步 · 销售 / 自然触达

```
触达渠道：
  ├── 销售员（地推 / 电话 / 微信）
  ├── 平台 SEO 搜索
  ├── 老商家推荐（refer-a-merchant）
  └── 行业活动 / 媒体
```

**Pricing 透明展示**：
- ✅ 平台全免费用
- ✅ 1 个游戏免费开
- 💎 多个游戏需要 Premium 订阅
- 💰 想拉新客？充钱开广告

---

## 第 2 步 · 注册 + 信用卡（认证关卡）

### 流程
```
POST /brands/register
  Body: {
    brand_name, brand_slug, 
    business_license_no, contact_phone, contact_email,
    industry, country, primary_city,
    branches: [
      {store_id, address, lat, lng, hours},
      ...
    ]
  }
  → mints brand_id (or master_id if multi-store)
  → status: "pending_verification"
```

**强制信用卡 / 支付方式**：
```
POST /brands/{bid}/payment-method/setup
  Body: {
    method: "credit_card"|"wechat_pay"|"alipay"|"corporate_account",
    card_token (from Stripe/payment gateway),
    holder_name, billing_address
  }
  → required even for FREE tier
  → 第一年不扣费，但卡必须有效
  → 防止坏人乱刷免费账号 + 第二年自动升级订阅有支付
```

### 双重防御
1. **认证**（business license + 电话验证）→ 防虚假账号
2. **信用卡**（即使不扣）→ 防一人开 100 个测试账号刷 KiX

### 销售话术
> 「填一下信用卡，KiX 第一年不会扣你一分钱。这是给你保留账号优先权 + 防止恶意刷号。」

---

## 第 3 步 · 创建游戏（免费上钩）

```
POST /games/create
  Body: {
    brand_id, 
    name: "南洋茶饮 - 奶茶大转盘",
    recipe_id: "starbucks_loyalty",  // 从 79 个 Recipe 选
    OR description: "我想要一个用户每天能扫码玩的转盘游戏"  // NL→Recipe
    voucher_template_id: "free_small_drink",
    visual_config: {...}
  }
  → mints game_id
  → 检查 tier 限额：
    FREE tier: 已有 0 个 → 允许
    FREE tier: 已有 1 个 → 422 "upgrade to premium"
    PREMIUM tier: 不限
```

### Tier 限额

| Tier | 月费 | 游戏数 | 模块数 | 高级功能 |
|------|------|--------|--------|---------|
| **FREE** | ¥0 | 1 个 | 全部 | 无 |
| **STARTER** | ¥199/月 | 3 个 | 全部 | A/B 测试 |
| **GROWTH** | ¥999/月 | 10 个 | 全部 | A/B + 自定义品牌色 |
| **ENTERPRISE** | ¥5000/月 | 无限 | 全部 | + 白标 + SLA + 专属客服 |

**关键**：第一年 STARTER 也免费送（信用卡留着兜底自动续费）。

---

## 第 4 步 · 收到欢迎礼包 🎁

注册并创建第一个游戏后，自动触发：

```
POST /brands/{bid}/welcome-kit/generate
  → 自动生成：
  - PDF 桌牌（含 QR + brand logo + slogan）→ 下载
  - 桌牌运到门店地址（可选物流）→ KiX 寄送
  - 收银台立牌
  - 玻璃门贴纸（"扫码玩游戏赢奖励"）
  - WeChat / 抖音 朋友圈海报模板
```

### 心理钩子
> 商家看到 KiX 寄来的实体 QR 桌牌（带他自己的 logo）→ 感觉「这玩意是真的」→ 摆到收银台 → 顾客真的扫了 → 真的拿到优惠券 → 真的来核销

---

## 第 5 步 · 看到每日扫码 + 转化（养成爱用习惯）

商家 Portal 首页（FREE tier 都能看）：

```
┌────────────────────────────────────────────────┐
│  今日                                          │
│  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ │
│  🔍 12 次 QR 扫描                              │
│  🎮 9 次游戏完成（75% 转化）                    │
│  🎁 6 张优惠券领取                              │
│  ✅ 3 张已核销 (50%)                            │
│  ⭐ 2 个新注册用户                              │
│  📞 +2 个手机号绑定                             │
│                                                │
│  累计                                          │
│  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ │
│  👥 145 个粘性用户                              │
│  💰 ¥2,340 节省 CAC（vs 美团抽佣）              │
└────────────────────────────────────────────────┘
```

**心理学**：
- 每天看到「今天又有 X 个客户用了你的游戏」 = 多巴胺
- 看到「已节省 CAC ¥2,340」 = 数字胜利
- 不需要付费就有数据 = 上瘾

### 后台对应
```
GET /brands/{bid}/dashboard/today
  → {scans, game_plays, vouchers_issued, redeemed, new_users, phone_linked}

GET /brands/{bid}/dashboard/cumulative
  → {sticky_users, cac_saved_cents, ...}
```

---

## 第 6 步 · 主动升级到 Premium（KiX 收第一笔钱）

商家用 FREE 1 个月，看到效果，想做更多 → 自然升级：

### 升级触发点

| 触发 | 升级到 |
|------|--------|
| 想做第 2 个游戏 | STARTER |
| 想做 A/B 测试 | STARTER |
| 想做 5+ 个游戏 | GROWTH |
| 想要白标 / 无 KiX 标 | ENTERPRISE |
| 想要专属客服 / SLA | ENTERPRISE |

### 流程
```
POST /brands/{bid}/subscription/upgrade
  Body: {to_tier: "starter"|"growth"|"enterprise"}
  → 立刻使用 Premium 功能
  → 第一年仍免费（合同标记）
  → 第二年自动扣信用卡（年付 / 月付）
```

### 销售话术
> 「现在解锁多游戏 + A/B 测试，让 KiX 帮你测出最赚钱的玩法。第一年还是免费，第二年才扣信用卡。」

---

## 第 7 步 · 启动广告活动（KiX 收第二笔钱）

商家完全自助，看到数据稳定增长，想拉新：

### 两种广告类型

```
POST /brands/{bid}/wallet/topup
  Body: {amount_cents: 100000}  // 充值 ¥1000

POST /campaigns/create
  Body: {
    type: "in_store"|"cross_brand",
    
    // in_store: 把人拉到我店里
    type === "in_store" → {
      objective: "geo_visit",
      bid_strategy: "cpv",  // 每次到店付 ¥3
      target_audience: "new_users_only",
      targeting: {geo: {around_store, radius_km: 5}}
    },
    
    // cross_brand: 让别家用户来买我
    type === "cross_brand" → {
      objective: "acquire" 或 "sales",
      bid_strategy: "cpa" 或 "cps",
      target_audience: "new_users_only",
      // KiX 算法在网络里找匹配用户
    }
  }
  → 立刻进入拍卖池
```

### 一句话
> 「FREE 版让你看到效果。Premium 版让你做更多。Wallet 充钱让你赚到更多。」

---

## 💰 KiX 的双重收入护城河

```
收入 1（基础）：广告 CPA/CPS/CPM/CPV/CPE
  → 商家充钱 → 拍卖扣费 → 拉新成功才收钱
  → 跟 TikTok Ads / Google Ads 同模式

收入 2（订阅）：Premium 月费
  → STARTER ¥199 / GROWTH ¥999 / ENTERPRISE ¥5000
  → 第二年开始扣
  → 全是 ARR 高利润

总公式：
  KiX 收入 = 商家广告支出 × take_rate(0.3-0.7)
          + 商家订阅 ARR
```

---

## 🛡 双重防刷设计

| 风险 | 防御 |
|------|------|
| 一人开 100 个免费账号 | ⚠️ 信用卡留底 + 一卡一账号 |
| 假商家骗免费工具 | ⚠️ 营业执照认证 + 电话验证 |
| 真商家不付 Premium 钱 | ⚠️ 第一年免费免疑虑，第二年自动续费 |
| 商家恶意刷 KiX 推流量 | ⚠️ Fraud 模块 trust_score + AML |
| 跨账号撞品牌 | ⚠️ business_license_no 唯一 |

---

## 🎯 7 步漏斗预期转化

| 步骤 | 漏斗 | 累计 |
|------|------|------|
| 触达商家 | 100% | 100% |
| 注册（含信用卡）| 30% | 30% |
| 创建第一个游戏 | 85% | 25.5% |
| 收到欢迎礼包 | 95% | 24.2% |
| 持续登录看数据 | 70% | 16.9% |
| 升级 Premium | 25% | 4.2% |
| 启动广告 | 50% | 2.1% |

**单年单商家 LTV**：
```
Premium ARR ¥999 × 70% 续费率 × 3 年 = ¥2,100
广告支出 ¥2000/月 × 12 × take_rate 30% = ¥7,200
总 LTV ≈ ¥9,300 / 商家

CAC（销售 / 物流 / 客服）≈ ¥200-500
ROI ≈ 18-46x
```

---

## 📦 当前代码缺口（需要补的）

| 能力 | 状态 | 优先级 |
|------|------|--------|
| Brand 注册 + 多门店 | ✅ master_accounts | - |
| 信用卡 / 支付方式存储 | ⚠️ wallet 仅 topup，缺 method-on-file | **P0** |
| **Brand-level subscription tier** | ❌ 缺 | **P0** |
| 游戏数量 quota 限制 | ❌ 缺 | **P0** |
| Welcome kit 自动生成（PDF 桌牌）| ❌ 缺 | P1 |
| Today dashboard（每日多巴胺）| ⚠️ 数据有，UI 缺 | **P0** |
| 累计 CAC saved 计算 | ✅ /auction/admin/savings | - |
| 自助升级订阅 | ❌ 缺 brand-level subscription | **P0** |
| 信用卡自动续费 | ❌ 缺自动扣款 worker | P1 |
| Business license verification | ❌ 缺人工 / API 核验 | P1 |

**核心 P0**：
1. **Brand subscription tier**（不只是 user tier，是 brand 自己的 FREE/STARTER/GROWTH/ENTERPRISE）
2. **支付方式 on-file**（信用卡留底，不一定扣）
3. **游戏数 quota** 按 tier 限制
4. **Today dashboard** UI（养成习惯的核心）
5. **自助升级流**（点击升级 → tier 切换 → 配额放开）

---

## 🎁 商家激励 = 客户激励

```
商家激励轴：
  - 注册即送：免费工具 + 欢迎礼包
  - 上瘾点：每日 dashboard 多巴胺
  - 升级动机：第 2 个游戏需要 Premium
  - 长期绑定：信用卡 + 第二年订阅

客户激励轴（USER_FLOW_TRUTH.md）：
  - 注册即送：手机号留下保住战利品
  - 上瘾点：每次扫码都有惊喜
  - 升级动机：下载 App 拿更多券
  - 长期绑定：跨商家奖励聚合
```

两条漏斗在 KiX 平台交汇 → 网络效应自我催化。
