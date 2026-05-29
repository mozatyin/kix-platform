# KiX 系统体检报告 — 现在还有什么问题

> 不奉承，直说。
> Trinity 第一性原理：能写 26000 行不代表能上线。

**日期**: 2026-05-29 · **版本**: 体检 v1.0 · **维度**: 6 大类 / 47 项问题

---

## 🔴 P0 — 不修无法上线（19 项）

### 1. 支付通道全是 stub 🔴

| 缺口 | 现状 | 影响 |
|------|------|------|
| Stripe / WeChat Pay / Alipay 实际签约 | 全是模拟成功 | **完全不能收钱** |
| SMS OTP（kix_id.identity-link）| `verification_token` 字段假 | 用户注册全是空 |
| 银行转账（payouts → 商家提现）| 完全 stub | 商家钱出不去 |
| 微信 / 支付宝小程序支付 SDK | 没接 | 中国市场不能用 |

**修复**: 至少接 Stripe（国际）+ 微信支付（中国）+ Twilio/阿里短信

### 2. Voucher Reserve 5 分钟锁定流程不完整 🔴

```
当前：用户赢券 → 显示 → 关页面 → 券就丢了
应该：用户赢券 → 5 min reserve → 注册补 → 永久保存
```

懒-到-上钩漏斗第 2 步 **必须** bug-free，这是 80% → 20% 流失改善的核心。

### 3. 店长核销 UI 完全缺失 🔴

`vouchers.redeem` API 存在，但 portal.html 没有店长视图。
**真实场景**：用户进店给店长看券 → 店长怎么核销？没界面。

**修复**: portal.html 加 `view-redeem`：
- 扫用户 QR / 手输优惠券编号
- 验证有效性 + 显示价值
- 一键核销 + 收据
- 当日核销统计

### 4. 3-month trial 第 91 天自动扣款 cron 没实现 🔴

```python
# brand_subscriptions.upgrade with first_year_free=True
next_charge_at = now + TRIAL_SECONDS  # 90 days
```

**但谁在 day 91 真去扣？** 没有 worker。商家试用结束后 KiX 永远收不到钱。

**修复**: 写一个 cron / Redis Stream consumer：
- 每小时扫 `brand:*:subscription` HASH
- 找 `next_charge_at < now` 且 `auto_renew=true`
- 调 `payment_methods.charge`
- 失败 → 降级到 FREE + 通知商家

### 5. 真实游戏生成（ELTM）链路没跑通 🔴

```
Recipe → playable HTML 完整生成链路
当前：creative_gen.request 写到 game_order:{id} HASH
但 ELTM API 实际是否能产出 HTML？没验证过。
```

**修复**: 真跑一次 NL→Recipe→HTML 完整流程，确保用户扫码看到游戏。

### 6. Push notification 真实送达零实现 🔴

`push_engine.dispatch` 写 `push:outbound:queue` LIST → **没有 worker 消费**。

**修复**:
- FCM（Android）+ APNS（iOS）+ 微信公众号模板消息
- 启一个 `push_worker.py` 消费 outbound queue
- 失败重试 + 退避

### 7. 没有真正的内容审核 🔴

商家可以在创建广告时塞任何文字 + 图片。
- 黄赌毒 / 政治敏感 / 虚假宣传
- compliance scanner 只查关键词，不查图片
- 没有人工 review queue 接入

**修复**:
- 图片审核（接 Google Vision Safe Search / 阿里云内容审核）
- 文本 LLM 自动初审 + 人工复审 queue
- 商家被举报机制

### 8. KiX App 没有 Native 版本 🔴

只有 `landing/app/index.html`（H5）。
- 不能接 push notification（需要 native）
- 不能 LBS 后台触发
- 不能扫码（依赖浏览器相机权限）
- App Store / Google Play 都没上

**修复**: Capacitor / React Native 包一个壳，最少 iOS + Android.

### 9. 商家用户授权 OAuth 流没接前端 🔴

`kix_id.connect/authorize` API 存在，但没有用户授权页面。
**当商家点 "Connect with KiX" 时，用户看到什么？** 没界面。

**修复**: 写 `landing/connect.html` 标准 OAuth 授权页（类似 "Allow App to access your data"）。

### 10. 用户公开 storefront 不够漂亮 🔴

`landing/storefront.html` 是技术 demo 样式。
**实际商家会觉得：「这就是我的品牌主页？太丑了」**。
- 商家想自定义品牌色 / logo 位置 / 模块顺序
- 没有移动端优化
- 没有 SEO meta tags

**修复**: 改一版正经设计 + 移动端响应式 + SEO.

### 11. Logistic for welcome kit 寄送没接入 🔴

`welcome_kit.shipping/request` 只是 enqueue。
**谁把 QR 桌牌真的寄到商家门店？** 没有印刷 / 物流 partner.

**修复**:
- 接 print-on-demand（凡科 / 八彩）
- 接 顺丰 / 京东物流 API
- 或先手动运营做

### 12. 商家信用卡 day-91 自动扣失败的体面降级 🔴

如果商家信用卡失败：
- 平台应该怎么处理？立刻关账号？给宽限期？
- 没有 dunning 流程（催收）
- 没有降级通知

**修复**: 标准 SaaS dunning：3 天宽限期 + 邮件提醒 + 7 天后降级 FREE

### 13. PostgreSQL 大量未用 🔴

```
当前：90% 数据在 Redis
但 Redis 单点故障 = 数据全丢
```

**修复**:
- 关键数据（brand_config / wallet 余额 / subscription / 支付方式）写 PG
- Redis 作 cache + ephemeral state
- 加 backup strategy

### 14. 没有 CI/CD 🔴

- 全靠手动 git push
- 没有 test 自动跑
- 没有 deploy pipeline

**修复**: GitHub Actions + Docker build + 自动测试 + Staging deploy

### 15. 没有真正的测试 🔴

- pytest 框架配置了但没有单元测试
- E2E 只有 super-app 24/24 + 18 商家 sim
- 边缘场景（并发 / 网络故障 / 数据库故障）都没测

**修复**: 至少:
- 每个 router 一个 unit test (覆盖核心逻辑)
- WATCH/MULTI 并发场景的 stress test
- payment / billing 100% 测覆盖

### 16. OpenAPI 有 PydanticUserError 🔴

`/app/{path:path}` 的 `_Request` forward ref 问题导致 `/openapi.json` 报错。
**Swagger UI 部分坏**。

**修复**: 找到这个 forward ref，要么 import 要么删。

### 17. 没有 Production deploy 文档 🔴

- README 是 dev guide
- docker-compose.yml 存在但没人真跑过
- 没有 nginx config / SSL cert / DNS 设置文档
- 没有 monitoring / alerting

**修复**: 写 PRODUCTION.md 把生产环境一切讲清楚

### 18. 法律合规零 🔴

- 没有商家合同模板
- 没有用户协议
- 没有隐私政策
- KiX 公司主体 / 商标 / 域名所有权？

**修复**: 找法务起草至少 4 份合同（商家服务协议 + 用户协议 + 隐私政策 + Cookie 政策）

### 19. 税务 / 发票 完全没写 🔴

- 商家充值 ¥10000 需要发票
- 跨境（印尼商家 IDR）需要外汇 + 增值税
- 商家提现需要个人所得税申报

**修复**: 接金税系统 / 阿里商业云税务

---

## 🟡 P1 — 上线后 3 个月内必修（12 项）

### 20. 没有商家拉商家的 affiliate 机制
老王想推荐老李，KiX 完全没机制激励。**病毒商家增长缺失**。

### 21. 没有自动 industry 模板推荐
商家注册选 "餐饮" 应该立刻看到 5 个适合的 Recipe，现在要自己翻 79 个。

### 22. Master quota 是 per-brand 不是 per-master
多店商家可能各店各开 1 个 game = 10 个 game。tier quota 失效。

### 23. 拍卖延迟未测过 10K campaigns 场景
当前一次 `/auction/run` 查所有 `campaigns:active` SET。10K 时性能未知。

### 24. 归因 journey 累积无上限
用户 1 年累计 1000+ 事件，每次 conversion 都遍历？

### 25. 没有商家行业 case study 自动生成
老王做得好，KiX 应该自动生成 "南洋茶饮 KiX 案例" 给老李看，激励他升级。

### 26. 商家联营 / 跨品牌合作完全没产品化
partnerships.py 是 OPTIONAL，但理论上**主动**做联营是真有市场（银行+航司联名）。

### 27. 没有商家培训内容 / 视频
新商家不知道怎么开始第二个游戏 / 优化 quality score / 看 dashboard。

### 28. 销售 CRM 集成零
销售拉到 lead → 注册 → ... 全靠手动跟。

### 29. 没有 referral tracking 内部分析
KiX 自己的 marketing campaign ROI 怎么算？

### 30. 商家发票 / billing dashboard 缺
商家看不到自己 YTD 花了多少 / 收到多少。

### 31. 用户社交关系发现缺
看不到朋友在玩什么。`social.py` 有 follow，但 KiX App 不展示。

### 32. Quality Score 商家不会调
透明化端点 `/campaigns/{cid}/quality` 存在但没有可视化和操作指引。

---

## 🟢 P2 — 长期改进（16 项）

### 33-48. 产品/体验/规模化优化
- 33. 真正的设计师参与 portal.html 重画
- 34. 多语言 i18n（印尼语 / 英文 / 泰文 / 日文）
- 35. A/B testing 商家页面布局
- 36. 商家自定义品牌主题（除色彩外）
- 37. Mobile app 推送 deep link 优化
- 38. KiX 站内 Search（按行业 / 地理 / 类别找商家）
- 39. Redis Cluster / Sentinel
- 40. PostgreSQL 主从分离
- 41. CDN 接入 + 图片优化
- 42. WebSocket 实时推送（vs 当前 polling）
- 43. Monitoring（Prometheus + Grafana + Sentry）
- 44. Log aggregation（ELK / Datadog）
- 45. 数据备份 / 灾难恢复
- 46. GDPR Data Portability（让用户带走数据）
- 47. 安全审计（OWASP Top 10 + 渗透测试）
- 48. ELTM 模型自由切换（不只依赖 Claude）

---

## 🎯 优先级矩阵（资源有限就这么排）

### 6 周 MVP 上线（最小可商用）
```
Week 1-2: 真接 Stripe + 微信支付 + SMS OTP
Week 2-3: 店长核销 UI + 用户 OAuth 授权页
Week 3-4: Voucher 5min reserve + ELTM 真实游戏生成
Week 4-5: Push notification worker + 自动续费 cron
Week 5-6: 法务合同 + 税务发票 + production deploy + PostgreSQL 关键数据持久化
```

### 6 周 + 3 个月（商用稳定）
```
Month 2: 内容审核 + Native App + 商家培训 + dunning 流程
Month 3: 商家拉商家 + 自动 case study + CRM 集成 + Quality Score 可视化
Month 4: 拍卖性能优化 + journey 归档 + Master quota 修复
```

### 6 个月+（规模化）
```
Q3: 多语言 + 设计重做 + Mobile App 优化
Q4: Redis Cluster + PG 主从 + 监控完善 + 灾备
```

---

## 🚨 最容易被忽略的几个隐患

### 隐患 1: 我们 build 太快了
26000 行代码 11 轮迭代 = 平均每轮 2300 行。
但**没有任何 commit 之间被 review 过**。
风险：bug 没人发现，安全漏洞潜伏。

### 隐患 2: 测试覆盖几乎为 0
sim 是 end-to-end smoke test，不是 unit test。
WATCH/MULTI 并发场景没测 → 高并发下数据可能不一致。

### 隐患 3: Anthropic 依赖
全栈 LLM 都 Claude。如果 API 涨价 / 政策变 / 服务中断，KiX 全栈瘫痪。
应该至少抽象 LLM 接口能切换 OpenAI / 国产模型。

### 隐患 4: 数据持久性
Redis 重启 = 数据丢。
PostgreSQL 几乎未用。
**实际生产 Redis 挂的概率 > 0**。

### 隐患 5: 没有真实商家试运营
18 个 sim 全是脚本模拟。
**真实商家会不按脚本来**：填错数据 / 网络断 / 重复点击 / 滥用 / 投诉。
所有 edge case 都没经验过。

### 隐患 6: 销售 / 物流 / 客服流程零
我们写了 Bible 但没有**人**：
- 没销售团队
- 没物流寄 QR 桌牌
- 没客服回答商家问题
- 没运营审核内容

代码再完美，没人接客户也是 0。

---

## 💡 反思：Trinity 的局限

Trinity Protocol（业界 → 学术 → 现实）让我们**横向覆盖广**，但有 2 个盲区：

### 盲区 1: 「现实」用 sim 代替了真实商家
sim_laowang 跑通 ≠ 真老王能跑通。
真老王会：
- 中文输入法切换错乱
- 不知道什么是 OAuth
- 信用卡填错导致试用过期
- 收到 QR 桌牌后丢角落不用

→ 需要**真商家 alpha 测试**

### 盲区 2: 「业界」对标限于公开信息
TikTok Ads / Google Ads 公开的我们都参考了。
**但他们内部秘密**（反作弊算法 / Quality Score 真实权重 / 拍卖 tie-breaking）我们看不到。

→ 需要**ex-Google/TikTok 员工咨询**

### 盲区 3: 「学术」第一性原理偏理论
拍卖理论 / 网络效应 / 双边市场都对，但**现实摩擦**（用户懒 / 商家保守 / 监管不可预测）打不到。

→ 需要**多读 startup 失败 post-mortem**

---

## 🎯 一句话总结

> **KiX 现在是一辆设计完美的赛车，但还没装上**：
> - 真发动机（payment gateway）
> - 油（销售团队 + 营销）
> - 轮胎（Native App + Push）
> - 安全气囊（合规 + 法务）
> - 保险（监控 + 灾备）
>
> 写代码占总工作量的 30%，剩下 70% 是这份反思列出的事。
>
> **不要被 26000 行代码 + Trinity 11 轮 + 18 sim 的数字误导**。
> 真上线还有 6-12 周硬仗。

---

📖 *This reflection is the painful truth. Engineering excellence ≠ Production readiness.*
