"""Rule Engine — When-Then composition over all KiX modules.

The merchant-facing brain of the platform. Every other module (Streak,
Tier, Pass, Quest, Roulette, Coupon, Voucher, Network triggers, …) is
self-contained; this engine lets brands *compose* them with arbitrary
boolean logic:

    "When a friend redeems an invite, AND the user has converted
     >= 10 invites, AND played >= 3 games, then grant voucher
     vou_50_off_250 AND award 500 XP."

Pure deterministic evaluator. No LLM. Recursive AND / OR / NOT /
THRESHOLD composition. Conditions read from Redis via a metric
resolver. Actions are pushed onto a per-user pending_actions queue —
real wiring to the target endpoints happens in a separate worker.

Redis schema (all brand-isolated by ``brand_id``):

    brand:{bid}:rules                              HASH  rule_id → JSON
    brand:{bid}:rule_firings:{rule_id}:user:{uid}  STR   firing count
    brand:{bid}:user:{uid}:rule_log                LIST  firing audit records
    brand:{bid}:user:{uid}:pending_actions         LIST  action records
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ──────────────────────────────────────────────────────────────

CONDITION_TYPES = {
    "count",
    "value",
    "streak",
    "tier",
    "time_window",
    "user_attribute",
    "random",
}

COMPOSITION_OPS = {"AND", "OR", "NOT", "THRESHOLD"}

COMPARE_OPS = {">=", ">", "<=", "<", "==", "!="}

# Map module.method → internal API path (used by the deferred executor)
ACTION_ROUTES: dict[str, str] = {
    "progression.award_xp": "/api/v1/progression/award/xp",
    "progression.award_badge": "/api/v1/progression/award/badge",
    "primitives.currency.grant": "/api/v1/primitives/currency/grant",
    "primitives.item.grant": "/api/v1/primitives/item/grant",
    "primitives.tier.set": "/api/v1/primitives/tier/set",
    "voucher.grant": "/api/v1/brands/vouchers/grant",
    "commerce.coupon.claim": "/api/v1/commerce/coupon/claim",
    "network.share_to_win.init": "/api/v1/network/share-to-win/init",
    "network.energy_invite.init": "/api/v1/network/energy-invite/init",
    "streak.rescue": "/api/v1/streak/rescue",
}

RULE_LOG_MAX = 200


# Attribute-watch / new-style rule trigger kinds (FEATURE 1).
# Existing event-fired rules go through ``emit_event`` unchanged; the new
# ``attribute_changed`` family is dispatched by ``on_attribute_changed``.
WHEN_TYPES_NEW = {
    "attribute_changed",
    "event_fired",
    "metric_threshold",
}

ATTR_CONDITION_TYPES = {
    "crosses_threshold",
    "increases_by_pct",
    "equals_value",
    "matches_pattern",
}

# Higher-level action types (FEATURE 3 + recipient indirection FEATURE 2).
ACTION_TYPES_HIGH_LEVEL = {
    "issue_voucher",
    "award_xp",
    "send_push",
    "fire_achievement",
    "trigger_webhook",
    "create_audience_membership",
}


# ── Pydantic models ────────────────────────────────────────────────────────


class Condition(BaseModel):
    """A leaf condition or composite node.

    Leaf: ``type`` ∈ CONDITION_TYPES, ``op`` + ``value`` describe a
    comparison against a resolved metric / attribute.

    Composite: ``op`` ∈ COMPOSITION_OPS, ``children`` recursively.
    For ``THRESHOLD`` an additional ``n`` ≥ 1 must be provided.
    """

    # Composite
    op: str | None = None
    children: list["Condition"] | None = None
    n: int | None = None  # for THRESHOLD

    # Leaf
    type: str | None = None
    metric: str | None = None
    attribute: str | None = None
    value: Any = None
    window_seconds: int | None = None

    @field_validator("op")
    @classmethod
    def _op_known(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v in COMPOSITION_OPS or v in COMPARE_OPS:
            return v
        raise ValueError(f"Unknown op: {v}")


Condition.model_rebuild()


class Action(BaseModel):
    """A single side-effect to enqueue when a rule fires."""

    module: str
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class Rule(BaseModel):
    """The full When-Then specification."""

    id: str
    brand_id: str
    name: str
    trigger_event: str
    conditions: Condition | None = None
    actions: list[Action] = Field(default_factory=list)
    max_triggers_per_user: int | None = 1
    active_from: int | None = None       # unix seconds
    active_until: int | None = None      # unix seconds
    active: bool = True
    description: str | None = None


class EventEmit(BaseModel):
    brand_id: str
    user_id: str
    event_name: str
    payload: dict[str, Any] = Field(default_factory=dict)


class DryRunRequest(BaseModel):
    user_id: str
    payload: dict[str, Any] = Field(default_factory=dict)


# ── Redis key helpers ──────────────────────────────────────────────────────


def _k_rules(brand_id: str) -> str:
    return f"brand:{brand_id}:rules"


def _k_firings(brand_id: str, rule_id: str, user_id: str) -> str:
    return f"brand:{brand_id}:rule_firings:{rule_id}:user:{user_id}"


def _k_rule_log(brand_id: str, user_id: str) -> str:
    return f"brand:{brand_id}:user:{user_id}:rule_log"


def _k_pending_actions(brand_id: str, user_id: str) -> str:
    return f"brand:{brand_id}:user:{user_id}:pending_actions"


# ── Metric resolver ────────────────────────────────────────────────────────


def _xp_to_level(xp: float) -> int:
    """Crude level curve — every 1000 XP = 1 level. Engine treats level as
    derived, so the precise curve can change without touching rules."""
    return int(xp // 1000) + 1


async def _resolve_metric(
    metric_name: str,
    user_id: str,
    brand_id: str,
    r: aioredis.Redis,
) -> float:
    """Map a metric name → numeric value pulled from Redis.

    Returns 0.0 for missing keys so comparisons remain well-defined.
    Unknown metrics raise ValueError so a misconfigured rule fails loud.
    """
    if metric_name == "xp":
        v = await r.get(f"user:{user_id}:xp")
        return float(v or 0)

    if metric_name == "level":
        v = await r.get(f"user:{user_id}:xp")
        return float(_xp_to_level(float(v or 0)))

    if metric_name == "streak":
        v = await r.get(f"user:{user_id}:checkin:streak")
        return float(v or 0)

    if metric_name == "games_played":
        v = await r.get(f"user:{user_id}:games_played:{brand_id}")
        return float(v or 0)

    if metric_name == "invites_sent":
        total = 0
        async for key in r.scan_iter(f"brand:{brand_id}:viral:*:invited"):
            v = await r.get(key)
            try:
                total += int(v or 0)
            except (TypeError, ValueError):
                pass
        return float(total)

    if metric_name == "invites_converted":
        total = 0
        async for key in r.scan_iter(f"brand:{brand_id}:viral:*:converted"):
            v = await r.get(key)
            try:
                total += int(v or 0)
            except (TypeError, ValueError):
                pass
        return float(total)

    if metric_name == "purchases_count":
        v = await r.get(f"brand:{brand_id}:user:{user_id}:purchases_count")
        return float(v or 0)

    if metric_name == "lifetime_spend":
        v = await r.get(f"brand:{brand_id}:user:{user_id}:lifetime_spend_cents")
        return float(v or 0)

    if metric_name == "friend_count":
        return float(await r.scard(f"user:{user_id}:friends") or 0)

    if metric_name == "badge_count":
        return float(await r.scard(f"user:{user_id}:badges") or 0)

    if metric_name == "tier_id":
        # tier is non-numeric — returned as 0 in the metric API; rules
        # that need to match a specific tier should use type="tier".
        return 0.0

    raise ValueError(f"Unknown metric: {metric_name}")


async def _resolve_tier(user_id: str, brand_id: str, r: aioredis.Redis) -> str | None:
    return await r.get(f"user:{user_id}:tier:{brand_id}")


async def _resolve_user_attribute(
    attribute: str,
    user_id: str,
    r: aioredis.Redis,
) -> Any:
    """Read user.* attributes from the canonical user hash."""
    return await r.hget(f"user:{user_id}", attribute)


async def _last_event_age_seconds(
    event_name: str,
    user_id: str,
    brand_id: str,
    r: aioredis.Redis,
) -> float | None:
    """Read the last-seen timestamp for an event of this user/brand.

    Conventionally written by ``/events/emit`` itself — see
    ``_record_event_seen`` below.
    """
    v = await r.get(f"brand:{brand_id}:user:{user_id}:event:{event_name}:last_at")
    if v is None:
        return None
    try:
        return time.time() - float(v)
    except (TypeError, ValueError):
        return None


async def _record_event_seen(
    event_name: str,
    user_id: str,
    brand_id: str,
    r: aioredis.Redis,
) -> None:
    await r.set(
        f"brand:{brand_id}:user:{user_id}:event:{event_name}:last_at",
        str(time.time()),
    )


# ── Comparison primitives ──────────────────────────────────────────────────


def _compare(lhs: Any, op: str, rhs: Any) -> bool:
    """Numeric / string aware comparison. Falls back to == / != for
    non-orderable mixed types."""
    if op == "==":
        return lhs == rhs
    if op == "!=":
        return lhs != rhs

    # numeric ordering — coerce if both look numeric
    try:
        lf = float(lhs)
        rf = float(rhs)
        if op == ">=":
            return lf >= rf
        if op == ">":
            return lf > rf
        if op == "<=":
            return lf <= rf
        if op == "<":
            return lf < rf
    except (TypeError, ValueError):
        pass

    # Fallback: lexicographic for strings
    if isinstance(lhs, str) and isinstance(rhs, str):
        if op == ">=":
            return lhs >= rhs
        if op == ">":
            return lhs > rhs
        if op == "<=":
            return lhs <= rhs
        if op == "<":
            return lhs < rhs

    return False


# ── Recursive condition evaluator ──────────────────────────────────────────


async def _evaluate(
    cond: Condition | None,
    user_id: str,
    brand_id: str,
    payload: dict[str, Any],
    r: aioredis.Redis,
    trace: list[dict[str, Any]] | None = None,
) -> bool:
    """Depth-first evaluator. Short-circuits AND / OR.

    A ``None`` condition is vacuously true (use case: "fire on every
    event of this name").
    """
    if cond is None:
        return True

    # ── Composite ────────────────────────────────────────────────────
    if cond.op in COMPOSITION_OPS:
        children = cond.children or []

        if cond.op == "AND":
            for c in children:
                if not await _evaluate(c, user_id, brand_id, payload, r, trace):
                    return False
            return True

        if cond.op == "OR":
            for c in children:
                if await _evaluate(c, user_id, brand_id, payload, r, trace):
                    return True
            return False

        if cond.op == "NOT":
            if not children:
                return True
            # NOT applies to first child only
            inner = await _evaluate(children[0], user_id, brand_id, payload, r, trace)
            return not inner

        if cond.op == "THRESHOLD":
            need = cond.n or 1
            hits = 0
            for c in children:
                if await _evaluate(c, user_id, brand_id, payload, r, trace):
                    hits += 1
                    if hits >= need:
                        return True
            return False

    # ── Leaf ─────────────────────────────────────────────────────────
    t = cond.type
    result = False
    actual: Any = None

    if t == "count":
        if not cond.metric or cond.op not in COMPARE_OPS:
            raise ValueError("count condition requires metric + comparison op")
        actual = await _resolve_metric(cond.metric, user_id, brand_id, r)
        result = _compare(actual, cond.op, cond.value)

    elif t == "value":
        # Value lookup happens against the event payload first, then
        # falls back to the metric resolver if a metric name is given.
        if cond.metric and cond.metric in payload:
            actual = payload[cond.metric]
        elif cond.metric:
            actual = await _resolve_metric(cond.metric, user_id, brand_id, r)
        else:
            actual = cond.value  # degenerate but well-defined
        if cond.op in COMPARE_OPS:
            result = _compare(actual, cond.op, cond.value)
        else:
            result = actual == cond.value

    elif t == "streak":
        op = cond.op if cond.op in COMPARE_OPS else ">="
        actual = await _resolve_metric("streak", user_id, brand_id, r)
        result = _compare(actual, op, cond.value)

    elif t == "tier":
        actual = await _resolve_tier(user_id, brand_id, r)
        op = cond.op or "=="
        result = _compare(actual, op, cond.value)

    elif t == "time_window":
        # ``metric`` here is the event name; ``window_seconds`` is the
        # max age. Condition is true iff the event was seen recently.
        ev = cond.metric
        win = cond.window_seconds
        if not ev or win is None:
            raise ValueError("time_window requires metric=event_name + window_seconds")
        age = await _last_event_age_seconds(ev, user_id, brand_id, r)
        actual = age
        result = age is not None and age <= float(win)

    elif t == "user_attribute":
        attr = cond.attribute or cond.metric
        if not attr:
            raise ValueError("user_attribute requires attribute")
        actual = await _resolve_user_attribute(attr, user_id, r)
        op = cond.op or "=="
        result = _compare(actual, op, cond.value)

    elif t == "random":
        # ``value`` is a probability in [0, 1]; ``op`` ignored.
        try:
            p = float(cond.value)
        except (TypeError, ValueError):
            p = 0.0
        roll = random.random()
        actual = roll
        result = roll < p

    else:
        raise ValueError(f"Unknown condition type: {t}")

    if trace is not None:
        trace.append(
            {
                "type": t,
                "metric": cond.metric,
                "attribute": cond.attribute,
                "op": cond.op,
                "expected": cond.value,
                "actual": actual,
                "result": result,
            }
        )
    return result


# ── Action executor (deferred — pushes onto pending queue) ─────────────────


async def _execute_action(
    action: Action,
    user_id: str,
    brand_id: str,
    rule_id: str,
    r: aioredis.Redis,
) -> dict[str, Any]:
    """Push the action onto the user's pending_actions queue.

    A separate worker will drain this queue and HTTP-POST to the
    appropriate internal endpoint (looked up via ACTION_ROUTES). This
    keeps the engine fast, idempotent, and crash-safe.
    """
    key = f"{action.module}.{action.method}"
    record = {
        "action_id": uuid4().hex[:12],
        "rule_id": rule_id,
        "module": action.module,
        "method": action.method,
        "route": ACTION_ROUTES.get(key),  # may be None — worker decides
        "params": action.params,
        "user_id": user_id,
        "brand_id": brand_id,
        "enqueued_at": time.time(),
        "status": "pending",
    }
    await r.rpush(_k_pending_actions(brand_id, user_id), json.dumps(record))
    return record


# ── Rule store helpers ─────────────────────────────────────────────────────


async def _load_rule(
    brand_id: str, rule_id: str, r: aioredis.Redis
) -> Rule | None:
    raw = await r.hget(_k_rules(brand_id), rule_id)
    if not raw:
        return None
    try:
        return Rule.model_validate_json(raw)
    except Exception as e:  # noqa: BLE001
        logger.exception("Corrupt rule %s/%s: %s", brand_id, rule_id, e)
        return None


async def _save_rule(rule: Rule, r: aioredis.Redis) -> None:
    await r.hset(
        _k_rules(rule.brand_id),
        rule.id,
        rule.model_dump_json(),
    )


async def _list_rules_for_brand(brand_id: str, r: aioredis.Redis) -> list[Rule]:
    raw_map = await r.hgetall(_k_rules(brand_id))
    out: list[Rule] = []
    for rid, raw in (raw_map or {}).items():
        try:
            out.append(Rule.model_validate_json(raw))
        except Exception:  # noqa: BLE001
            logger.warning("Skipping corrupt rule %s/%s", brand_id, rid)
    return out


def _rule_is_active_now(rule: Rule) -> bool:
    if not rule.active:
        return False
    now = time.time()
    if rule.active_from is not None and now < rule.active_from:
        return False
    if rule.active_until is not None and now > rule.active_until:
        return False
    return True


async def _firings_for_user(
    brand_id: str, rule_id: str, user_id: str, r: aioredis.Redis
) -> int:
    v = await r.get(_k_firings(brand_id, rule_id, user_id))
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# ── API: rule CRUD ─────────────────────────────────────────────────────────


@router.post("/configure")
async def configure_rule(
    rule: Rule, r: aioredis.Redis = Depends(get_redis)
) -> dict[str, Any]:
    """Upsert a rule. Returns the stored rule."""
    if not rule.id or not rule.brand_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="rule.id and rule.brand_id are required",
        )
    await _save_rule(rule, r)
    logger.info(
        "rule_configured brand=%s rule=%s event=%s actions=%d",
        rule.brand_id,
        rule.id,
        rule.trigger_event,
        len(rule.actions),
    )
    return {"status": "ok", "rule": rule.model_dump()}


@router.get("/{brand_id}")
async def list_rules(
    brand_id: str, r: aioredis.Redis = Depends(get_redis)
) -> dict[str, Any]:
    rules = await _list_rules_for_brand(brand_id, r)
    return {
        "brand_id": brand_id,
        "count": len(rules),
        "rules": [x.model_dump() for x in rules],
    }


@router.get("/{brand_id}/{rule_id}")
async def get_rule(
    brand_id: str,
    rule_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    rule = await _load_rule(brand_id, rule_id, r)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    return rule.model_dump()


@router.post("/{brand_id}/{rule_id}/disable")
async def disable_rule(
    brand_id: str,
    rule_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    rule = await _load_rule(brand_id, rule_id, r)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    rule.active = False
    await _save_rule(rule, r)
    return {"status": "ok", "rule_id": rule_id, "active": False}


@router.post("/{brand_id}/{rule_id}/enable")
async def enable_rule(
    brand_id: str,
    rule_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    rule = await _load_rule(brand_id, rule_id, r)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    rule.active = True
    await _save_rule(rule, r)
    return {"status": "ok", "rule_id": rule_id, "active": True}


@router.delete("/{brand_id}/{rule_id}")
async def delete_rule(
    brand_id: str,
    rule_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    removed = await r.hdel(_k_rules(brand_id), rule_id)
    if not removed:
        raise HTTPException(status_code=404, detail="rule not found")
    return {"status": "ok", "rule_id": rule_id, "deleted": True}


# ── API: event emission (the hot path) ─────────────────────────────────────


@router.post("/events/emit")
async def emit_event(
    ev: EventEmit, r: aioredis.Redis = Depends(get_redis)
) -> dict[str, Any]:
    """Fire an event. The engine finds all rules whose
    ``trigger_event`` matches, evaluates each one's conditions, and (for
    those that pass) enqueues the listed actions and writes an audit
    record.

    Returns a summary describing which rules matched and which actions
    were enqueued.
    """
    # Record event-last-seen so ``time_window`` conditions can reference
    # it on subsequent events.
    await _record_event_seen(ev.event_name, ev.user_id, ev.brand_id, r)

    candidates = await _list_rules_for_brand(ev.brand_id, r)
    matched: list[dict[str, Any]] = []
    enqueued: list[dict[str, Any]] = []

    for rule in candidates:
        if rule.trigger_event != ev.event_name:
            continue
        if not _rule_is_active_now(rule):
            continue

        # Idempotency gate
        if rule.max_triggers_per_user is not None:
            fired = await _firings_for_user(
                ev.brand_id, rule.id, ev.user_id, r
            )
            if fired >= rule.max_triggers_per_user:
                continue

        trace: list[dict[str, Any]] = []
        try:
            passed = await _evaluate(
                rule.conditions, ev.user_id, ev.brand_id, ev.payload, r, trace
            )
        except ValueError as e:
            logger.warning(
                "rule_eval_error brand=%s rule=%s err=%s",
                ev.brand_id,
                rule.id,
                e,
            )
            continue

        if not passed:
            continue

        # Fire — bump counter, push actions, write audit log
        await r.incr(_k_firings(ev.brand_id, rule.id, ev.user_id))

        rule_actions: list[dict[str, Any]] = []
        for action in rule.actions:
            record = await _execute_action(
                action, ev.user_id, ev.brand_id, rule.id, r
            )
            rule_actions.append(record)
            enqueued.append(record)

        audit = {
            "rule_id": rule.id,
            "rule_name": rule.name,
            "fired_at": time.time(),
            "event_name": ev.event_name,
            "payload": ev.payload,
            "conditions_state": trace,
            "actions": rule_actions,
        }
        await r.lpush(_k_rule_log(ev.brand_id, ev.user_id), json.dumps(audit))
        await r.ltrim(_k_rule_log(ev.brand_id, ev.user_id), 0, RULE_LOG_MAX - 1)

        matched.append(
            {
                "rule_id": rule.id,
                "rule_name": rule.name,
                "conditions_state": trace,
                "actions_enqueued": len(rule_actions),
            }
        )

    return {
        "status": "ok",
        "event": ev.event_name,
        "user_id": ev.user_id,
        "brand_id": ev.brand_id,
        "candidates_considered": len(candidates),
        "rules_matched": matched,
        "actions_executed": enqueued,
    }


# ── API: introspection / audit ─────────────────────────────────────────────


@router.get("/{brand_id}/{rule_id}/firings")
async def rule_firings(
    brand_id: str,
    rule_id: str,
    user_id: str,
    limit: int = 50,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Audit log of every time this rule fired for this user."""
    count = await _firings_for_user(brand_id, rule_id, user_id, r)
    raw_log = await r.lrange(_k_rule_log(brand_id, user_id), 0, max(limit - 1, 0))

    out: list[dict[str, Any]] = []
    for raw in raw_log:
        try:
            rec = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if rec.get("rule_id") == rule_id:
            out.append(rec)

    return {
        "brand_id": brand_id,
        "rule_id": rule_id,
        "user_id": user_id,
        "firings_count": count,
        "firings": out,
    }


@router.get("/{brand_id}/user/{user_id}/pending-actions")
async def list_pending_actions(
    brand_id: str,
    user_id: str,
    limit: int = 100,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Inspect the per-user pending-actions queue (the side-effect inbox)."""
    raw_list = await r.lrange(
        _k_pending_actions(brand_id, user_id), 0, max(limit - 1, 0)
    )
    out: list[dict[str, Any]] = []
    for raw in raw_list:
        try:
            out.append(json.loads(raw))
        except (TypeError, ValueError):
            continue
    return {
        "brand_id": brand_id,
        "user_id": user_id,
        "count": len(out),
        "actions": out,
    }


@router.post("/{brand_id}/{rule_id}/dry-run")
async def dry_run_rule(
    brand_id: str,
    rule_id: str,
    body: DryRunRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Evaluate a rule against a user *without* firing actions or
    bumping counters. Returns the full condition trace + which actions
    would have been enqueued."""
    rule = await _load_rule(brand_id, rule_id, r)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")

    fired = await _firings_for_user(brand_id, rule.id, body.user_id, r)
    would_block_for_max = (
        rule.max_triggers_per_user is not None
        and fired >= rule.max_triggers_per_user
    )

    trace: list[dict[str, Any]] = []
    try:
        passed = await _evaluate(
            rule.conditions, body.user_id, brand_id, body.payload, r, trace
        )
        eval_error: str | None = None
    except ValueError as e:
        passed = False
        eval_error = str(e)

    would_fire = (
        passed
        and not would_block_for_max
        and _rule_is_active_now(rule)
    )

    return {
        "rule_id": rule.id,
        "user_id": body.user_id,
        "brand_id": brand_id,
        "active_now": _rule_is_active_now(rule),
        "fired_count_so_far": fired,
        "max_triggers_per_user": rule.max_triggers_per_user,
        "blocked_by_max": would_block_for_max,
        "conditions_passed": passed,
        "conditions_state": trace,
        "eval_error": eval_error,
        "would_fire": would_fire,
        "would_enqueue_actions": [
            a.model_dump() for a in rule.actions
        ] if would_fire else [],
    }


# ── API: per-user metric inspection (debug aid) ────────────────────────────


@router.get("/{brand_id}/user/{user_id}/metrics")
async def get_user_metrics(
    brand_id: str,
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Dump every supported metric for one user — useful for debugging
    why a rule did / didn't fire."""
    metrics = [
        "xp",
        "level",
        "streak",
        "games_played",
        "invites_sent",
        "invites_converted",
        "purchases_count",
        "lifetime_spend",
        "friend_count",
        "badge_count",
    ]
    out: dict[str, Any] = {}
    for m in metrics:
        try:
            out[m] = await _resolve_metric(m, user_id, brand_id, r)
        except ValueError:
            out[m] = None
    out["tier_id"] = await _resolve_tier(user_id, brand_id, r)
    return {
        "brand_id": brand_id,
        "user_id": user_id,
        "metrics": out,
    }


# ═════════════════════════════════════════════════════════════════════════
# Extended rules — attribute-watch, recipient indirection, fire_achievement,
# bulk dry-run / test-resolution. Layered on top of the legacy Rule store
# so existing event-driven rules keep working untouched.
#
# Redis schema additions:
#   rule:{rule_id}                       HASH  rule fields (flat)
#   brand:{bid}:attr_rules:{key}         SET   rule_ids watching this key
#   brand:{bid}:rules_v2                 SET   all v2 rule_ids for this brand
#   user:{uid}:relationship_by_type:{r}  SET   related user_ids
# ═════════════════════════════════════════════════════════════════════════


import re  # noqa: E402 — kept local to the extension to ease future split


# ── Models for the v2 rule shape ──────────────────────────────────────────


class AttrWhen(BaseModel):
    """``when:`` clause for v2 rules.

    For ``attribute_changed`` rules ``attribute_key`` + ``condition`` are
    required. For the other ``type`` values the engine accepts free-form
    config and delegates routing to the caller (kept open for forward
    compatibility — only ``attribute_changed`` is wired here).
    """

    type: str
    attribute_key: str | None = None
    event_name: str | None = None
    metric: str | None = None
    condition: dict[str, Any] | None = None
    lookback_window_seconds: int | None = None

    @field_validator("type")
    @classmethod
    def _known_when_type(cls, v: str) -> str:
        # Be permissive: accept any string, but flag the well-known set.
        return v


class AttrThen(BaseModel):
    """``then:`` clause — high-level action with optional recipient
    indirection. ``recipient_user_id_attr`` names a relationship-type
    (e.g. ``"parent_of"``); the action is then expanded to every
    related user."""

    action_type: str
    action_config: dict[str, Any] = Field(default_factory=dict)
    recipient_user_id_attr: str | None = None


class RuleV2Create(BaseModel):
    brand_id: str
    name: str
    when: AttrWhen
    then: AttrThen
    id: str | None = None  # generated if absent
    active: bool = True
    max_triggers_per_user: int | None = None
    description: str | None = None


class TestResolutionBody(BaseModel):
    actor_user_id: str


class V2DryRunBody(BaseModel):
    actor_user_id: str
    simulated_event: dict[str, Any] = Field(default_factory=dict)


# ── Redis-key helpers for the v2 layer ────────────────────────────────────


def _k_rule_v2(rule_id: str) -> str:
    return f"rule:{rule_id}"


def _k_attr_rules(brand_id: str, attribute_key: str) -> str:
    return f"brand:{brand_id}:attr_rules:{attribute_key}"


def _k_brand_rules_v2(brand_id: str) -> str:
    return f"brand:{brand_id}:rules_v2"


def _k_relationship(user_id: str, rel_type: str) -> str:
    return f"user:{user_id}:relationship_by_type:{rel_type}"


# ── Condition evaluation for attribute_changed ────────────────────────────


def _to_float(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _evaluate_attr_condition(
    cond: dict[str, Any] | None,
    old_value: Any,
    new_value: Any,
) -> bool:
    """Pure, side-effect-free evaluator for ``attribute_changed`` rules.

    Recognised ``cond.type`` values:

      * ``crosses_threshold`` — old < threshold <= new (uni-directional rise)
      * ``increases_by_pct``  — (new-old)/old * 100 >= increase_pct
      * ``equals_value``      — new == equals
      * ``matches_pattern``   — re.search(pattern, str(new))

    Unknown/missing ``cond`` → False (never fire on misconfigured rule).
    """
    if not cond or not isinstance(cond, dict):
        return False

    ctype = cond.get("type")

    if ctype == "crosses_threshold":
        thr = _to_float(cond.get("threshold"))
        nv = _to_float(new_value)
        ov = _to_float(old_value)
        if thr is None or nv is None:
            return False
        if ov is None:
            # First-ever write: counts as a cross iff new value meets it.
            return nv >= thr
        return ov < thr <= nv

    if ctype == "increases_by_pct":
        pct = _to_float(cond.get("increase_pct"))
        nv = _to_float(new_value)
        ov = _to_float(old_value)
        if pct is None or nv is None or ov is None or ov == 0:
            return False
        return ((nv - ov) / abs(ov)) * 100.0 >= pct

    if ctype == "equals_value":
        target = cond.get("equals")
        return str(new_value) == str(target)

    if ctype == "matches_pattern":
        pattern = cond.get("pattern")
        if not pattern:
            return False
        try:
            return re.search(pattern, str(new_value)) is not None
        except re.error:
            return False

    return False


# ── Recipient resolution ──────────────────────────────────────────────────


async def _resolve_recipients(
    r: aioredis.Redis,
    actor_user_id: str,
    recipient_user_id_attr: str | None,
) -> list[str]:
    """Map the actor → list of recipients.

    No indirection → just ``[actor]``. Otherwise read the membership SET
    at ``user:{actor}:relationship_by_type:{attr}`` and return its
    members (sorted for determinism). Returns ``[]`` if the relationship
    set is empty — callers should treat this as a no-op fire.
    """
    if not recipient_user_id_attr:
        return [actor_user_id]
    members = await r.smembers(_k_relationship(actor_user_id, recipient_user_id_attr))
    return sorted(members or [])


# ── v2 rule store ─────────────────────────────────────────────────────────


def _rule_v2_to_hash(rule_id: str, body: RuleV2Create) -> dict[str, str]:
    return {
        "id": rule_id,
        "brand_id": body.brand_id,
        "name": body.name,
        "active": "1" if body.active else "0",
        "when_type": body.when.type,
        "attribute_key": body.when.attribute_key or "",
        "event_name": body.when.event_name or "",
        "metric": body.when.metric or "",
        "when_condition": json.dumps(body.when.condition or {}),
        "lookback_window_seconds": (
            str(body.when.lookback_window_seconds)
            if body.when.lookback_window_seconds is not None
            else ""
        ),
        "action_type": body.then.action_type,
        "action_config": json.dumps(body.then.action_config or {}),
        "recipient_user_id_attr": body.then.recipient_user_id_attr or "",
        "max_triggers_per_user": (
            str(body.max_triggers_per_user)
            if body.max_triggers_per_user is not None
            else ""
        ),
        "description": body.description or "",
        "created_at": str(time.time()),
    }


def _hash_to_rule_v2_view(h: dict[str, Any]) -> dict[str, Any]:
    """Project the flat hash form back into the documented nested shape."""
    return {
        "id": h.get("id"),
        "brand_id": h.get("brand_id"),
        "name": h.get("name"),
        "active": h.get("active") == "1",
        "when": {
            "type": h.get("when_type"),
            "attribute_key": h.get("attribute_key") or None,
            "event_name": h.get("event_name") or None,
            "metric": h.get("metric") or None,
            "condition": json.loads(h.get("when_condition") or "{}"),
            "lookback_window_seconds": (
                int(h["lookback_window_seconds"])
                if h.get("lookback_window_seconds")
                else None
            ),
        },
        "then": {
            "action_type": h.get("action_type"),
            "action_config": json.loads(h.get("action_config") or "{}"),
            "recipient_user_id_attr": h.get("recipient_user_id_attr") or None,
        },
        "max_triggers_per_user": (
            int(h["max_triggers_per_user"])
            if h.get("max_triggers_per_user")
            else None
        ),
        "description": h.get("description") or None,
    }


# ── Action executor (v2: enqueues high-level actions) ─────────────────────


# Map high-level action types → internal POST routes (consumed by the
# same pending-actions worker as ACTION_ROUTES — we just record the
# resolved URL so the worker doesn't have to know about both shapes).
ACTION_TYPE_ROUTES: dict[str, str] = {
    "issue_voucher": "/api/v1/brands/vouchers/grant",
    "award_xp": "/api/v1/progression/award/xp",
    "send_push": "/api/v1/notify/push",
    "fire_achievement": "/api/v1/primitives/achievement/{achievement_id}/progress",
    "trigger_webhook": "/api/v1/integrations/webhook/dispatch",
    "create_audience_membership": "/api/v1/audiences/membership/add",
}


def _build_action_record(
    rule: dict[str, Any],
    recipient_user_id: str,
    brand_id: str,
    actor_user_id: str,
) -> dict[str, Any]:
    """Compose the queued action payload for one (rule, recipient) pair."""
    action_type = rule.get("action_type") or ""
    config = json.loads(rule.get("action_config") or "{}")

    # ``fire_achievement`` resolves the {achievement_id} placeholder.
    route = ACTION_TYPE_ROUTES.get(action_type)
    if route and "{achievement_id}" in route:
        route = route.replace(
            "{achievement_id}", str(config.get("achievement_id", ""))
        )

    return {
        "action_id": uuid4().hex[:12],
        "rule_id": rule.get("id"),
        "action_type": action_type,
        "route": route,
        "config": config,
        "user_id": recipient_user_id,        # ← the *target* of the action
        "actor_user_id": actor_user_id,      # ← who triggered it
        "brand_id": brand_id,
        "enqueued_at": time.time(),
        "status": "pending",
    }


async def _execute_v2_action(
    r: aioredis.Redis,
    rule: dict[str, Any],
    actor_user_id: str,
) -> list[dict[str, Any]]:
    """Enqueue one action record per resolved recipient.

    The recipient list is computed via ``_resolve_recipients``; for
    rules without ``recipient_user_id_attr`` this collapses to a single
    record targeting the actor (the historic behaviour)."""
    brand_id = rule.get("brand_id") or ""
    recipients = await _resolve_recipients(
        r, actor_user_id, rule.get("recipient_user_id_attr") or None
    )

    records: list[dict[str, Any]] = []
    for recipient in recipients:
        record = _build_action_record(rule, recipient, brand_id, actor_user_id)
        await r.rpush(
            _k_pending_actions(brand_id, recipient), json.dumps(record)
        )
        records.append(record)
    return records


# ── Public hook: called by primitives.attribute_log on every write ───────


async def on_attribute_changed(
    r: aioredis.Redis,
    *,
    user_id: str,
    brand_id: str,
    key: str,
    old_value: Any,
    new_value: Any,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatch attribute-watch rules.

    Walks ``brand:{bid}:attr_rules:{key}`` → for each rule, decodes its
    flat hash, evaluates ``when.condition`` against ``(old, new)``, and
    on match expands recipients + enqueues the action.

    Returns a summary suitable for logging from the caller. Safe to
    call from any write path — failures are swallowed per-rule so a
    single bad rule cannot break the underlying attribute write.
    """
    meta = meta or {}
    rule_ids = await r.smembers(_k_attr_rules(brand_id, key))
    fired: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for rid in sorted(rule_ids or []):
        rule = await r.hgetall(_k_rule_v2(rid))
        if not rule:
            skipped.append({"rule_id": rid, "reason": "missing"})
            continue
        if rule.get("active") != "1":
            skipped.append({"rule_id": rid, "reason": "inactive"})
            continue
        if rule.get("when_type") != "attribute_changed":
            skipped.append({"rule_id": rid, "reason": "wrong_when_type"})
            continue

        # max-triggers-per-user gate
        max_per_user = rule.get("max_triggers_per_user") or ""
        if max_per_user:
            try:
                limit = int(max_per_user)
            except (TypeError, ValueError):
                limit = None
            if limit is not None:
                fired_so_far = await _firings_for_user(
                    brand_id, rid, user_id, r
                )
                if fired_so_far >= limit:
                    skipped.append({"rule_id": rid, "reason": "max_triggers"})
                    continue

        cond = json.loads(rule.get("when_condition") or "{}")
        if not _evaluate_attr_condition(cond, old_value, new_value):
            skipped.append({"rule_id": rid, "reason": "condition_false"})
            continue

        try:
            records = await _execute_v2_action(r, rule, actor_user_id=user_id)
        except Exception as e:  # noqa: BLE001
            logger.exception("attr_rule_action_failed rule=%s err=%s", rid, e)
            skipped.append({"rule_id": rid, "reason": f"action_error: {e}"})
            continue

        await r.incr(_k_firings(brand_id, rid, user_id))
        fired.append(
            {
                "rule_id": rid,
                "recipients": [rec["user_id"] for rec in records],
                "action_type": rule.get("action_type"),
            }
        )

    return {
        "user_id": user_id,
        "brand_id": brand_id,
        "key": key,
        "old_value": old_value,
        "new_value": new_value,
        "fired": fired,
        "skipped": skipped,
    }


# ── API: v2 rule CRUD ─────────────────────────────────────────────────────


@router.post("/rules/create")
async def create_rule_v2(
    body: RuleV2Create, r: aioredis.Redis = Depends(get_redis)
) -> dict[str, Any]:
    """Create a v2 rule (attribute-watch / recipient-indirection-capable).

    For ``when.type == "attribute_changed"``: the rule_id is added to
    the per-brand, per-attribute index so writes through
    ``primitives.attribute_log`` fan out to it.
    """
    rule_id = body.id or f"rule_{uuid4().hex[:12]}"
    when = body.when
    then = body.then

    if when.type == "attribute_changed" and not when.attribute_key:
        raise HTTPException(
            422, detail="attribute_changed rules require when.attribute_key"
        )
    if then.action_type not in ACTION_TYPES_HIGH_LEVEL:
        raise HTTPException(
            422,
            detail=(
                f"unknown action_type {then.action_type!r}; "
                f"expected one of {sorted(ACTION_TYPES_HIGH_LEVEL)}"
            ),
        )
    if then.action_type == "fire_achievement" and not then.action_config.get(
        "achievement_id"
    ):
        raise HTTPException(
            422, detail="fire_achievement requires action_config.achievement_id"
        )

    mapping = _rule_v2_to_hash(rule_id, body)
    await r.hset(_k_rule_v2(rule_id), mapping=mapping)
    await r.sadd(_k_brand_rules_v2(body.brand_id), rule_id)
    if when.type == "attribute_changed" and when.attribute_key:
        await r.sadd(_k_attr_rules(body.brand_id, when.attribute_key), rule_id)

    logger.info(
        "rule_v2_created brand=%s rule=%s when=%s key=%s action=%s recipient_attr=%s",
        body.brand_id,
        rule_id,
        when.type,
        when.attribute_key,
        then.action_type,
        then.recipient_user_id_attr,
    )
    return {"status": "ok", "rule_id": rule_id, "rule": _hash_to_rule_v2_view(mapping)}


@router.get("/rules/{rule_id}")
async def get_rule_v2(
    rule_id: str, r: aioredis.Redis = Depends(get_redis)
) -> dict[str, Any]:
    h = await r.hgetall(_k_rule_v2(rule_id))
    if not h:
        raise HTTPException(404, detail="rule not found")
    return _hash_to_rule_v2_view(h)


@router.delete("/rules/{rule_id}")
async def delete_rule_v2(
    rule_id: str, r: aioredis.Redis = Depends(get_redis)
) -> dict[str, Any]:
    h = await r.hgetall(_k_rule_v2(rule_id))
    if not h:
        raise HTTPException(404, detail="rule not found")
    brand_id = h.get("brand_id") or ""
    attr_key = h.get("attribute_key") or ""
    pipe = r.pipeline()
    pipe.delete(_k_rule_v2(rule_id))
    if brand_id:
        pipe.srem(_k_brand_rules_v2(brand_id), rule_id)
    if brand_id and attr_key:
        pipe.srem(_k_attr_rules(brand_id, attr_key), rule_id)
    await pipe.execute()
    return {"status": "ok", "rule_id": rule_id, "deleted": True}


@router.post("/rules/{rule_id}/test-resolution")
async def test_rule_resolution(
    rule_id: str,
    body: TestResolutionBody,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Inspect recipient resolution without firing anything.

    Useful for verifying that a child→parents indirection is wired up
    correctly *before* an exam-score attribute write actually happens.
    """
    h = await r.hgetall(_k_rule_v2(rule_id))
    if not h:
        raise HTTPException(404, detail="rule not found")
    recipient_attr = h.get("recipient_user_id_attr") or None
    resolved = await _resolve_recipients(r, body.actor_user_id, recipient_attr)
    return {
        "rule_id": rule_id,
        "actor": body.actor_user_id,
        "recipient_attr": recipient_attr,
        "resolved_recipients": resolved,
        "resolved_count": len(resolved),
    }


@router.post("/rules/{rule_id}/dry-run")
async def dry_run_rule_v2(
    rule_id: str,
    body: V2DryRunBody,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Evaluate a v2 rule without writing anything.

    For ``attribute_changed`` rules, the caller supplies the simulated
    ``old_value`` + ``new_value`` (and optionally a ``key`` override) in
    ``simulated_event``; we run the same condition-evaluator + recipient
    resolver the live path uses and report what would happen.

    Returns the canonical envelope::

        { would_fire, recipient_user_ids, action_preview,
          reasons_if_blocked }
    """
    h = await r.hgetall(_k_rule_v2(rule_id))
    if not h:
        raise HTTPException(404, detail="rule not found")

    reasons: list[str] = []
    if h.get("active") != "1":
        reasons.append("rule_inactive")

    when_type = h.get("when_type")
    sim = body.simulated_event or {}
    condition_passed = False

    if when_type == "attribute_changed":
        # Allow caller to override the watched key for what-if testing.
        sim_key = sim.get("key") or h.get("attribute_key") or ""
        expected_key = h.get("attribute_key") or ""
        if expected_key and sim_key != expected_key:
            reasons.append(
                f"attribute_key_mismatch: rule={expected_key} sim={sim_key}"
            )
        cond = json.loads(h.get("when_condition") or "{}")
        condition_passed = _evaluate_attr_condition(
            cond, sim.get("old_value"), sim.get("new_value")
        )
        if not condition_passed:
            reasons.append("condition_false")
    else:
        # Non-attribute-changed v2 rules aren't actively dispatched yet;
        # we still resolve recipients but flag that nothing would fire.
        reasons.append(f"when_type_not_dispatched: {when_type}")

    max_per_user = h.get("max_triggers_per_user") or ""
    if max_per_user:
        try:
            limit = int(max_per_user)
        except (TypeError, ValueError):
            limit = None
        if limit is not None:
            fired_so_far = await _firings_for_user(
                h.get("brand_id") or "", rule_id, body.actor_user_id, r
            )
            if fired_so_far >= limit:
                reasons.append("max_triggers_per_user_reached")

    recipient_attr = h.get("recipient_user_id_attr") or None
    recipients = await _resolve_recipients(
        r, body.actor_user_id, recipient_attr
    )
    if not recipients:
        reasons.append("no_recipients_resolved")

    would_fire = condition_passed and not reasons

    action_preview = {
        "action_type": h.get("action_type"),
        "config": json.loads(h.get("action_config") or "{}"),
        "route": ACTION_TYPE_ROUTES.get(h.get("action_type") or ""),
        "recipient_attr": recipient_attr,
        "per_recipient_records": [
            _build_action_record(h, rec, h.get("brand_id") or "", body.actor_user_id)
            for rec in recipients
        ],
    }

    return {
        "rule_id": rule_id,
        "would_fire": would_fire,
        "recipient_user_ids": recipients,
        "action_preview": action_preview,
        "reasons_if_blocked": reasons,
        "condition_passed": condition_passed,
    }
