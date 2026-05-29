"""Recipe Library — pre-built gamification blueprints (1-click apply).

A **Recipe** is a JSON blueprint that bundles:
  - a set of gamification modules to enable
  - per-module config / params
  - RuleEngine rules wiring events to actions
  - merchant-facing metadata (icon, descriptions in EN/CN, source story)

Endpoints (registered under /api/v1/recipes):
  GET    /                          — list catalog (filter by category / tag)
  GET    /{recipe_id}               — full recipe
  POST   /{recipe_id}/apply         — atomically apply to a brand
  POST   /{recipe_id}/preview       — dry-run (no writes)
  POST   /create                    — merchant saves a custom recipe
  POST   /{recipe_id}/clone         — fork an existing recipe with modifications
  GET    /brands/{brand_id}/applied — list recipes active on a brand

Redis schema:
  recipes:catalog          HASH   recipe_id → JSON   (platform library)
  brand:{bid}:custom_recipes HASH recipe_id → JSON   (merchant customs)
  brand:{bid}:recipes_applied SET of recipe_id
  brand:{bid}:modules      HASH   (existing brand_modules storage)
  rules:{bid}              HASH   (existing rule_engine storage)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Known module catalog ───────────────────────────────────────────────────
# All module IDs that may appear in a recipe. Anything else is rejected at
# validation time. Sourced from /api/v1/modules, /api/v1/network,
# /api/v1/commerce, /api/v1/groups, /api/v1/multiplayer, /api/v1/p2p,
# /api/v1/social, /api/v1/vouchers, /api/v1/primitives, /api/v1/progression,
# /api/v1/streak.
KNOWN_MODULES: set[str] = {
    # progression layer
    "progression", "currency", "item", "achievement", "quest", "tier",
    "event",
    # composable mechanics
    "roulette", "league", "pass", "smartquests", "storyquest", "lives",
    "tourney", "collection", "badgewall",
    # streak / loyalty
    "streak",
    # voucher
    "voucher_builder", "voucher",
    # social / share / network-effect triggers
    "social_graph", "social_feed", "auto_share", "share_to_win",
    "energy_invite", "friend_challenge", "ladder_climb", "streak_rescue",
    "leaderboard", "network_effect",
    # commerce loop
    "score_to_coupon", "energy", "upsell", "redemption_store", "rate_limit",
    # group viral
    "group_actions", "groupbuy", "atomic_group", "pricecut",
    # multiplayer coop
    "coop_quest", "raid", "squad", "territory",
    # p2p
    "gift_sending", "trading_post", "group_reward", "fcfs",
    # misc primitives
    "limited_drop", "triggers",
}


# ── Pydantic models ────────────────────────────────────────────────────────


# Industry taxonomy — kept in sync with recipe_generator.Industry. Recipes
# may tag themselves with an industry so the catalog can be filtered per
# vertical (老李 community / book_club, 老黄 baby_products / ecommerce, …).
# Unknown / legacy values fall back to "other".
Industry = Literal[
    # Food & Beverage
    "coffee", "bubble_tea", "food", "restaurant", "luxury_dining", "qsr",
    # Retail / commerce
    "retail", "ecommerce", "luxury_retail", "fashion", "marketplace",
    # Health & Wellness
    "fitness", "beauty", "wellness", "healthcare",
    # Medical
    "medical", "medical_aesthetics",
    # Family / Pet
    "baby_products", "kids_education", "parenting", "pet",
    # Community
    "community", "book_club", "education", "co_working", "religious",
    # Hospitality / Travel
    "hotel", "travel", "airline",
    # Entertainment
    "gaming", "music", "events", "cinema",
    # Services
    "automotive", "real_estate", "financial_services", "fintech",
    "telecom", "logistics", "sharing_economy",
    # Catch-all
    "other",
]


class RecipeModule(BaseModel):
    id: str
    params: dict[str, Any] = Field(default_factory=dict)


class RecipeRule(BaseModel):
    """A rule fragment. Lives in the recipe without a brand_id; the apply
    step injects the brand_id and a deterministic rule id."""

    name: str | None = None
    trigger_event: str
    conditions: dict[str, Any] | None = None
    actions: list[dict[str, Any]] = Field(default_factory=list)
    max_triggers_per_user: int | None = 1
    description: str | None = None


class Recipe(BaseModel):
    id: str
    name: str
    name_cn: str | None = None
    description_en: str | None = None
    description_cn: str | None = None
    icon: str | None = None
    category: str = "uncategorized"
    industry: Industry | None = None
    tags: list[str] = Field(default_factory=list)
    modules: list[RecipeModule] = Field(default_factory=list)
    rules: list[RecipeRule] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApplyRequest(BaseModel):
    brand_id: str
    overrides: dict[str, Any] = Field(default_factory=dict)
    rollback_on_conflict: bool = False


class PreviewRequest(BaseModel):
    brand_id: str
    overrides: dict[str, Any] = Field(default_factory=dict)


class CloneRequest(BaseModel):
    brand_id: str
    new_id: str
    modifications: dict[str, Any] = Field(default_factory=dict)


# ── Redis key helpers ──────────────────────────────────────────────────────


CATALOG_KEY = "recipes:catalog"


def _k_custom(brand_id: str) -> str:
    return f"brand:{brand_id}:custom_recipes"


def _k_applied(brand_id: str) -> str:
    return f"brand:{brand_id}:recipes_applied"


def _k_brand_modules(brand_id: str) -> str:
    return f"brand:{brand_id}:modules"


def _k_brand_rules(brand_id: str) -> str:
    return f"rules:{brand_id}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Validation ─────────────────────────────────────────────────────────────


def _validate_recipe(recipe: Recipe) -> None:
    """Raise HTTPException if recipe references unknown modules."""
    unknown = [m.id for m in recipe.modules if m.id not in KNOWN_MODULES]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown module IDs: {unknown}. Allowed: see KNOWN_MODULES.",
        )
    for i, rule in enumerate(recipe.rules):
        for j, action in enumerate(rule.actions):
            mod = action.get("module")
            if mod and mod not in KNOWN_MODULES:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Unknown action.module '{mod}' in rule[{i}].actions[{j}]"
                    ),
                )


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ── Catalog loader (called from main lifespan) ─────────────────────────────


async def load_seed_recipes(r: aioredis.Redis) -> int:
    """Read recipes_seed.json from app/data/ and load into Redis catalog.
    Returns number of recipes loaded. Existing entries are overwritten."""
    seed_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "recipes_seed.json"
    )
    if not os.path.isfile(seed_path):
        logger.warning("recipes: seed file not found at %s", seed_path)
        return 0

    try:
        with open(seed_path, encoding="utf-8") as f:
            seed = json.load(f)
    except Exception as exc:
        logger.error("recipes: failed to read seed file: %s", exc)
        return 0

    if not isinstance(seed, list):
        logger.error("recipes: seed file must be a JSON list")
        return 0

    mapping: dict[str, str] = {}
    for entry in seed:
        try:
            recipe = Recipe.model_validate(entry)
            _validate_recipe(recipe)
        except Exception as exc:
            logger.error(
                "recipes: bad seed entry %s: %s",
                entry.get("id", "<no-id>"), exc,
            )
            continue
        mapping[recipe.id] = json.dumps(recipe.model_dump())

    if mapping:
        await r.hset(CATALOG_KEY, mapping=mapping)
    logger.info("recipes: loaded %d seed recipes", len(mapping))
    return len(mapping)


async def _get_recipe(
    recipe_id: str, brand_id: str | None, r: aioredis.Redis
) -> Recipe | None:
    """Look up a recipe in the platform catalog, then (optionally) in the
    brand's custom recipes."""
    raw = await r.hget(CATALOG_KEY, recipe_id)
    if raw is None and brand_id:
        raw = await r.hget(_k_custom(brand_id), recipe_id)
    if raw is None:
        return None
    try:
        return Recipe.model_validate(json.loads(raw))
    except Exception as exc:
        logger.warning("recipes: bad json for %s: %s", recipe_id, exc)
        return None


# ── Apply planning (shared by /apply and /preview) ─────────────────────────


async def _plan_apply(
    recipe: Recipe,
    brand_id: str,
    overrides: dict[str, Any],
    r: aioredis.Redis,
) -> dict[str, Any]:
    """Build the change-set without writing anything. Returns dict with
    module_ops, rule_ops, conflicts."""
    module_overrides = overrides.get("modules", {}) if isinstance(overrides, dict) else {}
    rule_overrides = overrides.get("rules", {}) if isinstance(overrides, dict) else {}

    existing_modules_raw = await r.hgetall(_k_brand_modules(brand_id))
    existing_rules = await r.hkeys(_k_brand_rules(brand_id))
    existing_rules_set = set(existing_rules)

    module_ops: list[dict[str, Any]] = []
    for m in recipe.modules:
        merged = _deep_merge(m.params, module_overrides.get(m.id, {}))
        prev_raw = existing_modules_raw.get(m.id)
        prev_params: dict[str, Any] = {}
        prev_enabled = False
        if prev_raw:
            try:
                prev = json.loads(prev_raw)
                prev_params = prev.get("params", {}) or {}
                prev_enabled = bool(prev.get("enabled", False))
            except json.JSONDecodeError:
                pass
        module_ops.append({
            "module_id": m.id,
            "previous_enabled": prev_enabled,
            "previous_params": prev_params,
            "next_params": merged,
            "will_overwrite": bool(prev_raw),
        })

    rule_ops: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for idx, rule in enumerate(recipe.rules):
        rule_id = f"{recipe.id}__r{idx}"
        is_conflict = rule_id in existing_rules_set
        merged_rule = {
            "id": rule_id,
            "brand_id": brand_id,
            "name": rule.name or f"{recipe.name} rule #{idx + 1}",
            "trigger_event": rule.trigger_event,
            "conditions": rule.conditions,
            "actions": rule.actions,
            "max_triggers_per_user": rule.max_triggers_per_user,
            "active": True,
            "description": rule.description or f"From recipe {recipe.id}",
        }
        if rule_id in rule_overrides and isinstance(rule_overrides[rule_id], dict):
            merged_rule = _deep_merge(merged_rule, rule_overrides[rule_id])
        op = {
            "rule_id": rule_id,
            "rule": merged_rule,
            "conflict": is_conflict,
        }
        rule_ops.append(op)
        if is_conflict:
            conflicts.append({"type": "rule_exists", "rule_id": rule_id})

    return {
        "module_ops": module_ops,
        "rule_ops": rule_ops,
        "conflicts": conflicts,
    }


# ── API: catalog read ─────────────────────────────────────────────────────


@router.get("")
@router.get("/")
async def list_recipes(
    category: str | None = Query(None),
    tags: str | None = Query(None, description="comma-separated tag list"),
    industry: str | None = Query(None, description="filter by industry vertical"),
    brand_id: str | None = Query(None, description="also include this brand's customs"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """List recipes in the platform catalog (and optionally a brand's customs)."""
    raw = await r.hgetall(CATALOG_KEY)
    out: list[dict[str, Any]] = []
    for rid, payload in raw.items():
        try:
            out.append(json.loads(payload))
        except json.JSONDecodeError:
            continue

    if brand_id:
        custom = await r.hgetall(_k_custom(brand_id))
        for rid, payload in custom.items():
            try:
                entry = json.loads(payload)
                entry["_custom"] = True
                out.append(entry)
            except json.JSONDecodeError:
                continue

    if category:
        out = [x for x in out if x.get("category") == category]

    if industry:
        out = [x for x in out if x.get("industry") == industry]

    if tags:
        wanted = {t.strip() for t in tags.split(",") if t.strip()}
        out = [x for x in out if wanted.intersection(set(x.get("tags", [])))]

    out.sort(key=lambda x: (x.get("category", ""), x.get("id", "")))
    return {"count": len(out), "recipes": out}


@router.get("/_catalog/reload")
async def reload_catalog(
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Re-read recipes_seed.json into Redis. Useful in dev / after edits."""
    n = await load_seed_recipes(r)
    return {"ok": True, "loaded": n}


@router.get("/{recipe_id}")
async def get_recipe(
    recipe_id: str,
    brand_id: str | None = Query(None),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    recipe = await _get_recipe(recipe_id, brand_id, r)
    if recipe is None:
        raise HTTPException(status_code=404, detail="recipe not found")
    return recipe.model_dump()


# ── API: preview & apply ──────────────────────────────────────────────────


@router.post("/{recipe_id}/preview")
async def preview_recipe(
    recipe_id: str,
    body: PreviewRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Dry-run: return the change-set without writing anything."""
    recipe = await _get_recipe(recipe_id, body.brand_id, r)
    if recipe is None:
        raise HTTPException(status_code=404, detail="recipe not found")
    plan = await _plan_apply(recipe, body.brand_id, body.overrides, r)
    return {
        "recipe_id": recipe_id,
        "brand_id": body.brand_id,
        "would_enable_modules": [op["module_id"] for op in plan["module_ops"]],
        "would_overwrite_modules": [
            op["module_id"] for op in plan["module_ops"] if op["will_overwrite"]
        ],
        "would_create_rules": [
            op["rule_id"] for op in plan["rule_ops"] if not op["conflict"]
        ],
        "conflicts": plan["conflicts"],
        "module_ops": plan["module_ops"],
        "rule_ops": plan["rule_ops"],
    }


@router.post("/{recipe_id}/apply")
async def apply_recipe(
    recipe_id: str,
    body: ApplyRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Atomically apply the recipe to a brand. Uses a Redis pipeline so all
    module config + rule writes commit together.

    Returns {applied, skipped, conflicts}.
    If ``rollback_on_conflict`` is true and any rule conflicts with an existing
    one, the entire apply is aborted (no writes) and conflicts are returned.
    Otherwise conflicting rules are skipped, non-conflicting writes proceed.
    """
    recipe = await _get_recipe(recipe_id, body.brand_id, r)
    if recipe is None:
        raise HTTPException(status_code=404, detail="recipe not found")
    _validate_recipe(recipe)

    plan = await _plan_apply(recipe, body.brand_id, body.overrides, r)

    if plan["conflicts"] and body.rollback_on_conflict:
        return {
            "ok": False,
            "applied": [],
            "skipped": [],
            "conflicts": plan["conflicts"],
            "rolled_back": True,
        }

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    pipe = r.pipeline(transaction=True)
    now = _now_iso()

    # 1. modules
    for op in plan["module_ops"]:
        config = {
            "id": op["module_id"],
            "enabled": True,
            "params": op["next_params"],
            "updated_at": now,
            "source_recipe": recipe.id,
        }
        pipe.hset(
            _k_brand_modules(body.brand_id),
            op["module_id"],
            json.dumps(config),
        )
        applied.append({"type": "module", "id": op["module_id"]})

    # 2. rules — skip conflicts, write new
    for op in plan["rule_ops"]:
        if op["conflict"]:
            skipped.append({
                "type": "rule",
                "id": op["rule_id"],
                "reason": "already_exists",
            })
            continue
        pipe.hset(
            _k_brand_rules(body.brand_id),
            op["rule_id"],
            json.dumps(op["rule"]),
        )
        applied.append({"type": "rule", "id": op["rule_id"]})

    # 3. record applied recipe
    pipe.sadd(_k_applied(body.brand_id), recipe.id)
    pipe.hset(
        f"brand:{body.brand_id}:recipes_applied_meta",
        recipe.id,
        json.dumps({"applied_at": now, "recipe_id": recipe.id}),
    )

    await pipe.execute()

    logger.info(
        "recipe_applied brand=%s recipe=%s modules=%d rules_new=%d rules_skipped=%d",
        body.brand_id,
        recipe.id,
        len(plan["module_ops"]),
        sum(1 for op in plan["rule_ops"] if not op["conflict"]),
        sum(1 for op in plan["rule_ops"] if op["conflict"]),
    )

    return {
        "ok": True,
        "recipe_id": recipe.id,
        "brand_id": body.brand_id,
        "applied": applied,
        "skipped": skipped,
        "conflicts": plan["conflicts"],
        "rolled_back": False,
    }


# ── API: custom recipes ───────────────────────────────────────────────────


@router.post("/create")
async def create_custom_recipe(
    recipe: Recipe,
    brand_id: str = Query(..., description="owning brand"),
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Save a merchant-authored recipe under brand:{bid}:custom_recipes."""
    _validate_recipe(recipe)

    # ── Tier-quota gate (recipes) ──────────────────────────────────────
    # Block creation when the brand has hit the recipes quota for its
    # current subscription tier. Fail-open if the subscription module is
    # unavailable.
    try:
        from app.routers.brand_subscriptions import check_quota
        allowed, info = await check_quota(brand_id, "recipes", r)
        if not allowed:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "tier_limit_reached",
                    "message": (
                        f"Your {info['tier']} tier allows "
                        f"{info['limit']} custom recipes. Upgrade to "
                        f"{info['upgrade_required_to']} for more."
                    ),
                    "tier": info["tier"],
                    "current": info["current"],
                    "limit": info["limit"],
                    "upgrade_required_to": info["upgrade_required_to"],
                },
            )
    except HTTPException:
        raise
    except (ImportError, ValueError):
        # Module not available or unknown resource — fail-open.
        pass

    payload = json.dumps(recipe.model_dump())
    # HSET returns 1 for new field, 0 if overwriting. Only INCR on a
    # genuinely new recipe so re-saves don't inflate the counter.
    was_new = await r.hset(_k_custom(brand_id), recipe.id, payload)
    if was_new:
        await r.incr(f"brand:{brand_id}:recipes_count")
    logger.info(
        "recipe_custom_created brand=%s recipe=%s modules=%d rules=%d new=%s",
        brand_id, recipe.id, len(recipe.modules), len(recipe.rules), bool(was_new),
    )
    return {"ok": True, "recipe": recipe.model_dump()}


@router.post("/{recipe_id}/clone")
async def clone_recipe(
    recipe_id: str,
    body: CloneRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Fork an existing recipe into the brand's custom library with
    deep-merged modifications applied on top."""
    base = await _get_recipe(recipe_id, body.brand_id, r)
    if base is None:
        raise HTTPException(status_code=404, detail="recipe not found")
    if not body.new_id:
        raise HTTPException(status_code=400, detail="new_id is required")

    cloned = _deep_merge(base.model_dump(), body.modifications)
    cloned["id"] = body.new_id

    try:
        new_recipe = Recipe.model_validate(cloned)
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"clone produced invalid recipe: {exc}"
        )
    _validate_recipe(new_recipe)

    # ── Tier-quota gate (recipes) ──────────────────────────────────────
    # Cloning also produces a new custom recipe, so the same quota
    # applies. Fail-open if the subscription module is unavailable.
    try:
        from app.routers.brand_subscriptions import check_quota
        allowed, info = await check_quota(body.brand_id, "recipes", r)
        if not allowed:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "tier_limit_reached",
                    "message": (
                        f"Your {info['tier']} tier allows "
                        f"{info['limit']} custom recipes. Upgrade to "
                        f"{info['upgrade_required_to']} for more."
                    ),
                    "tier": info["tier"],
                    "current": info["current"],
                    "limit": info["limit"],
                    "upgrade_required_to": info["upgrade_required_to"],
                },
            )
    except HTTPException:
        raise
    except (ImportError, ValueError):
        pass

    was_new = await r.hset(
        _k_custom(body.brand_id),
        new_recipe.id,
        json.dumps(new_recipe.model_dump()),
    )
    if was_new:
        await r.incr(f"brand:{body.brand_id}:recipes_count")
    logger.info(
        "recipe_cloned brand=%s from=%s to=%s",
        body.brand_id, recipe_id, new_recipe.id,
    )
    return {"ok": True, "recipe": new_recipe.model_dump()}


# ── API: brand-applied recipes ────────────────────────────────────────────


@router.get("/brands/{brand_id}/applied")
async def list_applied_recipes(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """List recipes currently flagged as applied to this brand."""
    applied_ids = await r.smembers(_k_applied(brand_id))
    meta_raw = await r.hgetall(f"brand:{brand_id}:recipes_applied_meta")

    out: list[dict[str, Any]] = []
    for rid in sorted(applied_ids):
        meta = {}
        if rid in meta_raw:
            try:
                meta = json.loads(meta_raw[rid])
            except json.JSONDecodeError:
                pass
        recipe = await _get_recipe(rid, brand_id, r)
        out.append({
            "id": rid,
            "applied_at": meta.get("applied_at"),
            "name": recipe.name if recipe else None,
            "category": recipe.category if recipe else None,
            "icon": recipe.icon if recipe else None,
            "exists": recipe is not None,
        })

    return {"brand_id": brand_id, "count": len(out), "applied": out}


@router.delete("/brands/{brand_id}/applied/{recipe_id}")
async def unmark_applied_recipe(
    brand_id: str,
    recipe_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Remove the applied-flag (does NOT undo module/rule writes).
    Use this if a merchant wants to forget that a recipe was applied
    without rolling back its effects."""
    removed = await r.srem(_k_applied(brand_id), recipe_id)
    await r.hdel(f"brand:{brand_id}:recipes_applied_meta", recipe_id)
    return {"ok": True, "removed": bool(removed)}
