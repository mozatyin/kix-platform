"""Recipe Generator — NL → KiX Recipe via LLM.

Merchants describe in natural language what gamification they want.
This router uses an LLM (via ``eltm.llm.call_llm``) to map their intent
to a Recipe JSON — a composition of KiX modules + RuleEngine rules —
and offers to apply it.

Storage:
    Redis HASH at  brand:{bid}:generated_recipes
        field  = recipe_id (uuid4)
        value  = JSON {recipe, confidence, modules_used,
                       explanation_cn, explanation_en,
                       estimated_complexity, source_description,
                       created_at}

Endpoints:
    POST /from-description           NL → Recipe (preview, not applied)
    POST /refine                     iterate on a Recipe via free-text feedback
    POST /explain                    Recipe → plain English/Chinese
    POST /apply-from-description     One-shot: generate + apply

LLM:
    Uses ``eltm.llm.call_llm`` (same pattern as kix_channel). If the
    LLM is unreachable or unconfigured we fall back to a deterministic
    heuristic mapper so the endpoint is still useful in dev.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis

# ── ELTM LLM bridge ────────────────────────────────────────────────────────
sys.path.insert(0, "/Users/mozat/eltm")
try:
    from eltm.llm import call_llm as _eltm_call_llm  # type: ignore
except Exception as _imp_err:  # noqa: BLE001
    _eltm_call_llm = None
    _ELTM_IMPORT_ERROR: Exception | None = _imp_err
else:
    _ELTM_IMPORT_ERROR = None

logger = logging.getLogger(__name__)

router = APIRouter()

# Wave C: TriSoul-biased template picker (flag-gated, 30% selection
# probability so merchants still see legacy library matches most of the
# time). Identity no-op when the integration module is missing.
try:  # pragma: no cover — best-effort
    from app.routers.trisoul_integration import (  # type: ignore
        maybe_pick_recipe_by_affinity as _trisoul_pick_recipe,
    )
except ImportError:  # pragma: no cover
    async def _trisoul_pick_recipe(  # type: ignore[misc]
        uid: str | None,
        candidate_recipes: list[dict[str, Any]],
        r: aioredis.Redis,
        *,
        selection_probability: float = 0.30,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        return None, {"trisoul": "unavailable"}


# ── Module Catalog ─────────────────────────────────────────────────────────

MODULE_CATALOG: dict[str, dict[str, str]] = {
    # Foundation
    "xp": {"name": "XP/Experience", "description": "Award points for actions"},
    "level": {"name": "Levels", "description": "User levels derived from XP"},
    "badge": {"name": "Badges", "description": "Achievement badges"},
    "streak": {"name": "Streak", "description": "Daily consecutive activity"},
    "energy": {"name": "Energy", "description": "Consumable currency"},
    # Primitives
    "currency": {"name": "Custom Currency", "description": "Stars/coins/gems"},
    "item": {"name": "Item Inventory", "description": "Collectible items"},
    "achievement": {"name": "Achievement Tracker", "description": "Multi-step goals"},
    "quest": {"name": "Multi-step Quest", "description": "Branching missions"},
    "tier": {"name": "Loyalty Tiers", "description": "Bronze/Silver/Gold etc"},
    "event": {"name": "Time-Windowed Event", "description": "Limited-time campaigns"},
    # Network Effect
    "share_to_win": {"name": "Share to Win", "description": "User shares score, friend joins, both rewarded"},
    "energy_invite": {"name": "Energy Invite", "description": "Invite friends to refill energy"},
    "friend_challenge": {"name": "Friend Challenge", "description": "1v1 challenges"},
    "ladder_climb": {"name": "Ladder Climb", "description": "Invite N friends to climb tier"},
    "streak_rescue": {"name": "Streak Rescue", "description": "Friend rescues your streak"},
    "auto_share": {"name": "Auto Share", "description": "Auto-generate share cards on milestones"},
    # Commerce
    "score_to_coupon": {"name": "Score → Coupon", "description": "Score thresholds unlock tiered coupons"},
    "energy_to_purchase": {"name": "Energy Buy", "description": "Buy energy with real money"},
    "reward_chain": {"name": "Reward Chain", "description": "Expiring vouchers"},
    "upsell_moment": {"name": "Upsell Moment", "description": "Suggest upgrade at checkout"},
    "redemption_store": {"name": "Redemption Store", "description": "Spend points on real items"},
    # Top Modules
    "reward_roulette": {"name": "Reward Roulette", "description": "Spin wheel for prizes (BARQ-style)"},
    "league": {"name": "Weekly League", "description": "Duolingo-style cohort competition"},
    "tier_starbucks": {"name": "Starbucks-style Tier", "description": "Lifetime XP loyalty tiers"},
    "battle_pass": {"name": "Battle Pass", "description": "Fortnite-style seasonal pass"},
    "smart_quests": {"name": "Adaptive Quests", "description": "AI-adjusted difficulty"},
    "story_quest": {"name": "Story Quest", "description": "Narrative chapters"},
    "life_system": {"name": "Life System", "description": "Heart/lives with regen"},
    "tourney": {"name": "Tournament", "description": "Limited-time competition"},
    "collection": {"name": "Collection", "description": "Pokemon-style gacha"},
    "badge_wall": {"name": "Badge Wall", "description": "Visual achievement display"},
    # Groups
    "group_buy": {"name": "Group Buy", "description": "Pinduoduo-style N-person discount"},
    "group_atomic": {"name": "Group Atomic", "description": "N users must all complete in window"},
    "price_cut": {"name": "Price Cut", "description": "Friends help reduce price"},
    # Voucher Builder
    "voucher_template": {"name": "Conditional Voucher", "description": "Vouchers with min_purchase/tier/date conditions"},
    # Social
    "social_graph": {"name": "Friends/Following", "description": "Friend connections"},
    "social_feed": {"name": "Activity Feed", "description": "Friend activity stream"},
    "kudos": {"name": "Kudos/Likes", "description": "Strava-style appreciation"},
    # Triggers
    "user_attribute": {"name": "Attribute Trigger", "description": "Birthday/anniversary triggers"},
    "rate_limit": {"name": "Rate Limit", "description": "1 per day limits"},
    "limited_drop": {"name": "Limited Drop", "description": "Scarcity-based items"},
    "perk_activation": {"name": "Perk Activation", "description": "Tier-locked features"},
    "fcfs": {"name": "First Come First Serve", "description": "Race to claim rewards"},
    # P2P
    "gift_sending": {"name": "Gift", "description": "Send items/currency to friends"},
    "trading_post": {"name": "Trade", "description": "Bilateral asset trades"},
    # Multiplayer
    "coop_quest": {"name": "Cooperative Quest", "description": "Shared multi-user goals"},
    "group_raid": {"name": "Raid", "description": "Team boss fight"},
    "squad_multiplier": {"name": "Squad Bonus", "description": "Bonus when friends active together"},
    "territory": {"name": "Territory", "description": "Pokemon Go gym-style claim"},
    # Rule Engine
    "rule": {"name": "Rule Engine", "description": "When-Then logic that ties modules together"},
}

VALID_MODULE_IDS: set[str] = set(MODULE_CATALOG.keys())

# Rule-engine vocabulary (kept in sync with rule_engine.py).
COMPARE_OPS: set[str] = {">=", ">", "<=", "<", "==", "!="}
COMPOSITION_OPS: set[str] = {"AND", "OR", "NOT", "THRESHOLD"}

# Lightweight library of "known recipe templates" used for confidence scoring.
# Each entry is a frozenset of module IDs that together implement a well-known
# pattern. If the LLM's module set matches one of these exactly we boost
# confidence to 1.0.
KNOWN_RECIPE_TEMPLATES: list[dict[str, Any]] = [
    {
        "name": "invite_for_voucher",
        "modules": frozenset({"share_to_win", "voucher_template", "rule"}),
    },
    {
        "name": "daily_streak_to_coupon",
        "modules": frozenset({"streak", "score_to_coupon", "rule"}),
    },
    {
        "name": "viral_growth_starter",
        "modules": frozenset({"share_to_win", "energy_invite", "xp", "rule"}),
    },
    {
        "name": "loyalty_tier",
        "modules": frozenset({"tier_starbucks", "voucher_template", "xp", "rule"}),
    },
    {
        "name": "seasonal_pass",
        "modules": frozenset({"battle_pass", "quest", "xp", "rule"}),
    },
    {
        "name": "pdd_group_buy",
        "modules": frozenset({"group_buy", "voucher_template", "rule"}),
    },
]


# ── Pydantic models ────────────────────────────────────────────────────────


Style = Literal["viral", "loyalty", "premium", "casual"]
# Expanded industry taxonomy — covers 老李 (community/book_club), 老黄
# (baby_products/ecommerce), luxury, healthcare, automotive, real_estate,
# fintech and more. Anything not on this list falls back to "other"; existing
# recipes stored with industry="other" remain valid.
Industry = Literal[
    # Food & Beverage
    "coffee", "bubble_tea", "food", "restaurant", "luxury_dining", "qsr",
    # Retail
    "retail", "ecommerce", "luxury_retail", "fashion",
    # Health & Wellness
    "fitness", "beauty", "wellness", "healthcare",
    # Family
    "baby_products", "kids_education", "parenting",
    # Community
    "community", "book_club", "education", "co_working", "religious",
    # Hospitality
    "hotel", "travel", "airline",
    # Entertainment
    "gaming", "music", "events", "cinema",
    # Services
    "automotive", "real_estate", "financial_services", "telecom",
    # Catch-all
    "other",
]
Complexity = Literal["easy", "medium", "complex"]


class FromDescriptionRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    description: str = Field(..., min_length=3, max_length=4000)
    style: Style | None = None
    industry: Industry | None = None
    # Wave C: optional user_id enables TriSoul-biased template selection
    # when the per-user flag is on. Backwards-compatible: when omitted,
    # the legacy library-first → LLM fallback path is unchanged.
    user_id: str | None = Field(default=None, min_length=1)


class RefineRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    previous_recipe: dict[str, Any]
    feedback: str = Field(..., min_length=1, max_length=2000)


class ExplainRequest(BaseModel):
    recipe_id: str | None = None
    recipe: dict[str, Any] | None = None
    brand_id: str | None = None


class ApplyFromDescriptionRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)
    description: str = Field(..., min_length=3, max_length=4000)
    style: Style | None = None
    industry: Industry | None = None


class RecipeResponse(BaseModel):
    recipe_id: str
    recipe: dict[str, Any]
    confidence: float
    modules_used: list[str]
    explanation_cn: str
    explanation_en: str
    estimated_complexity: Complexity
    warnings: list[str] = []


# ── Helpers ────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(brand_id: str) -> str:
    return f"brand:{brand_id}:generated_recipes"


def _module_catalog_for_prompt() -> str:
    """Render the module catalog as compact JSON for the LLM prompt."""
    return json.dumps(MODULE_CATALOG, ensure_ascii=False, indent=2)


def _rule_schema_for_prompt() -> str:
    """Describe the RuleEngine schema for the LLM."""
    return json.dumps(
        {
            "trigger_event": "string (e.g. 'invite.redeemed', 'game.completed', 'order.paid')",
            "conditions": {
                "_doc": "Recursive boolean tree. Composition op or leaf.",
                "composition_ops": sorted(COMPOSITION_OPS),
                "compare_ops": sorted(COMPARE_OPS),
                "leaf_example": {
                    "type": "count",
                    "metric": "invites_redeemed",
                    "op": ">=",
                    "value": 10,
                },
                "tree_example": {
                    "op": "AND",
                    "children": [
                        {"type": "count", "metric": "invites_redeemed", "op": ">=", "value": 10},
                        {"type": "tier", "op": ">=", "value": "silver"},
                    ],
                },
            },
            "actions": [
                {"type": "voucher.grant", "params": {"template_id": "vou_free_coffee"}},
                {"type": "progression.award_xp", "params": {"amount": 500}},
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def _example_recipes_for_prompt() -> str:
    return json.dumps(
        [
            {
                "name": "Invite 10 friends → free coffee voucher",
                "description_cn": "邀请10位好友，解锁免费咖啡券",
                "modules": [
                    {"id": "share_to_win", "params": {"reward_xp": 50}},
                    {
                        "id": "voucher_template",
                        "params": {
                            "template_id": "vou_free_coffee",
                            "value": {"type": "free_item", "amount": 1, "currency": "USD"},
                            "expires_in_days": 30,
                        },
                    },
                    {"id": "rule", "params": {}},
                ],
                "rules": [
                    {
                        "trigger_event": "invite.redeemed",
                        "conditions": {
                            "type": "count",
                            "metric": "invites_redeemed",
                            "op": ">=",
                            "value": 10,
                        },
                        "actions": [
                            {
                                "type": "voucher.grant",
                                "params": {"template_id": "vou_free_coffee"},
                            }
                        ],
                    }
                ],
            }
        ],
        ensure_ascii=False,
        indent=2,
    )


def _build_prompt(
    description: str,
    industry: str | None,
    style: str | None,
    *,
    prior_recipe: dict[str, Any] | None = None,
    feedback: str | None = None,
) -> str:
    """Compose the full LLM prompt."""
    sections: list[str] = []
    sections.append(
        "You are a gamification expert. A merchant describes what they want "
        "to build. Map their description to a Recipe JSON combining KiX "
        "platform modules.\n"
    )
    sections.append("Available modules (use ONLY these IDs):\n")
    sections.append(_module_catalog_for_prompt())
    sections.append("\nRuleEngine schema:\n")
    sections.append(_rule_schema_for_prompt())
    sections.append("\nExamples of well-formed recipes:\n")
    sections.append(_example_recipes_for_prompt())
    sections.append("\nRecipe JSON schema:\n")
    sections.append(
        json.dumps(
            {
                "name": "str",
                "description_cn": "str",
                "modules": [{"id": "module_id_here", "params": {}}],
                "rules": [{"trigger_event": "str", "conditions": {}, "actions": []}],
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    if prior_recipe is not None:
        sections.append("\nPrevious recipe to refine:\n")
        sections.append(json.dumps(prior_recipe, ensure_ascii=False, indent=2))
    if feedback:
        sections.append(f'\nMerchant feedback: "{feedback}"\n')

    sections.append(f'\nThe merchant wants: "{description}"\n')
    sections.append(f"Industry: {industry or 'unspecified'}\n")
    sections.append(f"Style: {style or 'unspecified'}\n")

    sections.append(
        "\nGenerate a Recipe JSON. Be conservative — only include modules "
        "CLEARLY needed. Each module's params should be reasonable defaults. "
        "Rules should connect modules logically.\n\n"
        "After the JSON, explain in 1-2 sentences (Chinese) what you built "
        "and why, then 1-2 sentences (English).\n\n"
        "Output format EXACTLY:\n"
        "```json\n{recipe_here}\n```\n\n"
        "EXPLANATION_CN:\n<Chinese explanation>\n\n"
        "EXPLANATION_EN:\n<English explanation>\n"
    )
    return "".join(sections)


def _extract_recipe_and_explanations(
    text: str,
) -> tuple[dict[str, Any] | None, str, str]:
    """Parse the LLM output into (recipe_json, explanation_cn, explanation_en)."""
    recipe: dict[str, Any] | None = None
    # 1) fenced ```json … ``` block
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            recipe = json.loads(m.group(1))
        except json.JSONDecodeError:
            recipe = None
    # 2) fallback: first balanced {...}
    if recipe is None:
        m2 = re.search(r"(\{.*\})", text, re.DOTALL)
        if m2:
            try:
                recipe = json.loads(m2.group(1))
            except json.JSONDecodeError:
                recipe = None

    cn = ""
    en = ""
    m_cn = re.search(r"EXPLANATION_CN:\s*(.+?)(?:\n\s*EXPLANATION_EN:|\Z)", text, re.DOTALL)
    if m_cn:
        cn = m_cn.group(1).strip()
    m_en = re.search(r"EXPLANATION_EN:\s*(.+?)\Z", text, re.DOTALL)
    if m_en:
        en = m_en.group(1).strip()
    return recipe, cn, en


def _heuristic_recipe(description: str, industry: str | None, style: str | None) -> dict[str, Any]:
    """Deterministic fallback when LLM is unavailable.

    Keyword-driven, intentionally conservative. Produces something
    plausible so the endpoint never hard-fails in dev environments.
    """
    desc = description.lower()
    modules: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []
    chosen: set[str] = set()

    def add(mid: str, params: dict[str, Any] | None = None) -> None:
        if mid in chosen or mid not in VALID_MODULE_IDS:
            return
        chosen.add(mid)
        modules.append({"id": mid, "params": params or {}})

    if any(k in desc for k in ("invite", "share", "refer", "邀请", "分享")):
        add("share_to_win", {"reward_xp": 50})
    if any(k in desc for k in ("voucher", "coupon", "券", "discount")):
        add("voucher_template", {
            "template_id": "vou_default",
            "value": {"type": "percent", "amount": 10, "currency": "USD"},
            "expires_in_days": 30,
        })
    if any(k in desc for k in ("streak", "daily", "每日", "签到")):
        add("streak")
    if any(k in desc for k in ("tier", "level", "loyalty", "vip", "等级")):
        add("tier_starbucks")
    if any(k in desc for k in ("league", "tournament", "compete", "比赛", "排行")):
        add("league")
    if any(k in desc for k in ("group", "team", "拼团")):
        add("group_buy")
    if any(k in desc for k in ("wheel", "roulette", "spin", "转盘")):
        add("reward_roulette")
    if any(k in desc for k in ("pass", "season", "battle pass")):
        add("battle_pass")
    if any(k in desc for k in ("quest", "mission", "任务")):
        add("quest")
    if not chosen:
        # absolute fallback: XP + streak
        add("xp")
        add("streak")
    # Always include xp + rule engine glue
    add("xp")
    add("rule")

    # Simple rule when share + voucher chosen
    if "share_to_win" in chosen and "voucher_template" in chosen:
        rules.append(
            {
                "trigger_event": "invite.redeemed",
                "conditions": {
                    "type": "count",
                    "metric": "invites_redeemed",
                    "op": ">=",
                    "value": 10,
                },
                "actions": [
                    {
                        "type": "voucher.grant",
                        "params": {"template_id": "vou_default"},
                    },
                    {"type": "progression.award_xp", "params": {"amount": 500}},
                ],
            }
        )

    return {
        "name": (description[:60] + "…") if len(description) > 60 else description,
        "description_cn": description,
        "modules": modules,
        "rules": rules,
    }


def _validate_and_repair(recipe: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Validate Recipe shape; drop / repair offending pieces in place.

    Returns (cleaned_recipe, warnings). Never raises — caller can decide
    whether warnings should escalate.
    """
    warnings: list[str] = []
    if not isinstance(recipe, dict):
        return ({"name": "invalid", "modules": [], "rules": []}, ["recipe was not an object"])

    recipe.setdefault("name", "Untitled recipe")
    recipe.setdefault("description_cn", "")
    raw_modules = recipe.get("modules") or []
    raw_rules = recipe.get("rules") or []

    # Modules
    cleaned_modules: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for m in raw_modules:
        if not isinstance(m, dict):
            warnings.append("dropped a non-object module entry")
            continue
        mid = m.get("id")
        if mid not in VALID_MODULE_IDS:
            warnings.append(f"dropped unknown module id: {mid!r}")
            continue
        if mid in seen_ids:
            warnings.append(f"dropped duplicate module id: {mid!r}")
            continue
        seen_ids.add(mid)
        params = m.get("params")
        if not isinstance(params, dict):
            params = {}
        cleaned_modules.append({"id": mid, "params": params})
    recipe["modules"] = cleaned_modules

    # Rules
    cleaned_rules: list[dict[str, Any]] = []
    for r_ in raw_rules:
        if not isinstance(r_, dict):
            warnings.append("dropped a non-object rule entry")
            continue
        if not r_.get("trigger_event"):
            warnings.append("dropped rule without trigger_event")
            continue
        cond = r_.get("conditions")
        if cond is not None and not _conditions_ok(cond, warnings):
            warnings.append(f"rule {r_.get('trigger_event')}: conditions used unknown ops; cleared")
            r_["conditions"] = {}
        actions = r_.get("actions")
        if not isinstance(actions, list):
            warnings.append(f"rule {r_.get('trigger_event')}: actions normalized to list")
            r_["actions"] = []
        cleaned_rules.append(r_)
    recipe["rules"] = cleaned_rules

    # If rules reference modules, ensure rule_engine module is in modules list
    if cleaned_rules and "rule" not in seen_ids:
        cleaned_modules.append({"id": "rule", "params": {}})
        seen_ids.add("rule")

    # Circular-dependency check (best-effort): we don't have an explicit
    # dependency graph, but we can detect modules whose params reference
    # themselves.
    for m in cleaned_modules:
        if m["id"] in json.dumps(m.get("params") or {}):
            # benign self-reference is allowed; not flagged
            pass

    return recipe, warnings


def _conditions_ok(node: Any, warnings: list[str]) -> bool:
    """Best-effort recursive validator for the conditions tree."""
    if not isinstance(node, dict):
        return False
    if "op" in node and node["op"] in COMPOSITION_OPS:
        children = node.get("children") or []
        if not isinstance(children, list):
            return False
        return all(_conditions_ok(c, warnings) for c in children)
    if "op" in node and node["op"] in COMPARE_OPS:
        return True
    # leaf without op is acceptable — let downstream rule engine decide
    return True


def _confidence(recipe: dict[str, Any]) -> float:
    module_ids = frozenset(m["id"] for m in recipe.get("modules", []))
    if not module_ids:
        return 0.3
    for tpl in KNOWN_RECIPE_TEMPLATES:
        if module_ids == tpl["modules"]:
            return 1.0
    # 80%+ overlap with a known template
    for tpl in KNOWN_RECIPE_TEMPLATES:
        inter = module_ids & tpl["modules"]
        union = module_ids | tpl["modules"]
        if union and len(inter) / len(union) >= 0.8:
            return 0.85
    # novel but all well-known modules
    if module_ids <= VALID_MODULE_IDS:
        return 0.7
    return 0.5


def _complexity(recipe: dict[str, Any]) -> Complexity:
    n_mod = len(recipe.get("modules", []))
    n_rule = len(recipe.get("rules", []))
    if n_mod <= 3 and n_rule <= 1:
        return "easy"
    if n_mod <= 6 and n_rule <= 3:
        return "medium"
    return "complex"


def _user_flow_from_recipe(recipe: dict[str, Any]) -> list[str]:
    """Naive narrative generator: walk modules + rules into a step list."""
    steps: list[str] = []
    mods = [m["id"] for m in recipe.get("modules", [])]
    if "share_to_win" in mods:
        steps.append("User shares their score / invite link with friends.")
    if "energy_invite" in mods:
        steps.append("Friend taps the link and signs up.")
    if "streak" in mods:
        steps.append("User checks in daily to extend their streak.")
    if "tier" in mods or "tier_starbucks" in mods:
        steps.append("Cumulative XP pushes the user into the next loyalty tier.")
    if "battle_pass" in mods:
        steps.append("User progresses through the seasonal battle pass.")
    if "league" in mods:
        steps.append("User competes against a weekly cohort.")
    if "reward_roulette" in mods:
        steps.append("User spins the wheel and wins a prize.")
    if "voucher_template" in mods or "score_to_coupon" in mods:
        steps.append("System grants a voucher / coupon to the user.")
    if "group_buy" in mods:
        steps.append("User invites N friends to unlock a group discount.")
    for r_ in recipe.get("rules", []):
        ev = r_.get("trigger_event", "event")
        steps.append(f"When `{ev}` fires, the configured actions are executed.")
    if not steps:
        steps.append("System awards XP for the configured user actions.")
    return steps


def _llm_or_fallback(prompt: str) -> tuple[str, str]:
    """Call ELTM LLM if configured; otherwise return ('', 'fallback')."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or _eltm_call_llm is None:
        logger.info(
            "recipe_generator: LLM unavailable (api_key=%s, import=%s) — using heuristic",
            bool(api_key),
            "ok" if _eltm_call_llm else f"fail:{_ELTM_IMPORT_ERROR!r}",
        )
        return "", "fallback"
    try:
        text = _eltm_call_llm(
            api_key,
            prompt,
            system=(
                "You are a precise gamification systems designer. "
                "You only emit recipes using the supplied module catalog and "
                "RuleEngine schema."
            ),
        )
        return text, "llm"
    except Exception as e:  # noqa: BLE001
        logger.warning("recipe_generator: LLM call failed: %s — heuristic fallback", e)
        return "", "fallback"


async def _generate(
    description: str,
    industry: str | None,
    style: str | None,
    *,
    prior_recipe: dict[str, Any] | None = None,
    feedback: str | None = None,
) -> tuple[dict[str, Any], str, str, list[str]]:
    """Run prompt+LLM (or fallback) and validate. Retries the LLM once
    if validation produces warnings on the first attempt."""
    prompt = _build_prompt(
        description, industry, style, prior_recipe=prior_recipe, feedback=feedback
    )
    text, source = _llm_or_fallback(prompt)

    recipe: dict[str, Any] | None = None
    cn = ""
    en = ""
    if source == "llm":
        recipe, cn, en = _extract_recipe_and_explanations(text)

    if recipe is None:
        recipe = _heuristic_recipe(description, industry, style)
        # i18n: ``recipe_generator-heuristic-fallback`` (en-SG / zh-Hans-SG)
        from app.i18n import t as _t
        if not cn:
            cn = _t("recipe_generator-heuristic-fallback", locale="zh-Hans-SG")
        if not en:
            en = _t("recipe_generator-heuristic-fallback", locale="en-SG")

    recipe, warnings = _validate_and_repair(recipe)

    # If LLM produced warnings, retry once.
    if source == "llm" and warnings:
        retry_prompt = (
            prompt
            + "\n\nThe previous attempt produced these issues — please fix:\n- "
            + "\n- ".join(warnings)
        )
        text2, _ = _llm_or_fallback(retry_prompt)
        if text2:
            r2, cn2, en2 = _extract_recipe_and_explanations(text2)
            if r2:
                r2, warnings2 = _validate_and_repair(r2)
                if len(warnings2) < len(warnings):
                    recipe, warnings = r2, warnings2
                    cn, en = cn2 or cn, en2 or en
    return recipe, cn, en, warnings


async def _store_generated(
    r: aioredis.Redis,
    brand_id: str,
    payload: dict[str, Any],
) -> str:
    recipe_id = f"rcp_{uuid4().hex[:12]}"
    payload = dict(payload, recipe_id=recipe_id, created_at=_now_iso())
    await r.hset(_key(brand_id), recipe_id, json.dumps(payload, ensure_ascii=False))
    return recipe_id


async def _load_generated(
    r: aioredis.Redis, brand_id: str, recipe_id: str
) -> dict[str, Any] | None:
    raw = await r.hget(_key(brand_id), recipe_id)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


# ── Endpoints ──────────────────────────────────────────────────────────────


# ── Library matcher (prefer seeded recipes over LLM generation) ───────────


# Industry-keyword index used to map free-text descriptions back to an
# industry tag when the merchant didn't pass one explicitly. Conservative —
# we only fire on unambiguous phrases.
_INDUSTRY_KEYWORDS: dict[str, list[str]] = {
    "book_club": ["book club", "读书会", "读书俱乐部", "book group", "reading club"],
    "education": ["course", "lesson", "tutor", "学习", "课程", "education", "quiz",
                  "知识", "trivia"],
    "fitness": ["gym", "class", "workout", "健身", "fitness", "yoga", "运动"],
    "kids_education": ["kids", "child", "parent", "family", "亲子", "儿童", "孩子"],
    "wellness": ["wellness", "health", "meditation", "sleep", "habit", "健康",
                 "冥想", "睡眠"],
    "events": ["event", "meetup", "rsvp", "活动", "社群活动"],
    "community": ["subscription", "renewal", "membership", "订阅", "会员",
                  "community", "社区"],
}


def _infer_industry(description: str) -> str | None:
    """Best-effort industry inference from a free-text description."""
    d = description.lower()
    for ind, kws in _INDUSTRY_KEYWORDS.items():
        if any(k in d for k in kws):
            return ind
    return None


async def find_matching_recipes(
    industry: str | None,
    intent: str,
    r: aioredis.Redis,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Search seeded recipes that match industry + intent keywords.

    Returns a ranked list of {recipe, score, reason}. Scoring is simple
    additive:
      +5  industry exact match
      +1  per matched keyword (description / tag / name)
    A merchant UI can show the top hits and let the merchant either pick
    one or fall through to LLM generation.
    """
    raw = await r.hgetall("recipes:catalog")
    candidates: list[dict[str, Any]] = []
    intent_lc = (intent or "").lower()
    # Light keyword set built from the intent — at least 3 chars, deduped.
    tokens = {
        t.strip(".,!?;:'\"")
        for t in re.split(r"\s+", intent_lc)
        if len(t.strip(".,!?;:'\"")) >= 3
    }

    for _rid, payload in raw.items():
        try:
            rec = json.loads(payload)
        except json.JSONDecodeError:
            continue
        score = 0
        reasons: list[str] = []

        rec_industry = rec.get("industry")
        if industry and rec_industry == industry:
            score += 5
            reasons.append(f"industry={industry}")

        # haystack = name + descriptions + tags
        hay_parts = [
            rec.get("name", ""), rec.get("name_cn", ""),
            rec.get("description_en", ""), rec.get("description_cn", ""),
        ]
        hay_parts.extend(rec.get("tags") or [])
        haystack = " ".join(hay_parts).lower()
        hit_kws = [t for t in tokens if t and t in haystack]
        if hit_kws:
            score += len(hit_kws)
            reasons.append(f"keywords={hit_kws[:5]}")

        if score > 0:
            candidates.append({
                "recipe_id": rec.get("id"),
                "name": rec.get("name"),
                "industry": rec_industry,
                "score": score,
                "reasons": reasons,
                "recipe": rec,
            })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:limit]


class MatchLibraryRequest(BaseModel):
    description: str = Field(..., min_length=3, max_length=4000)
    industry: Industry | None = None
    limit: int = 5


@router.post("/match-library")
async def match_library(
    body: MatchLibraryRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Search the seeded recipe catalog for matches against an NL intent.

    Returns ranked hits with score + reasons. Useful BEFORE calling
    /from-description so the merchant UI can suggest a 1-click apply
    instead of forcing LLM generation. Falls back to inferring industry
    from the description when one is not supplied.
    """
    industry = body.industry or _infer_industry(body.description)
    hits = await find_matching_recipes(industry, body.description, r, limit=body.limit)
    return {
        "industry_used": industry,
        "industry_inferred": body.industry is None,
        "count": len(hits),
        "matches": hits,
    }


@router.post("/from-description", response_model=RecipeResponse)
async def from_description(
    body: FromDescriptionRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> RecipeResponse:
    """Map a natural-language description to a Recipe (preview, not applied).

    Library-first strategy: if a seeded recipe scores >= 6 (industry match
    + at least 1 keyword hit), we return it as the candidate instead of
    generating from scratch. The merchant can still call /refine to tweak.
    """
    # 1) Library hit?
    industry_for_match = body.industry or _infer_industry(body.description)
    if industry_for_match:
        # Wave C: when a user_id is supplied and TriSoul is enabled, fetch
        # up to 5 library candidates so the TriSoul picker can re-rank
        # them by user affinity. Otherwise keep legacy limit=1 behavior.
        _hit_limit = 5 if body.user_id else 1
        hits = await find_matching_recipes(
            industry_for_match, body.description, r, limit=_hit_limit
        )
        # TriSoul-biased selection (30% of users when flag on, identity
        # otherwise). Returns None when the legacy path should win.
        if body.user_id and hits:
            cand_recipes = [h["recipe"] for h in hits if h.get("score", 0) >= 6]
            picked, _ts_meta = await _trisoul_pick_recipe(
                body.user_id, cand_recipes, r,
            )
            if picked is not None:
                # Re-order hits so the TriSoul pick is first.
                picked_id = picked.get("id")
                hits.sort(
                    key=lambda h: (0 if h["recipe"].get("id") == picked_id else 1,
                                   -h.get("score", 0)),
                )
        if hits and hits[0]["score"] >= 6:
            chosen = hits[0]["recipe"]
            modules_used = [m["id"] for m in chosen.get("modules", [])]
            # i18n keys: ``recipe_generator-match-found`` + ``-match-score``
            from app.i18n import t as _t
            reasons_text = ", ".join(hits[0]["reasons"])
            cn_name = chosen.get("name_cn") or chosen.get("name")
            cn = (
                _t("recipe_generator-match-found", locale="zh-Hans-SG", recipe_name=cn_name)
                + _t("recipe_generator-match-score", locale="zh-Hans-SG", score=hits[0]["score"], reasons=reasons_text)
            )
            en = (
                _t("recipe_generator-match-found", locale="en-SG", recipe_name=chosen.get("name"))
                + " "
                + _t("recipe_generator-match-score", locale="en-SG", score=hits[0]["score"], reasons=reasons_text)
            )
            payload = {
                "recipe": chosen,
                "confidence": 0.95,
                "modules_used": modules_used,
                "explanation_cn": cn,
                "explanation_en": en,
                "estimated_complexity": _complexity(chosen),
                "warnings": [],
                "source_description": body.description,
                "industry": industry_for_match,
                "style": body.style,
                "matched_from_library": True,
                "library_match_score": hits[0]["score"],
            }
            recipe_id = await _store_generated(r, body.brand_id, payload)
            return RecipeResponse(
                recipe_id=recipe_id,
                recipe=chosen,
                confidence=0.95,
                modules_used=modules_used,
                explanation_cn=cn,
                explanation_en=en,
                estimated_complexity=_complexity(chosen),
                warnings=[],
            )

    # 2) No strong library hit → LLM (or heuristic fallback)
    recipe, cn, en, warnings = await _generate(
        body.description, body.industry, body.style
    )
    conf = _confidence(recipe)
    cplx = _complexity(recipe)
    modules_used = [m["id"] for m in recipe.get("modules", [])]

    payload = {
        "recipe": recipe,
        "confidence": conf,
        "modules_used": modules_used,
        "explanation_cn": cn,
        "explanation_en": en,
        "estimated_complexity": cplx,
        "warnings": warnings,
        "source_description": body.description,
        "industry": body.industry,
        "style": body.style,
    }
    recipe_id = await _store_generated(r, body.brand_id, payload)
    return RecipeResponse(
        recipe_id=recipe_id,
        recipe=recipe,
        confidence=conf,
        modules_used=modules_used,
        explanation_cn=cn,
        explanation_en=en,
        estimated_complexity=cplx,
        warnings=warnings,
    )


@router.post("/refine", response_model=RecipeResponse)
async def refine(
    body: RefineRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> RecipeResponse:
    """Iterate on a Recipe via free-text feedback."""
    desc = body.previous_recipe.get("description_cn") or body.previous_recipe.get("name", "")
    recipe, cn, en, warnings = await _generate(
        desc, None, None, prior_recipe=body.previous_recipe, feedback=body.feedback
    )
    conf = _confidence(recipe)
    cplx = _complexity(recipe)
    modules_used = [m["id"] for m in recipe.get("modules", [])]

    payload = {
        "recipe": recipe,
        "confidence": conf,
        "modules_used": modules_used,
        "explanation_cn": cn,
        "explanation_en": en,
        "estimated_complexity": cplx,
        "warnings": warnings,
        "refined_from": body.previous_recipe,
        "feedback": body.feedback,
    }
    recipe_id = await _store_generated(r, body.brand_id, payload)
    return RecipeResponse(
        recipe_id=recipe_id,
        recipe=recipe,
        confidence=conf,
        modules_used=modules_used,
        explanation_cn=cn,
        explanation_en=en,
        estimated_complexity=cplx,
        warnings=warnings,
    )


@router.post("/explain")
async def explain(
    body: ExplainRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Render a Recipe into plain English / Chinese + a step-by-step user flow."""
    recipe: dict[str, Any] | None = body.recipe
    if recipe is None and body.recipe_id and body.brand_id:
        stored = await _load_generated(r, body.brand_id, body.recipe_id)
        if stored is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="recipe_id not found"
            )
        recipe = stored.get("recipe")
    if recipe is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide either 'recipe' or ('recipe_id' + 'brand_id').",
        )

    recipe, _warnings = _validate_and_repair(recipe)
    mods = [m["id"] for m in recipe.get("modules", [])]
    pretty_mods = [MODULE_CATALOG.get(m, {}).get("name", m) for m in mods]
    n_rules = len(recipe.get("rules", []))

    # i18n key: ``recipe_generator-summary-recipe-includes``
    from app.i18n import t as _t
    module_list_en = ", ".join(pretty_mods) or _t(
        "recipe_generator-summary-empty-modules", locale="en-SG")
    module_list_cn = ", ".join(pretty_mods) or _t(
        "recipe_generator-summary-empty-modules", locale="zh-Hans-SG")
    untitled_en = _t("recipe_generator-summary-untitled", locale="en-SG")
    untitled_cn = _t("recipe_generator-summary-untitled", locale="zh-Hans-SG")
    en = _t(
        "recipe_generator-summary-recipe-includes",
        locale="en-SG",
        recipe_name=recipe.get("name") or untitled_en,
        module_count=len(mods),
        module_list=module_list_en,
        rule_count=n_rules,
    )
    cn = _t(
        "recipe_generator-summary-recipe-includes",
        locale="zh-Hans-SG",
        recipe_name=recipe.get("name") or untitled_cn,
        module_count=len(mods),
        module_list=module_list_cn,
        rule_count=n_rules,
    )
    flow = _user_flow_from_recipe(recipe)
    return {"plain_english": en, "plain_chinese": cn, "user_flow_steps": flow}


@router.post("/apply-from-description")
async def apply_from_description(
    body: ApplyFromDescriptionRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """One-shot: generate a Recipe from a description and apply it.

    "Apply" here means: write each module entry into the brand's
    ``brand:{bid}:modules`` HASH (mirroring brand_modules.py) and write
    each rule into ``brand:{bid}:rules``. We do this in-router so we
    don't need an HTTP round-trip.
    """
    recipe, cn, en, warnings = await _generate(
        body.description, body.industry, body.style
    )
    conf = _confidence(recipe)
    cplx = _complexity(recipe)
    modules_used = [m["id"] for m in recipe.get("modules", [])]

    # Persist the recipe first so the merchant can inspect it later.
    payload = {
        "recipe": recipe,
        "confidence": conf,
        "modules_used": modules_used,
        "explanation_cn": cn,
        "explanation_en": en,
        "estimated_complexity": cplx,
        "warnings": warnings,
        "source_description": body.description,
        "applied": True,
    }
    recipe_id = await _store_generated(r, body.brand_id, payload)

    # Apply modules
    applied_modules: list[str] = []
    mod_key = f"brand:{body.brand_id}:modules"
    now = _now_iso()
    for m in recipe.get("modules", []):
        entry = {
            "id": m["id"],
            "enabled": True,
            "params": m.get("params", {}),
            "updated_at": now,
            "source_recipe_id": recipe_id,
        }
        await r.hset(mod_key, m["id"], json.dumps(entry, ensure_ascii=False))
        applied_modules.append(m["id"])

    # Apply rules
    applied_rules: list[str] = []
    rule_key = f"brand:{body.brand_id}:rules"
    for rule in recipe.get("rules", []):
        rule_id = f"rul_{uuid4().hex[:10]}"
        entry = dict(rule, id=rule_id, enabled=True, created_at=now, source_recipe_id=recipe_id)
        await r.hset(rule_key, rule_id, json.dumps(entry, ensure_ascii=False))
        applied_rules.append(rule_id)

    summary = (
        f"Applied {len(applied_modules)} module(s) and {len(applied_rules)} rule(s) "
        f"for brand {body.brand_id} (recipe {recipe_id}, confidence {conf:.2f})."
    )
    return {
        "applied": True,
        "recipe_id": recipe_id,
        "applied_modules": applied_modules,
        "applied_rules": applied_rules,
        "confidence": conf,
        "estimated_complexity": cplx,
        "explanation_cn": cn,
        "explanation_en": en,
        "warnings": warnings,
        "summary": summary,
    }


@router.get("/catalog")
async def get_catalog():
    """Expose the module catalog so the merchant UI can render hints."""
    return {"modules": MODULE_CATALOG, "count": len(MODULE_CATALOG)}


@router.get("/brands/{brand_id}/recipes")
async def list_generated(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """List all generated recipes stored for a brand."""
    raw = await r.hgetall(_key(brand_id))
    out: list[dict[str, Any]] = []
    for rid, payload in raw.items():
        try:
            out.append(json.loads(payload))
        except json.JSONDecodeError:
            logger.warning("recipe_generator: bad json for %s/%s", brand_id, rid)
            continue
    out.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return out
