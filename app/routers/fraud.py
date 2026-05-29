"""Fraud / AML / Trust Score / Incident router.

Cross-brand compliance + safety subsystem. Five sibling brands all need a
shared spine here because the failure modes are isomorphic even though the
surface verticals differ:

    laotian (sharing economy)  → vandalism / asset damage
    laohu   (C2C marketplace)  → scams / fake listings / chargebacks
    laojia  (logistics)        → 虚假签收 / fake delivery proof
    laozheng(fintech)          → AML / SAR / large-amount + velocity
    laocai  (hospital)         → medical incident report / abuse

Three intertwined concerns:

    Incidents   anything needing manual review or escalation
    TrustScore  per-kid running risk score (0..100, higher = safer)
    AML         financial regulatory reports + blocklist (SAR equivalents)

Velocity / anomaly counters are the glue: a velocity spike feeds the trust
score and can trigger a synthetic incident automatically.

Redis schema (mirrors the contract in the spec):

    incident:{iid}                          HASH
    brand:{bid}:incidents                   ZSET (score=opened_at)
    incident:{iid}:timeline                 LIST
    user:{uid}:incidents:as_actor           SET
    user:{uid}:incidents:as_target          SET

    user:{uid}:trust_score                  HASH
    user:{uid}:trust_score:history          LIST
    user:{uid}:trust_signals                HASH (raw counters)

    aml:report:{rid}                        HASH
    aml:reports                             ZSET (score=opened_at)
    aml:blocklist                           SET

    fraud:velocity:{uid}:{action}:{hour}    counter, EX 7200
    fraud:anomalies                         LIST (capped)

External hooks exported at module level so sibling routers (disputes,
payouts, wallet, listings, p2p) can integrate without HTTP round-trips:

    check_trust_internal(r, user_id, action, threshold) -> (bool, int, list)
    record_incident_internal(r, **kwargs)               -> incident_id
    update_trust_signal(r, user_id, signal_name, delta) -> new_score
"""

from __future__ import annotations

import json
import logging
import math
import time
from typing import Any, Literal
from uuid import uuid4

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ────────────────────────────────────────────────────────────
INCIDENT_TYPES = {
    "vandalism",
    "fake_delivery",
    "scam",
    "chargeback",
    "identity_theft",
    "abuse",
    "medical_incident",
    "other",
}

SEVERITIES = ("low", "medium", "high", "critical")
SEVERITY_WEIGHT = {"low": 1, "medium": 3, "high": 6, "critical": 10}

INCIDENT_OPEN_STATES = {"open", "under_review"}
INCIDENT_TERMINAL_STATES = {
    "resolved_confirmed_fraud",
    "resolved_false_positive",
    "resolved_no_action",
    "auto_resolved",
}

AML_FLAG_TYPES = {
    "large_amount",
    "velocity",
    "unusual_pattern",
    "counterparty_blocklist",
    "structuring",
}

AML_STATES = {"open", "under_review", "escalated", "cleared", "filed"}

TRUST_DEFAULT_BASE = 50
TRUST_MIN = 0
TRUST_MAX = 100
TRUST_HISTORY_MAX = 200
TIMELINE_MAX = 200
ANOMALY_LOG_MAX = 500

# Threshold table for the trust-gate fast path. Other routers may pass an
# explicit threshold to override.
TRUST_THRESHOLDS: dict[str, int] = {
    "create_listing": 25,
    "high_value_transaction": 60,
    "send_money": 40,
    "post_review": 20,
    "withdraw_funds": 55,
    "open_dispute": 30,
}

# Velocity defaults — per-action ceiling within a rolling window.
VELOCITY_THRESHOLDS: dict[str, int] = {
    "login": 20,
    "create_listing": 10,
    "send_money": 5,
    "high_value_transaction": 3,
    "report_incident": 8,
    "default": 30,
}
VELOCITY_DEFAULT_WINDOW = 3600  # seconds

# AML auto-escalation thresholds (cents). Aligned with common CTR/SAR limits
# but configurable per-deployment via Redis policy hash in future iters.
AML_LARGE_AMOUNT_CENTS = 1_000_000  # $10k equivalent
AML_STRUCTURING_THRESHOLD_CENTS = 900_000

MAX_NOTE_LEN = 2048
MAX_URL_PER_INCIDENT = 10
MAX_LIST_LIMIT = 500


# ── Key helpers ──────────────────────────────────────────────────────────
def _k_incident(iid: str) -> str:
    return f"incident:{iid}"


def _k_incident_timeline(iid: str) -> str:
    return f"incident:{iid}:timeline"


def _k_brand_incidents(bid: str) -> str:
    return f"brand:{bid}:incidents"


def _k_user_actor(uid: str) -> str:
    return f"user:{uid}:incidents:as_actor"


def _k_user_target(uid: str) -> str:
    return f"user:{uid}:incidents:as_target"


def _k_trust(uid: str) -> str:
    return f"user:{uid}:trust_score"


def _k_trust_history(uid: str) -> str:
    return f"user:{uid}:trust_score:history"


def _k_trust_signals(uid: str) -> str:
    return f"user:{uid}:trust_signals"


def _k_brand_trust_index(bid: str) -> str:
    return f"brand:{bid}:trust_index"


def _k_aml_report(rid: str) -> str:
    return f"aml:report:{rid}"


def _k_velocity(uid: str, action: str, hour_bucket: int) -> str:
    return f"fraud:velocity:{uid}:{action}:{hour_bucket}"


_K_AML_REPORTS = "aml:reports"
_K_AML_BLOCKLIST = "aml:blocklist"
_K_ANOMALIES = "fraud:anomalies"


# ── Pydantic models ──────────────────────────────────────────────────────
class Evidence(BaseModel):
    description: str = Field(..., min_length=1, max_length=MAX_NOTE_LEN)
    urls: list[str] = Field(default_factory=list, max_length=MAX_URL_PER_INCIDENT)
    severity: Literal["low", "medium", "high", "critical"] = "medium"


class IncidentReportRequest(BaseModel):
    brand_id: str = Field(..., min_length=1, max_length=128)
    incident_type: Literal[
        "vandalism",
        "fake_delivery",
        "scam",
        "chargeback",
        "identity_theft",
        "abuse",
        "medical_incident",
        "other",
    ]
    actor_user_id: str | None = None
    target_user_id: str | None = None
    related_resource_id: str | None = None
    evidence: Evidence
    reporter_user_id: str = Field(..., min_length=1, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _at_least_one_party(self) -> "IncidentReportRequest":
        if not any([self.actor_user_id, self.target_user_id, self.related_resource_id]):
            raise ValueError(
                "one of actor_user_id, target_user_id, related_resource_id required"
            )
        return self


class IncidentReportResponse(BaseModel):
    incident_id: str
    status: str
    auto_resolved: bool = False
    actor_trust_score: int | None = None
    target_trust_score: int | None = None


class IncidentDetail(BaseModel):
    incident_id: str
    brand_id: str
    incident_type: str
    actor_user_id: str | None
    target_user_id: str | None
    related_resource_id: str | None
    severity: str
    status: str
    evidence_description: str
    evidence_urls: list[str]
    reporter_user_id: str
    opened_at: float
    resolved_at: float | None
    decision: str | None
    notes: str | None
    timeline: list[dict[str, Any]]
    metadata: dict[str, Any]


class IncidentSummary(BaseModel):
    incident_id: str
    brand_id: str
    incident_type: str
    severity: str
    status: str
    actor_user_id: str | None
    target_user_id: str | None
    opened_at: float


class IncidentResolveRequest(BaseModel):
    admin_token: str = Field(..., min_length=1, max_length=256)
    decision: Literal["confirmed_fraud", "false_positive", "resolved_no_action"]
    impact_on_trust: int | None = Field(None, ge=-50, le=50)
    notes: str = Field(..., min_length=1, max_length=MAX_NOTE_LEN)


class TrustFactor(BaseModel):
    factor: str
    weight: float
    contribution: float


class TrustScoreResponse(BaseModel):
    user_id: str
    score: int
    factors: list[TrustFactor]
    history: list[dict[str, Any]]
    flags: list[str]
    last_updated: float


class TrustAdjustRequest(BaseModel):
    delta: int = Field(..., ge=-100, le=100)
    reason: str = Field(..., min_length=1, max_length=MAX_NOTE_LEN)
    evidence: str | None = Field(None, max_length=MAX_NOTE_LEN)


class TrustGateRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    action: str = Field(..., min_length=1, max_length=64)
    threshold: int | None = Field(None, ge=0, le=100)


class TrustGateResponse(BaseModel):
    allowed: bool
    score: int
    required_threshold: int
    reasons: list[str]


class TrustDistributionResponse(BaseModel):
    brand_id: str
    total_users: int
    buckets: dict[str, int]
    avg_score: float


class AMLReportRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    transaction_id: str | None = None
    amount_cents: int = Field(..., ge=0, le=10**12)
    currency: str = Field(..., min_length=3, max_length=8)
    flag_type: Literal[
        "large_amount",
        "velocity",
        "unusual_pattern",
        "counterparty_blocklist",
        "structuring",
    ]
    evidence: str = Field(..., min_length=1, max_length=MAX_NOTE_LEN)


class AMLReportResponse(BaseModel):
    aml_report_id: str
    escalated: bool
    status: str


class AMLReportDetail(BaseModel):
    aml_report_id: str
    user_id: str
    transaction_id: str | None
    amount_cents: int
    currency: str
    flag_type: str
    status: str
    escalated: bool
    evidence: str
    opened_at: float


class AMLBlocklistRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=MAX_NOTE_LEN)
    source: Literal["regulatory", "internal", "law_enforcement"]


class VelocityCheckRequest(BaseModel):
    user_id: str | None = None
    device_fp: str | None = None
    action_type: str = Field(..., min_length=1, max_length=64)
    window_seconds: int = Field(VELOCITY_DEFAULT_WINDOW, ge=60, le=86400)

    @model_validator(mode="after")
    def _need_identifier(self) -> "VelocityCheckRequest":
        if not (self.user_id or self.device_fp):
            raise ValueError("user_id or device_fp required")
        return self


class VelocityCheckResponse(BaseModel):
    count_in_window: int
    threshold: int
    anomaly: bool
    action: Literal["allow", "throttle", "block"]


class AnomalyEntry(BaseModel):
    ts: float
    user_id: str | None
    device_fp: str | None
    action_type: str
    count: int
    severity: str
    brand_id: str | None = None


# ── Utility helpers ──────────────────────────────────────────────────────
def _now() -> float:
    return time.time()


def _i(raw: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(raw.get(key) or default)
    except (TypeError, ValueError):
        return default


def _f(raw: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(raw.get(key) or default)
    except (TypeError, ValueError):
        return default


async def _append_timeline(
    r: aioredis.Redis,
    incident_id: str,
    actor: str,
    kind: str,
    payload: dict[str, Any] | None = None,
) -> None:
    event = {
        "ts": _now(),
        "actor": actor,
        "kind": kind,
        "payload": payload or {},
    }
    key = _k_incident_timeline(incident_id)
    async with r.pipeline(transaction=False) as pipe:
        pipe.rpush(key, json.dumps(event, ensure_ascii=False))
        pipe.ltrim(key, -TIMELINE_MAX, -1)
        await pipe.execute()


async def _load_timeline(
    r: aioredis.Redis, incident_id: str
) -> list[dict[str, Any]]:
    raw = await r.lrange(_k_incident_timeline(incident_id), 0, -1)
    out: list[dict[str, Any]] = []
    for entry in raw:
        try:
            out.append(json.loads(entry))
        except (TypeError, ValueError):
            continue
    return out


# ── Trust score core ─────────────────────────────────────────────────────
def _compute_trust_score(signals: dict[str, int | float]) -> tuple[int, list[TrustFactor]]:
    """Apply the published trust formula and return (score, factor breakdown).

    Pure function: deterministic given the signal hash. Saves us a round-trip
    when sibling routers want to preview a hypothetical score.
    """
    base = float(TRUST_DEFAULT_BASE)
    factors: list[TrustFactor] = []

    # Positive contributions (capped per-factor so a single dimension can't
    # dominate — Goodhart resistance).
    pos_specs = [
        ("transactions_completed_30d", 0.5, 20.0),
        ("kyc_verified", 10.0, 10.0),
        ("account_age_days_div30", 1.0, 15.0),  # caller pre-divides
        ("positive_reviews", 0.5, 10.0),
    ]
    for name, weight, cap in pos_specs:
        raw_val = float(signals.get(name, 0) or 0)
        contrib = min(raw_val * weight, cap)
        if contrib != 0:
            factors.append(
                TrustFactor(factor=name, weight=weight, contribution=contrib)
            )
        base += contrib

    # Negative contributions — uncapped intentionally so repeated abuse can
    # drive the score below zero (clamped to [0,100] at the end).
    neg_specs = [
        ("incidents_confirmed_30d", -5.0),
        ("disputes_lost", -3.0),
        ("velocity_anomalies", -2.0),
        ("aml_flags", -10.0),
    ]
    for name, weight in neg_specs:
        raw_val = float(signals.get(name, 0) or 0)
        contrib = raw_val * weight
        if contrib != 0:
            factors.append(
                TrustFactor(factor=name, weight=weight, contribution=contrib)
            )
        base += contrib

    score = max(TRUST_MIN, min(TRUST_MAX, int(round(base))))
    return score, factors


async def _load_signals(r: aioredis.Redis, user_id: str) -> dict[str, float]:
    raw = await r.hgetall(_k_trust_signals(user_id))
    out: dict[str, float] = {}
    for k, v in raw.items():
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            out[k] = 0.0
    # Derive account_age_days_div30 from a stored created_at if present.
    if "created_at" in out and "account_age_days_div30" not in out:
        days = max(0.0, (_now() - out["created_at"]) / 86400.0)
        out["account_age_days_div30"] = days / 30.0
    return out


async def _persist_trust_score(
    r: aioredis.Redis,
    user_id: str,
    score: int,
    factors: list[TrustFactor],
    reason: str,
    brand_id: str | None = None,
) -> None:
    """Write the canonical score hash + push an audit row + brand index."""
    now = _now()
    factors_json = json.dumps(
        [f.model_dump() for f in factors], ensure_ascii=False
    )
    async with r.pipeline(transaction=False) as pipe:
        pipe.hset(
            _k_trust(user_id),
            mapping={
                "user_id": user_id,
                "score": score,
                "factors": factors_json,
                "last_updated": now,
                "last_reason": reason,
            },
        )
        pipe.rpush(
            _k_trust_history(user_id),
            json.dumps(
                {
                    "ts": now,
                    "score": score,
                    "reason": reason,
                },
                ensure_ascii=False,
            ),
        )
        pipe.ltrim(_k_trust_history(user_id), -TRUST_HISTORY_MAX, -1)
        if brand_id:
            pipe.zadd(_k_brand_trust_index(brand_id), {user_id: score})
        await pipe.execute()


async def _recompute_and_persist(
    r: aioredis.Redis,
    user_id: str,
    reason: str,
    brand_id: str | None = None,
) -> int:
    signals = await _load_signals(r, user_id)
    score, factors = _compute_trust_score(signals)
    await _persist_trust_score(r, user_id, score, factors, reason, brand_id)
    return score


# ── Exported integration hooks ───────────────────────────────────────────
async def update_trust_signal(
    r: aioredis.Redis,
    user_id: str,
    signal_name: str,
    delta: float,
    brand_id: str | None = None,
) -> int:
    """Sibling routers call this on relevant events.

    e.g. wallet → ('transactions_completed_30d', +1), disputes →
    ('disputes_lost', +1), velocity check → ('velocity_anomalies', +1).
    Returns the new score after recompute.
    """
    await r.hincrbyfloat(_k_trust_signals(user_id), signal_name, delta)
    return await _recompute_and_persist(
        r, user_id, reason=f"signal:{signal_name}:{delta:+g}", brand_id=brand_id
    )


async def check_trust_internal(
    r: aioredis.Redis,
    user_id: str,
    action: str,
    threshold: int | None = None,
) -> tuple[bool, int, list[str]]:
    """Gate function consumed by other routers (listings, p2p, payouts).

    Lazy-initialises a neutral score if the user is unknown. Blocklisted
    users are always denied regardless of score.
    """
    reasons: list[str] = []

    # Blocklist short-circuit.
    if await r.sismember(_K_AML_BLOCKLIST, user_id):
        reasons.append("aml_blocklisted")
        return False, 0, reasons

    raw = await r.hgetall(_k_trust(user_id))
    if not raw:
        # Lazy init at neutral baseline so first-time gating doesn't 500.
        score = await _recompute_and_persist(r, user_id, reason="lazy_init")
    else:
        score = _i(raw, "score", TRUST_DEFAULT_BASE)

    required = threshold if threshold is not None else TRUST_THRESHOLDS.get(
        action, 30
    )
    allowed = score >= required
    if not allowed:
        reasons.append(f"score_below_threshold:{score}<{required}")
    return allowed, score, reasons


async def record_incident_internal(
    r: aioredis.Redis,
    *,
    brand_id: str,
    incident_type: str,
    reporter_user_id: str,
    evidence_description: str,
    severity: str = "medium",
    actor_user_id: str | None = None,
    target_user_id: str | None = None,
    related_resource_id: str | None = None,
    evidence_urls: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Programmatic incident creation for sibling routers.

    Returns the incident_id. Updates trust signals + indices the same way
    the HTTP endpoint does. Skips the auto-resolve fast path — callers that
    need that should hit the public endpoint.
    """
    if incident_type not in INCIDENT_TYPES:
        incident_type = "other"
    if severity not in SEVERITY_WEIGHT:
        severity = "medium"

    iid = uuid4().hex
    now = _now()
    record = {
        "incident_id": iid,
        "brand_id": brand_id,
        "incident_type": incident_type,
        "actor_user_id": actor_user_id or "",
        "target_user_id": target_user_id or "",
        "related_resource_id": related_resource_id or "",
        "severity": severity,
        "status": "open",
        "evidence_description": evidence_description[:MAX_NOTE_LEN],
        "evidence_urls": json.dumps(evidence_urls or [], ensure_ascii=False),
        "reporter_user_id": reporter_user_id,
        "opened_at": now,
        "metadata": json.dumps(metadata or {}, ensure_ascii=False),
    }
    async with r.pipeline(transaction=False) as pipe:
        pipe.hset(_k_incident(iid), mapping=record)
        pipe.zadd(_k_brand_incidents(brand_id), {iid: now})
        if actor_user_id:
            pipe.sadd(_k_user_actor(actor_user_id), iid)
        if target_user_id:
            pipe.sadd(_k_user_target(target_user_id), iid)
        await pipe.execute()

    await _append_timeline(
        r,
        iid,
        actor="system" if reporter_user_id == "system" else "user",
        kind="opened",
        payload={
            "incident_type": incident_type,
            "severity": severity,
            "reporter_user_id": reporter_user_id,
        },
    )

    # Tentative trust impact — a *report* alone doesn't confirm fault, but
    # we do nudge signals so repeated allegations escalate eventually. The
    # weight is intentionally smaller than confirmed-incident weight.
    sev_w = SEVERITY_WEIGHT.get(severity, 3)
    if actor_user_id:
        # Increment the "alleged" counter (separate from confirmed) so
        # confirmed fraud at resolve-time can be additive.
        await update_trust_signal(
            r,
            actor_user_id,
            "velocity_anomalies",  # treat report as soft anomaly
            sev_w * 0.2,
            brand_id=brand_id,
        )

    logger.info(
        "incident recorded id=%s brand=%s type=%s sev=%s actor=%s target=%s",
        iid,
        brand_id,
        incident_type,
        severity,
        actor_user_id,
        target_user_id,
    )
    return iid


# ── POST /incident/report ────────────────────────────────────────────────
@router.post("/incident/report", response_model=IncidentReportResponse)
async def report_incident(
    body: IncidentReportRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> IncidentReportResponse:
    """Public surface — wraps record_incident_internal with auto-resolve.

    Auto-resolve heuristic: a 'low' severity 'other' with no human party
    referenced (resource-only) gets auto_resolved=True so we don't queue
    janitorial noise for admin review.
    """
    iid = await record_incident_internal(
        r,
        brand_id=body.brand_id,
        incident_type=body.incident_type,
        reporter_user_id=body.reporter_user_id,
        evidence_description=body.evidence.description,
        severity=body.evidence.severity,
        actor_user_id=body.actor_user_id,
        target_user_id=body.target_user_id,
        related_resource_id=body.related_resource_id,
        evidence_urls=body.evidence.urls,
        metadata=body.metadata,
    )

    auto_resolved = (
        body.evidence.severity == "low"
        and body.incident_type == "other"
        and not body.actor_user_id
        and not body.target_user_id
    )
    if auto_resolved:
        await r.hset(
            _k_incident(iid),
            mapping={
                "status": "auto_resolved",
                "resolved_at": _now(),
                "decision": "resolved_no_action",
                "notes": "auto_resolved_low_severity_no_party",
            },
        )
        await _append_timeline(
            r,
            iid,
            actor="system",
            kind="auto_resolved",
            payload={"reason": "low_severity_no_party"},
        )

    actor_score = (
        await _recompute_and_persist(
            r, body.actor_user_id, reason=f"incident:{iid}", brand_id=body.brand_id
        )
        if body.actor_user_id
        else None
    )
    target_score = (
        await _recompute_and_persist(
            r,
            body.target_user_id,
            reason=f"incident_target:{iid}",
            brand_id=body.brand_id,
        )
        if body.target_user_id
        else None
    )

    return IncidentReportResponse(
        incident_id=iid,
        status="auto_resolved" if auto_resolved else "open",
        auto_resolved=auto_resolved,
        actor_trust_score=actor_score,
        target_trust_score=target_score,
    )


# ── GET /incident/{iid} ──────────────────────────────────────────────────
@router.get("/incident/{incident_id}", response_model=IncidentDetail)
async def get_incident(
    incident_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> IncidentDetail:
    raw = await r.hgetall(_k_incident(incident_id))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "incident_not_found"},
        )
    timeline = await _load_timeline(r, incident_id)
    try:
        urls = json.loads(raw.get("evidence_urls") or "[]")
    except (TypeError, ValueError):
        urls = []
    try:
        meta = json.loads(raw.get("metadata") or "{}")
    except (TypeError, ValueError):
        meta = {}

    return IncidentDetail(
        incident_id=raw.get("incident_id", incident_id),
        brand_id=raw.get("brand_id", ""),
        incident_type=raw.get("incident_type", "other"),
        actor_user_id=raw.get("actor_user_id") or None,
        target_user_id=raw.get("target_user_id") or None,
        related_resource_id=raw.get("related_resource_id") or None,
        severity=raw.get("severity", "medium"),
        status=raw.get("status", "open"),
        evidence_description=raw.get("evidence_description", ""),
        evidence_urls=urls,
        reporter_user_id=raw.get("reporter_user_id", ""),
        opened_at=_f(raw, "opened_at"),
        resolved_at=_f(raw, "resolved_at") or None,
        decision=raw.get("decision") or None,
        notes=raw.get("notes") or None,
        timeline=timeline,
        metadata=meta,
    )


# ── GET /incident/brand/{brand_id} ───────────────────────────────────────
@router.get("/incident/brand/{brand_id}", response_model=list[IncidentSummary])
async def list_brand_incidents(
    brand_id: str,
    status_filter: str | None = Query(None, alias="status"),
    severity: str | None = Query(None),
    from_ts: float | None = Query(None, alias="from"),
    to_ts: float | None = Query(None, alias="to"),
    limit: int = Query(100, ge=1, le=MAX_LIST_LIMIT),
    r: aioredis.Redis = Depends(get_redis),
) -> list[IncidentSummary]:
    lo = from_ts if from_ts is not None else "-inf"
    hi = to_ts if to_ts is not None else "+inf"
    # Newest first by reverse range — ZSET is scored on opened_at.
    ids = await r.zrevrangebyscore(
        _k_brand_incidents(brand_id), hi, lo, start=0, num=limit * 3
    )
    out: list[IncidentSummary] = []
    for iid in ids:
        if len(out) >= limit:
            break
        raw = await r.hgetall(_k_incident(iid))
        if not raw:
            continue
        st = raw.get("status", "open")
        sv = raw.get("severity", "medium")
        if status_filter and st != status_filter:
            continue
        if severity and sv != severity:
            continue
        out.append(
            IncidentSummary(
                incident_id=raw.get("incident_id", iid),
                brand_id=raw.get("brand_id", brand_id),
                incident_type=raw.get("incident_type", "other"),
                severity=sv,
                status=st,
                actor_user_id=raw.get("actor_user_id") or None,
                target_user_id=raw.get("target_user_id") or None,
                opened_at=_f(raw, "opened_at"),
            )
        )
    return out


# ── POST /incident/{iid}/resolve ─────────────────────────────────────────
@router.post("/incident/{incident_id}/resolve")
async def resolve_incident(
    incident_id: str,
    body: IncidentResolveRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await r.hgetall(_k_incident(incident_id))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "incident_not_found"},
        )
    if raw.get("status") in INCIDENT_TERMINAL_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "incident_already_resolved",
                "status": raw.get("status"),
            },
        )

    now = _now()
    new_status = {
        "confirmed_fraud": "resolved_confirmed_fraud",
        "false_positive": "resolved_false_positive",
        "resolved_no_action": "resolved_no_action",
    }[body.decision]

    await r.hset(
        _k_incident(incident_id),
        mapping={
            "status": new_status,
            "resolved_at": now,
            "decision": body.decision,
            "notes": body.notes,
        },
    )
    await _append_timeline(
        r,
        incident_id,
        actor="admin",
        kind="resolved",
        payload={
            "decision": body.decision,
            "notes": body.notes,
            "impact_on_trust": body.impact_on_trust,
        },
    )

    actor = raw.get("actor_user_id") or None
    target = raw.get("target_user_id") or None
    brand_id = raw.get("brand_id", "")
    severity = raw.get("severity", "medium")

    # Apply trust deltas.
    if body.decision == "confirmed_fraud" and actor:
        sev_w = SEVERITY_WEIGHT.get(severity, 3)
        await update_trust_signal(
            r,
            actor,
            "incidents_confirmed_30d",
            sev_w,
            brand_id=brand_id,
        )
        if body.impact_on_trust:
            # Admin override beyond the formula — recorded as direct delta.
            await _apply_direct_trust_delta(
                r,
                actor,
                body.impact_on_trust,
                reason=f"admin_override:{incident_id}",
                brand_id=brand_id,
            )
    elif body.decision == "false_positive" and actor:
        # Roll back the soft anomaly we added on report.
        await update_trust_signal(
            r,
            actor,
            "velocity_anomalies",
            -SEVERITY_WEIGHT.get(severity, 3) * 0.2,
            brand_id=brand_id,
        )

    return {
        "ok": True,
        "incident_id": incident_id,
        "status": new_status,
        "decision": body.decision,
    }


async def _apply_direct_trust_delta(
    r: aioredis.Redis,
    user_id: str,
    delta: int,
    reason: str,
    brand_id: str | None = None,
) -> int:
    """Admin / system-initiated bypass that writes straight to the score.

    Distinct from update_trust_signal which goes through the formula. This
    is for one-shot punishments or restorations.
    """
    raw = await r.hgetall(_k_trust(user_id))
    cur = _i(raw, "score", TRUST_DEFAULT_BASE)
    new_score = max(TRUST_MIN, min(TRUST_MAX, cur + delta))
    factors_json = raw.get("factors") or "[]"
    try:
        factors_raw = json.loads(factors_json)
    except (TypeError, ValueError):
        factors_raw = []
    factors_raw.append(
        {
            "factor": "admin_direct_delta",
            "weight": 1.0,
            "contribution": float(delta),
        }
    )
    now = _now()
    async with r.pipeline(transaction=False) as pipe:
        pipe.hset(
            _k_trust(user_id),
            mapping={
                "user_id": user_id,
                "score": new_score,
                "factors": json.dumps(factors_raw, ensure_ascii=False),
                "last_updated": now,
                "last_reason": reason,
            },
        )
        pipe.rpush(
            _k_trust_history(user_id),
            json.dumps(
                {"ts": now, "score": new_score, "reason": reason, "delta": delta},
                ensure_ascii=False,
            ),
        )
        pipe.ltrim(_k_trust_history(user_id), -TRUST_HISTORY_MAX, -1)
        if brand_id:
            pipe.zadd(_k_brand_trust_index(brand_id), {user_id: new_score})
        await pipe.execute()
    return new_score


# ── GET /trust-score/{user_id} ───────────────────────────────────────────
@router.get("/trust-score/{user_id}", response_model=TrustScoreResponse)
async def get_trust_score(
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> TrustScoreResponse:
    raw = await r.hgetall(_k_trust(user_id))
    if not raw:
        # Lazy init — same path as the internal gate so consumers see a
        # consistent neutral default.
        await _recompute_and_persist(r, user_id, reason="lazy_init_via_get")
        raw = await r.hgetall(_k_trust(user_id))

    try:
        factors_raw = json.loads(raw.get("factors") or "[]")
    except (TypeError, ValueError):
        factors_raw = []
    factors = [
        TrustFactor(
            factor=str(f.get("factor", "")),
            weight=float(f.get("weight", 0)),
            contribution=float(f.get("contribution", 0)),
        )
        for f in factors_raw
        if isinstance(f, dict)
    ]

    history_raw = await r.lrange(_k_trust_history(user_id), -20, -1)
    history: list[dict[str, Any]] = []
    for entry in history_raw:
        try:
            history.append(json.loads(entry))
        except (TypeError, ValueError):
            continue

    flags: list[str] = []
    if await r.sismember(_K_AML_BLOCKLIST, user_id):
        flags.append("aml_blocklisted")
    score = _i(raw, "score", TRUST_DEFAULT_BASE)
    if score < 20:
        flags.append("high_risk")
    elif score < 40:
        flags.append("elevated_risk")

    return TrustScoreResponse(
        user_id=user_id,
        score=score,
        factors=factors,
        history=history,
        flags=flags,
        last_updated=_f(raw, "last_updated"),
    )


# ── POST /trust-score/{user_id}/adjust ───────────────────────────────────
@router.post("/trust-score/{user_id}/adjust")
async def adjust_trust_score(
    user_id: str,
    body: TrustAdjustRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    new_score = await _apply_direct_trust_delta(
        r,
        user_id,
        body.delta,
        reason=f"manual:{body.reason}",
    )
    return {
        "ok": True,
        "user_id": user_id,
        "new_score": new_score,
        "delta": body.delta,
    }


# ── GET /trust-score/brand/{bid}/distribution ─────────────────────────────
@router.get(
    "/trust-score/brand/{brand_id}/distribution",
    response_model=TrustDistributionResponse,
)
async def trust_distribution(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> TrustDistributionResponse:
    """Histogram bucketed at 0-19 / 20-39 / 40-59 / 60-79 / 80-100."""
    buckets = {"0-19": 0, "20-39": 0, "40-59": 0, "60-79": 0, "80-100": 0}
    total = 0
    score_sum = 0.0

    # Scan ZSET — bounded by brand size; this isn't a public hot path.
    cursor = 0
    while True:
        # Pull a page of (member, score) with zscan since ZSET supports it.
        cursor, page = await r.zscan(
            _k_brand_trust_index(brand_id), cursor=cursor, count=200
        )
        # zscan returns flat [member, score, member, score, ...] in some
        # client versions; redis-py >=4 yields list of (member, score).
        if page and isinstance(page, list) and page and isinstance(page[0], tuple):
            pairs = page
        else:
            pairs = list(zip(page[::2], page[1::2])) if page else []
        for _uid, score in pairs:
            try:
                s = int(float(score))
            except (TypeError, ValueError):
                continue
            total += 1
            score_sum += s
            if s < 20:
                buckets["0-19"] += 1
            elif s < 40:
                buckets["20-39"] += 1
            elif s < 60:
                buckets["40-59"] += 1
            elif s < 80:
                buckets["60-79"] += 1
            else:
                buckets["80-100"] += 1
        if cursor == 0:
            break

    avg = (score_sum / total) if total else 0.0
    return TrustDistributionResponse(
        brand_id=brand_id,
        total_users=total,
        buckets=buckets,
        avg_score=round(avg, 2),
    )


# ── POST /trust-gate ─────────────────────────────────────────────────────
@router.post("/trust-gate", response_model=TrustGateResponse)
async def trust_gate(
    body: TrustGateRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> TrustGateResponse:
    allowed, score, reasons = await check_trust_internal(
        r, body.user_id, body.action, body.threshold
    )
    required = body.threshold if body.threshold is not None else TRUST_THRESHOLDS.get(
        body.action, 30
    )
    return TrustGateResponse(
        allowed=allowed, score=score, required_threshold=required, reasons=reasons
    )


# ── AML ──────────────────────────────────────────────────────────────────
def _should_auto_escalate(flag_type: str, amount_cents: int) -> bool:
    if flag_type in {"counterparty_blocklist", "structuring"}:
        return True
    if flag_type == "large_amount" and amount_cents >= AML_LARGE_AMOUNT_CENTS:
        return True
    if flag_type == "velocity" and amount_cents >= AML_STRUCTURING_THRESHOLD_CENTS:
        return True
    return False


@router.post("/aml/report", response_model=AMLReportResponse)
async def aml_report(
    body: AMLReportRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> AMLReportResponse:
    rid = uuid4().hex
    now = _now()
    escalated = _should_auto_escalate(body.flag_type, body.amount_cents)
    new_status = "escalated" if escalated else "open"

    record = {
        "aml_report_id": rid,
        "user_id": body.user_id,
        "transaction_id": body.transaction_id or "",
        "amount_cents": body.amount_cents,
        "currency": body.currency,
        "flag_type": body.flag_type,
        "status": new_status,
        "escalated": "1" if escalated else "0",
        "evidence": body.evidence,
        "opened_at": now,
    }
    async with r.pipeline(transaction=False) as pipe:
        pipe.hset(_k_aml_report(rid), mapping=record)
        pipe.zadd(_K_AML_REPORTS, {rid: now})
        await pipe.execute()

    # AML flags always push the trust signal — even unescalated ones, so a
    # pattern of small-flag accumulation surfaces over time.
    await update_trust_signal(r, body.user_id, "aml_flags", 1)

    logger.info(
        "AML report id=%s user=%s flag=%s amount=%s escalated=%s",
        rid,
        body.user_id,
        body.flag_type,
        body.amount_cents,
        escalated,
    )

    return AMLReportResponse(
        aml_report_id=rid, escalated=escalated, status=new_status
    )


@router.get("/aml/reports", response_model=list[AMLReportDetail])
async def list_aml_reports(
    status_filter: str | None = Query(None, alias="status"),
    from_ts: float | None = Query(None, alias="from"),
    to_ts: float | None = Query(None, alias="to"),
    limit: int = Query(100, ge=1, le=MAX_LIST_LIMIT),
    r: aioredis.Redis = Depends(get_redis),
) -> list[AMLReportDetail]:
    lo = from_ts if from_ts is not None else "-inf"
    hi = to_ts if to_ts is not None else "+inf"
    ids = await r.zrevrangebyscore(
        _K_AML_REPORTS, hi, lo, start=0, num=limit * 3
    )
    out: list[AMLReportDetail] = []
    for rid in ids:
        if len(out) >= limit:
            break
        raw = await r.hgetall(_k_aml_report(rid))
        if not raw:
            continue
        st = raw.get("status", "open")
        if status_filter and st != status_filter:
            continue
        out.append(
            AMLReportDetail(
                aml_report_id=raw.get("aml_report_id", rid),
                user_id=raw.get("user_id", ""),
                transaction_id=raw.get("transaction_id") or None,
                amount_cents=_i(raw, "amount_cents"),
                currency=raw.get("currency", ""),
                flag_type=raw.get("flag_type", "unusual_pattern"),
                status=st,
                escalated=raw.get("escalated") == "1",
                evidence=raw.get("evidence", ""),
                opened_at=_f(raw, "opened_at"),
            )
        )
    return out


@router.post("/aml/blocklist")
async def aml_blocklist(
    body: AMLBlocklistRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Compliance blocklist — cascades into consent, kix_id, wallet gating.

    The cascade is realised by sibling routers calling check_trust_internal
    (which short-circuits on blocklist) rather than this endpoint pushing
    notifications. Lazy cascade = no fan-out failure modes.
    """
    now = _now()
    async with r.pipeline(transaction=False) as pipe:
        pipe.sadd(_K_AML_BLOCKLIST, body.user_id)
        pipe.hset(
            f"aml:blocklist:meta:{body.user_id}",
            mapping={
                "user_id": body.user_id,
                "reason": body.reason,
                "source": body.source,
                "added_at": now,
            },
        )
        await pipe.execute()

    # Slam the trust score to floor — a blocklist is the strongest signal.
    await _apply_direct_trust_delta(
        r,
        body.user_id,
        delta=-100,
        reason=f"aml_blocklist:{body.source}:{body.reason[:64]}",
    )

    logger.warning(
        "AML blocklist add user=%s source=%s reason=%s",
        body.user_id,
        body.source,
        body.reason,
    )
    return {"ok": True, "user_id": body.user_id, "blocklisted": True}


# ── Velocity / anomaly ───────────────────────────────────────────────────
def _velocity_keys_for_window(
    identifier: str, action_type: str, window_seconds: int
) -> list[str]:
    """Return the hour-bucket keys covering the rolling window.

    Each bucket holds counts for a 3600s slice. For sub-hour windows we
    still address only the current bucket — the worst-case overcounting
    is bounded by the window/3600 ratio which is acceptable for an
    anti-abuse gate.
    """
    now = int(_now())
    cur_hour = now // 3600
    span_hours = max(1, math.ceil(window_seconds / 3600))
    return [
        _k_velocity(identifier, action_type, cur_hour - i)
        for i in range(span_hours)
    ]


@router.post("/velocity/check", response_model=VelocityCheckResponse)
async def velocity_check(
    body: VelocityCheckRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> VelocityCheckResponse:
    identifier = body.user_id or body.device_fp or ""
    threshold = VELOCITY_THRESHOLDS.get(
        body.action_type, VELOCITY_THRESHOLDS["default"]
    )
    keys = _velocity_keys_for_window(identifier, body.action_type, body.window_seconds)

    # Increment current bucket and read all relevant buckets in one pipeline.
    cur_key = keys[0]
    async with r.pipeline(transaction=False) as pipe:
        pipe.incr(cur_key)
        pipe.expire(cur_key, 7200)
        for k in keys:
            pipe.get(k)
        results = await pipe.execute()
    # results[0]=incr, results[1]=expire, results[2:]=gets
    raw_counts = results[2:]
    total = 0
    for v in raw_counts:
        try:
            total += int(v or 0)
        except (TypeError, ValueError):
            continue

    anomaly = total > threshold
    if total > threshold * 2:
        action = "block"
    elif anomaly:
        action = "throttle"
    else:
        action = "allow"

    if anomaly:
        severity = "high" if action == "block" else "medium"
        anomaly_entry = {
            "ts": _now(),
            "user_id": body.user_id,
            "device_fp": body.device_fp,
            "action_type": body.action_type,
            "count": total,
            "severity": severity,
        }
        async with r.pipeline(transaction=False) as pipe:
            pipe.rpush(
                _K_ANOMALIES, json.dumps(anomaly_entry, ensure_ascii=False)
            )
            pipe.ltrim(_K_ANOMALIES, -ANOMALY_LOG_MAX, -1)
            await pipe.execute()
        if body.user_id:
            # Recurring velocity hits accumulate as a signal on the user.
            await update_trust_signal(
                r, body.user_id, "velocity_anomalies", 1
            )

    return VelocityCheckResponse(
        count_in_window=total,
        threshold=threshold,
        anomaly=anomaly,
        action=action,
    )


@router.get("/anomalies", response_model=list[AnomalyEntry])
async def list_anomalies(
    brand_id: str | None = Query(None),
    min_severity: Literal["low", "medium", "high", "critical"] | None = Query(None),
    limit: int = Query(50, ge=1, le=MAX_LIST_LIMIT),
    r: aioredis.Redis = Depends(get_redis),
) -> list[AnomalyEntry]:
    raw = await r.lrange(_K_ANOMALIES, -limit * 3, -1)
    min_w = SEVERITY_WEIGHT.get(min_severity, 0) if min_severity else 0
    out: list[AnomalyEntry] = []
    for entry in reversed(raw):  # newest first
        if len(out) >= limit:
            break
        try:
            d = json.loads(entry)
        except (TypeError, ValueError):
            continue
        sev = d.get("severity", "low")
        if SEVERITY_WEIGHT.get(sev, 0) < min_w:
            continue
        if brand_id and d.get("brand_id") and d.get("brand_id") != brand_id:
            continue
        out.append(
            AnomalyEntry(
                ts=float(d.get("ts", 0)),
                user_id=d.get("user_id"),
                device_fp=d.get("device_fp"),
                action_type=d.get("action_type", "unknown"),
                count=int(d.get("count", 0)),
                severity=sev,
                brand_id=d.get("brand_id"),
            )
        )
    return out
