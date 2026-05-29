"""A/B Testing Engine — split-test voucher/campaign/push/recipe configs.

Any subject in KiX (a voucher template, a campaign, a push template, a
recipe) can be wrapped in an *experiment* with two or more variants.
Users get a deterministic, sticky assignment based on a hash of their
``kid``; exposures / conversions / revenue are counted per variant; and
a two-proportion z-test computes statistical significance so the engine
can recommend a winner once ``min_sample_size`` is reached.

Concepts
--------
* **Experiment** — wrapper around a target ``(subject_type, subject_id)``.
* **Variant** — a config payload with an allocation weight (``0.0–1.0``).
* **Allocation strategies** — ``random``, ``hash_kid``, ``hash_brand``.
* **Goal metrics** — ``ctr``, ``cvr``, ``revenue_per_user``, ``retention_d7``.
* **Lifecycle** — ``draft → running → paused/concluded``.

Statistics
----------
The winner test is a vanilla two-proportion z-test using the pooled
standard error.  ``p_value`` is two-tailed via the error function.  We
also compute a 95 % Wald confidence interval per variant.  The engine
recommends ``ship_<winner>`` only when:

* both arms have ≥ ``min_sample_size`` exposures, **and**
* ``p_value ≤ (1 - confidence_threshold)``  (default 0.05), **and**
* the higher-mean variant is unambiguously better.

Otherwise it returns ``continue_testing``.

Redis schema
------------
``ab_exp:{exp_id}``                       HASH (config + status)
``ab_exp:{exp_id}:variants``              LIST of JSON variant entries
``ab_exp:{exp_id}:variant:{vid}:exposures``    COUNTER
``ab_exp:{exp_id}:variant:{vid}:conversions``  COUNTER
``ab_exp:{exp_id}:variant:{vid}:revenue_cents`` COUNTER
``ab_exp:{exp_id}:variant:{vid}:users``        SET of kids (for revenue_per_user)
``ab_exp:{exp_id}:assignments:{kid}``     STRING (variant_id, TTL 30d)
``brand:{bid}:ab_exps``                   SET of exp_ids
``ab_subject_index:{subject_type}:{subject_id}``  SET of exp_ids

Integration
-----------
``get_active_exp_for(subject_type, subject_id, r)``,
``assign_variant_internal(exp_id, kid, r)`` and
``record_event_internal(exp_id, kid, event_type, value, r)`` are exported
for in-process callers (voucher.issue, push_engine.send, campaigns.run).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random as _random
from typing import Any, Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field, field_validator

from app.api_standards import (
    error_response,
    list_response,
    mint_id,
    not_found,
    now_ts,
    validation_failed,
)
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ────────────────────────────────────────────────────────────

ASSIGNMENT_TTL_SECONDS = 30 * 86400  # 30 days — sticky assignments
DEFAULT_CONFIDENCE_THRESHOLD = 0.95
DEFAULT_MIN_SAMPLE_SIZE = 1000

SUBJECT_TYPES = ("voucher", "campaign", "push", "recipe")
ALLOCATIONS = ("random", "hash_kid", "hash_brand")
GOAL_METRICS = ("ctr", "cvr", "revenue_per_user", "retention_d7")
STATUSES = ("draft", "running", "paused", "concluded")

# Key templates
K_EXP = "ab_exp:{exp_id}"
K_EXP_VARIANTS = "ab_exp:{exp_id}:variants"
K_EXP_VAR_EXPOSURES = "ab_exp:{exp_id}:variant:{vid}:exposures"
K_EXP_VAR_CONVERSIONS = "ab_exp:{exp_id}:variant:{vid}:conversions"
K_EXP_VAR_REVENUE = "ab_exp:{exp_id}:variant:{vid}:revenue_cents"
K_EXP_VAR_USERS = "ab_exp:{exp_id}:variant:{vid}:users"
K_EXP_ASSIGN = "ab_exp:{exp_id}:assignments:{kid}"
K_BRAND_EXPS = "brand:{bid}:ab_exps"
K_SUBJECT_INDEX = "ab_subject_index:{subject_type}:{subject_id}"


# ── Pydantic models ──────────────────────────────────────────────────────


class VariantIn(BaseModel):
    """One arm of an experiment."""

    id: str = Field(..., min_length=1, max_length=32, description="e.g. 'A', 'B', 'control'")
    weight: float = Field(..., ge=0.0, le=1.0, description="Allocation share (0..1)")
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Config payload merged over the subject's defaults",
    )


class CreateExperimentIn(BaseModel):
    subject_type: Literal["voucher", "campaign", "push", "recipe"]
    subject_id: str = Field(..., min_length=1)
    variants: list[VariantIn] = Field(..., min_length=2, max_length=10)
    allocation: Literal["random", "hash_kid", "hash_brand"] = "hash_kid"
    goal_metric: Literal["ctr", "cvr", "revenue_per_user", "retention_d7"] = "cvr"
    min_sample_size: int = Field(DEFAULT_MIN_SAMPLE_SIZE, ge=10, le=10_000_000)
    confidence_threshold: float = Field(
        DEFAULT_CONFIDENCE_THRESHOLD, ge=0.5, le=0.999
    )
    brand_id: str | None = None
    name: str | None = Field(None, max_length=200)

    @field_validator("variants")
    @classmethod
    def _weights_sum_to_one(cls, v: list[VariantIn]) -> list[VariantIn]:
        total = sum(x.weight for x in v)
        if not math.isclose(total, 1.0, abs_tol=1e-3):
            raise ValueError(f"variant weights must sum to 1.0 (got {total:.4f})")
        ids = [x.id for x in v]
        if len(set(ids)) != len(ids):
            raise ValueError("variant ids must be unique")
        return v


class ConcludeIn(BaseModel):
    decision: Literal["auto", "manual"] = "auto"
    chosen_variant: str | None = None


class AssignIn(BaseModel):
    kid: str = Field(..., min_length=1)
    brand_id: str | None = None


class RecordEventIn(BaseModel):
    kid: str = Field(..., min_length=1)
    event_type: Literal["exposure", "conversion", "revenue"]
    value: float | None = Field(
        None,
        description=(
            "For event_type='revenue': amount in cents. Ignored otherwise."
        ),
    )


# ── Statistics ───────────────────────────────────────────────────────────


def two_proportion_z_test(
    success_a: int, total_a: int, success_b: int, total_b: int
) -> tuple[float, float]:
    """Two-proportion pooled z-test. Returns ``(z_score, p_value_two_tailed)``."""
    if total_a == 0 or total_b == 0:
        return 0.0, 1.0
    p_a = success_a / total_a
    p_b = success_b / total_b
    p_pool = (success_a + success_b) / (total_a + total_b)
    se = math.sqrt(p_pool * (1.0 - p_pool) * (1.0 / total_a + 1.0 / total_b))
    if se == 0.0:
        return 0.0, 1.0
    z = (p_b - p_a) / se
    # Two-tailed p-value via normal CDF using erf.
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(z) / math.sqrt(2.0))))
    # Clamp into [0,1] to defend against fp drift.
    if p < 0.0:
        p = 0.0
    elif p > 1.0:
        p = 1.0
    return z, p


def wald_ci(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wald confidence interval on a proportion."""
    if total == 0:
        return 0.0, 0.0
    p = successes / total
    half = z * math.sqrt(max(p * (1.0 - p), 0.0) / total)
    return max(0.0, p - half), min(1.0, p + half)


def _z_for_confidence(confidence: float) -> float:
    """Inverse normal at confidence/2 tail (for two-sided CI)."""
    # Cheap lookup — enough for common confidence levels.
    table = {
        0.80: 1.2816,
        0.90: 1.6449,
        0.95: 1.96,
        0.975: 2.2414,
        0.99: 2.5758,
        0.999: 3.2905,
    }
    # Find nearest key.
    best = min(table.keys(), key=lambda k: abs(k - confidence))
    return table[best]


# ── Assignment ───────────────────────────────────────────────────────────


def _assign_variant_pure(
    exp_id: str, kid: str, variants: list[dict], allocation: str
) -> dict:
    """Deterministic-when-possible variant assignment.

    ``hash_kid``   — md5(exp_id:kid) → stable per user
    ``hash_brand`` — md5(exp_id:brand_id) hashed via the kid as proxy
                     (caller injects the brand_id into the kid slot)
    ``random``     — non-deterministic; sticky only via Redis cache
    """
    if not variants:
        raise ValueError("variants must be non-empty")

    if allocation == "random":
        hash_unit = _random.random()
    else:
        digest = hashlib.md5(f"{exp_id}:{kid}".encode("utf-8")).hexdigest()
        hash_unit = int(digest[:8], 16) / 0xFFFFFFFF

    cumulative = 0.0
    for v in variants:
        cumulative += float(v["weight"])
        if hash_unit <= cumulative:
            return v
    return variants[-1]


# ── Storage helpers ──────────────────────────────────────────────────────


async def _load_experiment(
    exp_id: str, r: aioredis.Redis
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = await r.hgetall(K_EXP.format(exp_id=exp_id))
    if not raw:
        raise not_found("experiment", exp_id)
    config = {
        k.decode() if isinstance(k, bytes) else k:
        v.decode() if isinstance(v, bytes) else v
        for k, v in raw.items()
    }
    var_raw = await r.lrange(K_EXP_VARIANTS.format(exp_id=exp_id), 0, -1)
    variants: list[dict[str, Any]] = []
    for entry in var_raw:
        if isinstance(entry, bytes):
            entry = entry.decode()
        try:
            variants.append(json.loads(entry))
        except json.JSONDecodeError:
            continue
    return config, variants


async def _persist_experiment(
    exp_id: str,
    config: dict[str, Any],
    variants: list[dict[str, Any]],
    r: aioredis.Redis,
) -> None:
    # Only string-coerced primitives go into HASH.
    flat: dict[str, str] = {}
    for k, v in config.items():
        if isinstance(v, (dict, list)):
            flat[k] = json.dumps(v)
        elif v is None:
            flat[k] = ""
        else:
            flat[k] = str(v)
    await r.hset(K_EXP.format(exp_id=exp_id), mapping=flat)
    # Replace variants list.
    key = K_EXP_VARIANTS.format(exp_id=exp_id)
    await r.delete(key)
    for v in variants:
        await r.rpush(key, json.dumps(v))


# ── Per-variant stats ────────────────────────────────────────────────────


async def _variant_stats(
    exp_id: str,
    variant_id: str,
    confidence: float,
    r: aioredis.Redis,
) -> dict[str, Any]:
    exposures_raw = await r.get(
        K_EXP_VAR_EXPOSURES.format(exp_id=exp_id, vid=variant_id)
    )
    conversions_raw = await r.get(
        K_EXP_VAR_CONVERSIONS.format(exp_id=exp_id, vid=variant_id)
    )
    revenue_raw = await r.get(
        K_EXP_VAR_REVENUE.format(exp_id=exp_id, vid=variant_id)
    )
    unique_users = await r.scard(
        K_EXP_VAR_USERS.format(exp_id=exp_id, vid=variant_id)
    )

    exposures = int(exposures_raw) if exposures_raw else 0
    conversions = int(conversions_raw) if conversions_raw else 0
    revenue_cents = int(revenue_raw) if revenue_raw else 0

    conv_rate = (conversions / exposures) if exposures else 0.0
    z_for_ci = _z_for_confidence(confidence)
    ci_lower, ci_upper = wald_ci(conversions, exposures, z=z_for_ci)
    rev_per_user = (revenue_cents / unique_users) if unique_users else 0.0

    return {
        "id": variant_id,
        "exposures": exposures,
        "conversions": conversions,
        "conversion_rate": round(conv_rate, 6),
        "ci_lower": round(ci_lower, 6),
        "ci_upper": round(ci_upper, 6),
        "revenue_cents": revenue_cents,
        "unique_users": unique_users,
        "revenue_per_user_cents": round(rev_per_user, 4),
    }


def _winner_and_recommendation(
    stats: list[dict[str, Any]],
    min_sample_size: int,
    confidence_threshold: float,
    goal_metric: str,
) -> dict[str, Any]:
    """Decide winner across N variants.

    Strategy: pick the arm with the best mean on ``goal_metric``; then
    run the z-test of that arm vs the next-best.  Recommend ``ship_X``
    only if both arms reached ``min_sample_size`` and ``p_value`` is below
    ``1 - confidence_threshold``.
    """
    if len(stats) < 2:
        return {
            "winner": None,
            "lift": 0.0,
            "p_value": 1.0,
            "statistical_significance": 0.0,
            "recommendation": "continue_testing",
        }

    if goal_metric in ("cvr", "ctr"):
        score_key = "conversion_rate"
        success_key = "conversions"
        total_key = "exposures"
    elif goal_metric == "revenue_per_user":
        score_key = "revenue_per_user_cents"
        # For revenue we still want a directional p-value — fall back to
        # treating any-revenue as a "conversion" so the z-test stays well
        # defined.  Real production would use a t-test on per-user revenue.
        success_key = "conversions"
        total_key = "exposures"
    else:  # retention_d7 — treat conversions as "retained" count.
        score_key = "conversion_rate"
        success_key = "conversions"
        total_key = "exposures"

    ranked = sorted(stats, key=lambda s: s.get(score_key, 0.0), reverse=True)
    leader = ranked[0]
    runner_up = ranked[1]

    leader_total = leader.get(total_key, 0)
    leader_succ = leader.get(success_key, 0)
    runner_total = runner_up.get(total_key, 0)
    runner_succ = runner_up.get(success_key, 0)

    _z, p_value = two_proportion_z_test(
        runner_succ, runner_total, leader_succ, leader_total
    )

    leader_score = leader.get(score_key, 0.0)
    runner_score = runner_up.get(score_key, 0.0)
    lift = (
        (leader_score - runner_score) / runner_score
        if runner_score
        else 0.0
    )

    enough_data = (
        leader_total >= min_sample_size and runner_total >= min_sample_size
    )
    alpha = 1.0 - confidence_threshold
    significant = p_value <= alpha and leader_score > runner_score

    if enough_data and significant:
        recommendation = f"ship_{leader['id']}"
    else:
        recommendation = "continue_testing"

    return {
        "winner": leader["id"] if significant else None,
        "lift": round(lift, 6),
        "p_value": round(p_value, 6),
        "statistical_significance": round(1.0 - p_value, 6),
        "recommendation": recommendation,
    }


# ── Internal helpers (called by voucher / push / campaign) ───────────────


async def get_active_exp_for(
    subject_type: str,
    subject_id: str,
    r: aioredis.Redis,
) -> str | None:
    """Return the first *running* experiment id for a subject, if any."""
    key = K_SUBJECT_INDEX.format(
        subject_type=subject_type, subject_id=subject_id
    )
    exp_ids = await r.smembers(key)
    for eid_b in exp_ids:
        eid = eid_b.decode() if isinstance(eid_b, bytes) else eid_b
        status_raw = await r.hget(K_EXP.format(exp_id=eid), "status")
        status_str = (
            status_raw.decode() if isinstance(status_raw, bytes) else status_raw
        )
        if status_str == "running":
            return eid
    return None


async def assign_variant_internal(
    exp_id: str,
    kid: str,
    r: aioredis.Redis,
    brand_id: str | None = None,
) -> dict[str, Any] | None:
    """Deterministic, sticky assignment.

    Caches the assignment in Redis for ``ASSIGNMENT_TTL_SECONDS`` so
    re-entry returns the same variant.  Returns ``None`` if the experiment
    does not exist or is not running.
    """
    config, variants = await _load_experiment(exp_id, r)
    if config.get("status") != "running":
        return None
    if not variants:
        return None

    # Sticky cache lookup first.
    cache_key = K_EXP_ASSIGN.format(exp_id=exp_id, kid=kid)
    cached = await r.get(cache_key)
    if cached:
        vid = cached.decode() if isinstance(cached, bytes) else cached
        for v in variants:
            if v["id"] == vid:
                return v
        # Cache pointed at a stale variant id — fall through to re-assign.

    allocation = config.get("allocation", "hash_kid")
    hash_input = kid
    if allocation == "hash_brand" and brand_id:
        hash_input = brand_id
    variant = _assign_variant_pure(exp_id, hash_input, variants, allocation)

    await r.set(cache_key, variant["id"], ex=ASSIGNMENT_TTL_SECONDS)
    return variant


async def record_event_internal(
    exp_id: str,
    kid: str,
    event_type: str,
    value: float | None,
    r: aioredis.Redis,
) -> bool:
    """Record exposure / conversion / revenue for the kid's variant.

    Returns ``True`` if recorded, ``False`` if the kid has no assignment
    or the experiment is not running.
    """
    config_status = await r.hget(K_EXP.format(exp_id=exp_id), "status")
    if not config_status:
        return False
    status_str = (
        config_status.decode()
        if isinstance(config_status, bytes)
        else config_status
    )
    if status_str != "running":
        return False

    cached = await r.get(K_EXP_ASSIGN.format(exp_id=exp_id, kid=kid))
    if not cached:
        return False
    vid = cached.decode() if isinstance(cached, bytes) else cached

    if event_type == "exposure":
        await r.incr(K_EXP_VAR_EXPOSURES.format(exp_id=exp_id, vid=vid))
    elif event_type == "conversion":
        await r.incr(K_EXP_VAR_CONVERSIONS.format(exp_id=exp_id, vid=vid))
        await r.sadd(
            K_EXP_VAR_USERS.format(exp_id=exp_id, vid=vid), kid
        )
    elif event_type == "revenue":
        cents = int(round(value)) if value is not None else 0
        if cents > 0:
            await r.incrby(
                K_EXP_VAR_REVENUE.format(exp_id=exp_id, vid=vid), cents
            )
            await r.sadd(
                K_EXP_VAR_USERS.format(exp_id=exp_id, vid=vid), kid
            )
    else:
        return False
    return True


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("/create", status_code=status.HTTP_201_CREATED)
async def create_experiment(
    body: CreateExperimentIn,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Create an experiment in ``draft`` status."""
    exp_id = mint_id("exp")
    ts = now_ts()

    variants_serialized = [
        {"id": v.id, "weight": v.weight, "config": v.config}
        for v in body.variants
    ]

    config: dict[str, Any] = {
        "id": exp_id,
        "subject_type": body.subject_type,
        "subject_id": body.subject_id,
        "allocation": body.allocation,
        "goal_metric": body.goal_metric,
        "min_sample_size": body.min_sample_size,
        "confidence_threshold": body.confidence_threshold,
        "brand_id": body.brand_id or "",
        "name": body.name or "",
        "status": "draft",
        "created_at": ts,
        "updated_at": ts,
        "started_at": "",
        "concluded_at": "",
        "winner": "",
    }
    await _persist_experiment(exp_id, config, variants_serialized, r)

    # Index by subject so integration points can look up active exps cheaply.
    await r.sadd(
        K_SUBJECT_INDEX.format(
            subject_type=body.subject_type, subject_id=body.subject_id
        ),
        exp_id,
    )
    if body.brand_id:
        await r.sadd(K_BRAND_EXPS.format(bid=body.brand_id), exp_id)

    return {
        "id": exp_id,
        "subject_type": body.subject_type,
        "subject_id": body.subject_id,
        "status": "draft",
        "variants": variants_serialized,
        "allocation": body.allocation,
        "goal_metric": body.goal_metric,
        "min_sample_size": body.min_sample_size,
        "confidence_threshold": body.confidence_threshold,
        "created_at": ts,
    }


async def _transition_status(
    exp_id: str,
    expected_from: tuple[str, ...],
    to: str,
    r: aioredis.Redis,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config, variants = await _load_experiment(exp_id, r)
    current = config.get("status")
    if current not in expected_from:
        raise error_response(
            409,
            "invalid_status_transition",
            f"cannot move from {current} to {to}",
            current_status=current,
            target_status=to,
            allowed_from=list(expected_from),
        )
    config["status"] = to
    config["updated_at"] = now_ts()
    if extra:
        config.update(extra)
    await _persist_experiment(exp_id, config, variants, r)
    return config


@router.post("/{exp_id}/start")
async def start_experiment(
    exp_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    cfg = await _transition_status(
        exp_id, ("draft", "paused"), "running", r,
        extra={"started_at": now_ts()},
    )
    return {"id": exp_id, "status": cfg["status"], "started_at": cfg.get("started_at")}


@router.post("/{exp_id}/pause")
async def pause_experiment(
    exp_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    cfg = await _transition_status(exp_id, ("running",), "paused", r)
    return {"id": exp_id, "status": cfg["status"]}


@router.post("/{exp_id}/conclude")
async def conclude_experiment(
    exp_id: str,
    body: ConcludeIn,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    config, variants = await _load_experiment(exp_id, r)
    if config.get("status") in ("concluded",):
        raise error_response(
            409,
            "invalid_status_transition",
            "experiment already concluded",
            current_status=config.get("status"),
        )

    chosen: str | None
    if body.decision == "manual":
        if not body.chosen_variant:
            raise validation_failed(
                "chosen_variant", "required when decision='manual'"
            )
        valid_ids = {v["id"] for v in variants}
        if body.chosen_variant not in valid_ids:
            raise validation_failed(
                "chosen_variant",
                f"unknown variant id; expected one of {sorted(valid_ids)}",
            )
        chosen = body.chosen_variant
        verdict: dict[str, Any] = {
            "winner": chosen,
            "lift": 0.0,
            "p_value": 0.0,
            "statistical_significance": 1.0,
            "recommendation": f"ship_{chosen}",
        }
    else:
        confidence = float(config.get("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD))
        min_sample = int(config.get("min_sample_size", DEFAULT_MIN_SAMPLE_SIZE))
        stats = [
            await _variant_stats(exp_id, v["id"], confidence, r)
            for v in variants
        ]
        verdict = _winner_and_recommendation(
            stats,
            min_sample_size=min_sample,
            confidence_threshold=confidence,
            goal_metric=config.get("goal_metric", "cvr"),
        )
        chosen = verdict.get("winner")

    cfg = await _transition_status(
        exp_id, ("running", "paused", "draft"), "concluded", r,
        extra={
            "concluded_at": now_ts(),
            "winner": chosen or "",
        },
    )
    return {
        "id": exp_id,
        "status": cfg["status"],
        "decision": body.decision,
        "winner": chosen,
        "verdict": verdict,
    }


@router.post("/{exp_id}/assign")
async def assign_endpoint(
    exp_id: str,
    body: AssignIn,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return ``{variant_id, config}`` for this user (sticky)."""
    # Ensure experiment exists for proper 404.
    await _load_experiment(exp_id, r)
    variant = await assign_variant_internal(
        exp_id, body.kid, r, brand_id=body.brand_id
    )
    if variant is None:
        raise error_response(
            409,
            "experiment_not_running",
            "assignment requires experiment in 'running' state",
        )
    return {
        "exp_id": exp_id,
        "kid": body.kid,
        "variant_id": variant["id"],
        "config": variant.get("config", {}),
    }


@router.post("/{exp_id}/record-event")
async def record_event_endpoint(
    exp_id: str,
    body: RecordEventIn,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    # Ensure experiment exists.
    await _load_experiment(exp_id, r)
    recorded = await record_event_internal(
        exp_id, body.kid, body.event_type, body.value, r
    )
    if not recorded:
        # Try to give the caller a useful reason.
        status_raw = await r.hget(K_EXP.format(exp_id=exp_id), "status")
        status_str = (
            status_raw.decode() if isinstance(status_raw, bytes) else status_raw
        )
        if status_str != "running":
            raise error_response(
                409,
                "experiment_not_running",
                "events are only accepted while running",
                current_status=status_str,
            )
        raise error_response(
            409,
            "no_assignment",
            "kid has no sticky assignment; call /assign first",
            kid=body.kid,
        )
    return {"recorded": True, "exp_id": exp_id, "event_type": body.event_type}


@router.get("/{exp_id}/results")
async def get_results(
    exp_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    config, variants = await _load_experiment(exp_id, r)
    confidence = float(
        config.get("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD)
    )
    min_sample = int(config.get("min_sample_size", DEFAULT_MIN_SAMPLE_SIZE))
    goal = config.get("goal_metric", "cvr")

    stats = [
        await _variant_stats(exp_id, v["id"], confidence, r)
        for v in variants
    ]
    verdict = _winner_and_recommendation(
        stats,
        min_sample_size=min_sample,
        confidence_threshold=confidence,
        goal_metric=goal,
    )

    return {
        "exp_id": exp_id,
        "subject_type": config.get("subject_type"),
        "subject_id": config.get("subject_id"),
        "status": config.get("status"),
        "goal_metric": goal,
        "min_sample_size": min_sample,
        "confidence_threshold": confidence,
        "variants": stats,
        **verdict,
    }


@router.get("/{exp_id}")
async def get_experiment(
    exp_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    config, variants = await _load_experiment(exp_id, r)
    return {
        "id": exp_id,
        "subject_type": config.get("subject_type"),
        "subject_id": config.get("subject_id"),
        "status": config.get("status"),
        "allocation": config.get("allocation"),
        "goal_metric": config.get("goal_metric"),
        "min_sample_size": int(config.get("min_sample_size", DEFAULT_MIN_SAMPLE_SIZE)),
        "confidence_threshold": float(
            config.get("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD)
        ),
        "brand_id": config.get("brand_id") or None,
        "name": config.get("name") or None,
        "created_at": int(config.get("created_at") or 0),
        "updated_at": int(config.get("updated_at") or 0),
        "started_at": int(config["started_at"]) if config.get("started_at") else None,
        "concluded_at": int(config["concluded_at"]) if config.get("concluded_at") else None,
        "winner": config.get("winner") or None,
        "variants": variants,
    }


@router.get("/brand/{bid}")
async def list_brand_experiments(
    bid: str,
    limit: int = 50,
    offset: int = 0,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    exp_ids_raw = await r.smembers(K_BRAND_EXPS.format(bid=bid))
    exp_ids = sorted(
        e.decode() if isinstance(e, bytes) else e for e in exp_ids_raw
    )
    total = len(exp_ids)
    page_ids = exp_ids[offset : offset + limit]

    items: list[dict[str, Any]] = []
    for eid in page_ids:
        raw = await r.hgetall(K_EXP.format(exp_id=eid))
        if not raw:
            continue
        cfg = {
            (k.decode() if isinstance(k, bytes) else k):
            (v.decode() if isinstance(v, bytes) else v)
            for k, v in raw.items()
        }
        items.append(
            {
                "id": eid,
                "subject_type": cfg.get("subject_type"),
                "subject_id": cfg.get("subject_id"),
                "status": cfg.get("status"),
                "goal_metric": cfg.get("goal_metric"),
                "winner": cfg.get("winner") or None,
                "created_at": int(cfg.get("created_at") or 0),
            }
        )
    return list_response(items, total=total, limit=limit, offset=offset)
