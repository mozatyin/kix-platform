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
