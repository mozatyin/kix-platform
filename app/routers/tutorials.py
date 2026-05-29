"""Tutorial Engine — convert Recipes into step-by-step guided tours.

Where Recipe.apply is 1-click magic, the Tutorial flow walks the merchant
through the Portal so they LEARN the system. A merchant clicks
"Walk me through it" → backend generates a TutorialPlan (deterministic
mapping from Recipe) → frontend renders the tour step by step.

Endpoints (registered under /api/v1/tutorials):
  POST   /from-recipe             — generate from a recipe in the catalog
  POST   /from-recipe-json        — generate from an inline recipe JSON
                                     (i.e. the output of /recipe-gen)
  GET    /{tutorial_id}           — fetch current state
  POST   /{tutorial_id}/advance   — step++, optionally with validation result
  POST   /{tutorial_id}/skip      — advance without validating
  POST   /{tutorial_id}/validate-current
                                  — check the current step's exit criteria
  POST   /{tutorial_id}/abandon   — mark abandoned
  GET    /brand/{brand_id}        — list a brand's tutorials

Redis schema:
  tutorial:{tid}              HASH  {brand_id, recipe_id, plan, current_step,
                                     status, started_at, updated_at}
  brand:{bid}:tutorials       SET   of tutorial_ids

The Recipe→TutorialPlan conversion is purely deterministic (NO LLM): given
the same recipe JSON we always produce the same plan structure.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.redis_client import get_redis
from app.routers.recipes import Recipe, _get_recipe

logger = logging.getLogger(__name__)

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Module display metadata: id → (name_en, name_cn, selector)
# Used by the deterministic plan generator to pick selectors and humanize
# instructions. Sourced from KNOWN_MODULES in recipes.py — must stay in sync.
# ─────────────────────────────────────────────────────────────────────────────

MODULE_META: dict[str, dict[str, str]] = {
    # progression
    "progression": {"name_en": "Progression",      "name_cn": "成长体系"},
    "currency":    {"name_en": "Currency",         "name_cn": "代币系统"},
    "item":        {"name_en": "Item",             "name_cn": "道具系统"},
    "achievement": {"name_en": "Achievement",      "name_cn": "成就系统"},
    "quest":       {"name_en": "Quest",            "name_cn": "任务系统"},
    "tier":        {"name_en": "Tier",             "name_cn": "等级 (Tier)"},
    "event":       {"name_en": "Event",            "name_cn": "活动事件"},
    # composable mechanics
    "roulette":         {"name_en": "Reward Roulette", "name_cn": "抽奖轮盘"},
    "league":           {"name_en": "League",          "name_cn": "联赛"},
    "pass":             {"name_en": "Battle Pass",     "name_cn": "战令通行证"},
    "smartquests":      {"name_en": "Smart Quests",    "name_cn": "智能任务"},
    "storyquest":       {"name_en": "Story Quest",     "name_cn": "剧情任务"},
    "lives":            {"name_en": "Lives",           "name_cn": "生命值"},
    "tourney":          {"name_en": "Tournament",      "name_cn": "锦标赛"},
    "collection":       {"name_en": "Collection",      "name_cn": "收藏册"},
    "badgewall":        {"name_en": "Badge Wall",      "name_cn": "勋章墙"},
    # streak / loyalty
    "streak": {"name_en": "Streak", "name_cn": "连续打卡"},
    # voucher
    "voucher_builder": {"name_en": "Voucher Builder", "name_cn": "优惠券模板"},
    "voucher":         {"name_en": "Voucher",         "name_cn": "优惠券"},
    # social / share / network-effect
    "social_graph":     {"name_en": "Social Graph",      "name_cn": "社交图谱"},
    "social_feed":      {"name_en": "Social Feed",       "name_cn": "社交动态"},
    "auto_share":       {"name_en": "Auto Share",        "name_cn": "自动分享"},
    "share_to_win":     {"name_en": "Share to Win",      "name_cn": "分享得奖"},
    "energy_invite":    {"name_en": "Energy Invite",     "name_cn": "邀请送能量"},
    "friend_challenge": {"name_en": "Friend Challenge",  "name_cn": "好友挑战"},
    "ladder_climb":     {"name_en": "Ladder Climb",      "name_cn": "天梯攀升"},
    "streak_rescue":    {"name_en": "Streak Rescue",     "name_cn": "续命挽救"},
    "leaderboard":      {"name_en": "Leaderboard",       "name_cn": "排行榜"},
    "network_effect":   {"name_en": "Network Effect",    "name_cn": "网络效应"},
    # commerce loop
    "score_to_coupon":  {"name_en": "Score → Coupon",    "name_cn": "积分换券"},
    "energy":           {"name_en": "Energy",            "name_cn": "能量系统"},
    "upsell":           {"name_en": "Upsell",            "name_cn": "增值推荐"},
    "redemption_store": {"name_en": "Redemption Store",  "name_cn": "兑换商店"},
    "rate_limit":       {"name_en": "Rate Limit",        "name_cn": "频率限制"},
    # group viral
    "group_actions": {"name_en": "Group Actions", "name_cn": "团购助力"},
    "groupbuy":      {"name_en": "Group Buy",     "name_cn": "拼团"},
    "atomic_group":  {"name_en": "Atomic Group",  "name_cn": "原子团"},
    "pricecut":      {"name_en": "Price Cut",     "name_cn": "砍一刀"},
    # multiplayer coop
    "coop_quest": {"name_en": "Coop Quest", "name_cn": "合作任务"},
    "raid":       {"name_en": "Raid",       "name_cn": "副本"},
    "squad":      {"name_en": "Squad",      "name_cn": "战队"},
    "territory":  {"name_en": "Territory",  "name_cn": "领地战"},
    # p2p
    "gift_sending": {"name_en": "Gift Sending", "name_cn": "送礼"},
    "trading_post": {"name_en": "Trading Post", "name_cn": "交易所"},
    "group_reward": {"name_en": "Group Reward", "name_cn": "团体奖励"},
    "fcfs":         {"name_en": "First-Come First-Served", "name_cn": "先到先得"},
    # primitives
    "limited_drop": {"name_en": "Limited Drop", "name_cn": "限量发放"},
    "triggers":     {"name_en": "Triggers",     "name_cn": "触发器"},
}


def _module_selector(module_id: str) -> str:
    """CSS selector for a module's tile on the Portal Engagement page."""
    return f"[data-module-id='{module_id}']"


def _module_name(module_id: str, language: str) -> str:
    meta = MODULE_META.get(module_id)
    if not meta:
        return module_id
    return meta["name_cn"] if language == "cn" else meta["name_en"]


# ─────────────────────────────────────────────────────────────────────────────
# Step instruction templates (CN/EN). Keep these short and imperative.
# ─────────────────────────────────────────────────────────────────────────────

INSTRUCTION_TEMPLATES: dict[str, dict[str, str]] = {
    "intro": {
        "cn": "我们将引导你搭建「{recipe_name}」。包含 {module_count} 个模块、{rule_count} 条规则。",
        "en": "We'll walk you through setting up “{recipe_name}”. {module_count} modules and {rule_count} rules.",
    },
    "navigate_engagement": {
        "cn": "点击侧边栏的 Engagement 进入模块市场",
        "en": "Click Engagement in the sidebar to open the module marketplace",
    },
    "navigate_vouchers": {
        "cn": "进入侧边栏的 Vouchers 配置优惠券模板",
        "en": "Open Vouchers in the sidebar to configure voucher templates",
    },
    "navigate_rules": {
        "cn": "进入侧边栏的 Rules 配置事件规则",
        "en": "Open Rules in the sidebar to configure event rules",
    },
    "enable_module": {
        "cn": "启用 {module_name} 模块",
        "en": "Enable the {module_name} module",
    },
    "configure_module": {
        "cn": "配置 {module_name}：{params_summary}",
        "en": "Configure {module_name}: {params_summary}",
    },
    "create_voucher_template": {
        "cn": "创建优惠券模板：{template_summary}",
        "en": "Create voucher template: {template_summary}",
    },
    "create_rule": {
        "cn": "创建规则：当 {trigger_event} 触发时执行 {actions_summary}",
        "en": "Create rule: when {trigger_event} → {actions_summary}",
    },
    "test_action": {
        "cn": "让我们模拟一次「{event_name}」来测试规则",
        "en": "Let's simulate “{event_name}” to test the rules",
    },
    "celebrate": {
        "cn": "完成！你的「{recipe_name}」体系已经上线 🎉",
        "en": "Done! Your “{recipe_name}” setup is live 🎉",
    },
}


def _t(key: str, language: str, **fmt: Any) -> str:
    tpl = INSTRUCTION_TEMPLATES.get(key, {})
    raw = tpl.get(language) or tpl.get("cn") or key
    try:
        return raw.format(**fmt)
    except (KeyError, IndexError):
        return raw


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# ─────────────────────────────────────────────────────────────────────────────


StepType = Literal[
    "info",
    "navigate",
    "enable_module",
    "configure_module",
    "create_voucher_template",
    "create_rule",
    "test_action",
    "celebrate",
]


class TutorialStep(BaseModel):
    id: str
    type: StepType
    title_cn: str | None = None
    title_en: str | None = None
    instruction_cn: str | None = None
    instruction_en: str | None = None

    # Targeting (frontend renders highlight on this selector)
    target_view: str | None = None
    target_selector: str | None = None

    # Step-specific payload
    module_id: str | None = None
    suggested_params: dict[str, Any] | None = None
    voucher_template: dict[str, Any] | None = None
    rule_template: dict[str, Any] | None = None
    test_endpoint: str | None = None
    test_payload: dict[str, Any] | None = None

    # Validation hint — frontend can pre-check before /advance
    validation: dict[str, Any] | None = None


class TutorialPlan(BaseModel):
    tutorial_id: str
    brand_id: str
    recipe_id: str | None = None
    title: str
    title_cn: str | None = None
    total_steps: int
    current_step: int = 0
    status: Literal["active", "completed", "abandoned"] = "active"
    started_at: str
    updated_at: str | None = None
    language: Literal["cn", "en"] = "cn"
    steps: list[TutorialStep] = Field(default_factory=list)


class FromRecipeRequest(BaseModel):
    brand_id: str
    recipe_id: str
    language: Literal["cn", "en"] = "cn"


class FromRecipeJsonRequest(BaseModel):
    brand_id: str
    recipe: dict[str, Any]
    language: Literal["cn", "en"] = "cn"


class AdvanceRequest(BaseModel):
    validation_result: dict[str, Any] | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Redis key helpers
# ─────────────────────────────────────────────────────────────────────────────


def _k_tutorial(tid: str) -> str:
    return f"tutorial:{tid}"


def _k_brand_tutorials(bid: str) -> str:
    return f"brand:{bid}:tutorials"


def _k_brand_modules(bid: str) -> str:
    return f"brand:{bid}:modules"


def _k_brand_rules_primary(bid: str) -> str:
    # rule_engine.py canonical
    return f"brand:{bid}:rules"


def _k_brand_rules_legacy(bid: str) -> str:
    # recipes.py legacy write target — checked as fallback
    return f"rules:{bid}"


def _k_voucher_templates(bid: str) -> str:
    return f"brand:{bid}:voucher_templates"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_tid() -> str:
    return "tut_" + secrets.token_hex(6)


# ─────────────────────────────────────────────────────────────────────────────
# Plan generator (deterministic)
# ─────────────────────────────────────────────────────────────────────────────


def _summarize_params(params: dict[str, Any], language: str) -> str:
    """One-line human-readable summary of module params for instruction text."""
    if not params:
        return "默认参数" if language == "cn" else "default settings"
    parts: list[str] = []
    for key, value in list(params.items())[:3]:
        if isinstance(value, list):
            parts.append(f"{key}={len(value)} {'项' if language == 'cn' else 'items'}")
        elif isinstance(value, dict):
            parts.append(f"{key}={{…}}")
        else:
            parts.append(f"{key}={value}")
    return ", ".join(parts)


def _summarize_voucher(tpl: dict[str, Any], language: str) -> str:
    name = tpl.get("name") or tpl.get("id") or "voucher"
    discount = tpl.get("discount") or tpl.get("value") or tpl.get("amount")
    if discount is None:
        return str(name)
    if language == "cn":
        return f"{name}（{discount}）"
    return f"{name} ({discount})"


def _summarize_actions(actions: list[dict[str, Any]], language: str) -> str:
    if not actions:
        return "无动作" if language == "cn" else "no action"
    bits: list[str] = []
    for a in actions[:3]:
        mod = a.get("module", "?")
        meth = a.get("method", "?")
        amt = (a.get("params") or {}).get("amount")
        if amt is not None:
            bits.append(f"{mod}.{meth}({amt})")
        else:
            bits.append(f"{mod}.{meth}")
    return ", ".join(bits)


def _has_meaningful_config(params: dict[str, Any]) -> bool:
    """A configure step only adds value if there's something to configure."""
    if not isinstance(params, dict) or not params:
        return False
    # Strip obvious no-op flags
    keys = [k for k in params.keys() if k not in {"enabled", "id"}]
    return bool(keys)


def _build_plan(
    *,
    recipe: dict[str, Any],
    brand_id: str,
    language: str,
    tutorial_id: str,
) -> TutorialPlan:
    """Deterministic Recipe → TutorialPlan conversion.

    Algorithm:
      1. info: intro using recipe.description_*
      2. navigate to engagement view
      3. per module: enable_module (+ configure_module if non-trivial params)
      4. if any voucher templates → navigate vouchers + create steps
      5. per rule: navigate (once) + create_rule
      6. one test_action exercising the first rule's trigger
      7. celebrate
    """
    recipe_id = recipe.get("id", "custom")
    recipe_name = (
        recipe.get("name_cn") if language == "cn" else recipe.get("name")
    ) or recipe.get("name") or recipe_id
    description = (
        recipe.get("description_cn") if language == "cn"
        else recipe.get("description_en")
    ) or recipe.get("description_en") or recipe.get("description_cn") or ""

    modules: list[dict[str, Any]] = list(recipe.get("modules", []) or [])
    rules: list[dict[str, Any]] = list(recipe.get("rules", []) or [])
    voucher_templates: list[dict[str, Any]] = list(
        recipe.get("voucher_templates")
        or (recipe.get("metadata") or {}).get("voucher_templates")
        or []
    )

    steps: list[TutorialStep] = []
    counter = 0

    def _next_id() -> str:
        nonlocal counter
        counter += 1
        return f"step_{counter}"

    # 1. Intro
    intro_text = description or _t(
        "intro",
        language,
        recipe_name=recipe_name,
        module_count=len(modules),
        rule_count=len(rules),
    )
    steps.append(TutorialStep(
        id=_next_id(),
        type="info",
        title_cn="欢迎" if language == "cn" else None,
        title_en=None if language == "cn" else "Welcome",
        instruction_cn=intro_text if language == "cn" else None,
        instruction_en=intro_text if language != "cn" else None,
    ))

    # 2. Navigate to engagement
    if modules:
        steps.append(TutorialStep(
            id=_next_id(),
            type="navigate",
            target_view="engagement",
            target_selector="[data-view='engagement']",
            instruction_cn=_t("navigate_engagement", "cn") if language == "cn" else None,
            instruction_en=_t("navigate_engagement", "en") if language != "cn" else None,
        ))

    # 3. Per module: enable + (maybe) configure
    for m in modules:
        mid = m.get("id")
        if not mid:
            continue
        params = m.get("params") or {}
        mname = _module_name(mid, language)

        # Enable
        instr = _t("enable_module", language, module_name=mname)
        steps.append(TutorialStep(
            id=_next_id(),
            type="enable_module",
            module_id=mid,
            target_selector=f"{_module_selector(mid)} .toggle",
            instruction_cn=instr if language == "cn" else None,
            instruction_en=instr if language != "cn" else None,
            validation={
                "api_check": f"GET /api/v1/brands/{brand_id}/modules/{mid}",
                "expected": {"enabled": True},
            },
        ))

        # Configure (only if there are meaningful params)
        if _has_meaningful_config(params):
            summary = _summarize_params(params, language)
            instr_cfg = _t(
                "configure_module", language,
                module_name=mname, params_summary=summary,
            )
            steps.append(TutorialStep(
                id=_next_id(),
                type="configure_module",
                module_id=mid,
                target_selector=f"{_module_selector(mid)} .configure",
                instruction_cn=instr_cfg if language == "cn" else None,
                instruction_en=instr_cfg if language != "cn" else None,
                suggested_params=params,
                validation={
                    "api_check": f"GET /api/v1/brands/{brand_id}/modules/{mid}",
                    "expected_fields": [f"params.{k}" for k in list(params.keys())[:3]],
                },
            ))

    # 4. Voucher templates
    if voucher_templates:
        steps.append(TutorialStep(
            id=_next_id(),
            type="navigate",
            target_view="vouchers",
            target_selector="[data-view='vouchers']",
            instruction_cn=_t("navigate_vouchers", "cn") if language == "cn" else None,
            instruction_en=_t("navigate_vouchers", "en") if language != "cn" else None,
        ))
        for tpl in voucher_templates:
            tpl_summary = _summarize_voucher(tpl, language)
            instr_v = _t("create_voucher_template", language, template_summary=tpl_summary)
            tpl_id = tpl.get("id") or tpl.get("name") or "voucher_tpl"
            steps.append(TutorialStep(
                id=_next_id(),
                type="create_voucher_template",
                target_selector="[data-action='new-voucher-template']",
                voucher_template=tpl,
                instruction_cn=instr_v if language == "cn" else None,
                instruction_en=instr_v if language != "cn" else None,
                validation={
                    "voucher_template_id": tpl_id,
                },
            ))

    # 5. Per rule
    if rules:
        steps.append(TutorialStep(
            id=_next_id(),
            type="navigate",
            target_view="rules",
            target_selector="[data-view='rules']",
            instruction_cn=_t("navigate_rules", "cn") if language == "cn" else None,
            instruction_en=_t("navigate_rules", "en") if language != "cn" else None,
        ))
        for idx, rule in enumerate(rules):
            trigger = rule.get("trigger_event", "?")
            actions = rule.get("actions") or []
            instr_r = _t(
                "create_rule", language,
                trigger_event=trigger,
                actions_summary=_summarize_actions(actions, language),
            )
            steps.append(TutorialStep(
                id=_next_id(),
                type="create_rule",
                target_selector="[data-action='new-rule']",
                rule_template={
                    "name": rule.get("name") or f"{recipe_id} rule #{idx + 1}",
                    "trigger_event": trigger,
                    "conditions": rule.get("conditions"),
                    "actions": actions,
                    "max_triggers_per_user": rule.get("max_triggers_per_user", 1),
                },
                instruction_cn=instr_r if language == "cn" else None,
                instruction_en=instr_r if language != "cn" else None,
                validation={
                    "trigger_event": trigger,
                },
            ))

    # 6. Test action — exercise first rule (if any)
    if rules:
        first = rules[0]
        trig = first.get("trigger_event", "purchase_made")
        instr_t = _t("test_action", language, event_name=trig)
        steps.append(TutorialStep(
            id=_next_id(),
            type="test_action",
            target_selector="[data-action='emit-test-event']",
            test_endpoint="POST /api/v1/rules/events/emit",
            test_payload={"brand_id": brand_id, "event_name": trig, "user_id": "tutorial_user"},
            instruction_cn=instr_t if language == "cn" else None,
            instruction_en=instr_t if language != "cn" else None,
        ))

    # 7. Celebrate
    instr_c = _t("celebrate", language, recipe_name=recipe_name)
    steps.append(TutorialStep(
        id=_next_id(),
        type="celebrate",
        title_cn="完成 🎉" if language == "cn" else None,
        title_en=None if language == "cn" else "Done 🎉",
        instruction_cn=instr_c if language == "cn" else None,
        instruction_en=instr_c if language != "cn" else None,
    ))

    title_en = recipe.get("name") or recipe_id
    title_cn = recipe.get("name_cn") or title_en

    now = _now_iso()
    return TutorialPlan(
        tutorial_id=tutorial_id,
        brand_id=brand_id,
        recipe_id=recipe_id,
        title=title_en,
        title_cn=title_cn,
        total_steps=len(steps),
        current_step=0,
        status="active",
        started_at=now,
        updated_at=now,
        language=language,  # type: ignore[arg-type]
        steps=steps,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────


async def _save_plan(plan: TutorialPlan, r: aioredis.Redis) -> None:
    plan.updated_at = _now_iso()
    payload = {
        "brand_id": plan.brand_id,
        "recipe_id": plan.recipe_id or "",
        "plan": json.dumps(plan.model_dump()),
        "current_step": str(plan.current_step),
        "status": plan.status,
        "started_at": plan.started_at,
        "updated_at": plan.updated_at,
    }
    pipe = r.pipeline(transaction=True)
    pipe.hset(_k_tutorial(plan.tutorial_id), mapping=payload)
    pipe.sadd(_k_brand_tutorials(plan.brand_id), plan.tutorial_id)
    await pipe.execute()


async def _load_plan(tid: str, r: aioredis.Redis) -> TutorialPlan | None:
    raw = await r.hget(_k_tutorial(tid), "plan")
    if not raw:
        return None
    try:
        return TutorialPlan.model_validate(json.loads(raw))
    except Exception as exc:
        logger.warning("tutorials: bad plan json for %s: %s", tid, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Per-step validation
# ─────────────────────────────────────────────────────────────────────────────


async def _validate_step(
    plan: TutorialPlan,
    step: TutorialStep,
    r: aioredis.Redis,
) -> dict[str, Any]:
    """Return {valid, reason, fix_hint}. Reads Redis directly for speed
    rather than re-calling our own HTTP endpoints."""
    brand_id = plan.brand_id
    lang = plan.language

    if step.type in ("info", "navigate", "celebrate"):
        return {"valid": True}

    if step.type == "enable_module":
        mid = step.module_id
        if not mid:
            return {"valid": False, "reason": "step missing module_id"}
        raw = await r.hget(_k_brand_modules(brand_id), mid)
        if not raw:
            return {
                "valid": False,
                "reason": f"module '{mid}' not enabled",
                "fix_hint": (
                    f"在 Engagement 页找到 {_module_name(mid, lang)} 并点击启用"
                    if lang == "cn" else
                    f"Open Engagement and toggle {_module_name(mid, lang)} on"
                ),
            }
        try:
            cfg = json.loads(raw)
        except json.JSONDecodeError:
            return {"valid": False, "reason": f"module '{mid}' config is corrupt"}
        if not cfg.get("enabled"):
            return {
                "valid": False,
                "reason": f"module '{mid}' exists but is disabled",
                "fix_hint": (
                    f"开启 {_module_name(mid, lang)} 的启用开关"
                    if lang == "cn" else
                    f"Toggle {_module_name(mid, lang)} on"
                ),
            }
        return {"valid": True}

    if step.type == "configure_module":
        mid = step.module_id
        if not mid:
            return {"valid": False, "reason": "step missing module_id"}
        raw = await r.hget(_k_brand_modules(brand_id), mid)
        if not raw:
            return {
                "valid": False,
                "reason": f"module '{mid}' not enabled yet",
                "fix_hint": "请先完成上一步：启用模块",
            }
        try:
            cfg = json.loads(raw)
        except json.JSONDecodeError:
            return {"valid": False, "reason": f"module '{mid}' config corrupt"}
        params = cfg.get("params") or {}
        suggested = step.suggested_params or {}
        missing = [k for k in suggested.keys() if k not in params]
        if missing:
            return {
                "valid": False,
                "reason": f"missing params for {mid}: {missing}",
                "fix_hint": (
                    f"配置以下字段：{', '.join(missing)}"
                    if lang == "cn" else
                    f"Please configure: {', '.join(missing)}"
                ),
            }
        return {"valid": True}

    if step.type == "create_voucher_template":
        tpl = step.voucher_template or {}
        wanted_id = tpl.get("id")
        existing = await r.smembers(_k_voucher_templates(brand_id))
        if wanted_id and wanted_id not in existing:
            return {
                "valid": False,
                "reason": f"voucher template '{wanted_id}' not created",
                "fix_hint": (
                    "请在 Vouchers 页创建对应的模板"
                    if lang == "cn" else
                    "Create the voucher template in the Vouchers tab"
                ),
            }
        if not wanted_id and not existing:
            return {
                "valid": False,
                "reason": "no voucher templates exist",
                "fix_hint": "请创建至少一个优惠券模板",
            }
        return {"valid": True}

    if step.type == "create_rule":
        tpl = step.rule_template or {}
        trigger = tpl.get("trigger_event")
        # Check both possible rule storage keys (rule_engine vs recipes legacy)
        rules_primary = await r.hgetall(_k_brand_rules_primary(brand_id))
        rules_legacy = await r.hgetall(_k_brand_rules_legacy(brand_id))
        rules_all: dict[str, str] = {**rules_legacy, **rules_primary}
        if not rules_all:
            return {
                "valid": False,
                "reason": "no rules configured",
                "fix_hint": (
                    "请在 Rules 页创建一条规则" if lang == "cn"
                    else "Create a rule in the Rules tab"
                ),
            }
        if trigger:
            for rid, payload in rules_all.items():
                try:
                    rule_obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if rule_obj.get("trigger_event") == trigger:
                    return {"valid": True}
            return {
                "valid": False,
                "reason": f"no rule with trigger_event='{trigger}'",
                "fix_hint": (
                    f"请创建一条 trigger_event = {trigger} 的规则"
                    if lang == "cn" else
                    f"Add a rule with trigger_event = {trigger}"
                ),
            }
        return {"valid": True}

    if step.type == "test_action":
        # Optional: consider valid if frontend already invoked the endpoint.
        # We just acknowledge — strict validation would require an event log.
        return {"valid": True}

    return {"valid": True}


# ─────────────────────────────────────────────────────────────────────────────
# API endpoints
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/from-recipe")
async def from_recipe(
    body: FromRecipeRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Generate a tutorial plan from a recipe in the platform catalog
    (or the brand's custom recipes)."""
    recipe = await _get_recipe(body.recipe_id, body.brand_id, r)
    if recipe is None:
        raise HTTPException(status_code=404, detail="recipe not found")

    tid = _new_tid()
    plan = _build_plan(
        recipe=recipe.model_dump(),
        brand_id=body.brand_id,
        language=body.language,
        tutorial_id=tid,
    )
    await _save_plan(plan, r)
    logger.info(
        "tutorial_created brand=%s recipe=%s tid=%s steps=%d lang=%s",
        body.brand_id, body.recipe_id, tid, plan.total_steps, body.language,
    )
    return {"tutorial_id": tid, "plan": plan.model_dump()}


@router.post("/from-recipe-json")
async def from_recipe_json(
    body: FromRecipeJsonRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Generate a tutorial from an inline recipe JSON (e.g. the output of
    /api/v1/recipe-gen). The recipe is NOT persisted to the catalog — the
    tutorial owns its own snapshot via the embedded plan."""
    if not isinstance(body.recipe, dict):
        raise HTTPException(status_code=400, detail="recipe must be an object")
    # Light validation — try to coerce through the Recipe model but tolerate
    # extras since /recipe-gen may emit additional fields.
    try:
        validated = Recipe.model_validate(body.recipe)
        recipe_dict = validated.model_dump()
        # Preserve extra keys (e.g. voucher_templates) the model dropped.
        for k, v in body.recipe.items():
            recipe_dict.setdefault(k, v)
    except Exception:
        # Fall back to raw dict if it doesn't fit the strict Recipe schema —
        # we still need an id.
        if not body.recipe.get("id"):
            raise HTTPException(
                status_code=400,
                detail="recipe must include an 'id' field",
            )
        recipe_dict = body.recipe

    tid = _new_tid()
    plan = _build_plan(
        recipe=recipe_dict,
        brand_id=body.brand_id,
        language=body.language,
        tutorial_id=tid,
    )
    await _save_plan(plan, r)
    logger.info(
        "tutorial_created_inline brand=%s recipe=%s tid=%s steps=%d",
        body.brand_id, recipe_dict.get("id"), tid, plan.total_steps,
    )
    return {"tutorial_id": tid, "plan": plan.model_dump()}


@router.get("/{tutorial_id}")
async def get_tutorial(
    tutorial_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    plan = await _load_plan(tutorial_id, r)
    if plan is None:
        raise HTTPException(status_code=404, detail="tutorial not found")
    current = (
        plan.steps[plan.current_step]
        if 0 <= plan.current_step < len(plan.steps) else None
    )
    return {
        **plan.model_dump(),
        "current_step_detail": current.model_dump() if current else None,
    }


@router.post("/{tutorial_id}/advance")
async def advance_tutorial(
    tutorial_id: str,
    body: AdvanceRequest = AdvanceRequest(),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    plan = await _load_plan(tutorial_id, r)
    if plan is None:
        raise HTTPException(status_code=404, detail="tutorial not found")
    if plan.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"tutorial status is '{plan.status}', cannot advance",
        )

    plan.current_step += 1
    if plan.current_step >= plan.total_steps:
        plan.current_step = plan.total_steps
        plan.status = "completed"
        await _save_plan(plan, r)
        logger.info("tutorial_completed tid=%s", tutorial_id)
        return {
            "ok": True,
            "completed": True,
            "tutorial_id": tutorial_id,
            "status": plan.status,
        }

    await _save_plan(plan, r)
    next_step = plan.steps[plan.current_step]
    return {
        "ok": True,
        "completed": False,
        "tutorial_id": tutorial_id,
        "current_step": plan.current_step,
        "step": next_step.model_dump(),
    }


@router.post("/{tutorial_id}/skip")
async def skip_step(
    tutorial_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Same as advance but explicitly bypasses validation."""
    plan = await _load_plan(tutorial_id, r)
    if plan is None:
        raise HTTPException(status_code=404, detail="tutorial not found")
    if plan.status != "active":
        raise HTTPException(status_code=409, detail=f"status={plan.status}")

    skipped_idx = plan.current_step
    plan.current_step += 1
    completed = plan.current_step >= plan.total_steps
    if completed:
        plan.current_step = plan.total_steps
        plan.status = "completed"

    await _save_plan(plan, r)
    return {
        "ok": True,
        "skipped_step_index": skipped_idx,
        "completed": completed,
        "tutorial_id": tutorial_id,
        "current_step": plan.current_step,
        "status": plan.status,
    }


@router.post("/{tutorial_id}/validate-current")
async def validate_current(
    tutorial_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    plan = await _load_plan(tutorial_id, r)
    if plan is None:
        raise HTTPException(status_code=404, detail="tutorial not found")
    if not (0 <= plan.current_step < len(plan.steps)):
        return {"valid": True, "reason": "no active step (tutorial finished)"}
    step = plan.steps[plan.current_step]
    result = await _validate_step(plan, step, r)
    result["step_id"] = step.id
    result["step_type"] = step.type
    return result


@router.post("/{tutorial_id}/abandon")
async def abandon_tutorial(
    tutorial_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    plan = await _load_plan(tutorial_id, r)
    if plan is None:
        raise HTTPException(status_code=404, detail="tutorial not found")
    plan.status = "abandoned"
    await _save_plan(plan, r)
    logger.info("tutorial_abandoned tid=%s at_step=%d", tutorial_id, plan.current_step)
    return {"ok": True, "tutorial_id": tutorial_id, "status": plan.status}


@router.get("/brand/{brand_id}")
async def list_brand_tutorials(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """List all tutorials for a brand, with summary info."""
    ids = await r.smembers(_k_brand_tutorials(brand_id))
    out: list[dict[str, Any]] = []
    for tid in sorted(ids):
        data = await r.hgetall(_k_tutorial(tid))
        if not data:
            continue
        try:
            current_step = int(data.get("current_step", "0"))
        except ValueError:
            current_step = 0
        # Pull title + total from plan blob (cheap — already a small string)
        title = None
        total_steps = 0
        recipe_id = data.get("recipe_id") or None
        plan_raw = data.get("plan")
        if plan_raw:
            try:
                plan_obj = json.loads(plan_raw)
                title = plan_obj.get("title_cn") or plan_obj.get("title")
                total_steps = plan_obj.get("total_steps", 0)
                recipe_id = plan_obj.get("recipe_id") or recipe_id
            except json.JSONDecodeError:
                pass
        out.append({
            "tutorial_id": tid,
            "brand_id": brand_id,
            "recipe_id": recipe_id,
            "title": title,
            "status": data.get("status", "active"),
            "current_step": current_step,
            "total_steps": total_steps,
            "started_at": data.get("started_at"),
            "updated_at": data.get("updated_at"),
        })
    out.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return {"brand_id": brand_id, "count": len(out), "tutorials": out}
