# 用户旅程的真相 — 懒到上钩的三步漏斗

> **关键洞察**：80% 的用户会在"先注册再玩"的步骤流失。
> 正确做法：先让他赢，他自己会来注册（为了不丢战利品）。

---

## ❌ 错误流程（80% 流失）

```
扫码 → 注册 KiX → 下载 App → 玩游戏 → 拿奖
        ↑
   80% 在这里离开
```

**问题**：用户还没尝到甜头，凭什么填手机号？

---

## ✅ 正确流程（懒到上钩三步漏斗）

### 第 1 步 · 玩（零摩擦）

```
用户在店面看到 QR
  ↓
扫描 → 直接打开 H5 游戏 (partner.letskix.com/play?qr=xxx)
  ↓
后台：POST /kix-id/qr-scan/bind {device_fingerprint, qr_token}
  ↓
KiX 自动用 device_fp 创建匿名 kid（不需要任何用户信息）
  ↓
游戏立刻开始
```

**用户感受**：「咦，这游戏挺好玩」

### 第 2 步 · 上钩（赢了才动心）

```
用户赢了 → 弹窗：
  
  🎁 恭喜！你赢得了
     "南洋茶饮 中杯免费券"
     价值 ¥18
  
  [立即收藏 →]    [先看看]
```

**用户感受**：「我居然赢了！这个不能丢！」

### 第 3 步 · 收藏（必须注册才能保存）

```
点击「立即收藏」→
  
  📲 保存到我的 KiX 钱包
  
  请输入手机号: [____]
  [发送验证码]
  
  或：[微信一键登录]
  
  > 提示：登录后才能保存优惠券到店核销
```

**用户感受**：「就一个手机号，能保住 ¥18 不亏」

**注册流**：
```
POST /kix-id/identity-link {kid, phone_hash, verification_token}
→ 匿名 kid 升级为完整身份
→ voucher 永久属于这个 kid
→ 提示下载 KiX App（建议但不强制）
```

---

## 🏪 商家视角（说服店长的话术）

### 店长担心
- 「让客户装一个 KiX App，他们不烦吗？」
- 「客户会不会觉得我们多此一举？」

### 销售回应

> 「店长，通过你店里的 QR 注册的每一个用户，平台上永久标记是**你的客户**。
> 
> 这意味着：
> 1. 他下次来你店附近 50 米，KiX 自动推送你的新活动给他
> 2. 他去隔壁竞品消费，算法可以把他拉回你这（你不付费就不推）
> 3. 他生日，KiX 自动推送你设的生日礼券
> 4. 你的会员画像 / 复购率 / 留存率全自动追踪
> 5. **你不装这个 SaaS，等于把客户白送给装了的竞品。**
> 
> 用户拿到优惠券想保存 → 自然就装了。你只负责把 QR 摆好。」

---

## 🔧 技术实现要点

### 匿名 → 实名 升级链

```
1. 扫码瞬间：
   POST /kix-id/qr-scan/bind {device_fingerprint, qr_token}
   → 返回 kid_anon_xxx + session_token

2. 用户玩游戏：
   完成事件携带 device_fingerprint
   → user:{kid_anon}:journey 累积

3. 用户赢取奖励：
   POST /vouchers/{vid}/reserve {device_fp, kid_anon}
   → 5 分钟保留（防止页面关闭丢失）

4. 用户点击"收藏"：
   POST /kix-id/identity-link {kid_anon, phone, otp}
   → 同一个 kid 升级，journey/voucher 全部继承
   → 不创建新 kid（防止数据丢失）

5. 用户下载 App：
   App 启动后用同一 phone 登录
   → device_fp 与 phone 关联
   → 历史游戏记录 / 优惠券全部同步
```

### 防御性设计

**问题：用户扫码玩了但没注册，券会消失吗？**

```
方案：
- 匿名 kid 的 voucher 保留 7 天
- 期间任何相同 device_fp 重新扫码 → 自动恢复
- 7 天未注册 → 释放回库存
```

**问题：用户在多个店玩游戏，都是匿名 kid？**

```
方案：
- 同一 device_fp 复用同一 anon kid
- 多店活动累积到一个 anon kid 名下
- 注册时一次性继承所有历史
```

---

## 🎯 漏斗数据预期

| 步骤 | 漏斗 | 累计转化 |
|------|------|---------|
| 看到 QR | 100% | 100% |
| 扫码进入 | 60% | 60% |
| 完成一局游戏 | 70% | 42% |
| 赢取奖励（取决于游戏难度） | 50% | 21% |
| 点击「收藏」（看到奖励价值） | 70% | 14.7% |
| 输入手机号注册 | 80% | 11.8% |
| 下载 KiX App | 30% | 3.5% |

**对比传统"先注册再玩"模式**：
- 扫码 → 注册：20% 转化
- 注册 → 完成游戏：80%
- 完成 → 拿奖：50%
- 拿奖 → 下载 App：50%
- 最终下载：4%

**懒到上钩漏斗下载率 3.5% vs 传统 4%，看起来差不多？**

不！关键差别：
- 传统：4% 下载，但 60% 离开就再也不来了 = **失去 60% 数据**
- 懒-上钩：3.5% 下载 + 11.8% 注册手机 + 21% 玩到拿奖 + 42% 进游戏 = **保留 60% 数据**

**注册的人是高质量用户**（他们愿意保存奖励，未来转化率高）。

---

## 🚨 重要：商家 QR 的归属逻辑

每个商家在 Portal 生成的 QR 都带有 `brand_id` + `store_id` + `qr_token`：

```
扫码 → POST /kix-id/qr-scan/bind {
  qr_token: "brand_a:store_001",
  device_fingerprint,
  ...
}
→ 平台记录：
   user:{kid}:first_brand_touch:{brand_a} = timestamp
   brand:{brand_a}:users_acquired_via_qr += 1
   brand:{brand_a}:store_001:scans += 1
```

**这条记录决定了归属**：
- 用户后续在 brand_a 店转化 → 老客（不付费）
- 用户去 brand_b 店转化 → 跨店带客（KiX 抽佣，部分回老王）

---

## 📱 KiX App 价值（对用户）

| 功能 | 价值 |
|------|------|
| 保存所有优惠券 | 不丢战利品 |
| LBS 推送（附近商家活动） | 主动发现 |
| 跨品牌奖励合并 | 一个钱包看所有 |
| 朋友邀请奖励 | 病毒拉新 |
| 游戏排行榜 | 社交炫耀 |

---

## 📱 KiX App 价值（对商家）

| 功能 | 价值 |
|------|------|
| 用户永久绑定 | "我的客户" |
| LBS 推送（用户附近时） | 主动召回 |
| 跨店带客抽佣 | 反向引流 |
| 用户画像 | 精准运营 |
| 自动归因 | 知道客户来源 |

---

## 💡 一句话总结

> **"先玩 → 再赢 → 才注册"** 是漏斗的真相
> 
> 不要逼用户先注册，让他自己心甘情愿来注册
> 
> 因为他舍不得丢掉刚刚赢到的奖励

---

## 🔧 当前代码状态

| 能力 | 是否已支持 |
|------|-----------|
| 匿名 device_fp 创建 kid | ✅ kix_id.qr-scan/bind |
| 匿名 kid 玩游戏 + 累积 journey | ✅ attribution.track_event |
| Voucher 保留期 + 5 min reserve | ⚠️ 部分（voucher.issue 有 expires_at，但 reserve 流不完整）|
| Identity-link 升级匿名为实名 | ✅ kix_id.identity-link |
| OTP 验证手机 | ⚠️ 平台层有 verification_token，需短信网关接入 |
| QR 归属第一品牌 | ✅ user:{kid}:first_brand_touch:{bid} |
| 7 天匿名券保留 | ⚠️ Voucher 有 TTL，但 anon 用户的 voucher 转 phone-bound kid 流需完善 |
| 用户钱包累积所有券 | ✅ user_wallet.py (Round 10) + vouchers 索引 |
| 店长核销 voucher | ⚠️ vouchers.redeem 已有，需 portal 店长 UI |
| KiX App 收件箱 | ✅ push_engine /user/{kid}/inbox |

需要补的：
- 完善 voucher reserve / claim 流（5 min 锁定）
- 店长核销 UI（portal.html 加 view-redeem）
- 短信 OTP 网关接入（外部）

---

需要这些代码补完吗？或者先把这套流程做成产品 demo？
