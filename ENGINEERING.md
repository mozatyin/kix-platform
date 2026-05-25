# KiX Platform — 工程交接文档

> 版本 5.0.0 · 2026-05-25 · 三仓架构

---

## 1. 仓库与目录结构

```
/Users/mozat/
├── kix-platform/     ← 主仓库 (FastAPI + Portal + Landing)
│   ├── app/          # FastAPI 后端
│   │   ├── routers/  # API 路由 (game_catalog, portal_auth, brands, ...)
│   │   ├── services/ # 业务逻辑 (energy, qr, reward, session, token)
│   │   ├── models.py # SQLAlchemy 模型
│   │   └── schemas.py# Pydantic 请求/响应模型
│   ├── landing/      # 静态前端 (portal.html, index.html, play.html)
│   │   └── games/    # 生成的品牌游戏 HTML
│   ├── workers/      # 后台 Worker (game_builder.py)
│   ├── data/         # 游戏目录 (1012 款, enriched)
│   ├── lua/          # Redis Lua 脚本
│   ├── migrations/   # Alembic 数据库迁移
│   ├── config/       # Nginx + Centrifugo 配置
│   └── docker-compose.yml
│
├── eltm/             ← ELTM 引擎 (游戏研究 + 生成)
│   └── eltm/
│       ├── kix_channel.py      # KiX 商家接口
│       ├── kix_enrich.py       # 游戏库标注
│       ├── brand_injector.py   # 品牌确定性注入
│       ├── contract_verifier.py# 合约验证器
│       └── llm.py              # LLM 客户端 (OpenRouter 路由)
│
└── code-soul/        ← 代码生成引擎
    └── code_soul/
        ├── sdk.py              # generate_game() 主入口
        └── kernel/templates/   # 游戏模板库
```

**仓库地址：**
- `github.com/mozatyin/kix-platform`
- `github.com/mozatyin/eltm`
- `github.com/mozatyin/code-soul`

---

## 2. 架构全景

```
                  ┌──────────────┐
                  │  Nginx :80   │
                  └──────┬───────┘
                         │
          ┌──────────────┼──────────────┐
          │              │              │
     /landing/*    /api/v1/*      /ws (Centrifugo)
          │              │              │
   ┌──────▼──────┐ ┌────▼─────┐ ┌──────▼──────┐
   │ Static HTML │ │ FastAPI  │ │  Centrifugo │
   │ portal.html │ │  :8000   │ │  实时通信    │
   │ index.html  │ └────┬─────┘ └─────────────┘
   │ play.html   │      │
   │ games/*.html│ ┌────▼─────┐ ┌──────────┐
   └─────────────┘ │PostgreSQL│ │  Redis   │
                   │   :5432  │ │  :6379   │
                   └──────────┘ └────┬─────┘
                                     │
                              ┌──────▼──────┐
                              │   Worker    │
                              │game_builder │
                              │  轮询订单    │
                              └──────┬──────┘
                                     │
                          ┌──────────┼──────────┐
                          │          │          │
                    ┌─────▼─────┐ ┌─▼────┐ ┌───▼────┐
                    │   ELTM    │ │OpenAI│ │Code-Soul│
                    │研究+匹配   │ │      │ │代码生成  │
                    └───────────┘ └──────┘ └─────────┘
```

---

## 3. 环境搭建

### 3.1 依赖

```bash
# 系统依赖
brew install postgresql@16 redis

# Python 3.12+
python3 -m venv .venv

# kix-platform
cd /Users/mozat/kix-platform
.venv/bin/pip install -e .
.venv/bin/pip install anthropic  # 已安装

# eltm (从源码安装)
cd /Users/mozat/eltm
.venv/bin/pip install -e .

# code-soul (从源码安装)
cd /Users/mozat/code-soul
.venv/bin/pip install -e .
```

### 3.2 数据库

```bash
createdb kix
# 首次运行自动创建表 (Alembic 迁移在 kix-platform/migrations/)
cd /Users/mozat/kix-platform
.venv/bin/alembic upgrade head
```

### 3.3 环境变量

```bash
# kix-platform/.env
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=kix
POSTGRES_USER=mozat
REDIS_URL=redis://localhost:6379/0
JWT_SECRET=kix-dev-secret-change-in-production
ANTHROPIC_API_KEY=sk-or-v1-...   # OpenRouter key (必填)
```

### 3.4 启动服务

```bash
# 1. PostgreSQL + Redis (开机自启)
brew services start postgresql@16
brew services start redis

# 2. API 服务器
cd /Users/mozat/kix-platform
ANTHROPIC_API_KEY=sk-or-v1-... .venv/bin/uvicorn app.main:app \
  --host 0.0.0.0 --port 8000

# 3. Worker (游戏生成后台进程)
cd /Users/mozat/code-soul
.venv/bin/python /Users/mozat/kix-platform/workers/game_builder.py

# 验证
curl http://localhost:8000/health
# → {"status":"ok","version":"5.0.0","uptime_seconds":1}
```

---

## 4. 页面入口

| URL | 文件 | 用途 |
|-----|------|------|
| `http://localhost:8000/` | → `landing/index.html` | LetsKiX 产品首页（双语） |
| `/landing/portal.html` | 商家管理后台 | 登录 → Dashboard → 游戏管理 |
| `/landing/play.html?brand=BRAND_ID` | 玩家游戏端 | 顾客扫码后看到的界面 |

---

## 5. API 端点总览

### 5.1 Portal 认证

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/portal/auth/register` | 注册（email, password, brand_name, brand_color） |
| POST | `/api/v1/portal/auth/login` | 登录 → JWT token |
| POST | `/api/v1/portal/auth/refresh` | 刷新 token |

### 5.2 品牌管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/brands/{brand_id}` | 获取品牌配置 |
| PUT | `/api/v1/brands/{brand_id}` | 更新品牌配置 |
| GET | `/api/v1/brands/{brand_id}/locations` | 门店列表 |

### 5.3 游戏市场（核心）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/game-catalog/recommend` | **智能推荐** — 根据业务描述返回 Top 10 |
| POST | `/api/v1/game-catalog/build-for-business` | **异步生成** — 创建订单，Worker 处理 |
| GET | `/api/v1/game-catalog/orders/{brand_id}` | 查询所有订单状态 |
| GET | `/api/v1/game-catalog/orders/{brand_id}/{order_id}` | 查询单个订单状态 |
| GET | `/api/v1/game-catalog` | 搜索游戏目录 |
| POST | `/api/v1/game-catalog/order` | 自定义游戏订单（Route B） |
| POST | `/api/v1/game-catalog/add-to-brand` | 添加游戏到品牌 |

### 5.4 游戏会话

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/game/start` | 开始游戏会话 |
| POST | `/api/v1/game/end` | 结束游戏 → 提交分数 |

### 5.5 能量系统

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/energy/grant` | QR 扫码授予能量 |
| GET | `/api/v1/energy/balance/{user_id}` | 查询能量余额 |

### 5.6 排行榜 + 连胜 + 奖励

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/leaderboard/{brand_id}` | 排行榜 |
| GET | `/api/v1/streak/{user_id}` | 连胜查询 |
| POST | `/internal/reward/evaluate` | 奖励评估 |
| POST | `/internal/qr/generate` | 生成动态二维码 |

---

## 6. 核心业务流程

### 6.1 商家注册 → 推荐 → 生成游戏（Route A）

```
1. 注册
   POST /portal/auth/register {email, password, brand_name, brand_color}
   → 创建 BrandConfig (brand_id 自动生成，存 PostgreSQL + Redis)

2. 登录 → JWT
   POST /portal/auth/login → {access_token, brand_id}

3. 智能推荐
   POST /game-catalog/recommend {business_description: "星巴克咖啡"}
   → 内部调用 eltm.kix_channel.rank_games_for_business()
   → Layer 1: research_business() → BusinessProfile (1次LLM, ~3s)
   → Layer 2: _prefilter_games() → 1012→150候选 (确定性匹配)
   → Layer 3: LLM 五维度精排 → Top 10 (~5s)
   → 返回: [{slug, name, score, dimensions, reskin_difficulty, brand_integration}]

4. 商家选择 → 异步生成
   POST /game-catalog/build-for-business {business_description, game_slug: "match3"}
   → 写入 Redis: game_order:UUID {order_type: "build_for_business", status: "pending"}
   → 返回 order_id 立即

5. Worker 处理
   game_builder.py 每5秒轮询 Redis
   → 发现 pending 订单 → 标记 building
   → research_business() → BusinessProfile
   → build_for_business(profile, game_slug)
      → Phase 1: 加载游戏 brick (秒级 library hit)
      → Phase 2: LLM 品牌植入方案 (1次LLM)
      → Phase 3: build_core + benchmark enrichment
      → Phase 4: Code-Soul generate_game (PDCA)
      → Phase 5: brand_injector.post_process() + contract_verifier.auto_fix_html()
      → Phase 6: 保存 games/{brand_id}/{slug}.html
   → 标记 Redis order 为 completed,写入 game_file

6. Portal 轮询
   Portal 每5秒 GET /game-catalog/orders/{brand_id}/{order_id}
   → status=completed → 显示 "▶ 立即玩"
```

### 6.2 顾客玩游戏流程

```
1. 顾客扫码 → play.html?brand=BRAND_ID&qr=TOKEN
2. play.html 加载:
   → GET /brands/{brand_id} → 品牌名/颜色/游戏列表
   → POST /energy/grant (如有QR token) → 能量充值
3. 选择游戏 → POST /game/start → session_id
4. iframe 加载 games/{brand_id}/{slug}.html
5. 游戏通过 postMessage 上报分数:
   → window.parent.postMessage({type:'kix_score_update', score:N})
   → window.parent.postMessage({type:'kix_game_end', score:N})
6. play.html 接收 → POST /game/end → 评分/奖励
```

---

## 7. 关键模块详解

### 7.1 `eltm/kix_channel.py` — 商家游戏管线

```python
from eltm.kix_channel import research_business, rank_games_for_business, build_for_business

# Step 0: 研究商家
profile = research_business("星巴克")
# → BusinessProfile(
#     brand_name="星巴克", industry="餐饮",
#     products=["咖啡", "茶饮", "甜点"],
#     brand_colors={"primary": "#00704A", "accent": "#FFFFFF"},
#     keywords=["咖啡", "coffee", ...]
#   )

# Step 1: 推荐游戏
ranked = rank_games_for_business("星巴克", top_n=10)
# → [{slug: "coffee_shop", score: 0.95, dimensions: {...}}, ...]

# Step 2: 生成品牌游戏
result = build_for_business(profile, "match3", brand_id="brand-xxx")
# → KiXGameResult(ok=True, html_path="games/brand-xxx/match3.html")
```

**如何添加新的匹配维度：**
- 编辑 `_prefilter_games()` 中的 `_MODEL_CATEGORY_AFFINITY` 字典
- 或在 `rank_games_for_business()` 的 Layer 3 prompt 中添加新维度

### 7.2 `eltm/brand_injector.py` — 品牌确定性注入

```python
from eltm.brand_injector import inject_brand, post_process

# 单独注入品牌
html = inject_brand(html, brand_name="星巴克", primary_color="#00704A",
                    products=["咖啡", "甜点"], industry="餐饮")
# → HTML 被修改: CSS 颜色、标题、棋子 emoji

# 完整后处理 (品牌 + 合约验证)
html, fixes = post_process(html_path, brand_name="星巴克", ...)
```

**注入内容：**
1. CSS 覆盖：body 背景渐变、按钮/高亮色 → 品牌主色
2. 标题替换：`<h1>` 替换为品牌名 + 行业 emoji
3. 棋子 emoji：根据产品列表自动选择（咖啡→☕🫘）
4. KiX Score Bridge 确保连线

### 7.3 `eltm/contract_verifier.py` — 合约验证器

Code-Soul 生成的是 LLM 代码，不可靠。合约验证器做确定性检查：

```python
from eltm.contract_verifier import auto_fix_html, verify_html

# 检测报告
report = verify_html(html_path)
# → {action_contract: {unhandled: ['start_game']},
#    state_contract: {field_fixes: {'movesLeft': 'moves'}}}

# 自动修复
fixed_html, fixes = auto_fix_html(html)
# → ['action: start_game → navigate', 'field: movesLeft → moves', ...]
```

**检测三类断裂：**

| 类型 | 检测内容 | 后果 |
|------|---------|------|
| Action | `{type:'start_game'}` vs `case 'start_game':` 是否存在 | 点击无反应 |
| State | `state.movesLeft` vs `initState` 中是否声明 | 显示空白/NaN |
| 结构 | `startGame()` 是否调用了 `render()` | 白屏 |

**如何添加新的检测规则：**
- Action: 编辑 `build_action_fix_map()` 中的 `known` 字典
- State: 编辑 `check_state_contract()` 中的 `known_mismatches` 字典
- 结构: 编辑 `auto_fix_html()` 中的正则匹配

### 7.4 `eltm/kix_enrich.py` — 游戏库预标注

```bash
cd /Users/mozat/code-soul
.venv/bin/python -m eltm.kix_enrich
# → 处理 1012 游戏，25/批，41 批，可断点续跑
# → 输出: /Users/mozat/kix-platform/data/game_catalog_enriched.json
```

**每个游戏被标注 8 个维度：**
`suitable_industries`, `business_models`, `brand_integration`, `customer_scenarios`, `visual_mood`, `campaign_goals`, `session_minutes`, `reskin_difficulty`

### 7.5 `workers/game_builder.py` — 后台 Worker

```python
# 轮询 Redis，处理两种订单类型：
# 1. order_type = "build_for_business" → _process_build_for_business()
# 2. order_type = "custom" (或旧格式) → _process_fulfill_order()

# 运行: cd code-soul && .venv/bin/python /path/to/game_builder.py
```

**订单状态机：** `pending → building → completed / failed / spec_ready`

### 7.6 Portal 页面 (`landing/portal.html`)

单文件 SPA（约 2500 行），包含：
- 登录/注册（JWT + brand_color）
- Dashboard（品牌统计）
- **游戏市场**（4 步流程 + 我的游戏 + iframe 内嵌游戏）
- 优惠券管理、QR 码生成、门店管理、设置

**关键函数：**
```javascript
recommendGames()    // POST /game-catalog/recommend → 渲染推荐列表
selectGame(slug)    // POST /game-catalog/build-for-business → 轮询状态
loadMyGames()       // GET /orders + brand config → 显示已有游戏
openGamePlayer()    // 在 Portal 内全屏加载 play.html?brand=xxx
```

### 7.7 Play 页面 (`landing/play.html`)

移动端游戏启动器（约 1200 行），包含：
- 品牌主题渲染（颜色、名称）
- 能量系统（显示/恢复，当前15秒刷新一次用于 demo）
- 游戏卡片列表
- iframe 游戏加载 + postMessage 通信
- 排行榜 + 连胜 + 奖励

---

## 8. 如何修改功能

### 修改游戏推荐逻辑
**文件：** `eltm/kix_channel.py`
- 预过滤规则：`_prefilter_games()` 中的关键词权重和类别亲和度
- 精排 prompt：`rank_games_for_business()` 中的 Layer 3 prompt

### 修改品牌注入效果
**文件：** `eltm/brand_injector.py`
- 颜色方案：`_build_brand_css()` 和 `_darken_hex()`
- 产品→emoji 映射：`_PRODUCT_EMOJI_MAP`
- 标题注入：`_inject_brand_title()`

### 修改 API 认证
**文件：** `kix-platform/app/routers/portal_auth.py`
- JWT 过期时间：`_PORTAL_TOKEN_TTL_SECONDS`
- 注册字段：`PortalRegisterRequest` (schemas.py)

### 修改 Portal 界面
**文件：** `kix-platform/landing/portal.html`
- 单文件，CSS 在 `<style>` 标签内，JS 在底部 `<script>` 标签内
- 核心状态：`state = {token, brandId, brandName, ...}`
- 导航：`navigate(view)` → 切换 view + 加载数据

### 修改能量规则
**文件：** `kix-platform/app/services/energy.py` + `lua/energy_*.lua`
- 能量恢复速率在 `play.html` 中 `regenSec` 变量（当前 15s demo 模式）

### 添加新的游戏模板
**文件：** `code-soul/code_soul/kernel/templates/`
- 遵循 handleAction(state, action) → newState 模式
- render(state) 渲染 DOM
- 注册到 ELTM library

---

## 9. 如何开发新功能

### 9.1 添加新 API 端点

```python
# 1. 在 app/schemas.py 定义请求/响应模型
class NewFeatureRequest(BaseModel):
    param: str

# 2. 在 app/routers/ 创建或扩展路由文件
@router.post("/new-feature")
async def new_feature(body: NewFeatureRequest, ...):
    ...

# 3. 在 app/main.py 注册路由
app.include_router(router, prefix="/api/v1/...")

# 4. 重启 API 服务器
```

### 9.2 添加新的游戏生成能力

```python
# 1. eltm/kix_channel.py: 扩展 build_for_business() Phase
# 2. eltm/brand_injector.py: 添加新的注入维度
# 3. eltm/contract_verifier.py: 添加新的检测规则
# 4. code-soul/: 创建新的游戏模板
```

### 9.3 扩展 Portal 前端

Portal 是单文件 SPA，修改 `portal.html`:
- HTML: 添加新的 `<div id="view-xxx" class="view">` 区块
- CSS: 在 `<style>` 中定义新样式
- JS: 在 `navigate()` 的 switch 中添加 `case 'xxx':loadXxx();break;`
- 导航按钮: 添加 `data-view="xxx"` 的 `.nav-item`

### 9.4 添加 Landing Page 内容

编辑 `landing/index.html`:
- 中英双语通过 `<span class="en">` 和 `<span class="cn">` 包裹
- 语言切换: `setLang('en'/'cn')`，CSS 控制显示
- 滚动动画: 给元素加 `class="reveal"` 即可

---

## 10. 测试账号

| 邮箱 | 密码 | 品牌 | brand_id |
|------|------|------|----------|
| laowang@cafe.com | cafe123 | 老王咖啡 | brand-9c7223a6 |

已生成的游戏：
- `games/brand-9c7223a6/coffee-latte-art.html`
- `games/brand-9c7223a6/match3.html` (Match-3 消消乐 — 星巴克风格)

---

## 11. 已知限制

1. **游戏生成 10-20 分钟** — benchmark enrichment (45个研究问题) 是主要耗时点
2. **复杂游戏 PDCA 全败** — 经营模拟类（Coffee Shop）不适合当前生成管线
3. **无动画** — Code-Soul 纯函数式架构，DOM 直接替换无中间帧
4. **品牌注入是后处理** — 靠正则替换，不是 LLM 原生遵守
5. **合约验证只检测已知模式** — 新的断裂类型需要手动添加规则
6. **能量恢复到 15s** (demo 模式) — 生产环境应改回 300s
7. **API Key 需通过环境变量传入** — 生产部署需配置 ANTHROPIC_API_KEY

---

## 12. 常用命令速查

```bash
# 启动全部服务
cd /Users/mozat/kix-platform
ANTHROPIC_API_KEY=sk-or-v1-... .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 &

cd /Users/mozat/code-soul
.venv/bin/python /Users/mozat/kix-platform/workers/game_builder.py &

# 测试 API
curl http://localhost:8000/health

# 登录获取 token
curl -X POST http://localhost:8000/api/v1/portal/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"laowang@cafe.com","password":"cafe123"}'

# 智能推荐
curl -X POST http://localhost:8000/api/v1/game-catalog/recommend \
  -H 'Authorization: Bearer TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"business_description":"咖啡店","top_n":5}'

# 创建生成订单
curl -X POST http://localhost:8000/api/v1/game-catalog/build-for-business \
  -H 'Authorization: Bearer TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"business_description":"咖啡店","game_slug":"match3"}'

# 查询订单
curl http://localhost:8000/api/v1/game-catalog/orders/brand-9c7223a6 \
  -H 'Authorization: Bearer TOKEN'

# 游戏库标注 (一次性)
cd /Users/mozat/code-soul
.venv/bin/python -m eltm.kix_enrich

# 单独测试品牌注入
cd /Users/mozat/eltm && .venv/bin/python -c "
from eltm.brand_injector import post_process
html, fixes = post_process('path/to/game.html', brand_name='测试', primary_color='#ff0000')
print(fixes)
"
```

---

## 13. 拉取最新代码

```bash
cd /Users/mozat/eltm && git pull origin main
cd /Users/mozat/code-soul && git pull origin main
cd /Users/mozat/kix-platform && git pull origin main
```

三仓必须同时拉取，因为它们之间有 cross-import 依赖（kix-platform → eltm, eltm → code-soul）。
