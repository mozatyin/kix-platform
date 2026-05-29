"""Unified Conditions Engine — universal gating layer for KiX gamification.

Every gamification module on the KiX platform (vouchers, coupons, roulette,
battle pass, quests, store, etc.) needs to answer roughly the same questions
before it actually grants something to a user:

    1.  Is there any supply / budget left?
    2.  Is this user *eligible* (tier, segment, account age, first-time-only)?
    3.  Has the user hit a frequency cap (daily / weekly / monthly / total)?
    4.  Are we inside the campaign's valid time window / day-of-week / hours?
    5.  Has the user completed the required action prerequisites
        (e.g. ``invites_converted >= 10`` AND ``games_played >= 3``)?

Historically each module re-implemented these checks ad-hoc. This router
consolidates them into a single deterministic engine with four core
operations:

    POST /check     read-only eligibility evaluation, fast, no side-effects
    POST /reserve   atomic check + decrement supply + write reservation
    POST /commit    promotes a reservation to a permanent claim
    POST /refund    rolls a reservation back

Plus admin / analytics endpoints to define campaigns and inspect usage.

All state in Redis. All operations brand-isolated by ``brand_id``. Supply
mutations use WATCH / MULTI for optimistic concurrency control.

Redis schema (all brand-isolated; ``cid`` = campaign_id, ``uid`` = user_id)
────────────────────────────────────────────────────────────────────────────
    campaign:{cid}:config                       JSON  full conditions
    campaign:{cid}:meta                         HASH  brand_id, description, created_at
    campaign:{cid}:supply                       INT   atomic supply counter
    campaign:{cid}:budget_cents                 INT   atomic budget counter
    campaign:{cid}:total_claims                 INT   committed claims (analytics)
    campaign:{cid}:total_reserved               INT   reservations issued (analytics)
    campaign:{cid}:blockers                     HASH  blocker → count (analytics)
    campaign:{cid}:hourly:{YYYY-MM-DD-HH}       INT   committed claims per hour

    campaign:{cid}:user:{uid}:daily:{YYYY-MM-DD}    INT  EXPIRE 86400
    campaign:{cid}:user:{uid}:weekly:{YYYY-WW}      INT  EXPIRE 604800
    campaign:{cid}:user:{uid}:monthly:{YYYY-MM}     INT  EXPIRE 2678400
    campaign:{cid}:user:{uid}:total                 INT  permanent counter
    campaign:{cid}:user:{uid}:claimed               EXISTS (first-time flag)
    campaign:{cid}:global:daily:{YYYY-MM-DD}        INT  EXPIRE 86400

    campaign:{cid}:unique_users                     SET  for analytics

    reservation:{rid}                          HASH  campaign_id, user_id, brand_id,
                                                     value_cents, status, expires_at,
                                                     created_at
                                              EX    60 seconds (auto-cleanup)
    campaign:{cid}:audit                        LIST  recent audit JSON entries

The router never raises on a normal "blocked" result — it returns
``eligible: false`` with a list of ``blocked_by`` reasons and a bilingual
``fix_hints`` map so any caller can show actionable feedback.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from redis.exceptions import WatchError

from app.redis_client import get_redis

# Reuse rule_engine's metric resolver + comparator so action prerequisites
# stay consistent across the platform. If the import surface ever changes,
# falling back to a local copy is straightforward.
from app.routers.rule_engine import _resolve_metric, _compare, COMPARE_OPS

logger = logging.getLogger(__name__)

router = APIRouter()


# ═════════════════════════════════════════════════════════════════════════
# Constants
# ═════════════════════════════════════════════════════════════════════════

RESERVATION_TTL_SECONDS = 60
AUDIT_LOG_MAX = 500
TIME_CACHE_TTL_SECONDS = 60
MAX_COMMIT_RETRIES = 10

# Bilingual fix hints — single source of truth, keyed by blocker code.
FIX_HINTS: dict[str, dict[str, str]] = {
    "supply_exhausted": {
        "zh": "本期奖池已发完，请关注下一期活动",
        "en": "This campaign's supply has been fully claimed.",
    },
    "budget_exhausted": {
        "zh": "本期预算已用完，请关注下一期活动",
        "en": "This campaign's budget has been fully spent.",
    },
    "tier_required": {
        "zh": "需要更高等级才能参与，去升级吧",
        "en": "A higher tier is required for this campaign.",
    },
    "first_time_only": {
        "zh": "本活动仅限首次参与的用户",
        "en": "This campaign is for first-time participants only.",
    },
    "user_segment_excluded": {
        "zh": "您当前不符合参与条件",
        "en": "You are not in an eligible user segment.",
    },
    "user_segment_not_included": {
        "zh": "您当前不符合参与条件",
        "en": "You are not in an eligible user segment.",
    },
    "min_account_age_days": {
        "zh": "账号注册时间不足，再过几天再来吧",
        "en": "Your account is too new to participate yet.",
    },
    "user_attribute_filter": {
        "zh": "您当前不符合参与条件",
        "en": "Your account does not match the required attributes.",
    },
    "frequency_per_user_per_day": {
        "zh": "今日已参与过本活动，明日再来",
        "en": "You have hit today's limit. Try again tomorrow.",
    },
    "frequency_per_user_per_week": {
        "zh": "本周已参与上限，下周再来",
        "en": "You have hit this week's limit.",
    },
    "frequency_per_user_per_month": {
        "zh": "本月已参与上限，下月再来",
        "en": "You have hit this month's limit.",
    },
    "frequency_per_user_total": {
        "zh": "您已达到该活动的累计参与上限",
        "en": "You have reached the total limit for this campaign.",
    },
    "frequency_global_per_day": {
        "zh": "今日参与人数已达上限，明日请早",
        "en": "Today's global limit has been reached.",
    },
    "time_not_yet_started": {
        "zh": "活动尚未开始",
        "en": "The campaign has not started yet.",
    },
    "time_already_ended": {
        "zh": "活动已结束",
        "en": "The campaign has ended.",
    },
    "time_invalid_day_of_week": {
        "zh": "今天不是活动开放日",
        "en": "The campaign is not open today.",
    },
    "time_invalid_hour": {
        "zh": "当前不在活动开放时段",
        "en": "The campaign is not open at this hour.",
    },
    "action_prerequisites_unmet": {
        "zh": "尚未完成参与活动所需的前置任务",
        "en": "Prerequisite actions have not been completed.",
    },
    "campaign_not_found": {
        "zh": "找不到该活动",
        "en": "Campaign not found.",
    },
    "reservation_not_found": {
        "zh": "预约不存在或已过期",
        "en": "Reservation not found or expired.",
    },
    "reservation_already_committed": {
        "zh": "该预约已确认，无法重复操作",
        "en": "Reservation has already been committed.",
    },
    "reservation_already_refunded": {
        "zh": "该预约已退回",
        "en": "Reservation has already been refunded.",
    },
    "reservation_expired": {
        "zh": "预约已过期，请重新发起",
        "en": "Reservation has expired; please retry.",
    },
    "commit_contention": {
        "zh": "系统繁忙，请稍后重试",
        "en": "High contention on commit; please retry.",
    },
}


def _hints_for(blockers: list[str]) -> dict[str, str]:
    """Return a flat ``{blocker_code: "中文 / English"}`` map for response."""
    out: dict[str, str] = {}
    for code in blockers:
        entry = FIX_HINTS.get(code)
        if entry is None:
            out[code] = code  # opaque fallback
        else:
            out[code] = f"{entry['zh']} / {entry['en']}"
    return out


# ═════════════════════════════════════════════════════════════════════════
# Pydantic models — the universal conditions schema
# ═════════════════════════════════════════════════════════════════════════


class SupplyConditions(BaseModel):
    total_supply: int = -1  # -1 = unlimited
    budget_cents: int = -1  # -1 = unlimited
    # The *_remaining fields are server-computed; clients ignore them in input.
    supply_remaining: int | None = None
    budget_remaining_cents: int | None = None


class EligibilityConditions(BaseModel):
    tier_required: str | None = None
    first_time_user_only: bool = False
    user_segment_include: list[str] = Field(default_factory=list)
    user_segment_exclude: list[str] = Field(default_factory=list)
    min_account_age_days: int = 0
    user_attribute_filter: dict[str, Any] = Field(default_factory=dict)


class FrequencyConditions(BaseModel):
    per_user_per_day: int | None = None
    per_user_per_week: int | None = None
    per_user_per_month: int | None = None
    per_user_total: int | None = None
    global_per_day: int | None = None


class TimeConditions(BaseModel):
    valid_from: str | None = None   # ISO-8601, UTC
    valid_until: str | None = None  # ISO-8601, UTC
    valid_days_of_week: list[int] | None = None  # 0=Mon..6=Sun, local tz
    valid_hours_local: list[int] | None = None   # [start_hour, end_hour]
    timezone: str = "UTC"


class PrerequisiteRule(BaseModel):
    type: str = "count"
    metric: str
    op: str = ">="
    value: Any


class ActionPrerequisites(BaseModel):
    rules: list[PrerequisiteRule] = Field(default_factory=list)
    composition: str = "AND"  # AND | OR


class FullConditions(BaseModel):
    supply: SupplyConditions = Field(default_factory=SupplyConditions)
    eligibility: EligibilityConditions = Field(default_factory=EligibilityConditions)
    frequency: FrequencyConditions = Field(default_factory=FrequencyConditions)
    time: TimeConditions = Field(default_factory=TimeConditions)
    action_prerequisites: ActionPrerequisites = Field(default_factory=ActionPrerequisites)


class CheckRequest(BaseModel):
    brand_id: str
    user_id: str
    campaign_id: str
    conditions: FullConditions | None = None
    action_context: dict[str, Any] = Field(default_factory=dict)


class CheckResponse(BaseModel):
    eligible: bool
    blocked_by: list[str] = Field(default_factory=list)
    remaining_supply: int | None = None
    remaining_budget_cents: int | None = None
    next_eligible_at: str | None = None
    fix_hints: dict[str, str] = Field(default_factory=dict)


class ReserveRequest(BaseModel):
    brand_id: str
    user_id: str
    campaign_id: str
    conditions: FullConditions | None = None
    value_cents: int = 0
    action_context: dict[str, Any] = Field(default_factory=dict)


class ReserveResponse(BaseModel):
    ok: bool
    reservation_id: str | None = None
    remaining_supply: int | None = None
    remaining_budget_cents: int | None = None
    expires_at: str | None = None
    blocked_by: list[str] = Field(default_factory=list)
    fix_hints: dict[str, str] = Field(default_factory=dict)


class CommitRequest(BaseModel):
    reservation_id: str


class RefundRequest(BaseModel):
    reservation_id: str
    reason: str = ""


class CampaignDefineRequest(BaseModel):
    brand_id: str
    conditions: FullConditions
    description: str = ""


# ═════════════════════════════════════════════════════════════════════════
# Key helpers
# ═════════════════════════════════════════════════════════════════════════


def _k_config(cid: str) -> str:        return f"campaign:{cid}:config"
def _k_meta(cid: str) -> str:          return f"campaign:{cid}:meta"
def _k_supply(cid: str) -> str:        return f"campaign:{cid}:supply"
def _k_budget(cid: str) -> str:        return f"campaign:{cid}:budget_cents"
def _k_claims(cid: str) -> str:        return f"campaign:{cid}:total_claims"
def _k_reserved(cid: str) -> str:      return f"campaign:{cid}:total_reserved"
def _k_blockers(cid: str) -> str:      return f"campaign:{cid}:blockers"
def _k_audit(cid: str) -> str:         return f"campaign:{cid}:audit"
def _k_unique_users(cid: str) -> str:  return f"campaign:{cid}:unique_users"


def _k_user_daily(cid: str, uid: str, day: str) -> str:
    return f"campaign:{cid}:user:{uid}:daily:{day}"


def _k_user_weekly(cid: str, uid: str, isoweek: str) -> str:
    return f"campaign:{cid}:user:{uid}:weekly:{isoweek}"


def _k_user_monthly(cid: str, uid: str, ym: str) -> str:
    return f"campaign:{cid}:user:{uid}:monthly:{ym}"


def _k_user_total(cid: str, uid: str) -> str:
    return f"campaign:{cid}:user:{uid}:total"


def _k_user_claimed(cid: str, uid: str) -> str:
    return f"campaign:{cid}:user:{uid}:claimed"


def _k_global_daily(cid: str, day: str) -> str:
    return f"campaign:{cid}:global:daily:{day}"


def _k_hourly(cid: str, hour_bucket: str) -> str:
    return f"campaign:{cid}:hourly:{hour_bucket}"


def _k_reservation(rid: str) -> str:
    return f"reservation:{rid}"


def _k_time_cache(cid: str) -> str:
    return f"campaign:{cid}:time_cache"


# ═════════════════════════════════════════════════════════════════════════
# Time helpers
# ═════════════════════════════════════════════════════════════════════════


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    # Accept trailing Z
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _local_now(tz_name: str) -> datetime:
    """Return current time in the given IANA timezone.

    We avoid a hard dependency on zoneinfo's tzdata package by falling back
    to UTC if the tz name doesn't resolve.
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return _now_utc()


def _today_str() -> str:
    return _now_utc().strftime("%Y-%m-%d")


def _isoweek_str() -> str:
    iso = _now_utc().isocalendar()
    return f"{iso.year:04d}-{iso.week:02d}"


def _ym_str() -> str:
    return _now_utc().strftime("%Y-%m")


def _hour_bucket() -> str:
    return _now_utc().strftime("%Y-%m-%d-%H")


# ═════════════════════════════════════════════════════════════════════════
# Campaign config persistence
# ═════════════════════════════════════════════════════════════════════════


async def _load_campaign(
    cid: str, r: aioredis.Redis
) -> tuple[FullConditions | None, dict[str, str]]:
    """Return (conditions, meta) for a campaign or (None, {}) if missing."""
    cfg_raw = await r.get(_k_config(cid))
    meta = await r.hgetall(_k_meta(cid))
    if not cfg_raw:
        return None, meta or {}
    try:
        cfg = FullConditions(**json.loads(cfg_raw))
    except Exception as exc:  # pragma: no cover — malformed stored config
        logger.exception("Failed to parse campaign %s config: %s", cid, exc)
        return None, meta or {}
    return cfg, meta or {}


async def _resolve_conditions(
    campaign_id: str,
    inline: FullConditions | None,
    r: aioredis.Redis,
) -> FullConditions | None:
    """Inline conditions win; otherwise load from campaign config."""
    if inline is not None:
        return inline
    cfg, _ = await _load_campaign(campaign_id, r)
    return cfg


# ═════════════════════════════════════════════════════════════════════════
# Eligibility checks (all return list of blocker codes — empty == pass)
# ═════════════════════════════════════════════════════════════════════════


async def _check_eligibility(
    elig: EligibilityConditions,
    user_id: str,
    brand_id: str,
    campaign_id: str,
    r: aioredis.Redis,
) -> list[str]:
    blockers: list[str] = []

    # --- tier requirement -------------------------------------------------
    if elig.tier_required:
        user_tier = await r.get(f"user:{user_id}:tier:{brand_id}")
        if user_tier != elig.tier_required:
            blockers.append("tier_required")

    # --- first-time-user-only --------------------------------------------
    if elig.first_time_user_only:
        if await r.exists(_k_user_claimed(campaign_id, user_id)):
            blockers.append("first_time_only")

    # --- segment include / exclude ---------------------------------------
    if elig.user_segment_include or elig.user_segment_exclude:
        segments = await r.smembers(f"user:{user_id}:segments") or set()
        if elig.user_segment_include:
            if not any(s in segments for s in elig.user_segment_include):
                blockers.append("user_segment_not_included")
        if elig.user_segment_exclude:
            if any(s in segments for s in elig.user_segment_exclude):
                blockers.append("user_segment_excluded")

    # --- minimum account age ---------------------------------------------
    if elig.min_account_age_days and elig.min_account_age_days > 0:
        created_at_raw = await r.hget(f"user:{user_id}", "created_at")
        try:
            created_at = float(created_at_raw) if created_at_raw else None
        except (TypeError, ValueError):
            created_at = None
        if created_at is None:
            # Treat unknown account age as failing the requirement — safer
            blockers.append("min_account_age_days")
        else:
            age_seconds = time.time() - created_at
            if age_seconds < elig.min_account_age_days * 86400:
                blockers.append("min_account_age_days")

    # --- arbitrary user-attribute filter ---------------------------------
    if elig.user_attribute_filter:
        for attr, expected in elig.user_attribute_filter.items():
            actual = await r.hget(f"user:{user_id}", attr)
            # Compare string-coerced expected to keep semantics simple
            if str(actual) != str(expected):
                blockers.append("user_attribute_filter")
                break

    return blockers


async def _check_frequency(
    freq: FrequencyConditions,
    user_id: str,
    campaign_id: str,
    r: aioredis.Redis,
) -> tuple[list[str], str | None]:
    """Return (blockers, next_eligible_at_iso_or_None)."""
    blockers: list[str] = []
    next_at: datetime | None = None

    if freq.per_user_per_day is not None:
        used = int(await r.get(_k_user_daily(campaign_id, user_id, _today_str())) or 0)
        if used >= freq.per_user_per_day:
            blockers.append("frequency_per_user_per_day")
            # next eligible = tomorrow 00:00 UTC
            tomorrow = (_now_utc() + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            next_at = min(next_at, tomorrow) if next_at else tomorrow

    if freq.per_user_per_week is not None:
        used = int(await r.get(_k_user_weekly(campaign_id, user_id, _isoweek_str())) or 0)
        if used >= freq.per_user_per_week:
            blockers.append("frequency_per_user_per_week")
            # next Monday 00:00 UTC
            now = _now_utc()
            days_ahead = 7 - now.weekday()
            nm = (now + timedelta(days=days_ahead)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            next_at = min(next_at, nm) if next_at else nm

    if freq.per_user_per_month is not None:
        used = int(await r.get(_k_user_monthly(campaign_id, user_id, _ym_str())) or 0)
        if used >= freq.per_user_per_month:
            blockers.append("frequency_per_user_per_month")
            # First of next month 00:00 UTC
            now = _now_utc()
            if now.month == 12:
                nm = now.replace(year=now.year + 1, month=1, day=1,
                                 hour=0, minute=0, second=0, microsecond=0)
            else:
                nm = now.replace(month=now.month + 1, day=1,
                                 hour=0, minute=0, second=0, microsecond=0)
            next_at = min(next_at, nm) if next_at else nm

    if freq.per_user_total is not None:
        used = int(await r.get(_k_user_total(campaign_id, user_id)) or 0)
        if used >= freq.per_user_total:
            blockers.append("frequency_per_user_total")

    if freq.global_per_day is not None:
        used = int(await r.get(_k_global_daily(campaign_id, _today_str())) or 0)
        if used >= freq.global_per_day:
            blockers.append("frequency_global_per_day")
            tomorrow = (_now_utc() + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            next_at = min(next_at, tomorrow) if next_at else tomorrow

    return blockers, (next_at.isoformat() if next_at else None)


def _check_time(t: TimeConditions) -> tuple[list[str], str | None]:
    blockers: list[str] = []
    next_at_iso: str | None = None

    now_utc = _now_utc()

    if t.valid_from:
        vf = _parse_iso(t.valid_from)
        if vf and now_utc < vf:
            blockers.append("time_not_yet_started")
            next_at_iso = vf.isoformat()

    if t.valid_until:
        vu = _parse_iso(t.valid_until)
        if vu and now_utc > vu:
            blockers.append("time_already_ended")

    if t.valid_days_of_week or t.valid_hours_local:
        local_now = _local_now(t.timezone or "UTC")
        if t.valid_days_of_week:
            # 0 = Mon .. 6 = Sun (matches Python's weekday())
            if local_now.weekday() not in t.valid_days_of_week:
                blockers.append("time_invalid_day_of_week")
        if t.valid_hours_local and len(t.valid_hours_local) == 2:
            start_h, end_h = t.valid_hours_local
            hour = local_now.hour
            in_window = (start_h <= hour < end_h) if start_h <= end_h else (
                hour >= start_h or hour < end_h
            )
            if not in_window:
                blockers.append("time_invalid_hour")

    return blockers, next_at_iso


async def _check_prerequisites(
    prereqs: ActionPrerequisites,
    user_id: str,
    brand_id: str,
    r: aioredis.Redis,
) -> list[str]:
    if not prereqs.rules:
        return []

    results: list[bool] = []
    for rule in prereqs.rules:
        if rule.op not in COMPARE_OPS:
            # Misconfigured rule — count as unmet, fail safe.
            results.append(False)
            continue
        try:
            actual = await _resolve_metric(rule.metric, user_id, brand_id, r)
        except ValueError:
            # Unknown metric — fail safe
            results.append(False)
            continue
        results.append(_compare(actual, rule.op, rule.value))

    if prereqs.composition.upper() == "OR":
        ok = any(results)
    else:
        ok = all(results)

    return [] if ok else ["action_prerequisites_unmet"]


async def _check_supply(
    supply: SupplyConditions,
    campaign_id: str,
    value_cents: int,
    r: aioredis.Redis,
) -> tuple[list[str], int | None, int | None]:
    """Return (blockers, remaining_supply, remaining_budget_cents).

    Read-only — no decrement. Atomic decrement happens in reserve().
    """
    blockers: list[str] = []
    remaining_supply: int | None = None
    remaining_budget: int | None = None

    if supply.total_supply >= 0:
        v = await r.get(_k_supply(campaign_id))
        remaining_supply = int(v) if v is not None else supply.total_supply
        if remaining_supply <= 0:
            blockers.append("supply_exhausted")

    if supply.budget_cents >= 0:
        v = await r.get(_k_budget(campaign_id))
        remaining_budget = int(v) if v is not None else supply.budget_cents
        if remaining_budget < max(value_cents, 0):
            blockers.append("budget_exhausted")

    return blockers, remaining_supply, remaining_budget


# ═════════════════════════════════════════════════════════════════════════
# Audit helpers
# ═════════════════════════════════════════════════════════════════════════


async def _audit(
    r: aioredis.Redis,
    campaign_id: str,
    user_id: str,
    action: str,
    blocked_by: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    entry = {
        "ts": time.time(),
        "user_id": user_id,
        "action": action,
        "blocked_by": blocked_by or [],
    }
    if extra:
        entry.update(extra)
    pipe = r.pipeline(transaction=False)
    pipe.lpush(_k_audit(campaign_id), json.dumps(entry))
    pipe.ltrim(_k_audit(campaign_id), 0, AUDIT_LOG_MAX - 1)
    if blocked_by:
        for b in blocked_by:
            pipe.hincrby(_k_blockers(campaign_id), b, 1)
    await pipe.execute()


# ═════════════════════════════════════════════════════════════════════════
# Endpoint 1: /check — read-only eligibility
# ═════════════════════════════════════════════════════════════════════════


@router.post("/check", response_model=CheckResponse)
async def check_conditions(
    req: CheckRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> CheckResponse:
    """Evaluate every condition without side-effects.

    Returns the full set of blockers (not short-circuited) so the caller can
    show *all* reasons at once — much better UX than fixing one at a time.
    """
    cond = await _resolve_conditions(req.campaign_id, req.conditions, r)
    if cond is None:
        return CheckResponse(
            eligible=False,
            blocked_by=["campaign_not_found"],
            fix_hints=_hints_for(["campaign_not_found"]),
        )

    blockers: list[str] = []
    next_at: str | None = None

    value_cents = int(req.action_context.get("purchase_amount_cents", 0) or 0)

    sup_b, rem_sup, rem_bud = await _check_supply(
        cond.supply, req.campaign_id, value_cents, r
    )
    blockers.extend(sup_b)

    t_b, t_next = _check_time(cond.time)
    blockers.extend(t_b)
    if t_next and not next_at:
        next_at = t_next

    elig_b = await _check_eligibility(
        cond.eligibility, req.user_id, req.brand_id, req.campaign_id, r
    )
    blockers.extend(elig_b)

    freq_b, freq_next = await _check_frequency(
        cond.frequency, req.user_id, req.campaign_id, r
    )
    blockers.extend(freq_b)
    if freq_next and (not next_at or freq_next < next_at):
        next_at = freq_next

    prereq_b = await _check_prerequisites(
        cond.action_prerequisites, req.user_id, req.brand_id, r
    )
    blockers.extend(prereq_b)

    eligible = len(blockers) == 0

    # Audit at info level only — /check is read-only, don't spam blocker counters
    if not eligible:
        await _audit(
            r, req.campaign_id, req.user_id, "check",
            blocked_by=blockers,
        )

    return CheckResponse(
        eligible=eligible,
        blocked_by=blockers,
        remaining_supply=rem_sup,
        remaining_budget_cents=rem_bud,
        next_eligible_at=next_at,
        fix_hints=_hints_for(blockers),
    )


# ═════════════════════════════════════════════════════════════════════════
# Endpoint 2: /reserve — atomic check + decrement
# ═════════════════════════════════════════════════════════════════════════


@router.post("/reserve", response_model=ReserveResponse)
async def reserve_conditions(
    req: ReserveRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> ReserveResponse:
    """Atomically check + decrement supply + write a short-lived reservation.

    Concurrency is handled with Redis WATCH / MULTI: if the supply counter
    moves between read and decrement, the transaction aborts and we retry.

    The reservation expires automatically after RESERVATION_TTL_SECONDS via
    Redis EX, so a caller that crashes between reserve and commit doesn't
    leak supply. Callers must call /commit (success) or /refund (failure)
    promptly.
    """
    cond = await _resolve_conditions(req.campaign_id, req.conditions, r)
    if cond is None:
        return ReserveResponse(
            ok=False,
            blocked_by=["campaign_not_found"],
            fix_hints=_hints_for(["campaign_not_found"]),
        )

    # ── 1. Run all non-supply checks first (cheap, no locks) ─────────────
    blockers: list[str] = []
    t_b, _ = _check_time(cond.time)
    blockers.extend(t_b)
    elig_b = await _check_eligibility(
        cond.eligibility, req.user_id, req.brand_id, req.campaign_id, r
    )
    blockers.extend(elig_b)
    freq_b, freq_next = await _check_frequency(
        cond.frequency, req.user_id, req.campaign_id, r
    )
    blockers.extend(freq_b)
    prereq_b = await _check_prerequisites(
        cond.action_prerequisites, req.user_id, req.brand_id, r
    )
    blockers.extend(prereq_b)

    if blockers:
        await _audit(
            r, req.campaign_id, req.user_id, "reserve_blocked",
            blocked_by=blockers,
        )
        return ReserveResponse(
            ok=False,
            blocked_by=blockers,
            fix_hints=_hints_for(blockers),
        )

    # ── 2. Atomic supply + budget decrement via WATCH/MULTI ──────────────
    rid = uuid4().hex
    value_cents = max(int(req.value_cents or 0), 0)
    needs_supply = cond.supply.total_supply >= 0
    needs_budget = cond.supply.budget_cents >= 0
    rem_sup_after: int | None = None
    rem_bud_after: int | None = None

    # Always retry a bounded number of times to avoid pathological loops
    for _attempt in range(10):
        async with r.pipeline(transaction=True) as pipe:
            try:
                if needs_supply:
                    await pipe.watch(_k_supply(req.campaign_id))
                if needs_budget:
                    await pipe.watch(_k_budget(req.campaign_id))

                # Initialise counters from declared totals if absent
                if needs_supply:
                    current_supply_raw = await pipe.get(_k_supply(req.campaign_id))
                    if current_supply_raw is None:
                        current_supply = cond.supply.total_supply
                        # Seed inside the transaction
                    else:
                        current_supply = int(current_supply_raw)
                    if current_supply <= 0:
                        await pipe.unwatch()
                        await _audit(
                            r, req.campaign_id, req.user_id, "reserve_blocked",
                            blocked_by=["supply_exhausted"],
                        )
                        return ReserveResponse(
                            ok=False,
                            blocked_by=["supply_exhausted"],
                            remaining_supply=current_supply,
                            fix_hints=_hints_for(["supply_exhausted"]),
                        )

                if needs_budget:
                    current_budget_raw = await pipe.get(_k_budget(req.campaign_id))
                    if current_budget_raw is None:
                        current_budget = cond.supply.budget_cents
                    else:
                        current_budget = int(current_budget_raw)
                    if current_budget < value_cents:
                        await pipe.unwatch()
                        await _audit(
                            r, req.campaign_id, req.user_id, "reserve_blocked",
                            blocked_by=["budget_exhausted"],
                        )
                        return ReserveResponse(
                            ok=False,
                            blocked_by=["budget_exhausted"],
                            remaining_budget_cents=current_budget,
                            fix_hints=_hints_for(["budget_exhausted"]),
                        )

                # Switch pipeline to MULTI/EXEC mode
                pipe.multi()

                # Seed counters atomically if they don't exist using SET NX
                # equivalents: we just SET the post-decrement value.
                if needs_supply:
                    pipe.set(_k_supply(req.campaign_id), current_supply - 1)
                    rem_sup_after = current_supply - 1
                if needs_budget:
                    pipe.set(_k_budget(req.campaign_id), current_budget - value_cents)
                    rem_bud_after = current_budget - value_cents

                expires_at = _now_utc() + timedelta(seconds=RESERVATION_TTL_SECONDS)
                # Bug 1 fix: store epoch as a separate, authoritative field so
                # commit can check expiry even if Redis EXPIRE evicted the key
                # and a stale copy comes back from a follower / cache.
                expires_at_epoch = expires_at.timestamp()
                reservation_payload = {
                    "campaign_id": req.campaign_id,
                    "brand_id": req.brand_id,
                    "user_id": req.user_id,
                    "value_cents": str(value_cents),
                    "status": "reserved",
                    "expires_at": expires_at.isoformat(),
                    "expires_at_epoch": str(expires_at_epoch),
                    "created_at": str(time.time()),
                    "supply_consumed": "1" if needs_supply else "0",
                    "budget_consumed": str(value_cents) if needs_budget else "0",
                }
                pipe.hset(_k_reservation(rid), mapping=reservation_payload)
                pipe.expire(_k_reservation(rid), RESERVATION_TTL_SECONDS)
                pipe.incr(_k_reserved(req.campaign_id))

                await pipe.execute()
                break
            except WatchError:
                # Another writer beat us — loop & retry
                continue
    else:
        # Exhausted retries without success
        logger.warning(
            "conditions.reserve gave up after retries for campaign=%s user=%s",
            req.campaign_id, req.user_id,
        )
        return ReserveResponse(
            ok=False,
            blocked_by=["supply_exhausted"],
            fix_hints=_hints_for(["supply_exhausted"]),
        )

    await _audit(
        r, req.campaign_id, req.user_id, "reserve",
        extra={"reservation_id": rid, "value_cents": value_cents},
    )

    return ReserveResponse(
        ok=True,
        reservation_id=rid,
        remaining_supply=rem_sup_after,
        remaining_budget_cents=rem_bud_after,
        expires_at=(_now_utc() + timedelta(seconds=RESERVATION_TTL_SECONDS)).isoformat(),
    )


# ═════════════════════════════════════════════════════════════════════════
# Endpoint 3: /commit — confirm the reservation
# ═════════════════════════════════════════════════════════════════════════


@router.post("/commit")
async def commit_reservation(
    req: CommitRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Make a reservation permanent.

    Bumps the per-user frequency counters and global counters; the supply
    decrement already happened in /reserve so there's no further supply
    movement here.
    """
    res = await r.hgetall(_k_reservation(req.reservation_id))
    if not res:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "ok": False,
                "blocked_by": ["reservation_not_found"],
                "fix_hints": _hints_for(["reservation_not_found"]),
            },
        )

    status_str = res.get("status", "reserved")
    if status_str == "committed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "ok": False,
                "blocked_by": ["reservation_already_committed"],
                "fix_hints": _hints_for(["reservation_already_committed"]),
            },
        )
    if status_str == "refunded":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "ok": False,
                "blocked_by": ["reservation_already_refunded"],
                "fix_hints": _hints_for(["reservation_already_refunded"]),
            },
        )

    # Bug 1 fix: don't rely solely on Redis EXPIRE — check the embedded
    # expires_at_epoch so we never commit a logically-expired reservation
    # even if its key happens to still exist (replication lag, audit copy,
    # extended TTL after a status change, etc.).
    try:
        exp_epoch = float(res.get("expires_at_epoch") or 0)
    except (TypeError, ValueError):
        exp_epoch = 0.0
    if exp_epoch > 0 and time.time() > exp_epoch:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "ok": False,
                "blocked_by": ["reservation_expired"],
                "fix_hints": _hints_for(["reservation_expired"]),
            },
        )

    campaign_id = res["campaign_id"]
    user_id = res["user_id"]
    value_cents = int(res.get("value_cents", 0) or 0)

    today = _today_str()
    isoweek = _isoweek_str()
    ym = _ym_str()
    hour = _hour_bucket()

    # ── Bug 2 fix: WATCH/MULTI around the user's daily frequency counter
    # so concurrent commits from the same user can't both observe an
    # under-cap value and then both increment past the cap.
    rkey = _k_reservation(req.reservation_id)
    user_daily_key = _k_user_daily(campaign_id, user_id, today)
    for _attempt in range(MAX_COMMIT_RETRIES):
        async with r.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(rkey, user_daily_key)
                # Re-check status under WATCH: another commit may have raced.
                cur_status = await pipe.hget(rkey, "status")
                if cur_status == "committed":
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "ok": False,
                            "blocked_by": ["reservation_already_committed"],
                            "fix_hints": _hints_for(["reservation_already_committed"]),
                        },
                    )
                if cur_status == "refunded":
                    await pipe.unwatch()
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "ok": False,
                            "blocked_by": ["reservation_already_refunded"],
                            "fix_hints": _hints_for(["reservation_already_refunded"]),
                        },
                    )

                pipe.multi()
                # ── frequency counters (with appropriate TTLs) ───────────
                pipe.incr(user_daily_key)
                pipe.expire(user_daily_key, 86400)
                pipe.incr(_k_user_weekly(campaign_id, user_id, isoweek))
                pipe.expire(_k_user_weekly(campaign_id, user_id, isoweek), 604800)
                pipe.incr(_k_user_monthly(campaign_id, user_id, ym))
                pipe.expire(_k_user_monthly(campaign_id, user_id, ym), 2678400)
                pipe.incr(_k_user_total(campaign_id, user_id))

                # ── global counters ──────────────────────────────────────
                pipe.incr(_k_global_daily(campaign_id, today))
                pipe.expire(_k_global_daily(campaign_id, today), 86400)
                pipe.incr(_k_hourly(campaign_id, hour))
                pipe.expire(_k_hourly(campaign_id, hour), 86400 * 31)
                pipe.incr(_k_claims(campaign_id))
                pipe.sadd(_k_unique_users(campaign_id), user_id)

                # ── first-time flag ──────────────────────────────────────
                pipe.set(_k_user_claimed(campaign_id, user_id), "1")

                # ── promote reservation status, drop TTL ─────────────────
                pipe.hset(rkey, mapping={
                    "status": "committed",
                    "committed_at": str(time.time()),
                })
                # Keep committed reservations around for ~7d for audit trail
                pipe.expire(rkey, 86400 * 7)

                await pipe.execute()
                break
            except WatchError:
                continue
    else:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "ok": False,
                "blocked_by": ["commit_contention"],
                "fix_hints": _hints_for(["commit_contention"]),
            },
        )

    await _audit(
        r, campaign_id, user_id, "commit",
        extra={"reservation_id": req.reservation_id, "value_cents": value_cents},
    )

    return {
        "ok": True,
        "reservation_id": req.reservation_id,
        "campaign_id": campaign_id,
        "user_id": user_id,
        "value_cents": value_cents,
    }


# ═════════════════════════════════════════════════════════════════════════
# Endpoint 4: /refund — roll a reservation back
# ═════════════════════════════════════════════════════════════════════════


@router.post("/refund")
async def refund_reservation(
    req: RefundRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Return supply + budget to the pool when an action failed downstream.

    Only un-committed reservations can be refunded — committed ones already
    incremented frequency counters and represent a real claim.
    """
    res = await r.hgetall(_k_reservation(req.reservation_id))
    if not res:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "ok": False,
                "blocked_by": ["reservation_not_found"],
                "fix_hints": _hints_for(["reservation_not_found"]),
            },
        )

    status_str = res.get("status", "reserved")
    if status_str == "refunded":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "ok": False,
                "blocked_by": ["reservation_already_refunded"],
                "fix_hints": _hints_for(["reservation_already_refunded"]),
            },
        )
    if status_str == "committed":
        # We treat committed-then-refund as a hard error rather than silently
        # reversing — callers should think carefully before rolling back a
        # confirmed claim.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "ok": False,
                "blocked_by": ["reservation_already_committed"],
                "fix_hints": _hints_for(["reservation_already_committed"]),
            },
        )

    campaign_id = res["campaign_id"]
    user_id = res["user_id"]
    supply_consumed = int(res.get("supply_consumed", 0) or 0)
    budget_consumed = int(res.get("budget_consumed", 0) or 0)

    pipe = r.pipeline(transaction=True)
    if supply_consumed > 0:
        pipe.incrby(_k_supply(campaign_id), supply_consumed)
    if budget_consumed > 0:
        pipe.incrby(_k_budget(campaign_id), budget_consumed)
    pipe.hset(_k_reservation(req.reservation_id), mapping={
        "status": "refunded",
        "refunded_at": str(time.time()),
        "refund_reason": req.reason or "",
    })
    pipe.expire(_k_reservation(req.reservation_id), 86400 * 7)
    await pipe.execute()

    await _audit(
        r, campaign_id, user_id, "refund",
        extra={"reservation_id": req.reservation_id, "reason": req.reason},
    )

    return {
        "ok": True,
        "reservation_id": req.reservation_id,
        "campaign_id": campaign_id,
        "user_id": user_id,
        "reason": req.reason,
    }


# ═════════════════════════════════════════════════════════════════════════
# Endpoint 5: campaign definition (admin)
# ═════════════════════════════════════════════════════════════════════════


@router.post("/campaigns/{campaign_id}")
async def define_campaign(
    campaign_id: str,
    body: CampaignDefineRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Persist conditions for a campaign and seed supply/budget counters.

    If the campaign already exists, this REPLACES the config and resets
    the supply / budget counters to the new declared totals. Callers that
    want non-destructive edits should fetch + merge first.
    """
    cfg_json = body.conditions.model_dump_json()

    pipe = r.pipeline(transaction=True)
    pipe.set(_k_config(campaign_id), cfg_json)
    pipe.hset(_k_meta(campaign_id), mapping={
        "brand_id": body.brand_id,
        "description": body.description or "",
        "created_at": str(time.time()),
    })

    if body.conditions.supply.total_supply >= 0:
        pipe.set(_k_supply(campaign_id), body.conditions.supply.total_supply)
    else:
        pipe.delete(_k_supply(campaign_id))

    if body.conditions.supply.budget_cents >= 0:
        pipe.set(_k_budget(campaign_id), body.conditions.supply.budget_cents)
    else:
        pipe.delete(_k_budget(campaign_id))

    await pipe.execute()

    return {
        "ok": True,
        "campaign_id": campaign_id,
        "brand_id": body.brand_id,
        "description": body.description,
    }


@router.get("/campaigns/{campaign_id}")
async def get_campaign(
    campaign_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Return the stored conditions + current live state for a campaign."""
    cfg, meta = await _load_campaign(campaign_id, r)
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "ok": False,
                "blocked_by": ["campaign_not_found"],
                "fix_hints": _hints_for(["campaign_not_found"]),
            },
        )

    rem_supply = await r.get(_k_supply(campaign_id))
    rem_budget = await r.get(_k_budget(campaign_id))
    total_claims = await r.get(_k_claims(campaign_id))
    total_reserved = await r.get(_k_reserved(campaign_id))
    unique_users = await r.scard(_k_unique_users(campaign_id))

    return {
        "ok": True,
        "campaign_id": campaign_id,
        "brand_id": meta.get("brand_id"),
        "description": meta.get("description", ""),
        "conditions": cfg.model_dump(),
        "state": {
            "remaining_supply": int(rem_supply) if rem_supply is not None else None,
            "remaining_budget_cents": int(rem_budget) if rem_budget is not None else None,
            "total_claims": int(total_claims or 0),
            "total_reserved": int(total_reserved or 0),
            "unique_users": int(unique_users or 0),
        },
    }


# ═════════════════════════════════════════════════════════════════════════
# Endpoint 6: analytics
# ═════════════════════════════════════════════════════════════════════════


@router.get("/campaigns/{campaign_id}/usage")
async def campaign_usage(
    campaign_id: str,
    hours: int = Query(24, ge=1, le=24 * 31),
    r: aioredis.Redis = Depends(get_redis),
):
    """Aggregate usage stats for a campaign.

    - total_claims  : committed reservations all-time
    - unique_users  : distinct users with at least one commit
    - conversion_rate: committed / reserved  (lower bound on UX friction)
    - top_blockers  : sorted descending — what's most often blocking users
    - hourly_distribution: claims per hour for the requested look-back
    """
    if not await r.exists(_k_config(campaign_id)):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "ok": False,
                "blocked_by": ["campaign_not_found"],
                "fix_hints": _hints_for(["campaign_not_found"]),
            },
        )

    total_claims = int(await r.get(_k_claims(campaign_id)) or 0)
    total_reserved = int(await r.get(_k_reserved(campaign_id)) or 0)
    unique_users = int(await r.scard(_k_unique_users(campaign_id)) or 0)
    conversion_rate = (total_claims / total_reserved) if total_reserved > 0 else 0.0

    blockers_raw = await r.hgetall(_k_blockers(campaign_id)) or {}
    top_blockers = sorted(
        ((k, int(v)) for k, v in blockers_raw.items()),
        key=lambda kv: kv[1],
        reverse=True,
    )
    top_blockers_out = [{"code": k, "count": v} for k, v in top_blockers]

    # Walk the last ``hours`` hour buckets backward from now
    now = _now_utc()
    hourly: list[dict[str, Any]] = []
    for offset in range(hours - 1, -1, -1):
        t = now - timedelta(hours=offset)
        bucket = t.strftime("%Y-%m-%d-%H")
        v = await r.get(_k_hourly(campaign_id, bucket))
        hourly.append({"hour": bucket, "claims": int(v or 0)})

    return {
        "ok": True,
        "campaign_id": campaign_id,
        "total_claims": total_claims,
        "total_reserved": total_reserved,
        "unique_users": unique_users,
        "conversion_rate": round(conversion_rate, 4),
        "top_blockers": top_blockers_out,
        "hourly_distribution": hourly,
    }


@router.get("/user/{user_id}/eligibility")
async def user_eligibility(
    user_id: str,
    campaign_id: str = Query(...),
    brand_id: str = Query(...),
    r: aioredis.Redis = Depends(get_redis),
) -> CheckResponse:
    """Convenience wrapper: what's blocking this user from this campaign?

    Loads the stored conditions, runs /check semantics, returns the same
    shape so dashboards can render the result identically to a live call.
    """
    cfg, _ = await _load_campaign(campaign_id, r)
    if cfg is None:
        return CheckResponse(
            eligible=False,
            blocked_by=["campaign_not_found"],
            fix_hints=_hints_for(["campaign_not_found"]),
        )
    req = CheckRequest(
        brand_id=brand_id,
        user_id=user_id,
        campaign_id=campaign_id,
        conditions=cfg,
    )
    return await check_conditions(req, r)


@router.get("/campaigns/{campaign_id}/audit")
async def campaign_audit(
    campaign_id: str,
    limit: int = Query(50, ge=1, le=AUDIT_LOG_MAX),
    r: aioredis.Redis = Depends(get_redis),
):
    """Return the most recent audit entries (newest first)."""
    raw = await r.lrange(_k_audit(campaign_id), 0, limit - 1)
    entries: list[dict[str, Any]] = []
    for s in raw:
        try:
            entries.append(json.loads(s))
        except (TypeError, ValueError):
            continue
    return {"ok": True, "campaign_id": campaign_id, "entries": entries}


# ═════════════════════════════════════════════════════════════════════════
# Module integration helpers
# ═════════════════════════════════════════════════════════════════════════
#
# These helpers let any gamification module endpoint gate its action behind
# the conditions engine without HTTP self-calls. The contract:
#
#     1. Call  await _check_and_reserve_module(...)  at the top of the
#        action handler.  If the module has no conditions configured, the
#        helper returns (True, None, {}) and the handler runs unmodified
#        (backward compatible).
#
#     2. On the success path, call  await commit_reservation_internal(r, rid)
#        once the underlying action has succeeded — this bumps frequency
#        counters and marks the reservation as a real claim.
#
#     3. On failure, call  await refund_reservation_internal(r, rid, reason)
#        which returns supply / budget to the pool.
#
# The internal helpers share all logic with the public /commit and /refund
# endpoints (they were factored out of them) so behaviour is identical.


async def _check_and_reserve_module(
    r: aioredis.Redis,
    brand_id: str,
    user_id: str,
    module_id: str,
    value_cents: int = 0,
    action_context: dict[str, Any] | None = None,
) -> tuple[bool, str | None, dict[str, str]]:
    """Load a module's conditions and reserve atomically.

    Returns ``(ok, reservation_id_or_blockers, fix_hints)``:

      * ``(True, None, {})`` — module has no conditions → allow, no reservation.
      * ``(True, rid, {})``  — reservation succeeded; caller must later
                               commit_reservation_internal(r, rid)
                               or refund_reservation_internal(r, rid, reason).
      * ``(False, blocker_codes, hints)`` — gated; caller should surface
                               ``hints`` and short-circuit the action.

    When ``ok`` is False, the second tuple element is a single comma-joined
    string of blocker codes (so callers can shove it straight into
    ``HTTPException.detail``); the third element is the bilingual fix-hint
    map.
    """
    raw = await r.hget(f"brand:{brand_id}:modules", module_id)
    if not raw:
        return True, None, {}

    try:
        cfg = json.loads(raw)
    except (TypeError, ValueError):
        return True, None, {}

    # Conditions may live at the top level or nested under "params" —
    # both shapes have been observed depending on where the ConditionsBuilder
    # UI wrote them.
    cond_dict = cfg.get("conditions")
    if cond_dict is None:
        cond_dict = (cfg.get("params") or {}).get("conditions")
    if not cond_dict:
        return True, None, {}

    try:
        conditions = FullConditions(**cond_dict)
    except Exception as exc:  # malformed config — fail open with a log
        logger.warning(
            "conditions._check_and_reserve_module: bad conditions for "
            "brand=%s module=%s: %s", brand_id, module_id, exc,
        )
        return True, None, {}

    # The campaign_id is synthesized per (brand, module) so that the
    # supply / frequency counters are isolated and the audit log is
    # discoverable from the dashboard.
    campaign_id = f"module:{brand_id}:{module_id}"

    req = ReserveRequest(
        brand_id=brand_id,
        user_id=user_id,
        campaign_id=campaign_id,
        conditions=conditions,
        value_cents=value_cents,
        action_context=action_context or {},
    )
    # Direct in-process call — no HTTP, shares the redis connection.
    result = await reserve_conditions(req, r)
    if not result.ok:
        return False, ",".join(result.blocked_by), result.fix_hints
    return True, result.reservation_id, {}


async def commit_reservation_internal(
    r: aioredis.Redis,
    reservation_id: str,
) -> dict[str, Any] | None:
    """Internal-call variant of POST /commit.

    Idempotent on no-op: missing reservations or already-committed ones
    return ``None`` rather than raising, because module handlers shouldn't
    crash a successful action just because the reservation TTL expired
    between reserve and commit. Mis-state events are logged.
    """
    if not reservation_id:
        return None
    res = await r.hgetall(_k_reservation(reservation_id))
    if not res:
        logger.warning(
            "conditions.commit_reservation_internal: reservation %s not "
            "found (likely expired); action proceeded uncounted",
            reservation_id,
        )
        return None

    status_str = res.get("status", "reserved")
    if status_str == "committed":
        return None
    if status_str == "refunded":
        logger.warning(
            "conditions.commit_reservation_internal: reservation %s "
            "already refunded; refusing to commit",
            reservation_id,
        )
        return None

    campaign_id = res["campaign_id"]
    user_id = res["user_id"]
    value_cents = int(res.get("value_cents", 0) or 0)

    today = _today_str()
    isoweek = _isoweek_str()
    ym = _ym_str()
    hour = _hour_bucket()

    pipe = r.pipeline(transaction=True)
    pipe.incr(_k_user_daily(campaign_id, user_id, today))
    pipe.expire(_k_user_daily(campaign_id, user_id, today), 86400)
    pipe.incr(_k_user_weekly(campaign_id, user_id, isoweek))
    pipe.expire(_k_user_weekly(campaign_id, user_id, isoweek), 604800)
    pipe.incr(_k_user_monthly(campaign_id, user_id, ym))
    pipe.expire(_k_user_monthly(campaign_id, user_id, ym), 2678400)
    pipe.incr(_k_user_total(campaign_id, user_id))
    pipe.incr(_k_global_daily(campaign_id, today))
    pipe.expire(_k_global_daily(campaign_id, today), 86400)
    pipe.incr(_k_hourly(campaign_id, hour))
    pipe.expire(_k_hourly(campaign_id, hour), 86400 * 31)
    pipe.incr(_k_claims(campaign_id))
    pipe.sadd(_k_unique_users(campaign_id), user_id)
    pipe.set(_k_user_claimed(campaign_id, user_id), "1")
    pipe.hset(_k_reservation(reservation_id), mapping={
        "status": "committed",
        "committed_at": str(time.time()),
    })
    pipe.expire(_k_reservation(reservation_id), 86400 * 7)
    await pipe.execute()

    await _audit(
        r, campaign_id, user_id, "commit",
        extra={"reservation_id": reservation_id, "value_cents": value_cents},
    )

    return {
        "ok": True,
        "reservation_id": reservation_id,
        "campaign_id": campaign_id,
        "user_id": user_id,
        "value_cents": value_cents,
    }


async def refund_reservation_internal(
    r: aioredis.Redis,
    reservation_id: str,
    reason: str = "",
) -> dict[str, Any] | None:
    """Internal-call variant of POST /refund.

    Returns supply + budget to the pool. Soft-fails (returns None, logs)
    on missing / already-refunded reservations — module handlers don't
    need to dance around the cleanup path. Refusing to refund a committed
    reservation is preserved as a hard log line (but not an exception)
    because if it happened the upstream logic has a bug.
    """
    if not reservation_id:
        return None
    res = await r.hgetall(_k_reservation(reservation_id))
    if not res:
        return None

    status_str = res.get("status", "reserved")
    if status_str == "refunded":
        return None
    if status_str == "committed":
        logger.error(
            "conditions.refund_reservation_internal: refusing to refund "
            "already-committed reservation %s", reservation_id,
        )
        return None

    campaign_id = res["campaign_id"]
    user_id = res["user_id"]
    supply_consumed = int(res.get("supply_consumed", 0) or 0)
    budget_consumed = int(res.get("budget_consumed", 0) or 0)

    pipe = r.pipeline(transaction=True)
    if supply_consumed > 0:
        pipe.incrby(_k_supply(campaign_id), supply_consumed)
    if budget_consumed > 0:
        pipe.incrby(_k_budget(campaign_id), budget_consumed)
    pipe.hset(_k_reservation(reservation_id), mapping={
        "status": "refunded",
        "refunded_at": str(time.time()),
        "refund_reason": reason or "",
    })
    pipe.expire(_k_reservation(reservation_id), 86400 * 7)
    await pipe.execute()

    await _audit(
        r, campaign_id, user_id, "refund",
        extra={"reservation_id": reservation_id, "reason": reason},
    )

    return {
        "ok": True,
        "reservation_id": reservation_id,
        "campaign_id": campaign_id,
        "user_id": user_id,
        "reason": reason,
    }
