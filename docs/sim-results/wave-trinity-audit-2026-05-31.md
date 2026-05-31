# Trinity 三体迭代审计 — 最终版 (2026-05-31)

聚焦用户 3 个问题:
1. **System 承诺 — 优势/劣势** (我们 ship 的 vs PDFs 说的)
2. **竞争对手 unmet needs** — 我们能填的
3. **能否 follow Bible** — 执行纪律

数据源全部到位:
- ✅ Bible HEAD 436da2c (0% drift, 123 routers / 1,530 tests / 47 services / 11 migrations)
- ✅ master-strategy 2026-05-30 (80 页, 比 deck 更深入战略 + 90-day 执行月历)
- ✅ board-ready-deck v4 (30 页) — 最近 board 版本
- ✅ board-ready-deck v1 (30 页) — 原始版, 更诚实 (含 25% LOST 退出场景)
- ✅ 竞品扫描 (Playable/Flarie/CataBoom/BRAME/Gamify, 真 web research, 引 URL)
- ✅ 6 personas × 多 page × 多 round Trinity sim (v2 Sonnet + playwright)

---

## A. System promise — 优势 vs 劣势

### A.1 真正的优势 (over-deliver, deck 数字 vs 现实)

| 维度 | Deck 承诺 | 现实 (HEAD 436da2c) | 状态 |
|------|-----------|---------------------|------|
| API routers | 108 | **123** | 🟢 +14% over |
| Endpoints | ~925 | **1,064** | 🟢 +15% over |
| Tests | 1,685 (deck v4) / 1,248 (deck v1) | **1,530** | 🟢 v1 over, v4 略低 |
| Locales (real strings) | 11 scaffolded | **11 真翻译 + 7 SEA+RTL via OpenRouter** | 🟢 over |
| Compliance | 9 jurisdictions | **PDPA-SG + PDPA-MY + GDPR + Halal-aware + DPA + new-customer contract** | 🟢 +4 真文档化 |
| Game recipes | 50 templates (deck v4) | **79 recipes** | 🟢 over |
| Migrations | 7-10 | **11** (含 249 ISO seed) | 🟢 over |
| Country slots | "mechanism" | **真 implemented atomic + 249 ISO + counter** | 🟢 ship |
| Trinity 3T artifacts | "public" | **6 personas × 多 round v2 sim transcripts + bible_check.py 11/11 0%** | 🟢 真做到 |
| KiX-ID SSO | "cross-merchant catalyst" | **1857-line kix_id.py + test_kix_id.py + test_sso_bridge.py** | 🟡 code shipped, e2e cross-brand sim 还没跑 |
| Voucher pool cross-brand | "Day 90 first cross-border use" | **381-line voucher_pools.py + test_voucher_pooling.py** | 🟡 code shipped, e2e 真 transfer test? need verify |
| ML smart-bidding | "LightGBM production on 30d labels" | **174-line ml.py + test_ml_router.py** | 🟡 code shipped, "production" 程度? |
| Personas Trinity loop | n/a (后加) | **Ahmad→70%, Aminah→50%, Sarah→80%, Sandeep→70% closed** | 🟢 真 closed loop |
| WhatsApp ops | "WhatsApp Business contact" | **295-line whatsapp_template.py + 13 tests + whatsapp_otp + whatsapp_auth** | 🟢 ship |

**结论 A.1: 工程 surface 真 over-delivered. KiX-ID SSO / voucher pool / ML 都 ship 了 code (我初稿担心是 vapor — 实际不是), 但 cross-brand e2e + production-grade 验证仍缺 → 这是真正的 gap.**

### A.2 真正的劣势 (overpromise — 需 truth-up)

| 项 | Deck claim | 实际 | Gap |
|----|------------|------|-----|
| Payment methods | "60+ methods" | **5 PSP clients** (alipay_global/grabpay/ovo/paynow/wechat) + Stripe | 🔴 deck overstate ~12x. Truth: "5 PSPs supporting 30+ underlying methods" |
| Game library | "800-1,000 roadmap, 50 live" | **79 recipes, 10 staging stubs (Wave I.D plan)** | 🟡 OK 50 数, 但 800 是 12 月 roadmap (诚实)  |
| POS integrations | "Toast/Loyverse/Square live + StoreHub Q3" | **Stripe Terminal live, StoreHub adapter skeleton (no router yet), Toast/Loyverse/Square 没有 router 代码** | 🔴 大 overpromise |
| Native iOS+Android app | Wave I (M10) | **0 mobile app code** | 🔴 not started |
| Paying merchants | "100 by Day 90" (post-launch) | **0 (alpha cohort named in case-studies — 但 commit log 无 evidence Toast Box 真签了)** | ⚪ 等 launch |
| Sample_brander v2 | Slide 19 (deck v1) "strong-schema brand matching" | **brand_translation_service.py + wavef_brand_color.py, 没有 sample_brander 模块** | 🔴 deck 名字不对应 (可能 renamed wavef_brand_color, 但需 verify) |
| 5-min onboarding | core promise | **Aminah v3 sim 仍说 "too confusing, WhatsApp founder first"** | 🔴 概念是对的, 体验未达 |
| 1000+ merchants per employee | Year 1 goal | **0 paying** so denominator 1 employee | ⚪ ASPIRATIONAL |
| First cross-border voucher use | Day 90 | **No e2e test of cross-brand wallet transfer** | 🔴 needs build |

### A.3 优势/劣势汇总诊断

**最大优势 (真值得拿到 board):**
1. Code surface 比 deck 更深 (+14% routers, +15% endpoints)
2. 11 个 locale **真翻译了** (其他竞品都是 EN+几个欧洲语言)
3. Compliance 真有 contractual definition (Sarah verbatim: "most honest vendor page I've seen in 5 years")
4. Trinity 3T 真在跑 + bible_check 真 enforce + 6 personas sim transcripts
5. Country slots 真 implemented atomic + 249 国 seed
6. KiX-ID SSO / voucher pool / ML 都 ship 了 backend (验证后比初稿担心的更好)

**最大劣势 (真 board 时会被挑):**
1. **60+ payment methods 是大 overclaim** — 真 5 PSPs. Sandeep 已经在 sim 里 demand Oracle POS 这种细节
2. **POS integrations 大部分 stub** — StoreHub 我们 skeleton, Toast/Loyverse/Square 没代码. Ahmad's #1 scale blocker
3. **0 paying merchants** — deck v4 自己 admit, 但风险窗口仍开 (12-18 月 AI window)
4. **5-min onboarding promise vs Aminah v3 "太复杂"** — gap is real, Wave M consumer entry 需做
5. **Mobile app 0 进展** — web-only 暂时 OK, 但 deck M10 promise 会到期

---

## B. 竞争对手 unmet needs — 我们能填的

### B.1 验证后的竞品共同盲点 (我们能拿)

竞品扫描 (Playable/Flarie/CataBoom/BRAME/Gamify) 揭示:

| 竞品共同盲点 | KiX 已 ship 的对应 |
|--------------|---------------------|
| **所有 5 个都 target brand marketers, 不是 offline merchants** | 我们 hero copy 直接说 "for offline merchants" — 5 ICP gap |
| **0 / 5 ship AI-generated games** (homepage verified 2026-05-31) | 我们整个 brick/ELTM/Coder/PDCA pipeline 就是 AI-native |
| **0 / 5 close in-store redemption loop** (CataBoom 最接近但 hand-off) | 我们 5-step funnel Game→Reward→Register→Redeem→Return + voucher_pools 全 ship |
| **2 / 5 paywall self-serve (CataBoom $500/mo, BRAME €440/mo); 3 / 5 invisible** | 我们 country founding-100 = $0/月 forever |
| **0 / 5 有 SEA payment rails** | 我们 5 PSPs (grabpay/ovo/paynow/alipay/wechat) + Stripe |
| **0 / 5 有 RTL/Arabic/Hebrew** | 我们 ar-EG/ar-SA/he-IL 真 ship |
| **Template count is the brag (40/160/200+)** — 同一性 | 我们 brick + asset-slot brand-injection = 每商家 bespoke |
| **5-15 min onboarding** (template配置) | 我们 5-min self-serve target (实际 Aminah 仍说复杂, 但概念对) |

**结论 B.1: 真正的 unmet need 5 个, 我们都有 code for them, 但 still 缺 marketing 把这些差异化讲清楚 (尤其在 enterprise.html, 已经做了一半).**

### B.2 必须 STEAL (从竞品 learn) — 立即可做

**1. CataBoom 的 "publish pricing" 战术**
- CataBoom $500/mo, BRAME €440/mo (multi-currency) 都公开标价
- Playable/Flarie/Gamify 都 hide (高级感 vs 拒人千里之外)
- **KiX 已经做了** (pricing.html 公开), 但应该 anchor "Free + per-result" 比 $500/mo 强 5-10x
- 用 deck words: "Their cheapest tier is our roof. Our floor is $0."

**2. BRAME 的 multi-currency + template-count ladder**
- BRAME 显示 EUR / GBP / USD 并排 + 每 tier "templates included" 阶梯 (20+/30+/50+/100+)
- **KiX 应该模仿**: pricing.html 加 USD/EUR/GBP/SGD/MYR/IDR 6 货币并排 + 每 tier "AI games/month" (5/25/100/unlimited)
- Effort: 1 day frontend

**3. Flarie 的 paired-metric hero**
- "Garnier — 3x social CTR", "Klarna — +350% time-in-app" (每 case 1 句 1 数字)
- **KiX 应该重写 sg-case-studies.html hero**: "Heng Heng Kopi — S$4.90 D90 CAC, 53% return rate (90 days verified)"
- Effort: 1-2 hour copy refactor

**4. Gamify 的 "smallest-merchant + big-number" 模板 (Donut Papi case)**
- 所有其他竞品都 mid-market / enterprise. Gamify 独有 1 个 SMB case (Donut Papi, +581% organic shares)
- **KiX 优势**: 我们整个 cohort 就是 SMB. 应该 lead with smallest merchant (Aminah's Halal Hut: 23 orders week 1, 5x IG baseline)
- Effort: 0 (我们已有, 只是排序问题)

**5. CataBoom 的 "fraud + bot mitigation bundled at every tier"**
- 信号: 信任 baseline = day-1 included
- **KiX 已有 fraud router** — 但没在 pricing.html 显示作为 "bundled in every tier"
- Effort: 1 hour, 加到 pricing comparison

**6. Gamify KFC Japan 22% in-store redemption metric**
- 这是 category 公开 best-in-class merchant-funnel metric
- **KiX 应该 own this KPI** — sg-case-studies + enterprise.html 都 publish Game→Redeem 转化率 (Heng Heng 90d data 已 有)
- 当 Heng Heng D90 redemption rate > 22%, 公开 "outperforming Gamify's KFC Japan"

### B.3 反过来 — 竞品做的更好的 (我们要 catch up)

| 项 | 谁做得好 | 我们要做 |
|----|----------|----------|
| Marquee logos | BRAME (McD, KFC, Heineken, Lindt), CataBoom (Chipotle/Taco Bell/KFC/Whataburger) | 我们 0 大牌 logo. **必须签 1 tier-1 SG QSR 作 lighthouse** (Toast Box / Ya Kun / Old Chang Kee) |
| Case study 数据格式 | Flarie's pair-metric, Gamify's funnel detail | 我们 sg-case-studies 已有, 但需简化 + paired-metric refactor |
| Public pricing tier ladder | BRAME 4-tier multi-currency 公开 | 我们 pricing.html 有 RM/MY conversion 但没 6-currency 并排 |
| "Easy integration into your marketing tech stack" | BRAME, CataBoom marketing this concisely | 我们 connect.html 有 list 但需要 1-line  positioning |

### B.4 12 月预测: 谁会 copy 我们

**BRAME 最可能** (per agent analysis):
- 已有 multi-currency 基础设施
- McDonald's + KFC + SPAR roster 邻近 QSR/merchant
- Swiss eng team 快, 易 bolt AI 到 drag-and-drop
- 唯一有 Loyalty integration (最接近 KiX redemption loop)

**他们怎么 copy**: "AI-generate this template variant" button bolt 进 builder. 不会重建 engine. Marketing line: "AI-assisted gamification builder".

**Defense**:
1. Lock 100 merchants per city × 200 国 (deck v1 已写, 真做)
2. Publish funnel benchmark (Game→Redeem 转化率) 先 — 他们没 redemption loop 测不了
3. Open brick library + sample_brander 做 developer ecosystem
4. Trade-secret PDCA visual-polish pipeline (brick→ELTM→Coder→PDCA + property-oracle)
5. SEO category 先占 ("AI gamification platform", "AI marketing games for merchants")

---

## C. 能否 follow Bible — 执行纪律打分

### C.1 Bible 自己声明的原则

> "Software free, network paid." — 唯一商业模式
> "Bible matches code reality at HEAD ..."
> "Marketing copy has been removed. Every claim now carries a status badge."
> "If a claim is aspirational, it says so."
> "Numbers in this file are auto-checked by `scripts/bible_check.py`. Drift > 5% breaks CI."

### C.2 follow 程度评分

| 原则 | 评分 | 证据 |
|------|------|------|
| Numbers auto-check 真 enforce | ✅ A+ | bible_check.py 11/11 0% drift @ HEAD 436da2c (just synced) |
| Status badges 真 used | ✅ A | 整 Bible 用 ✅/🟡/🔵/📝, 12 ADRs 有 status |
| ADR 模式真 followed | ✅ A | ADR #11 country_slots + ADR #12 wallet recon 都新加;ADR #4 有 9 explicit tests |
| Marketing copy removed | 🟡 B | landing pages 仍有市场化 ("Win customers through play"), 但 enterprise.html 加了 antidote |
| Trinity artifacts public | 🟡 B+ | sim transcripts 在 /Users/mozat/a-docs/ — public to ME, 不是 commit-link 公开 |
| "Software free, network paid" 单一 model | 🟡 B | pricing.html 仍有 4 tier subscriptions ($0/$49/$199/$499), 不是纯 pay-per-result |
| 5-min onboarding 兑现 | 🔴 D+ | Aminah v3 sim 仍说 "too confusing" — 6 个 Wave M item 待做 |
| 60+ payment methods | 🔴 F | 真 5 PSPs vs 60 claim → 必须 truth-up |
| Toast Box Day 50 exclusive | ⚪ 待证 | git log 无 evidence — 但可能 sales 还在跑 |
| ML production 真 ship | 🟡 B- | 174-line ml.py + test 但 "30d real labels" 真 train? 需 verify |

**整体: B+ (执行纪律强 — 数字 + ADR + sim 模式真做; 但 5 个 marketing claim 仍 overclaim)**

### C.3 Bible 立即 truth-up 建议 (1 commit)

1. **payment methods**: "60+ methods supported via 5 PSP clients + roadmap to 12 PSPs (StoreHub/FPX/Razorpay/GCash/PromptPay/DANA/etc Q3-Q4)" — 诚实
2. **POS integrations**: 标 status — Stripe Terminal ✅, StoreHub 🔵 (skeleton), Toast/Loyverse/Square 📝 (deck claim, no code)
3. **Sample_brander v2**: 验证 wavef_brand_color.py 是否真是 sample_brander — 若是, rename or alias; 若不是, mark 📝
4. **5-min onboarding**: 标 🟡 (sim 真测过, Aminah 仍 friction; Wave M plan documented)
5. **Toast Box exclusive**: 不要 commit 到 Bible 直到 git log 或 sales pipeline 有 evidence

---

## D. 系统层面 P0-P2 改进 roadmap

### D.1 P0 — Truth-up Bible (今天, 1 commit)
- 修 60+ payment methods 声明
- 标 POS integrations status (Stripe Terminal live, StoreHub 🔵, 其他 📝)
- 验证 sample_brander 状态
- 标 5-min onboarding 🟡 (Aminah sim 引用)

### D.2 P0 — Trinity artifacts public (1 天)
- 写 /landing/trinity-artifacts.html
- 列所有 6 personas sim transcripts + 链接到 /a-docs/sim-v2-*
- 每个 sim 卡: persona + page + verdict + improvement delta
- Sarah verbatim: "most honest vendor page in 5 years" 作为 hero quote
- 让 Bible "artifacts public" 真做到

### D.3 P0 — Steal 3 竞品战术 (1-2 天)
- pricing.html 加 6-currency 并排 (BRAME 模仿) + "AI games / month" 阶梯
- sg-case-studies.html 改 paired-metric format (Flarie 模仿): "Heng Heng — S$4.90 CAC, 53% return"
- pricing.html 加 "fraud + bot mitigation bundled at every tier" 信号 (CataBoom 模仿)

### D.4 P1 — KiX-ID SSO cross-brand e2e (3-5 天)
- 写 tests/test_kix_id_sso_cross_brand.py
- 验证 user 注册 brand A → brand B 自动识别 (no re-login)
- 这是 deck v1 "Day 31-60 SSO across 10+ merchants" 的 P0
- 真 ship 这个 = 解锁 cross-brand network effect 真启动

### D.5 P1 — Cross-brand voucher pool e2e (5-7 天)
- 验证 user 在 brand A 赢 voucher → brand B 兑换 → ledger 双方正确扣 + 分账
- Saga 协调 + atomic transfer
- 这是 deck "first cross-border voucher use Day 90" P0

### D.6 P1 — StoreHub adapter 接 FastAPI + 真 webhook (3 天)
- 现有: storehub_adapter.py 25 tests 全 pure functions ✅
- 缺: app/routers/integrations/storehub.py + DB write
- 这是 Ahmad's #1 scale blocker
- RFC docs/rfc-storehub-fasttrack.md 已写, 实施它

### D.7 P1 — Wave M consumer entry polish (3-5 天, Aminah sim 驱动)
- Welcome modal 已 ship 7-fix v2 但 Aminah 仍 friction
- 缺: WhatsApp setup help integration (founder 在 Slack)
- 缺: i18next 在 welcome modal 真激活 (currently uses local T table)
- 缺: 真 sub-vertical-aware voucher language (nasi vs bubble tea verified)

### D.8 P2 — 真 9 个 PSP (matches deck 60+ 声明)
- Stripe Terminal (live) + 5 PSP (live) → 加 FPX/GCash/Razorpay/PromptPay/TrueMoney/DANA/DuitNow/MoMo/ZaloPay
- ~15-20 days dev (per PSP ~2 days)
- Deck honesty restoration

### D.9 P2 — Toast Box exclusive — 真签 (sales)
- 不是 eng work
- Bible 上 Toast Box exclusive 不要 commit 直到 signed
- 当 signed, 加 ADR #13 + e2e POS test + Heng Heng-style 90d cohort doc

### D.10 P2 — Mobile app scaffold (M10 deck commit)
- Capacitor wrap (web → iOS+Android shell)
- Effort: 5-10 days (大幅缩减 vs native)
- App Store + Play Store 真 submission

---

## E. 一句话诊断

> 工程 surface (123 routers / 1,530 tests / 47 services) 真比 deck 数字更深;
> 5 个深层 capability (KiX-ID SSO cross-brand / voucher pool e2e / POS / ML / 真 PSPs) 都 ship 了 code, 但 e2e 验证仍缺;
> Bible 执行纪律 B+ (数字 + ADR + sim 真做, 但 5 个 marketing claim 需 truth-up);
> 竞品 5 个 unmet need 我们全有 code for them, 但需要 marketing 把差异化讲清楚 + STEAL 3 个具体战术 (publish pricing tier ladder / paired-metric case / bundled trust).
>
> **最大 ship blocker = 真签 1 个 tier-1 SG QSR 作 lighthouse customer.** 一切其他都是 supporting.

---

## F. 推荐下一步 (按 ROI 排序)

| # | 项 | ROI 论据 | Effort | 推荐 |
|---|----|----------|--------|------|
| 1 | D.1 + D.2 (Bible truth-up + trinity-artifacts.html) | 0 code risk, 大 trust signal, Sarah-archetype 会 verify | 1 天 | 立即 |
| 2 | D.3 (steal 3 竞品战术: pricing ladder + paired metric + bundled trust) | 直接 close Sandeep "too SMB-focused" gap | 1-2 天 | 立即 |
| 3 | D.6 (StoreHub 真接 router) | Ahmad scale blocker dissolve | 3 天 | 本周 |
| 4 | D.7 (Wave M consumer entry — Aminah final close) | Ben Tan 0/10 sim → start closing consumer loop | 3-5 天 | 下周 |
| 5 | D.4 + D.5 (KiX-ID SSO + voucher pool e2e) | Deck "Day 31-60 + Day 90" milestones 真有 evidence | 8-12 天 | 月内 |
| 6 | D.9 (Toast Box sales signing) | Lighthouse customer = 解锁所有 enterprise sims | 不 eng | 并行 |
| 7 | D.8 (9 PSPs) | Deck honesty + 全 SEA payment 真覆盖 | 15-20 天 | Wave N |
| 8 | D.10 (mobile app) | Deck M10 promise, 但 web 暂时 OK | 5-10 天 | Wave O |

**第一周冲刺建议:**
- 周一: D.1 + D.2 (truth-up + artifacts page) — 1 commit
- 周二-周三: D.3 (steal 战术) — 1 commit
- 周四-周五: D.6 (StoreHub router + 真 webhook) — 1 commit + Ahmad re-sim
- 周末: D.7 part 1 (Wave M plan + first 2 fixes)

第一周末预期: 4 个新 commits, Trinity sim 全 6 personas 再跑一轮验证, Ahmad 70%→80%+, Sarah 80%→90%+, Aminah 50%→65%+.
