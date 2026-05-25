# KiX 构建过程：Prompt → 文档 → 代码 → 测试 → 扩展

> 不是"做了什么"，而是"**怎么做的**"——每一步的 prompt、文档、验证、迭代的完整记录。

---

## 0. 方法论总览

整个 KiX 的构建遵循一个固定循环：

```
用户说一句话（需求/反馈）
    ↓
写成文档（把模糊想法变成结构化的 spec）
    ↓
文档转代码（spec 驱动实现）
    ↓
模拟用户测试（打开 HTML，真的点、真的玩）
    ↓
发现问题 → 分类（管线问题 / 合约问题 / 体验问题）
    ↓
扩充代码修复 → 回到测试
```

这个循环在 2 天内转了约 15 轮。

---

## 第一轮：从一句话到一条产品规范

### 用户的 Prompt

> "问题非常清楚，老王提出这个需求是不对的，因为我们毕竟不是让用户提出一个需求的这么一个事情来决定游戏，因为游戏的数量是有限的，用户的想象力是无限的。首先呢，我们得必须得从一个现实的一个游戏库里面，去选出最relevant的这个这个这个游戏列表，让老王去选..."

**关键信息：**
- 否定了旧模式（用户描述游戏 → 系统生成）
- 定义了新模式（用户描述业务 → 系统匹配游戏 → 用户选择 → 定制）
- 一句话原则："游戏的数量是有限的，用户的想象力是无限的"

### 写成文档

**输出文件：** `a-docs/kix-game-customization-flow.md`

这是一份完整的产品规范，包含：
- 两条路线（A: 选择定制, B: 从零创造）
- Step 0-4 的完整流程
- 数据流全景图（ASCII art）
- 接口清单（7 个函数/端点，各自的输入/输出/LLM 调用次数）
- 6 条关键原则

**文档的质量标准：** 一个工程师读了就能实现，不需要再问产品经理。

### 文档转代码

文档中的每一行描述，直接映射到代码：

| 文档描述 | 代码实现 |
|---------|---------|
| "Step 0: ELTM 研究商家业务" | `research_business()` |
| "Step 1: 三层智能推荐" | `rank_games_for_business()` — `_prefilter_games()` — Layer 3 prompt |
| "Step 3: 品牌植入 + 游戏生成" | `build_for_business()` — 7 Phase 管线 |
| "预处理：游戏库业务适配标注" | `kix_enrich.py` — `enrich_catalog()` |

### 第一次验证

```python
profile = research_business("咖啡店")
# → 验证：brand_name、industry、products 是否正确

ranked = rank_games_for_business("咖啡店", top_n=5)
# → 验证：Coffee Shop 是否排第一？分数是否合理？
```

---

## 第二轮：真实用户模拟暴露核心问题

### 用户的 Prompt

> "请打开拉花游戏，我来测试一下"

然后是：

> "完全是空白的界面"

> "好像是点击了play没有反应"

> "因为是这个样子的，没有看到星巴克的那个素材。第二呢，我也不知道怎么玩"

### 模拟用户的方法

用户不是看代码。用户**真的打开 HTML，真的点按钮，真的玩**。

这暴露了三层问题：

**第一层：品牌注入根本没生效。**
- 文档写了"品牌颜色 #00704A，星巴克绿"
- prompt 里也注入了这些需求
- 但生成的 HTML 是**紫粉色背景**
- 根因：prompt 文本 ≠ 代码遵守。LLM 生成代码时会"忘记"约束。

**第二层：游戏点了没反应。**
- Play 按钮发 `{type: 'start_game'}`
- handleAction 的 switch 里没有 `case 'start_game':` → 落入 `default: break` → 什么都不做
- 这不是 crash，是静默失败。PDCA runtime gate 抓不到。

**第三层：游戏不知道怎么玩。**
- 棋盘是纯色方块，没有提示，用户不知道要点相邻交换
- Moves 显示空白 — render 读 `state.movesLeft`，initState 声明 `state.moves`，字段名不匹配

### 从"修一个 bug"到"修一类 bug"

用户反馈的不是"修好这个游戏"，是"修好这个系统"：

> "我们先不去管这个游戏的地方，反思一下到现在我们整个的一个过程流程还有什么是不好的"

然后：

> "全部修复"

### 文档转架构决策

从这次测试中，产生了两份核心文档：

**1. brand_injector.py 的设计文档（口头讨论 → 代码）**

核心洞察：**品牌植入不能依赖 LLM 遵守 prompt。必须是确定性的后处理。**

```
旧方式：品牌需求写入 PRD text → LLM 读 PRD → 生成 HTML
         （LLM 经常"忘记" → 星巴克出来是紫粉色）

新方式：Code-Soul 生成 HTML
         → brand_injector.post_process()
         → CSS 选择器直接替换颜色
         → HTML 标题直接替换品牌名
         → 棋子 emoji 直接替换产品图标
         → contract_verifier 检查无误
         （确定性保证，不依赖 LLM 遵守）
```

**2. contract_verifier.py 的设计（从 12 个手动修复 → 自动检测）**

手动修了 12+ 处断裂后，总结出三类模式：

| 断裂类型 | 模式 | 自动检测方式 |
|---------|------|------------|
| Action 不匹配 | dispatch `{type:'X'}` vs case `'X':` | 正则提取→集合对比 |
| State 字段不匹配 | `state.X` vs initState 声明 | 提取→对比→已知映射表 |
| 结构缺陷 | startGame 忘了调 render | 正则匹配函数体 |

每类都有已知的正确映射表。比如 `start_game → navigate`，`movesLeft → moves`。检测到 → 确定性替换。

---

## 第三轮：异步架构 — 从"能用"到"可部署"

### 用户的 Prompt

> "全部做"（指 gap 分析中的 P0-P3 全部修复）

关键之一是：

> "/build-for-business 是同步 HTTP 请求，10-20 分钟必定超时"

### 代码改动

把原来的同步调用：

```python
# 旧代码
result = build_for_business(profile, game_slug)
return {"status": "success"}
```

改为异步队列：

```python
# 新代码
order_id = str(uuid.uuid4())
await r.hset(f"game_order:{order_id}", mapping={
    "order_type": "build_for_business",
    "business_description": body.business_description,
    "game_slug": body.game_slug,
    "status": "pending"
})
return {"order_id": order_id, "status": "pending"}
```

Portal 端从同步等待改为 5 秒轮询：

```javascript
// 旧代码：同步 await（会超时）
const res = await apiFetch('/api/v1/game-catalog/build-for-business', {...});

// 新代码：enqueue → poll
const orderRes = await apiFetch('.../build-for-business', {...});
const orderId = orderRes.order_id;
const pollInterval = setInterval(async () => {
  const status = await apiFetch('.../orders/' + orderId);
  if (status.status === 'completed') {
    clearInterval(pollInterval);
    showSuccess();
  }
}, 5000);
```

---

## 第四轮：页面流转 — 模拟真实用户路径

### 用户的 Prompt 序列（模拟完整用户旅程）

> "首页是landing 页面 然后 可以的点击登陆进入portal portal 退出登陆后回到 landing"

> "修改整个网站从 portal 开始， 然后用户退出登陆之后回到portal"

> "给整个 landing page 增加一个中文的语言的版本， 入口语言选择。"

> "登陆之后 只有创造游戏 没有看到已经创造游戏。"

> "点击不是开始玩的link"

> "时间限制好像已经不能玩 下一次 了 把闲置时间缩短到15秒， 可以玩 游戏结束了可以回到portal"

> "再portal 内部load 游戏"

每一个 prompt 都是在模拟一个用户操作，发现一个断裂，然后修好。

### 每一步的改动

| Prompt | 发现的问题 | 修复 |
|--------|----------|------|
| landing→portal→退出→landing | root redirect 指向 portal，logout 留在 portal | `/ → index.html`，logout → `window.location='index.html'` |
| 首页改成中文 | 全英文 landing page | 80+ 处双语包裹，CSS class 切换，语言记忆 |
| 登录了看不到已有游戏 | Games tab 只有创建流程 | `loadMyGames()` 合并订单+brand config 双数据源 |
| 点击不是开始玩的 link | `<a href>` 不触发 | 改为 `<button onclick="window.open()">`，最终改为 iframe 嵌入 |
| 能量用完不能玩 | regenSec=300s | 改为 15s demo 模式 |
| 游戏结束回 portal | play.html 无返回机制 | closeGame() 发 postMessage → Portal 监听关闭 |
| Portal 内部 load 游戏 | 每次弹新窗口 | 全屏 iframe 覆盖层，`play.html?brand=xxx` 嵌入 |

---

## 第五轮：Bug 分类 — 从表象到根因

### 用户的 Prompt

> "登录完成之后，那个登录界面没有消失"

### 排查链路

1. 检查 `showApp()` → 正确调用了 `classList.remove('active')`
2. 检查 CSS → `.page{display:none}` 正确
3. 检查 `#login-page` 的 CSS → `#login-page{display:flex}` **ID 选择器优先级高于 class**
4. 根因：去掉 active class 后，`.page{display:none}` 被 `#login-page{display:flex}` 覆盖

### 另一个同类型 bug

> "登陆成功了之后没有去到developer主页"

1. 检查 JWT 解码 → `atob(data.access_token.split('.')[1])`
2. base64url 无 padding → `atob()` 抛异常 → catch 里 fallback 到 `'bean-brothers'`
3. `bean-brothers` 是不存在的 brand_id → Dashboard API 404
4. 修复：`while (payloadB64.length % 4) payloadB64 += '='`

### Bug 分类法

这两个 bug 表面完全不同，但根因同类：**前端对浏览器 API 的边界条件处理不足。**

- `atob()` 需要 padding 但 JWT 不提供 → 解码失败静默降级
- CSS 选择器优先级 → ID 覆盖 class → 视觉正确但逻辑错误

这类 bug 的共同特征：**代码没有 crash，静默失败，人工测试才能发现。**

---

## 第六轮：完整性检查 — 系统化发现缺口

### 用户的 Prompt

> "三体迭代，根据我们的 gamification platform 的整体规划 review 系统现在还缺什么"

### 方法

1. 读取完整的规划文档（`kix-game-customization-flow.md`）
2. 逐一检查每个接口/组件是否已实现
3. 对比"文档说的"和"代码有的"
4. 分级：P0（阻塞）= 系统跑不通，P1（核心）= 主线断裂，P2（体验）= 用户不爽

### 发现的 11 个 Gap

| 级别 | Gap | 发现方式 |
|------|-----|---------|
| P0 | API 未重启，新路由未加载 | 测试端点返回 404 |
| P0 | Worker 运行旧代码 | 检查进程启动时间 |
| P1 | /build-for-business 同步超时 | 代码 review：10-20 分钟的同步 HTTP 调用 |
| P1 | Portal 没有 Route B 入口 | 对比文档规范 |
| P2 | 注册不收集 brand_color | 对比文档中的注册字段 |
| P2 | 生成完成无"立即玩" | 模拟用户完成流程 |

---

## 第七轮：最后的系统验证

### 真实链路测试

```bash
# 1. 启动全部服务
uvicorn app.main:app --port 8000 &
python game_builder.py &

# 2. 验证 API 可达
curl http://localhost:8000/health
# → {"status":"ok"}

# 3. 验证页面可达
curl -o /dev/null -w '%{http_code}' http://localhost:8000/landing/portal.html
# → 200
curl -o /dev/null -w '%{http_code}' http://localhost:8000/landing/play.html
# → 200

# 4. 验证推荐端点
curl -X POST /api/v1/game-catalog/recommend -d '{"business_description":"咖啡店"}'
# → [{"slug":"coffee_shop","score":0.95},...]

# 5. 验证异步订单
curl -X POST /api/v1/game-catalog/build-for-business -d '{"business_description":"咖啡店","game_slug":"match3"}'
# → {"order_id":"...","status":"pending"}

# 6. Worker 自动处理 → 生成 HTML
# 7. 轮询 → status=completed → game_file=games/brand-9c7223a6/match3.html
```

### 用户打开浏览器验收

每个功能都是用户真实打开、真实点击通过的：
- Landing 首页双语切换 ✓
- Portal 登录 → Dashboard ✓
- Games → 已有游戏列表 → 点击立即玩 → iframe 播放器 ✓
- 游戏可玩（emoji 棋盘、品牌色、Moves 显示） ✓
- 游戏结束 → 自动回到 Portal ✓
- 退出 → 回到 Landing ✓

---

## 构建模式总结

### Prompt 特征

| 类型 | 示例 | 产出 |
|------|------|------|
| 产品定义 | "游戏数量有限，用户想象力无限" | 产品规范文档 |
| 架构决策 | "品牌植入 = 需求注入" | 模块设计 |
| 用户模拟 | "点击了play没有反应" | Bug 发现 |
| 系统反思 | "反思整个过程还有什么不好" | Gap 分析 |
| 完整性检查 | "三体迭代 review" | 11 Gap 分级 |
| 体验优化 | "增加中文版本" | UI 改进 |

### 文档的作用

文档不是"写完代码再补的"。文档是**代码之前的思考**：

```
用户需求（一句话）
    ↓
文档（结构化的 spec，所有人能看懂）
    ↓
代码（spec 的直接翻译）
    ↓
测试（用户真的用）
    ↓
新文档（发现的新问题、新设计）
    ↓
新代码（修复 + 扩展）
```

### 从 1 个 bug 到 1 类 bug

这是最重要的模式：

1. 用户报告一个具体问题（"点击 play 没反应"）
2. 修好这个具体问题（`start_game → navigate`）
3. 反思：**这一类问题会反复出现吗？**
4. 如果会 → 建自动化防线（contract_verifier.py）
5. 下次同类问题 → 自动检测、自动修复、零人工

brand_injector、contract_verifier、async queue、JWT padding fix — 每一个都是从"修一个"到"防一类"。

---

*本文档基于 2026 年 5 月 23-24 日 KiX 平台构建的完整对话记录。*
