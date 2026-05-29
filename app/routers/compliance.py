"""Compliance Scanner + Sensitive PII Audit — KiX legal screening spine.

Cross-brand creative + targeting flows touch three regulated surfaces
that consent (see ``consent.py``) does not cover:

* **Ad creative legality** — 广告法 §9/§25 (极限词/金融保本), 医疗广告管理办法
  §6, 房地产广告发布规定 §7, 双减 (education). Each industry has a
  banned-phrase list; some phrases must trigger an auto-injected
  disclaimer ("投资有风险, 入市需谨慎"); others are hard blocks.

* **Sensitive PI handling** — 个人信息保护法 §28 (敏感个人信息) requires
  separate explicit consent, retention class, and audit trail for ID
  cards, biometrics, medical records, financial accounts, and minors'
  data. §51 mandates the controller maintain that audit and surface it
  on request.

* **Anomalous access detection** — abnormal staff access patterns
  (e.g. one operator pulling 100 ID-card records in an hour) is the
  early-warning signal regulators expect us to surface to the brand.

Architecture
------------
Stateless **scanner**: rules live in Redis as ``{rule_id → JSON}``;
seeded at first call, then mutable via ``/rules/register`` and
``/rules/{id}/disable``. Scanning runs the full enabled set for the
requested ``industry`` (plus the always-on ``general`` set) against the
creative text. ``block``-severity hits fail the scan; ``warn``-severity
hits surface but pass; ``info`` is observability-only.

The ``/auto-inject`` endpoint is the dual: take any creative + industry,
return text with the legally required disclaimer appended (idempotent —
will not re-append if already present).

Sensitive-PI side keeps three Redis lists:
  - per-user trail (for §51 user-request export)
  - per-brand trail (for self-inspection / regulator request)
  - per (user, field) sliding counter for anomaly detection

Sibling integration
-------------------
``creative_gen`` may import :func:`scan_internal` to gate generated
creatives before approval; ``/api/v1/creative-gen/*`` should treat
``pass=False`` as a hard reject and surface ``violations`` to the
merchant. ``master_accounts``/``users`` should call
``log_sensitive_pi_internal`` whenever staff-side code dereferences a
sensitive field.

Redis key schema
----------------
    compliance:rules                          HASH  {rule_id → JSON(rule)}
    compliance:rules:by_industry:{industry}   SET   of rule_ids
    compliance:rules:disabled                 SET   of rule_ids
    compliance:disclaimers                    HASH  {industry → text}
    compliance:seed:loaded                    STR   "1" once seeded
    compliance:scan_log:{user_or_brand}       LIST  capped scans (1000)
    compliance:pii_audit:user:{uid}           LIST  capped audit (1000)
    compliance:pii_audit:brand:{bid}          LIST  capped audit (5000)
    compliance:pii_anomaly:{uid}:{field}      INT   counter, EX 3600

Audit + scan lists are capped via LTRIM. Counters auto-expire.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ─────────────────────────────────────────────────────────────

VALID_INDUSTRIES: set[str] = {
    "medical",
    "medical_aesthetics",
    "real_estate",
    "fintech",
    "education",
    "general",
    "cosmetics",
    "food",
}

VALID_SEVERITIES: set[str] = {"block", "warn", "info"}
# Jurisdictions matching `app.region.REGION_CONFIG` plus the `global` catch-all.
VALID_JURISDICTIONS: set[str] = {"CN", "US", "EU", "ID", "SG", "global"}

VALID_PII_FIELDS: set[str] = {
    "id_card",
    "phone",
    "address",
    "medical_record",
    "financial",
    "biometric",
    "minor_data",
    "passport",
    "other",
}

VALID_PII_ACTIONS: set[str] = {"access", "write", "delete", "export"}
VALID_PURPOSES: set[str] = {
    "kyc",
    "treatment",
    "shipping",
    "billing",
    "audit",
    "marketing",
    "other",
}
VALID_CONSENT_BASES: set[str] = {
    "explicit_consent",
    "contract",
    "legal_obligation",
    "vital_interest",
    "public_interest",
}

# Severity → risk_score weight contribution per hit
SEVERITY_WEIGHTS: dict[str, int] = {"block": 40, "warn": 15, "info": 3}

SCAN_LOG_MAX = 1000
PII_AUDIT_MAX_USER = 1000
PII_AUDIT_MAX_BRAND = 5000

# Anomaly thresholds — abnormally many accesses to a sensitive field in 1h.
ANOMALY_WINDOW_SEC = 3600
ANOMALY_THRESHOLDS: dict[str, int] = {
    "id_card": 30,
    "phone": 100,
    "address": 100,
    "medical_record": 20,
    "financial": 20,
    "biometric": 10,
    "minor_data": 10,
    "passport": 20,
    "other": 200,
}


# ── Seed data ─────────────────────────────────────────────────────────────

SEED_RULES: list[dict[str, Any]] = [
    # 医疗广告管理办法 §6 / §7
    {"industry": "medical", "phrase": "包治百病", "severity": "block",
     "rule_id": "med_001", "jurisdiction": "CN",
     "citation": "医疗广告管理办法 §6.1",
     "suggestion": "改为‘辅助治疗’或删除该承诺"},
    {"industry": "medical", "phrase": "祖传秘方", "severity": "block",
     "rule_id": "med_002", "jurisdiction": "CN",
     "citation": "医疗广告管理办法 §7"},
    {"industry": "medical", "phrase": "根治", "severity": "block",
     "rule_id": "med_003", "jurisdiction": "CN"},
    {"industry": "medical", "phrase": "保证治愈", "severity": "block",
     "rule_id": "med_004", "jurisdiction": "CN"},
    {"industry": "medical", "phrase": "100%有效", "severity": "block",
     "rule_id": "med_005", "jurisdiction": "CN"},
    {"industry": "medical", "phrase": "纯天然", "severity": "warn",
     "rule_id": "med_006", "jurisdiction": "CN",
     "suggestion": "‘纯天然’需有依据，建议替换为可证实表述"},
    {"industry": "medical", "phrase": "无副作用", "severity": "block",
     "rule_id": "med_007", "jurisdiction": "CN"},
    {"industry": "medical", "phrase": "立竿见影", "severity": "block",
     "rule_id": "med_008", "jurisdiction": "CN"},

    # 医疗美容 (subset of medical with stricter rules)
    {"industry": "medical_aesthetics", "phrase": "整形保险",
     "severity": "block", "rule_id": "med_aest_001", "jurisdiction": "CN",
     "citation": "医疗美容广告执法指南 (2021)"},
    {"industry": "medical_aesthetics", "phrase": "无痛",
     "severity": "warn", "rule_id": "med_aest_002", "jurisdiction": "CN"},
    {"industry": "medical_aesthetics", "phrase": "立即见效",
     "severity": "warn", "rule_id": "med_aest_003", "jurisdiction": "CN"},
    {"industry": "medical_aesthetics", "phrase": "永久",
     "severity": "block", "rule_id": "med_aest_004", "jurisdiction": "CN"},
    {"industry": "medical_aesthetics", "phrase": "改变命运",
     "severity": "block", "rule_id": "med_aest_005", "jurisdiction": "CN",
     "citation": "医疗美容广告执法指南 (2021)"},
    {"industry": "medical_aesthetics", "phrase": "蜕变",
     "severity": "warn", "rule_id": "med_aest_006", "jurisdiction": "CN"},

    # 房地产广告发布规定 §7
    {"industry": "real_estate", "phrase": "升值", "severity": "block",
     "rule_id": "re_001", "jurisdiction": "CN",
     "citation": "房地产广告发布规定 §7",
     "suggestion": "禁含升值/投资回报/学区/落户承诺"},
    {"industry": "real_estate", "phrase": "投资回报", "severity": "block",
     "rule_id": "re_002", "jurisdiction": "CN",
     "citation": "房地产广告发布规定 §7"},
    {"industry": "real_estate", "phrase": "学区房", "severity": "block",
     "rule_id": "re_003", "jurisdiction": "CN",
     "citation": "房地产广告发布规定 §7"},
    {"industry": "real_estate", "phrase": "学位", "severity": "warn",
     "rule_id": "re_004", "jurisdiction": "CN"},
    {"industry": "real_estate", "phrase": "落户", "severity": "block",
     "rule_id": "re_005", "jurisdiction": "CN"},
    {"industry": "real_estate", "phrase": "稳赚不赔", "severity": "block",
     "rule_id": "re_006", "jurisdiction": "CN"},
    {"industry": "real_estate", "phrase": "保值", "severity": "block",
     "rule_id": "re_007", "jurisdiction": "CN"},
    {"industry": "real_estate", "phrase": "首付分期", "severity": "warn",
     "rule_id": "re_008", "jurisdiction": "CN",
     "suggestion": "近年监管收紧，需有合规放贷资质"},

    # 广告法 §25 — 金融
    {"industry": "fintech", "phrase": "保本", "severity": "block",
     "rule_id": "fin_001", "jurisdiction": "CN",
     "citation": "广告法 §25"},
    {"industry": "fintech", "phrase": "保证收益", "severity": "block",
     "rule_id": "fin_002", "jurisdiction": "CN",
     "citation": "广告法 §25"},
    {"industry": "fintech", "phrase": "稳赚", "severity": "block",
     "rule_id": "fin_003", "jurisdiction": "CN"},
    {"industry": "fintech", "phrase": "零风险", "severity": "block",
     "rule_id": "fin_004", "jurisdiction": "CN",
     "citation": "广告法 §25"},
    {"industry": "fintech", "phrase": "无风险", "severity": "block",
     "rule_id": "fin_005", "jurisdiction": "CN"},
    {"industry": "fintech", "phrase": "高收益", "severity": "warn",
     "rule_id": "fin_006", "jurisdiction": "CN"},
    {"industry": "fintech", "phrase": "稳赚不赔", "severity": "block",
     "rule_id": "fin_007", "jurisdiction": "CN"},
    {"industry": "fintech", "phrase": "刚兑", "severity": "block",
     "rule_id": "fin_008", "jurisdiction": "CN"},

    # 教育 (双减后)
    {"industry": "education", "phrase": "保过", "severity": "block",
     "rule_id": "edu_001", "jurisdiction": "CN"},
    {"industry": "education", "phrase": "升学率", "severity": "warn",
     "rule_id": "edu_002", "jurisdiction": "CN"},
    {"industry": "education", "phrase": "提分", "severity": "warn",
     "rule_id": "edu_003", "jurisdiction": "CN"},
    {"industry": "education", "phrase": "重点学校", "severity": "warn",
     "rule_id": "edu_004", "jurisdiction": "CN"},
    {"industry": "education", "phrase": "名师", "severity": "warn",
     "rule_id": "edu_005", "jurisdiction": "CN",
     "suggestion": "教师姓名/资质须脱敏或附证书编号"},
    {"industry": "education", "phrase": "押题", "severity": "block",
     "rule_id": "edu_006", "jurisdiction": "CN"},

    # 化妆品广告管理办法
    {"industry": "cosmetics", "phrase": "速效", "severity": "block",
     "rule_id": "cos_001", "jurisdiction": "CN",
     "citation": "化妆品广告管理办法"},
    {"industry": "cosmetics", "phrase": "全效", "severity": "block",
     "rule_id": "cos_002", "jurisdiction": "CN"},
    {"industry": "cosmetics", "phrase": "特效", "severity": "block",
     "rule_id": "cos_003", "jurisdiction": "CN"},
    {"industry": "cosmetics", "phrase": "去除皱纹", "severity": "block",
     "rule_id": "cos_004", "jurisdiction": "CN"},

    # 食品安全法 §73
    {"industry": "food", "phrase": "治疗", "severity": "block",
     "rule_id": "food_001", "jurisdiction": "CN",
     "citation": "食品安全法 §73"},
    {"industry": "food", "phrase": "预防疾病", "severity": "block",
     "rule_id": "food_002", "jurisdiction": "CN"},
    {"industry": "food", "phrase": "排毒", "severity": "warn",
     "rule_id": "food_003", "jurisdiction": "CN"},
    {"industry": "food", "phrase": "壮阳", "severity": "block",
     "rule_id": "food_004", "jurisdiction": "CN"},

    # 广告法 §9 — 极限词 (always-on / general)
    {"industry": "general", "phrase": "最", "severity": "warn",
     "rule_id": "gen_001", "jurisdiction": "CN",
     "citation": "广告法 §9 极限词",
     "suggestion": "‘最/第一/唯一’等绝对化用语在中国大陆广告中禁止"},
    {"industry": "general", "phrase": "国家级", "severity": "block",
     "rule_id": "gen_002", "jurisdiction": "CN",
     "citation": "广告法 §9"},
    {"industry": "general", "phrase": "最佳", "severity": "block",
     "rule_id": "gen_003", "jurisdiction": "CN",
     "citation": "广告法 §9"},
    {"industry": "general", "phrase": "顶级", "severity": "block",
     "rule_id": "gen_004", "jurisdiction": "CN",
     "citation": "广告法 §9"},
    {"industry": "general", "phrase": "唯一", "severity": "block",
     "rule_id": "gen_005", "jurisdiction": "CN",
     "citation": "广告法 §9"},
    {"industry": "general", "phrase": "第一", "severity": "block",
     "rule_id": "gen_006", "jurisdiction": "CN",
     "citation": "广告法 §9"},
    {"industry": "general", "phrase": "极品", "severity": "block",
     "rule_id": "gen_007", "jurisdiction": "CN"},
    {"industry": "general", "phrase": "首选", "severity": "warn",
     "rule_id": "gen_008", "jurisdiction": "CN"},
    {"industry": "general", "phrase": "独家", "severity": "warn",
     "rule_id": "gen_009", "jurisdiction": "CN"},
    {"industry": "general", "phrase": "领导品牌", "severity": "block",
     "rule_id": "gen_010", "jurisdiction": "CN",
     "citation": "广告法 §9"},
]

REQUIRED_DISCLAIMERS: dict[str, str] = {
    "fintech": "投资有风险, 入市需谨慎。",
    "medical": "本广告仅供医疗专业人士参考，具体诊疗以医师诊断为准。",
    "medical_aesthetics": "医疗美容存在风险, 请到正规医疗机构由具备资质的医师实施。",
    "real_estate": "本广告为要约邀请, 具体内容以政府主管部门核准为准。",
    "education": "课程效果因人而异, 不作升学保证。",
}


# ── Pydantic models ───────────────────────────────────────────────────────


class ScanRequest(BaseModel):
    industry: Literal[
        "medical",
        "medical_aesthetics",
        "real_estate",
        "fintech",
        "education",
        "general",
        "cosmetics",
        "food",
    ]
    creative_text: str = Field(..., min_length=1, max_length=20000)
    media_urls: list[str] | None = None
    actor_id: str | None = Field(
        None,
        description="user_id or brand_id requesting the scan (for audit log)",
    )


class Violation(BaseModel):
    phrase: str
    severity: str
    rule_id: str
    citation: str | None = None
    suggestion: str | None = None
    matched_at: list[int]  # character offsets in creative_text


class ScanResponse(BaseModel):
    pass_: bool = Field(..., alias="pass")
    violations: list[Violation]
    auto_injected_warnings: list[str]
    risk_score: int
    industry: str
    scanned_at: int

    class Config:
        populate_by_name = True


class RuleRegisterRequest(BaseModel):
    industry: str = Field(..., min_length=1, max_length=64)
    phrase: str = Field(..., min_length=1, max_length=200)
    regex: str | None = None
    severity: Literal["block", "warn", "info"]
    rule_id: str = Field(..., min_length=1, max_length=64)
    jurisdiction: Literal["CN", "US", "EU", "global"] = "CN"
    citation: str | None = None
    suggestion: str | None = None


class RuleResponse(BaseModel):
    rule_id: str
    industry: str
    phrase: str
    regex: str | None = None
    severity: str
    jurisdiction: str
    citation: str | None = None
    suggestion: str | None = None
    disabled: bool = False


class AutoInjectRequest(BaseModel):
    creative_text: str = Field(..., min_length=1, max_length=20000)
    industry: str
    jurisdiction: Literal["CN", "US", "EU", "global"] = "CN"


class AutoInjectResponse(BaseModel):
    original_text: str
    injected_text: str
    disclaimers: list[str]
    changed: bool


class PIILogRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    brand_id: str = Field(..., min_length=1, max_length=128)
    action: Literal["access", "write", "delete", "export"]
    field: Literal[
        "id_card",
        "phone",
        "address",
        "medical_record",
        "financial",
        "biometric",
        "minor_data",
        "passport",
        "other",
    ]
    accessor_user_id: str | None = Field(
        None,
        description="kid of staff who accessed the field",
    )
    purpose: Literal[
        "kyc",
        "treatment",
        "shipping",
        "billing",
        "audit",
        "marketing",
        "other",
    ]
    consent_basis: Literal[
        "explicit_consent",
        "contract",
        "legal_obligation",
        "vital_interest",
        "public_interest",
    ] | None = None
    note: str | None = Field(None, max_length=500)


class PIILogResponse(BaseModel):
    audit_id: str
    user_id: str
    brand_id: str
    action: str
    field: str
    ts: int
    anomaly: bool = False
    anomaly_signals: list[str] | None = None


class AnomalyRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    field: Literal[
        "id_card",
        "phone",
        "address",
        "medical_record",
        "financial",
        "biometric",
        "minor_data",
        "passport",
        "other",
    ]


class AnomalyResponse(BaseModel):
    anomaly: bool
    signals: list[str]
    severity: str  # "none" | "low" | "medium" | "high"
    count_in_window: int
    window_sec: int
    threshold: int


# ── Helpers: rule storage ─────────────────────────────────────────────────


async def _seed_if_empty(r: aioredis.Redis) -> None:
    """Idempotently load SEED_RULES + REQUIRED_DISCLAIMERS into Redis."""
    already = await r.get("compliance:seed:loaded")
    if already:
        return

    pipe = r.pipeline()
    for rule in SEED_RULES:
        rid = rule["rule_id"]
        # Default fields
        full = {
            "rule_id": rid,
            "industry": rule["industry"],
            "phrase": rule["phrase"],
            "regex": rule.get("regex"),
            "severity": rule["severity"],
            "jurisdiction": rule.get("jurisdiction", "CN"),
            "citation": rule.get("citation"),
            "suggestion": rule.get("suggestion"),
        }
        pipe.hset("compliance:rules", rid, json.dumps(full))
        pipe.sadd(f"compliance:rules:by_industry:{rule['industry']}", rid)

    for industry, text in REQUIRED_DISCLAIMERS.items():
        pipe.hset("compliance:disclaimers", industry, text)

    pipe.set("compliance:seed:loaded", "1")
    await pipe.execute()
    logger.info(
        "compliance.seed.loaded rules=%d disclaimers=%d",
        len(SEED_RULES),
        len(REQUIRED_DISCLAIMERS),
    )


async def _load_rules_for_industry(
    r: aioredis.Redis, industry: str
) -> list[dict[str, Any]]:
    """Load all enabled rules for ``industry`` (does not include 'general')."""
    rule_ids = await r.smembers(f"compliance:rules:by_industry:{industry}")
    if not rule_ids:
        return []
    disabled = await r.smembers("compliance:rules:disabled")
    enabled_ids = [rid for rid in rule_ids if rid not in disabled]
    if not enabled_ids:
        return []
    raws = await r.hmget("compliance:rules", enabled_ids)
    out: list[dict[str, Any]] = []
    for raw in raws:
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def _find_offsets(text: str, phrase: str, regex: str | None) -> list[int]:
    """Return character offsets where ``phrase``/``regex`` matches."""
    offsets: list[int] = []
    if regex:
        try:
            for m in re.finditer(regex, text):
                offsets.append(m.start())
        except re.error:
            # Bad regex — fall through to substring match
            pass
    if not offsets and phrase:
        start = 0
        while True:
            idx = text.find(phrase, start)
            if idx < 0:
                break
            offsets.append(idx)
            start = idx + len(phrase)
    return offsets


def _disclaimer_already_present(text: str, disclaimer: str) -> bool:
    """Best-effort idempotency check — strip whitespace + punctuation."""
    norm_text = re.sub(r"\s+", "", text)
    norm_d = re.sub(r"\s+", "", disclaimer)
    return norm_d in norm_text


async def _log_scan(
    r: aioredis.Redis,
    actor_id: str | None,
    payload: dict[str, Any],
) -> None:
    if not actor_id:
        return
    key = f"compliance:scan_log:{actor_id}"
    await r.lpush(key, json.dumps(payload))
    await r.ltrim(key, 0, SCAN_LOG_MAX - 1)


# ── Core scanner (callable from sibling routers) ──────────────────────────


async def scan_internal(
    industry: str,
    creative_text: str,
    r: aioredis.Redis,
    media_urls: list[str] | None = None,
) -> dict[str, Any]:
    """Authoritative compliance scan.

    Sibling routers (``creative_gen``) should call this before approving
    generated creatives. Returns a dict with the same shape as
    :class:`ScanResponse`.

    Industry-specific rules are unioned with the always-on ``general``
    rule set (广告法 §9 极限词 etc.) so that e.g. a fintech creative is
    checked against both fintech AND general phrasing.
    """
    await _seed_if_empty(r)

    if industry not in VALID_INDUSTRIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unknown industry '{industry}'. "
                f"Allowed: {sorted(VALID_INDUSTRIES)}"
            ),
        )

    # Union industry-specific + general (general always-on except when industry=general itself)
    rules = await _load_rules_for_industry(r, industry)
    if industry != "general":
        rules += await _load_rules_for_industry(r, "general")

    violations: list[dict[str, Any]] = []
    risk_score = 0
    has_block = False

    for rule in rules:
        offsets = _find_offsets(
            creative_text, rule.get("phrase", ""), rule.get("regex")
        )
        if not offsets:
            continue
        sev = rule.get("severity", "info")
        if sev == "block":
            has_block = True
        risk_score += SEVERITY_WEIGHTS.get(sev, 0) * len(offsets)
        violations.append({
            "phrase": rule.get("phrase", ""),
            "severity": sev,
            "rule_id": rule.get("rule_id", ""),
            "citation": rule.get("citation"),
            "suggestion": rule.get("suggestion"),
            "matched_at": offsets,
        })

    # Auto-inject hint — which disclaimer SHOULD be appended for this industry
    disclaimer_text = await r.hget("compliance:disclaimers", industry)
    auto_injected_warnings: list[str] = []
    if disclaimer_text and not _disclaimer_already_present(
        creative_text, disclaimer_text
    ):
        auto_injected_warnings.append(disclaimer_text)

    risk_score = min(risk_score, 100)
    passed = not has_block

    result = {
        "pass": passed,
        "violations": violations,
        "auto_injected_warnings": auto_injected_warnings,
        "risk_score": risk_score,
        "industry": industry,
        "scanned_at": int(time.time()),
    }
    return result


# ── Sensitive PII audit (callable from sibling routers) ───────────────────


async def log_sensitive_pi_internal(
    r: aioredis.Redis,
    *,
    user_id: str,
    brand_id: str,
    action: str,
    field: str,
    accessor_user_id: str | None = None,
    purpose: str = "other",
    consent_basis: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Append a PIPL §51-compliant audit entry + bump anomaly counter.

    Returns the persisted entry with ``audit_id``, ``ts``, ``anomaly`` flag,
    and an optional list of anomaly signals.
    """
    if action not in VALID_PII_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid action: {action}",
        )
    if field not in VALID_PII_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid field: {field}",
        )
    if purpose not in VALID_PURPOSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid purpose: {purpose}",
        )
    if consent_basis is not None and consent_basis not in VALID_CONSENT_BASES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid consent_basis: {consent_basis}",
        )

    now = int(time.time())
    audit_id = uuid4().hex
    entry = {
        "audit_id": audit_id,
        "ts": now,
        "user_id": user_id,
        "brand_id": brand_id,
        "action": action,
        "field": field,
        "accessor_user_id": accessor_user_id,
        "purpose": purpose,
        "consent_basis": consent_basis,
        "note": note,
    }
    payload = json.dumps(entry)

    # Bump per-(user,field) sliding counter (only for read-type ops)
    anomaly_count = 0
    if action in ("access", "export"):
        anomaly_key = f"compliance:pii_anomaly:{user_id}:{field}"
        anomaly_count = await r.incr(anomaly_key)
        if anomaly_count == 1:
            await r.expire(anomaly_key, ANOMALY_WINDOW_SEC)

    threshold = ANOMALY_THRESHOLDS.get(field, 100)
    signals: list[str] = []
    if anomaly_count > threshold:
        signals.append(
            f"window_exceeded: {anomaly_count} >{threshold} reads/hour"
        )
    anomaly = bool(signals)

    severity = "none"
    if anomaly_count > threshold * 3:
        severity = "high"
    elif anomaly_count > threshold:
        severity = "medium"
    elif anomaly_count > threshold * 0.75:
        severity = "low"

    if anomaly:
        entry["anomaly"] = True
        entry["anomaly_signals"] = signals
        entry["severity"] = severity
        payload = json.dumps(entry)
        logger.warning(
            "compliance.pii.anomaly user=%s field=%s count=%d threshold=%d",
            user_id,
            field,
            anomaly_count,
            threshold,
        )

    # Persist to user + brand trails
    pipe = r.pipeline()
    user_key = f"compliance:pii_audit:user:{user_id}"
    brand_key = f"compliance:pii_audit:brand:{brand_id}"
    pipe.lpush(user_key, payload)
    pipe.ltrim(user_key, 0, PII_AUDIT_MAX_USER - 1)
    pipe.lpush(brand_key, payload)
    pipe.ltrim(brand_key, 0, PII_AUDIT_MAX_BRAND - 1)
    # Cross-brand anomaly log (queryable by /audit/anomalies)
    if anomaly:
        anom_entry = {
            "audit_id": audit_id,
            "ts": now,
            "user_id": user_id,
            "brand_id": brand_id,
            "field": field,
            "action": action,
            "count": anomaly_count,
            "threshold": threshold,
            "severity": severity,
            "signals": signals,
        }
        pipe.lpush(
            "compliance:pii_anomaly_log",
            json.dumps(anom_entry),
        )
        pipe.ltrim("compliance:pii_anomaly_log", 0, PII_AUDIT_MAX_BRAND - 1)
        pipe.lpush(
            f"compliance:pii_anomaly_log:brand:{brand_id}",
            json.dumps(anom_entry),
        )
        pipe.ltrim(
            f"compliance:pii_anomaly_log:brand:{brand_id}",
            0,
            PII_AUDIT_MAX_BRAND - 1,
        )
    # Per-brand write/read counters (last 30d rolling — via per-day buckets)
    day_bucket = now // 86400
    counter_key = f"compliance:pii_counter:brand:{brand_id}:{day_bucket}"
    if action == "write":
        pipe.hincrby(counter_key, "writes", 1)
    elif action in ("access", "export"):
        pipe.hincrby(counter_key, "reads", 1)
    pipe.expire(counter_key, 31 * 86400)  # keep 31d for 30d rollups
    await pipe.execute()

    return {
        "audit_id": audit_id,
        "user_id": user_id,
        "brand_id": brand_id,
        "action": action,
        "field": field,
        "ts": now,
        "anomaly": anomaly,
        "anomaly_signals": signals or None,
    }


# ── Endpoints: compliance scanner ─────────────────────────────────────────


@router.post("/scan", response_model=ScanResponse, response_model_by_alias=True)
async def scan(
    body: ScanRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> ScanResponse:
    """Scan creative text against industry banned-phrase list.

    Returns ``pass=False`` if any rule with ``severity=block`` matches.
    ``severity=warn`` and ``severity=info`` hits surface in
    ``violations`` but do not flip ``pass``.

    The ``general`` rule set (广告法 §9 极限词) is always unioned in
    unless the request itself targets ``industry=general``.

    ``auto_injected_warnings`` lists the disclaimer(s) that the caller
    should append before publishing (use ``/auto-inject`` to do it).
    """
    result = await scan_internal(
        body.industry, body.creative_text, r, media_urls=body.media_urls
    )

    # Best-effort scan log under the actor's namespace
    await _log_scan(
        r,
        body.actor_id,
        {
            "ts": result["scanned_at"],
            "industry": body.industry,
            "pass": result["pass"],
            "violation_count": len(result["violations"]),
            "risk_score": result["risk_score"],
        },
    )

    return ScanResponse(**result)


@router.post("/rules/register", response_model=RuleResponse)
async def register_rule(
    body: RuleRegisterRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> RuleResponse:
    """Register a new banned phrase / regex.

    Overwrites the rule if ``rule_id`` already exists (upsert). Adds the
    rule to the ``industry`` index and removes it from the ``disabled``
    set if it was previously disabled.
    """
    await _seed_if_empty(r)

    if body.industry not in VALID_INDUSTRIES:
        # We allow registering for new industries — just warn, don't reject.
        logger.info(
            "compliance.rules.register new industry=%s", body.industry
        )

    # Validate regex compiles
    if body.regex:
        try:
            re.compile(body.regex)
        except re.error as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid regex: {e}",
            ) from e

    record = {
        "rule_id": body.rule_id,
        "industry": body.industry,
        "phrase": body.phrase,
        "regex": body.regex,
        "severity": body.severity,
        "jurisdiction": body.jurisdiction,
        "citation": body.citation,
        "suggestion": body.suggestion,
    }
    pipe = r.pipeline()
    pipe.hset("compliance:rules", body.rule_id, json.dumps(record))
    pipe.sadd(f"compliance:rules:by_industry:{body.industry}", body.rule_id)
    pipe.srem("compliance:rules:disabled", body.rule_id)
    await pipe.execute()

    logger.info(
        "compliance.rules.register id=%s industry=%s severity=%s",
        body.rule_id,
        body.industry,
        body.severity,
    )
    return RuleResponse(**record, disabled=False)


@router.get("/rules", response_model=list[RuleResponse])
async def list_rules(
    industry: str | None = None,
    jurisdiction: str | None = None,
    r: aioredis.Redis = Depends(get_redis),
) -> list[RuleResponse]:
    """List rules with optional industry + jurisdiction filters."""
    await _seed_if_empty(r)

    if industry:
        rule_ids = await r.smembers(f"compliance:rules:by_industry:{industry}")
        if not rule_ids:
            return []
        raws = await r.hmget("compliance:rules", list(rule_ids))
    else:
        all_rules = await r.hgetall("compliance:rules")
        raws = list(all_rules.values())

    disabled = await r.smembers("compliance:rules:disabled")
    out: list[RuleResponse] = []
    for raw in raws:
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if jurisdiction and rec.get("jurisdiction") != jurisdiction:
            continue
        rec["disabled"] = rec.get("rule_id") in disabled
        out.append(RuleResponse(**rec))
    out.sort(key=lambda x: x.rule_id)
    return out


@router.post("/rules/{rule_id}/disable")
async def disable_rule(
    rule_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Soft-disable a rule (kept in catalog, skipped during scans)."""
    await _seed_if_empty(r)
    exists = await r.hexists("compliance:rules", rule_id)
    if not exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown rule_id '{rule_id}'",
        )
    await r.sadd("compliance:rules:disabled", rule_id)
    logger.info("compliance.rules.disable id=%s", rule_id)
    return {"rule_id": rule_id, "disabled": True}


@router.post("/rules/{rule_id}/enable")
async def enable_rule(
    rule_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Re-enable a previously disabled rule."""
    await _seed_if_empty(r)
    exists = await r.hexists("compliance:rules", rule_id)
    if not exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown rule_id '{rule_id}'",
        )
    await r.srem("compliance:rules:disabled", rule_id)
    return {"rule_id": rule_id, "disabled": False}


@router.post("/auto-inject", response_model=AutoInjectResponse)
async def auto_inject(
    body: AutoInjectRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> AutoInjectResponse:
    """Append required disclaimers for the given industry.

    Idempotent: if the disclaimer is already present in the text
    (whitespace-normalized substring match), no-op.
    """
    await _seed_if_empty(r)

    disclaimer = await r.hget("compliance:disclaimers", body.industry)
    if not disclaimer:
        return AutoInjectResponse(
            original_text=body.creative_text,
            injected_text=body.creative_text,
            disclaimers=[],
            changed=False,
        )

    if _disclaimer_already_present(body.creative_text, disclaimer):
        return AutoInjectResponse(
            original_text=body.creative_text,
            injected_text=body.creative_text,
            disclaimers=[disclaimer],
            changed=False,
        )

    injected = body.creative_text.rstrip() + "\n\n" + disclaimer
    return AutoInjectResponse(
        original_text=body.creative_text,
        injected_text=injected,
        disclaimers=[disclaimer],
        changed=True,
    )


# ── Endpoints: sensitive-PI audit ─────────────────────────────────────────


@router.post(
    "/sensitive-pi/log", response_model=PIILogResponse
)
async def log_sensitive_pi(
    body: PIILogRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> PIILogResponse:
    """Record a sensitive-PI access / write / delete / export.

    Required by PIPL §51 (controller must maintain processing record)
    and §28 (sensitive PI gets stricter audit). Calls into anomaly
    detection inline — if the access rate crosses
    :data:`ANOMALY_THRESHOLDS`, the response carries ``anomaly=True``
    and a list of signals.

    Sibling routers should call :func:`log_sensitive_pi_internal`
    directly to avoid the HTTP hop on hot paths (KYC, treatment record
    reads).
    """
    result = await log_sensitive_pi_internal(
        r,
        user_id=body.user_id,
        brand_id=body.brand_id,
        action=body.action,
        field=body.field,
        accessor_user_id=body.accessor_user_id,
        purpose=body.purpose,
        consent_basis=body.consent_basis,
        note=body.note,
    )
    return PIILogResponse(**result)


@router.get("/sensitive-pi/{user_id}/audit")
async def get_user_audit(
    user_id: str,
    from_ts: int | None = None,
    to_ts: int | None = None,
    action: str | None = None,
    field: str | None = None,
    limit: int = 100,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return the audit trail for a user (PIPL §51 user-request).

    Filterable by time window, action, field. Returns at most ``limit``
    entries newest-first. The user is the legal subject of this data —
    in production this endpoint MUST be gated by either the user's own
    session or a duly-recorded data-subject-access-request.
    """
    if limit < 1 or limit > 1000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="limit must be between 1 and 1000",
        )

    raws = await r.lrange(
        f"compliance:pii_audit:user:{user_id}", 0, PII_AUDIT_MAX_USER - 1
    )

    entries: list[dict[str, Any]] = []
    for raw in raws:
        try:
            e = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        ts = e.get("ts", 0)
        if from_ts is not None and ts < from_ts:
            continue
        if to_ts is not None and ts > to_ts:
            continue
        if action and e.get("action") != action:
            continue
        if field and e.get("field") != field:
            continue
        entries.append(e)
        if len(entries) >= limit:
            break

    return {
        "user_id": user_id,
        "count": len(entries),
        "entries": entries,
    }


@router.get("/sensitive-pi/brand/{brand_id}/audit")
async def get_brand_audit(
    brand_id: str,
    from_ts: int | None = None,
    to_ts: int | None = None,
    action: str | None = None,
    field: str | None = None,
    accessor_user_id: str | None = None,
    limit: int = 200,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Brand-level audit trail (self-inspection / regulator request).

    Includes a per-field summary count so the brand can spot which
    sensitive surface gets hit most often.
    """
    if limit < 1 or limit > 2000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="limit must be between 1 and 2000",
        )

    raws = await r.lrange(
        f"compliance:pii_audit:brand:{brand_id}", 0, PII_AUDIT_MAX_BRAND - 1
    )

    entries: list[dict[str, Any]] = []
    field_counts: dict[str, int] = {}
    action_counts: dict[str, int] = {}
    anomaly_count = 0

    for raw in raws:
        try:
            e = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        ts = e.get("ts", 0)
        if from_ts is not None and ts < from_ts:
            continue
        if to_ts is not None and ts > to_ts:
            continue
        if action and e.get("action") != action:
            continue
        if field and e.get("field") != field:
            continue
        if accessor_user_id and e.get("accessor_user_id") != accessor_user_id:
            continue

        field_counts[e.get("field", "?")] = field_counts.get(
            e.get("field", "?"), 0
        ) + 1
        action_counts[e.get("action", "?")] = action_counts.get(
            e.get("action", "?"), 0
        ) + 1
        if e.get("anomaly"):
            anomaly_count += 1

        if len(entries) < limit:
            entries.append(e)

    return {
        "brand_id": brand_id,
        "count": len(entries),
        "entries": entries,
        "summary": {
            "field_counts": field_counts,
            "action_counts": action_counts,
            "anomaly_count": anomaly_count,
        },
    }


@router.post(
    "/sensitive-pi/anomaly-check", response_model=AnomalyResponse
)
async def anomaly_check(
    body: AnomalyRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> AnomalyResponse:
    """Probe abnormal access patterns for (user, field).

    Sliding 1-hour counter against per-field thresholds (e.g. ID-card
    reads >30/hour). Use this to drive an SOC alert / require step-up
    auth on the staff side. Read-only — does not mutate the counter.
    """
    key = f"compliance:pii_anomaly:{body.user_id}:{body.field}"
    raw = await r.get(key)
    count = int(raw) if raw else 0
    threshold = ANOMALY_THRESHOLDS.get(body.field, 100)

    signals: list[str] = []
    severity = "none"
    if count > threshold * 3:
        signals.append(
            f"critical: {count} reads in last hour (>{threshold * 3})"
        )
        severity = "high"
    elif count > threshold:
        signals.append(
            f"window_exceeded: {count} reads in last hour (>{threshold})"
        )
        severity = "medium"
    elif count > threshold * 0.75:
        signals.append(
            f"approaching_threshold: {count} reads (>{int(threshold * 0.75)})"
        )
        severity = "low"

    return AnomalyResponse(
        anomaly=bool(signals) and severity in ("medium", "high"),
        signals=signals,
        severity=severity,
        count_in_window=count,
        window_sec=ANOMALY_WINDOW_SEC,
        threshold=threshold,
    )


# ── Endpoints: PIPL §51 audit aliases (/audit/*) ──────────────────────────
#
# Sibling teams (老郑/老蔡) expect ``/compliance/audit/*`` paths to mirror the
# existing ``/compliance/sensitive-pi/*`` audit endpoints. Alias the trail
# reads here so regulator-facing tooling has a stable URL shape.


@router.get("/audit/brand/{brand_id}")
async def get_brand_audit_alias(
    brand_id: str,
    from_ts: int | None = None,
    to_ts: int | None = None,
    action: str | None = None,
    field: str | None = None,
    accessor_user_id: str | None = None,
    limit: int = 200,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Alias of ``/sensitive-pi/brand/{brand_id}/audit`` (PIPL §51)."""
    return await get_brand_audit(
        brand_id=brand_id,
        from_ts=from_ts,
        to_ts=to_ts,
        action=action,
        field=field,
        accessor_user_id=accessor_user_id,
        limit=limit,
        r=r,
    )


@router.get("/audit/user/{user_id}/sensitive")
async def get_user_sensitive_audit_alias(
    user_id: str,
    from_ts: int | None = None,
    to_ts: int | None = None,
    action: str | None = None,
    field: str | None = None,
    limit: int = 100,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Alias of ``/sensitive-pi/{user_id}/audit`` (PIPL §51)."""
    return await get_user_audit(
        user_id=user_id,
        from_ts=from_ts,
        to_ts=to_ts,
        action=action,
        field=field,
        limit=limit,
        r=r,
    )


_SEVERITY_RANK: dict[str, int] = {"low": 1, "medium": 2, "high": 3}


@router.get("/audit/anomalies")
async def get_audit_anomalies(
    brand_id: str | None = None,
    min_severity: Literal["low", "medium", "high"] = "low",
    from_ts: int | None = None,
    to_ts: int | None = None,
    limit: int = 200,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return historical anomaly detections for SOC review.

    Backed by ``compliance:pii_anomaly_log`` (cross-brand) or
    ``compliance:pii_anomaly_log:brand:{brand_id}`` when scoped.
    """
    if limit < 1 or limit > 2000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="limit must be between 1 and 2000",
        )
    threshold_rank = _SEVERITY_RANK.get(min_severity, 1)

    list_key = (
        f"compliance:pii_anomaly_log:brand:{brand_id}"
        if brand_id
        else "compliance:pii_anomaly_log"
    )
    raws = await r.lrange(list_key, 0, PII_AUDIT_MAX_BRAND - 1)

    out: list[dict[str, Any]] = []
    severity_counts: dict[str, int] = {"low": 0, "medium": 0, "high": 0}
    for raw in raws:
        try:
            e = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        ts = e.get("ts", 0)
        if from_ts is not None and ts < from_ts:
            continue
        if to_ts is not None and ts > to_ts:
            continue
        sev = e.get("severity") or "low"
        if _SEVERITY_RANK.get(sev, 0) < threshold_rank:
            continue
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
        if len(out) < limit:
            out.append(e)

    return {
        "brand_id": brand_id,
        "min_severity": min_severity,
        "count": len(out),
        "anomalies": out,
        "summary": {"severity_counts": severity_counts},
    }


# ── Endpoints: retention class registry ──────────────────────────────────
#
# Used by ``/api/v1/consent/data/delete`` to know which scopes carry a
# legal-hold retention requirement and must NOT be erased.

VALID_RETENTION_SCOPES: set[str] = {
    "medical_data",
    "medical_record_retention",
    "financial_data",
    "financial_proof",
    "kyc",
    "pii_kyc",
    "audit",
    "audio_video_recording",
    "tax",
    "general",
}


class RetentionClassConfigure(BaseModel):
    scope: str = Field(..., min_length=1, max_length=64)
    retention_years: int = Field(..., ge=1, le=100)
    mandatory: bool = True
    citation: str | None = Field(default=None, max_length=256)


@router.post("/retention-class/configure")
async def configure_retention_class(
    body: RetentionClassConfigure,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Register / update a retention-class policy for ``scope``.

    The retention class is consulted by ``/consent/data/delete`` so that
    legally-mandated retention windows (e.g. medical records 15y, KYC
    audit 5y) are honored over a user's right-to-erasure request.
    """
    if body.scope not in VALID_RETENTION_SCOPES:
        # Allow unknown scopes — just log; do not reject.
        logger.info(
            "compliance.retention.unknown_scope %s", body.scope
        )
    record = {
        "scope": body.scope,
        "retention_years": str(body.retention_years),
        "mandatory": "1" if body.mandatory else "0",
        "citation": body.citation or "",
        "configured_at": str(int(time.time())),
    }
    await r.hset("compliance:retention_classes", body.scope, json.dumps(record))
    return {"ok": True, **record, "retention_years": body.retention_years, "mandatory": body.mandatory}


@router.get("/retention-class")
async def list_retention_classes(
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Return every configured retention class."""
    raw = await r.hgetall("compliance:retention_classes")
    classes: list[dict[str, Any]] = []
    for scope, payload in raw.items():
        try:
            rec = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            continue
        try:
            rec["retention_years"] = int(rec.get("retention_years", 0))
        except (TypeError, ValueError):
            rec["retention_years"] = 0
        rec["mandatory"] = rec.get("mandatory") in ("1", "true", "True")
        classes.append(rec)
    return {"count": len(classes), "classes": classes}


async def get_retention_classes_internal(
    r: aioredis.Redis,
) -> dict[str, dict[str, Any]]:
    """Return ``{scope: {retention_years, mandatory, citation}}``.

    Sibling routers (consent.delete_data) call this to decide which keys
    they must NOT delete.
    """
    raw = await r.hgetall("compliance:retention_classes")
    out: dict[str, dict[str, Any]] = {}
    for scope, payload in raw.items():
        try:
            rec = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            continue
        try:
            years = int(rec.get("retention_years", 0))
        except (TypeError, ValueError):
            years = 0
        out[scope] = {
            "retention_years": years,
            "mandatory": rec.get("mandatory") in ("1", "true", "True"),
            "citation": rec.get("citation") or None,
        }
    return out


# ── Endpoints: brand compliance dashboard ────────────────────────────────


@router.get("/brand/{brand_id}/dashboard")
async def get_brand_compliance_dashboard(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """One-shot regulator-facing dashboard for a brand.

    Aggregates 30d PII write/read counters, open anomalies, consent
    compliance rate, document signatures, last audit export. Hits only
    pre-computed counters so it's O(30) reads.
    """
    now = int(time.time())
    today_bucket = now // 86400
    total_writes = 0
    total_reads = 0
    counter_keys = [
        f"compliance:pii_counter:brand:{brand_id}:{today_bucket - i}"
        for i in range(30)
    ]
    # HASH counters — batched via pipeline.
    pipe = r.pipeline()
    for k in counter_keys:
        pipe.hgetall(k)
    bucket_results = await pipe.execute()
    for raw in bucket_results:
        if not raw:
            continue
        try:
            total_writes += int(raw.get("writes", 0) or 0)
        except (TypeError, ValueError):
            pass
        try:
            total_reads += int(raw.get("reads", 0) or 0)
        except (TypeError, ValueError):
            pass

    # Anomaly counts (open = high+medium, resolved = low or older)
    anom_raws = await r.lrange(
        f"compliance:pii_anomaly_log:brand:{brand_id}",
        0,
        PII_AUDIT_MAX_BRAND - 1,
    )
    anomalies_open = 0
    anomalies_resolved = 0
    cutoff = now - 7 * 86400
    for raw in anom_raws:
        try:
            e = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        sev = e.get("severity", "low")
        ts = e.get("ts", 0)
        if ts >= cutoff and sev in ("medium", "high"):
            anomalies_open += 1
        else:
            anomalies_resolved += 1

    # Consent compliance — opportunistic: ratio of users in brand audit
    # who also carry a non-empty consent:user record.
    audit_raws = await r.lrange(
        f"compliance:pii_audit:brand:{brand_id}", 0, 2000
    )
    distinct_users: set[str] = set()
    for raw in audit_raws:
        try:
            e = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        uid = e.get("user_id")
        if uid:
            distinct_users.add(uid)
    granted_users = 0
    if distinct_users:
        pipe = r.pipeline()
        for uid in distinct_users:
            pipe.exists(f"consent:user:{uid}")
        results = await pipe.execute()
        granted_users = sum(1 for x in results if x)
    consent_rate = (
        granted_users / len(distinct_users) if distinct_users else 0.0
    )

    # Document signatures count (best-effort: brand has no direct list,
    # this is a global tally for now).
    doc_sigs = 0
    try:
        doc_sigs = await r.hlen("compliance:document_sig_index") or 0
    except Exception:
        doc_sigs = 0

    # Last audit export (per brand-level marker)
    last_export = await r.get(f"compliance:last_audit_export:brand:{brand_id}")
    try:
        last_export_ts = int(last_export) if last_export else None
    except (TypeError, ValueError):
        last_export_ts = None

    return {
        "brand_id": brand_id,
        "generated_at": now,
        "total_pii_writes_30d": total_writes,
        "total_pii_reads_30d": total_reads,
        "anomalies_open": anomalies_open,
        "anomalies_resolved": anomalies_resolved,
        "consent_compliance_rate": round(consent_rate, 4),
        "tracked_users": len(distinct_users),
        "consenting_users": granted_users,
        "document_signatures_count": doc_sigs,
        "last_audit_export": last_export_ts,
    }


# ── Public exports ────────────────────────────────────────────────────────

__all__ = [
    "router",
    "scan_internal",
    "log_sensitive_pi_internal",
    "get_retention_classes_internal",
    "VALID_INDUSTRIES",
    "VALID_PII_FIELDS",
    "VALID_PII_ACTIONS",
    "VALID_RETENTION_SCOPES",
    "ANOMALY_THRESHOLDS",
    "REQUIRED_DISCLAIMERS",
]
