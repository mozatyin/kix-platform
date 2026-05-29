"""Dynamic Pricing rules engine.

Per-listing / per-SKU rule sets that adjust a base price based on time of
day, demand, inventory levels, or arbitrary context flags. The /quote
endpoint walks the rule list, fires matching rules, and returns the final
multiplier-adjusted price plus a trace of which rules fired.

Triggers (extensible):
  * ``time``           — wall-clock UTC window match (``hours``, ``days_of_week``).
  * ``peak_hour``      — alias for ``time`` with default 18-21 weekdays.
  * ``demand``         — ``demand_index`` ≥ threshold (caller-supplied).
  * ``inventory``      — inventory_units ≤ absolute threshold.
  * ``low_inventory``  — inventory_pct ≤ threshold_pct.
  * ``flag``           — context boolean flag (rain, surge, holiday, ...).

Multipliers compose multiplicatively in the order rules appear, capped at
``MAX_TOTAL_MULTIPLIER`` to avoid runaway pricing. Each rule may also set
``priority`` (default 0); rules are evaluated in priority desc, then index
order.

Redis schema
------------
    pricing:rule:{rule_id}                HASH (rule payload as JSON)
    brand:{bid}:pricing:rules             SET of rule_ids
    pricing:by_listing:{bid}:{sku}        SET of rule_ids   (lookup index)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


SUPPORTED_TRIGGERS = {
    "time",
    "peak_hour",
    "demand",
    "inventory",
    "low_inventory",
    "flag",
}

# Anti-runaway: a single quote cannot exceed 10× nor fall below 0.1× base.
MAX_TOTAL_MULTIPLIER = 10.0
MIN_TOTAL_MULTIPLIER = 0.1
MIN_RULE_MULTIPLIER = 0.01
MAX_RULE_MULTIPLIER = 100.0


# ── Key helpers ──────────────────────────────────────────────────────────
def _k_rule(rule_id: str) -> str:
    return f"pricing:rule:{rule_id}"


def _k_brand_rules(bid: str) -> str:
    return f"brand:{bid}:pricing:rules"


def _k_listing_rules(bid: str, sku: str) -> str:
    return f"pricing:by_listing:{bid}:{sku}"


# ── Pydantic models ──────────────────────────────────────────────────────
class PricingRule(BaseModel):
    """One leg of a rule set.

    ``trigger`` selects the matcher; ``condition`` is a free-form dict whose
    schema depends on the trigger. ``multiplier`` is multiplicative
    (1.0 = no-op, 1.3 = +30% surge, 0.8 = -20% discount).
    """
    trigger: Literal[
        "time", "peak_hour", "demand",
        "inventory", "low_inventory", "flag",
    ]
    condition: dict[str, Any] = Field(default_factory=dict)
    multiplier: float = Field(..., ge=MIN_RULE_MULTIPLIER, le=MAX_RULE_MULTIPLIER)
    priority: int = 0
    label: str | None = Field(default=None, max_length=64)


class ConfigureRulesRequest(BaseModel):
    brand_id: str = Field(..., min_length=1, max_length=128)
    sku_or_listing_id: str = Field(..., min_length=1, max_length=128)
    rules: list[PricingRule] = Field(..., min_length=1, max_length=64)

    @field_validator("rules")
    @classmethod
    def _has_at_least_one(cls, v: list[PricingRule]) -> list[PricingRule]:
        if not v:
            raise ValueError("at least one rule required")
        return v


class RuleSetRecord(BaseModel):
    rule_id: str
    brand_id: str
    sku_or_listing_id: str
    rules: list[PricingRule]
    created_at: float
    updated_at: float


class ConfigureRulesResponse(BaseModel):
    rule_id: str
    brand_id: str
    sku_or_listing_id: str
    rules: list[PricingRule]


class QuoteRequest(BaseModel):
    brand_id: str = Field(..., min_length=1, max_length=128)
    sku_or_listing_id: str = Field(..., min_length=1, max_length=128)
    base_price_cents: int = Field(..., gt=0, le=1_000_000_000)
    context: dict[str, Any] = Field(default_factory=dict)


class RuleFiringTrace(BaseModel):
    rule_id: str
    trigger: str
    multiplier: float
    label: str | None
    matched_condition: dict[str, Any]


class QuoteResponse(BaseModel):
    brand_id: str
    sku_or_listing_id: str
    base_price_cents: int
    quoted_price_cents: int
    multiplier_applied: float
    rules_fired: list[RuleFiringTrace]
    clamped: bool


# ── Internal helpers ─────────────────────────────────────────────────────
def _serialize_rules(rules: list[PricingRule]) -> str:
    return json.dumps([r.model_dump() for r in rules], ensure_ascii=False)


def _deserialize_rules(blob: str) -> list[PricingRule]:
    try:
        raw = json.loads(blob)
    except (TypeError, ValueError):
        return []
    out: list[PricingRule] = []
    for item in raw:
        try:
            out.append(PricingRule(**item))
        except Exception:
            continue
    return out


def _now() -> float:
    return time.time()


# ── Trigger matchers ─────────────────────────────────────────────────────
def _match_time(
    cond: dict[str, Any], ctx: dict[str, Any]
) -> bool:
    """Match ``hours`` (list[int]) and optional ``days_of_week`` (0=Mon)."""
    ts = ctx.get("time")
    if ts is None:
        ts = _now()
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return False

    hours = cond.get("hours")
    if hours:
        if dt.hour not in hours:
            return False
    days = cond.get("days_of_week")
    if days:
        if dt.weekday() not in days:
            return False
    return True


def _match_peak_hour(cond: dict[str, Any], ctx: dict[str, Any]) -> bool:
    """Peak hour = ``time`` with default 18-21 hour window."""
    enriched = dict(cond)
    enriched.setdefault("hours", [18, 19, 20, 21])
    return _match_time(enriched, ctx)


def _match_demand(cond: dict[str, Any], ctx: dict[str, Any]) -> bool:
    threshold = cond.get("threshold")
    if threshold is None:
        return False
    try:
        demand = float(ctx.get("demand_index", 0))
    except (TypeError, ValueError):
        return False
    return demand >= float(threshold)


def _match_inventory(cond: dict[str, Any], ctx: dict[str, Any]) -> bool:
    threshold = cond.get("threshold_units")
    if threshold is None:
        return False
    units = ctx.get("inventory_units")
    if units is None:
        return False
    try:
        return float(units) <= float(threshold)
    except (TypeError, ValueError):
        return False


def _match_low_inventory(cond: dict[str, Any], ctx: dict[str, Any]) -> bool:
    threshold_pct = cond.get("threshold_pct")
    if threshold_pct is None:
        return False
    pct = ctx.get("inventory_pct")
    if pct is None:
        # Derive from supply/total if provided.
        supply = ctx.get("supply") or ctx.get("inventory_units")
        total = ctx.get("inventory_total") or ctx.get("capacity")
        if supply is None or total in (None, 0):
            return False
        try:
            pct = float(supply) / float(total)
        except (TypeError, ValueError, ZeroDivisionError):
            return False
    try:
        return float(pct) <= float(threshold_pct)
    except (TypeError, ValueError):
        return False


def _match_flag(cond: dict[str, Any], ctx: dict[str, Any]) -> bool:
    name = cond.get("name")
    expected = cond.get("equals", True)
    if not name:
        return False
    return ctx.get(name) == expected


_MATCHERS = {
    "time": _match_time,
    "peak_hour": _match_peak_hour,
    "demand": _match_demand,
    "inventory": _match_inventory,
    "low_inventory": _match_low_inventory,
    "flag": _match_flag,
}


def _evaluate(
    rules: list[PricingRule], ctx: dict[str, Any]
) -> tuple[float, list[tuple[PricingRule, dict]]]:
    """Walk rules in priority desc order. Returns (combined_multiplier, fired)."""
    ordered = sorted(
        enumerate(rules),
        key=lambda kv: (-kv[1].priority, kv[0]),
    )
    combined = 1.0
    fired: list[tuple[PricingRule, dict]] = []
    for _, rule in ordered:
        matcher = _MATCHERS.get(rule.trigger)
        if matcher is None:
            continue
        try:
            ok = matcher(rule.condition, ctx)
        except Exception as exc:
            logger.warning(
                "pricing matcher error trigger=%s err=%s", rule.trigger, exc
            )
            continue
        if ok:
            combined *= float(rule.multiplier)
            fired.append((rule, rule.condition))
    return combined, fired


# ── POST /rule/configure ─────────────────────────────────────────────────
@router.post("/rule/configure", response_model=ConfigureRulesResponse)
async def configure_rule(
    body: ConfigureRulesRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> ConfigureRulesResponse:
    """Create or replace the pricing rule set for one SKU/listing.

    Replaces any prior rule_id bound to the same (brand_id, sku) — we treat
    the (brand, sku) pair as the natural primary key. The returned
    ``rule_id`` is stable across replacements.
    """
    # Idempotent: reuse existing rule_id for this (brand, sku) pair.
    existing_ids = await r.smembers(_k_listing_rules(body.brand_id, body.sku_or_listing_id))
    rule_id = next(iter(existing_ids), None) or uuid4().hex
    now = _now()

    blob = _serialize_rules(body.rules)
    pipe = r.pipeline()
    pipe.hset(
        _k_rule(rule_id),
        mapping={
            "rule_id": rule_id,
            "brand_id": body.brand_id,
            "sku_or_listing_id": body.sku_or_listing_id,
            "rules": blob,
            "created_at": now,
            "updated_at": now,
        },
    )
    pipe.sadd(_k_brand_rules(body.brand_id), rule_id)
    pipe.sadd(_k_listing_rules(body.brand_id, body.sku_or_listing_id), rule_id)
    await pipe.execute()

    logger.info(
        "pricing_rule_configured rule_id=%s brand=%s sku=%s rules=%d",
        rule_id, body.brand_id, body.sku_or_listing_id, len(body.rules),
    )
    return ConfigureRulesResponse(
        rule_id=rule_id,
        brand_id=body.brand_id,
        sku_or_listing_id=body.sku_or_listing_id,
        rules=body.rules,
    )


# ── POST /quote ──────────────────────────────────────────────────────────
@router.post("/quote", response_model=QuoteResponse)
async def quote(
    body: QuoteRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> QuoteResponse:
    """Resolve the price for one (brand, sku) given runtime context.

    If no rules are bound to the listing, returns the base price unchanged.
    Combined multiplier is clamped to [MIN_TOTAL_MULTIPLIER,
    MAX_TOTAL_MULTIPLIER] to keep accidents survivable.
    """
    rule_ids = await r.smembers(
        _k_listing_rules(body.brand_id, body.sku_or_listing_id)
    )
    if not rule_ids:
        return QuoteResponse(
            brand_id=body.brand_id,
            sku_or_listing_id=body.sku_or_listing_id,
            base_price_cents=body.base_price_cents,
            quoted_price_cents=body.base_price_cents,
            multiplier_applied=1.0,
            rules_fired=[],
            clamped=False,
        )

    # Merge all rule sets bound to this listing.
    all_rules: list[PricingRule] = []
    rule_id_for_rule: dict[int, str] = {}
    for rid in rule_ids:
        raw = await r.hgetall(_k_rule(rid))
        if not raw:
            continue
        rules = _deserialize_rules(raw.get("rules") or "[]")
        for rule in rules:
            rule_id_for_rule[id(rule)] = rid
            all_rules.append(rule)

    raw_multiplier, fired = _evaluate(all_rules, body.context)
    clamped = False
    multiplier = raw_multiplier
    if multiplier > MAX_TOTAL_MULTIPLIER:
        multiplier = MAX_TOTAL_MULTIPLIER
        clamped = True
    elif multiplier < MIN_TOTAL_MULTIPLIER:
        multiplier = MIN_TOTAL_MULTIPLIER
        clamped = True

    quoted = int(round(body.base_price_cents * multiplier))
    if quoted < 0:
        quoted = 0

    traces = [
        RuleFiringTrace(
            rule_id=rule_id_for_rule.get(id(rule), ""),
            trigger=rule.trigger,
            multiplier=rule.multiplier,
            label=rule.label,
            matched_condition=cond,
        )
        for rule, cond in fired
    ]

    logger.info(
        "pricing_quote brand=%s sku=%s base=%s quoted=%s mult=%.3f fired=%d",
        body.brand_id, body.sku_or_listing_id,
        body.base_price_cents, quoted, multiplier, len(fired),
    )
    return QuoteResponse(
        brand_id=body.brand_id,
        sku_or_listing_id=body.sku_or_listing_id,
        base_price_cents=body.base_price_cents,
        quoted_price_cents=quoted,
        multiplier_applied=multiplier,
        rules_fired=traces,
        clamped=clamped,
    )


# ── GET /brand/{brand_id}/rules ──────────────────────────────────────────
@router.get("/brand/{brand_id}/rules", response_model=list[RuleSetRecord])
async def list_brand_rules(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> list[RuleSetRecord]:
    rule_ids = await r.smembers(_k_brand_rules(brand_id))
    out: list[RuleSetRecord] = []
    for rid in rule_ids:
        raw = await r.hgetall(_k_rule(rid))
        if not raw:
            continue
        out.append(
            RuleSetRecord(
                rule_id=rid,
                brand_id=raw.get("brand_id") or brand_id,
                sku_or_listing_id=raw.get("sku_or_listing_id") or "",
                rules=_deserialize_rules(raw.get("rules") or "[]"),
                created_at=float(raw.get("created_at") or 0.0),
                updated_at=float(raw.get("updated_at") or 0.0),
            )
        )
    out.sort(key=lambda r_: r_.updated_at, reverse=True)
    return out


# ── DELETE /rule/{rule_id} ───────────────────────────────────────────────
@router.delete("/rule/{rule_id}")
async def delete_rule(
    rule_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    raw = await r.hgetall(_k_rule(rule_id))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "rule_not_found", "rule_id": rule_id},
        )
    brand_id = raw.get("brand_id") or ""
    sku = raw.get("sku_or_listing_id") or ""

    pipe = r.pipeline()
    pipe.delete(_k_rule(rule_id))
    if brand_id:
        pipe.srem(_k_brand_rules(brand_id), rule_id)
    if brand_id and sku:
        pipe.srem(_k_listing_rules(brand_id, sku), rule_id)
    await pipe.execute()

    logger.info("pricing_rule_deleted rule_id=%s brand=%s sku=%s",
                rule_id, brand_id, sku)
    return {"ok": True, "rule_id": rule_id}
