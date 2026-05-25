# KiX Gamification Platform — 构建历史

> 两天的完整构建记录。每一步可追溯、可复现。

---

## 起点：已有的基础设施

KiX 不是在空地上建起来的。以下组件在 Day 0 已经存在：

| 组件 | 状态 | 说明 |
|------|------|------|
| kix-platform/ 后端 | ✅ | FastAPI + PostgreSQL + Redis，26 个已有 API 端点 |
| Portal 登录/注册 | ✅ | JWT 认证，brand_id 体系 |
| play.html 玩家端 | ✅ | 移动端游戏启动器，能量/排行榜/连胜 |
| ELTM 引擎 | ✅ | 1012 款游戏库，build_core() 管线 |
| Code-Soul | ✅ | generate_game()，PDCA 修复循环 |
| game_catalog.json | ✅ | 612 款游戏（后被升级到 1012） |
| 旧 game_builder worker | ✅ | 只支持 fulfill_order 流程 |

**缺失的核心能力：** 商家不理解游戏→系统理解商家→匹配游戏→生成品牌游戏的整条链路，全是空的。

---

## Day 1（5 月 23 日）：核心管线

### 1.1 Game Catalog 升级：612 → 1012 款

**文件：** `kix-platform/data/game_catalog.json`

从 ELTM library 的 JSON 文件中导出全部游戏。3 款排除（app、test_app、todo — 不是游戏）。

---

### 1.2 `kix_channel.py` — 三大核心接口

**文件：** `eltm/eltm/kix_channel.py`（新建，~1000 行）

```python
research_business(input) -> BusinessProfile     # 商家→画像
rank_games_for_business(input) -> list[dict]    # 画像→推荐
build_for_business(profile, slug) -> KiXGameResult  # 选择→游戏
```

**`research_business()`** — 1 次 LLM 调用。输入可以是关键词、URL、品牌名、自由文本。输出 BusinessProfile（brand_name, industry, products, brand_colors, keywords, customer_context, visual_style）。

**`rank_games_for_business()`** — 三层管线：
- Layer 1: research_business() → BusinessProfile（~3s）
- Layer 2: _prefilter_games() — 确定性关键词+类别匹配，1012→150（~5ms）
- Layer 3: LLM 五维度精排（Theme/Model/Scenario/Integration/Accessibility），150→Top 10（~5s）

**`build_for_business()`** — 7 Phase 管线：
1. 加载基础游戏 brick（library hit，秒级）
2. LLM 品牌植入方案（1 次调用）
3. user_requirements 构造（KiX 平台约束 + 品牌需求）
4. build_core() — 正常 ELTM 管线
5. Code-Soul generate_game() — PDCA 迭代
6. Post-process — KiX Score Bridge + 品牌 CSS
7. 保存到 games/{brand_id}/{slug}.html

**`_prefilter_games()`** 的关键设计：用上了 enrichment 数据的 8 个维度（suitable_industries、business_models、reskin_difficulty 等），关键词匹配权重分层（名称匹配 5.0、别名 4.0、行业标签 3.0、描述 2.0）。

---

### 1.3 `kix_enrich.py` — 游戏库预标注

**文件：** `eltm/eltm/kix_enrich.py`（新建，~170 行）

对 1012 款游戏进行一次性深度标注，每款 8 个业务维度：
`suitable_industries`, `business_models`, `brand_integration`, `customer_scenarios`, `visual_mood`, `campaign_goals`, `session_minutes`, `reskin_difficulty`

批量处理：25 款/批，41 批，可断点续跑。LLM 标注，4000→8000 tokens（前两批 JSON 截断失败后修复）。最终 1012/1012 零失败完成。

输出：`kix-platform/data/game_catalog_enriched.json`（52K 行）

---

### 1.4 Portal UI — 四步游戏市场

**文件：** `kix-platform/landing/portal.html`（修改 ~200 行）

替换旧的"搜索游戏 + 自由文本下单"为四步流程：

```
Step 1: 描述您的业务（textarea）
  → 点击"智能推荐"
Step 2: 推荐列表（游戏卡片 + 五维度分数条 + 换皮难度标签 + 品牌植入建议）
  → 点击"选择这个"
Step 3: 正在生成（进度条动画）
Step 4: 生成完成（✓ + 操作按钮）
```

每张推荐卡片包含：游戏名、slug、匹配度百分比条、五维度 mini bars（主题/玩法/场景/植入/易用）、换皮难度 badge（easy=绿/medium=黄/hard=红）、品牌植入描述。

---

### 1.5 API 端点 — /recommend + /build-for-business

**文件：** `kix-platform/app/routers/game_catalog.py`（修改 ~100 行）

```python
POST /api/v1/game-catalog/recommend
  body: {business_description, top_n}
  → rank_games_for_business() → 推荐列表

POST /api/v1/game-catalog/build-for-business
  body: {business_description, game_slug}
  → build_for_business() → 品牌游戏
```

JWT 认证（`_get_portal_operator`），brand_id 从 token 中提取。

**注意：此端点第一版是同步的（直接调用 build_for_business，耗时 10-20 分钟）。Day 2 改为异步队列。**

---

### 1.6 Worker 双流程路由

**文件：** `kix-platform/workers/game_builder.py`（修改 ~80 行）

```python
def process_order(r, order):
    if order_type == "build_for_business":
        _process_build_for_business(r, order)   # NEW
    else:
        _process_fulfill_order(r, order)         # OLD
```

`_process_build_for_business()` 完整实现：
1. research_business() → BusinessProfile
2. build_for_business(profile, game_slug)
3. _handle_result() → 更新 Redis 订单状态

---

### 1.7 API Key 路由修复

`kix_channel.py` 中三处 LLM 调用直接用 `anthropic.Anthropic(api_key=...)`，但所有 key 都是 OpenRouter 的 `sk-or-v1-...` 前缀。ELTM 已有 `eltm/llm.py:_make_client()` 自动检测前缀并路由到 `openrouter.ai`。

修复：三处全部改为 `from eltm.llm import call_llm`。

---

## Day 2（5 月 24 日）：质量防火墙 + 完整闭环

### 2.1 Enrichment 批量执行 + 修复

运行 `enrich_catalog()`：41 批、1012 款游戏、max_tokens=8000。前两批失败（prompt 中 `{"slug": "..."}` 被 `.format()` 当作占位符 → KeyError）。修复后零失败完成。

---

### 2.2 端到端测试：星巴克 × 消消乐

```python
profile = research_business("Starbucks")
# → brand_name="星巴克", colors="#00704A", products=["咖啡","茶饮","甜点"]

result = build_for_business(profile, "match3")
# → ok=True, html_path="games/starbucks/match3.html", 37KB
```

**发现的问题（测试中的真实 bug）：**

| 问题 | 表现 | 根因 |
|------|------|------|
| 品牌色未生效 | 紫粉色背景，不是星巴克绿 | CSS 变量 `--kix-brand` 设了但无人引用 |
| Moves 显示空 | 数字不显示 | render 读 `state.movesLeft`，initState 声明 `state.moves` |
| 色块无意义 | 纯色方块 | 没有 emoji，没有品牌元素 |
| 点击无效 | 点棋子弹出 Game Over | `checkGameOver` 用 `state.boardSize`（undefined），循环不执行→默认返回 true |
| Play 按钮无反应 | 点击不进入游戏 | `{type:'start_game'}` 在 handleAction 中无对应 case |
| 其他按钮全坏 | pause/settings/shop 都不工作 | 11 处 action type 与 handleAction case 不匹配 |

**手动修复了 12+ 处。** 这些 bug 暴露了一个系统性问题。

---

### 2.3 `brand_injector.py` — 品牌确定性注入

**文件：** `eltm/eltm/brand_injector.py`（新建，~300 行）

**设计转变：** Gen 1 是把品牌需求作为文本贴在 PRD 里 → LLM 生成代码时"忘记"。Gen 2 是**代码生成后做确定性后处理**。

```python
post_process(html_path, brand_name, primary_color, products, ...)
  ├── CSS 覆盖：body 背景渐变 → 品牌主色
  ├── 按钮/高亮色 → 品牌主色
  ├── 标题替换 → "☕ 星巴克 Match"
  ├── 棋子 emoji：产品 → emoji 映射 (咖啡→☕🫘🧁🍪🍵🥐)
  ├── KiX Score Bridge 确保连线
  └── contract_verifier.auto_fix_html()
```

`_PRODUCT_EMOJI_MAP` 自动映射产品类别到 emoji 表。`_build_brand_css()` 生成真正被使用的 CSS 选择器（不是无人引用的 `:root` 变量）。

---

### 2.4 `contract_verifier.py` — 合约自动验证

**文件：** `eltm/eltm/contract_verifier.py`（新建，~200 行）

Code-Soul 是 LLM 生成的代码。LLM 不可靠。合约验证器是确定性防线。

**三类检测：**

| 类型 | 检测 | 示例 |
|------|------|------|
| Action | `{type:'X'}` vs `case 'X':` 存在？ | `start_game` → 无 case → `navigate` |
| State | `state.X` vs `initState` 中有 `X`？ | `movesLeft` → `moves` |
| 结构 | `startGame()` 是否调用了 `render()`？ | 漏调 → 白屏 |

`auto_fix_html()` 检测到后自动修补。

---

### 2.5 全面缺口分析 → 11 个 Gap 修复

**P0（阻塞）：**
- Gap 2: API 服务器未重启（新路由未加载）→ 重启
- Gap 3: Worker 运行旧代码 → 重启

**P1（核心功能）：**
- Gap 1: `/build-for-business` 改成异步队列（10-20 分钟同步调用会 HTTP 超时）
  - 改为：创建 Redis 订单 → 返回 order_id → Worker 处理 → Portal 5 秒轮询
  - 新增 `GET /orders/{brand_id}/{order_id}` 单订单轮询端点
- Gap 4: Portal Route B "从零创造" 入口 → 推荐列表底部加按钮

**P2（用户体验）：**
- Gap 5: 注册加 `brand_color` 字段 → PortalRegisterRequest schema + Portal 表单颜色选择器 + BrandConfig 存储
- Gap 6: Portal 生成完成后 "▶ 立即玩" 按钮 → 跳转 play.html

**修复中额外做了：**
- `GameOrderResponse` schema 扩展（加 game_file, game_name, order_type, error 字段）
- `anthropic` 安装到 kix-platform venv
- API 服务器静态文件 mount（`/landing/` 目录）

---

### 2.6 Portal ↔ Landing 页面打通

**修改：**

| 改动 | 文件 | 内容 |
|------|------|------|
| Root redirect | `main.py` | `/` → `/landing/index.html` |
| Logout redirect | `portal.html` | 退出 → `window.location.href = 'index.html'` |
| Landing → Portal | `index.html` | 已有 "Open Brand Portal" 链接 |
| Portal → Landing | `portal.html` | 登录页加 "← 返回 KiX 首页" 链接 |

**流程：**
```
http://localhost:8000/
  → index.html (LetsKiX 首页)
    → "Open Brand Portal" → portal.html (登录)
      → 登录 → Dashboard
      → 退出 → index.html
```

---

### 2.7 JWT 解码修复

`atob()` 解码 base64url 无 padding 的 JWT → 解码失败 → fallback 到 `bean-brothers` → Dashboard API 查不到品牌 → 页面空。

修复：`while (payloadB64.length % 4) payloadB64 += '='` 补 padding。修复了两处（登录回调 + 页面加载自动登录）。

---

### 2.8 CSS 登录页消失修复

`#login-page{display:flex}` 优先级高于 `.page{display:none}` → `showApp()` 去掉 active class 后，登录页不消失。

改为 `#login-page.active{display:flex}`，使 ID 选择器只在 active 时生效。

---

### 2.9 Landing Page 双语化

**文件：** `kix-platform/landing/index.html`（修改 ~80 处）

机制：CSS class `.lang-en` / `.lang-cn` 控制显示，`<body class="lang-cn">` 默认中文。每个文本元素包裹 `<span class="en">EN</span><span class="cn">中文</span>`。

语言切换器在导航栏右侧：`EN | 中文`。localStorage 记忆选择。

已翻译全部区域：导航栏、Hero、统计条、问题分析（3 卡片）、运作方式（3 步骤）、Demo（2 卡片）、四大模块（KID/KIN/KASH/KLUB）、NE 引擎、商业闭环、8 条铁律、对比表格（8 行）、CTA、页脚。

---

### 2.10 Portal "我的游戏" + 内嵌游戏播放器

**问题：** 登录后只看到创建流程，看不到已生成的游戏。

**修复：**
1. `loadMyGames()` — 合并两个数据源：Redis 已完成的订单 + BrandConfig config_json.games
2. 游戏卡片含"▶ 立即玩"按钮 → 触发 `openGamePlayer()`
3. 全屏 iframe 覆盖层加载 `play.html?brand=BRAND_ID`
4. 游戏结束后 `play.html` 发 `postMessage` → Portal 监听 → 1.5 秒后自动关闭 iframe + 刷新游戏列表

---

### 2.11 Demo 模式调整

- 能量恢复时间：300s → 15s（play.html: `regenSec = 15`）
- 游戏结束：`closeGame()` 发 `postMessage({type:'kix_close_game'})` → Portal 接收

---

### 2.12 代码提交 + 文档

| 仓库 | Commits | 内容 |
|------|---------|------|
| kix-platform | 3 commits (init + handoff + journey) | 全量代码 + 工程文档 |
| eltm | 4 new files | kix_channel, kix_enrich, brand_injector, contract_verifier |
| code-soul | 4 new files | match3 模板 |

**文档产出：**
- `kix-system-overview.md` — 12 页 PPT 概览
- `kix-engineering-handoff.md` — 13 章工程交接文档
- `kix-builder-journey.md` — 14 个月工厂 + 2 天 KiX 教学叙事
- `kix-build-history.md` — 本文档

---

## 最终系统状态

```
http://localhost:8000/
├── /                    → landing/index.html (双语产品首页)
├── /landing/portal.html  → 商家管理后台
│   ├── 登录/注册 (JWT + brand_color)
│   ├── Dashboard (品牌统计)
│   ├── Games (我的游戏 + 智能推荐 + iframe 游戏播放器)
│   └── Vouchers, QR, Locations, Settings
├── /landing/play.html?brand=xxx → 移动端游戏启动器
├── /landing/games/{brand_id}/{slug}.html → 生成的品牌游戏
│
├── POST /api/v1/game-catalog/recommend → 智能推荐
├── POST /api/v1/game-catalog/build-for-business → 异步订单
├── GET  /api/v1/game-catalog/orders/{brand_id}/{order_id} → 轮询状态
└── Worker: game_builder.py (Redis 轮询, 双流程)
```

**测试账号：** laowang@cafe.com / cafe123 / brand-9c7223a6

**已生成游戏：** match3 (星巴克版), coffee-latte-art
