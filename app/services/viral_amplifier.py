"""Viral Amplifier — Wave G #3.

Pushes K-factor from 0.40 (Wave F single-leg) toward the self-sustaining
1.0+ band by spreading invite/share emission across **7 high-intent
trigger points** rather than the single ``refer_friend`` mechanic Wave F
introduced.

Triggers
========

1. ``game_completion``     — "Beat your friend's score?" after every game
2. ``voucher_won``         — 2-for-1 "Give friend S$X, you get S$X"
3. ``brand_discovery``     — cross-brand voucher use → "try with friend"
4. ``achievement_unlock``  — streak / milestone → brag card (Wave F #5)
5. ``birthday``            — group-buy: 4 friends redeem together
6. ``re_engagement``       — lapsed user wins back via friend social proof
7. ``geofence_friend``     — "Sarah is at Toast Box now" real-time notif

Per-trigger probability product (P(invite_sent | trigger) × P(redeem |
invite)) is the *conditional K* — tracked separately so we can A/B which
triggers to ship and which to retire.

Compounding
-----------

When a redeemer is created, ``network_effect`` already auto-emits a
fresh invite token (Wave A "viral_loop_dead" fix). This module raises
the inheritance depth cap from 5 → 7 and grants a *depth bonus* voucher
when a chain reaches 5+. The auto-emission itself is implemented in
``network_effect._redeem_token`` — we read those counters here.

Redis schema (all brand-isolated)::

    va:trigger:{bid}:{trigger}:sent      INCR   sends per trigger
    va:trigger:{bid}:{trigger}:redeemed  INCR   redemptions per trigger
    va:user:{uid}:quota:{ymd}            INCR   per-day fatigue counter
    va:user:{uid}:last_trigger_at        STRING ts when last fired
    va:user:{uid}:chain_depth            STRING max depth reached
    va:depth_bonus:{uid}                 STRING "1" idempotency

NEW file — does **not** touch ``wavef_referral`` or ``network_effect``;
both can keep running side-by-side and feed the same K-factor counters.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────

TRIGGER_GAME_COMPLETION = "game_completion"
TRIGGER_VOUCHER_WON = "voucher_won"
TRIGGER_BRAND_DISCOVERY = "brand_discovery"
TRIGGER_ACHIEVEMENT_UNLOCK = "achievement_unlock"
TRIGGER_BIRTHDAY = "birthday"
TRIGGER_RE_ENGAGEMENT = "re_engagement"
TRIGGER_GEOFENCE_FRIEND = "geofence_friend"

ALL_AMP_TRIGGERS: tuple[str, ...] = (
    TRIGGER_GAME_COMPLETION,
    TRIGGER_VOUCHER_WON,
    TRIGGER_BRAND_DISCOVERY,
    TRIGGER_ACHIEVEMENT_UNLOCK,
    TRIGGER_BIRTHDAY,
    TRIGGER_RE_ENGAGEMENT,
    TRIGGER_GEOFENCE_FRIEND,
)

# Per-trigger ``invite_sent`` propensity (observed from BRAME viral
# studies + KiX 100M sim). Used by the orchestrator to weight which
# trigger to fire when multiple are eligible at the same instant.
TRIGGER_PRIOR_K: dict[str, float] = {
    TRIGGER_GAME_COMPLETION: 0.22,    # super-frequent, lower per-event K
    TRIGGER_VOUCHER_WON: 0.45,        # 2-for-1 mechanic — sticky
    TRIGGER_BRAND_DISCOVERY: 0.35,    # cross-brand novelty bonus
    TRIGGER_ACHIEVEMENT_UNLOCK: 0.40, # ego-driven share
    TRIGGER_BIRTHDAY: 0.65,           # group-buy strongest single trigger
    TRIGGER_RE_ENGAGEMENT: 0.15,      # lapsed = low click-through
    TRIGGER_GEOFENCE_FRIEND: 0.55,    # real-time presence = high intent
}

# A/B variants per trigger (copy line keys). Kept tiny — orchestrator
# sticks each user to one arm via hash.
TRIGGER_VARIANTS: dict[str, tuple[str, ...]] = {
    TRIGGER_GAME_COMPLETION: ("beat_score", "high_score_emoji"),
    TRIGGER_VOUCHER_WON: ("give_get", "double_value"),
    TRIGGER_BRAND_DISCOVERY: ("new_brand_friend", "double_with_friend"),
    TRIGGER_ACHIEVEMENT_UNLOCK: ("brag_card", "humble_share"),
    TRIGGER_BIRTHDAY: ("group_buy_4", "birthday_treat"),
    TRIGGER_RE_ENGAGEMENT: ("miss_you_friend", "friend_playing_now"),
    TRIGGER_GEOFENCE_FRIEND: ("friend_here_now", "join_friend_play"),
}

# Per-day per-user invite/prompt quota (anti-fatigue).
DAILY_USER_QUOTA = 3

# Quiet hours (local-time approximation; we use UTC offset 8 for SG by
# default — overridable via ``KIX_TZ_OFFSET`` env).
QUIET_HOURS_START_LOCAL = 22  # 10pm
QUIET_HOURS_END_LOCAL = 7     # 7am

# Compounding chain
MAX_INHERITANCE_DEPTH = 7
DEPTH_BONUS_THRESHOLD = 5
DEPTH_BONUS_VOUCHER_CENTS = 500

# Telemetry TTL ~35d so K-factor windows up to 30d remain stable.
COUNTER_TTL_SEC = 35 * 24 * 3600
TOKEN_TTL_SEC = 7 * 24 * 3600

# Historical baseline that triggered Wave G #3 (100m sim, K=0.40).
HISTORICAL_BASELINE_K = 0.40
TARGET_K_SELF_SUSTAINING = 1.0


# ── Redis key helpers ─────────────────────────────────────────────────────


def _k_trigger_sent(bid: str, trigger: str) -> str:
    return f"va:trigger:{bid}:{trigger}:sent"


def _k_trigger_redeemed(bid: str, trigger: str) -> str:
    return f"va:trigger:{bid}:{trigger}:redeemed"


def _k_trigger_sent_day(bid: str, trigger: str, ymd: str) -> str:
    return f"va:trigger:{bid}:{trigger}:sent:day:{ymd}"


def _k_trigger_redeemed_day(bid: str, trigger: str, ymd: str) -> str:
    return f"va:trigger:{bid}:{trigger}:redeemed:day:{ymd}"


def _k_user_quota(uid: str, ymd: str) -> str:
    return f"va:user:{uid}:quota:{ymd}"


def _k_user_last(uid: str) -> str:
    return f"va:user:{uid}:last_trigger_at"


def _k_user_chain_depth(uid: str) -> str:
    return f"va:user:{uid}:chain_depth"


def _k_depth_bonus(uid: str) -> str:
    return f"va:depth_bonus:{uid}"


def _k_invite(token: str) -> str:
    return f"va:invite:{token}"


def _k_user_ab(uid: str, trigger: str) -> str:
    return f"va:user:{uid}:ab:{trigger}"


def _k_audit(bid: str, ymd: str) -> str:
    return f"va:audit:{bid}:{ymd}"


# ── Time helpers ─────────────────────────────────────────────────────────


def _now() -> int:
    return int(time.time())


def _today_ymd() -> str:
    return date.today().isoformat()


def _tz_offset_hours() -> int:
    try:
        return int(os.environ.get("KIX_TZ_OFFSET", "8"))
    except ValueError:
        return 8


def is_quiet_hours(ts: int | None = None) -> bool:
    """Return True if ``ts`` (epoch) falls in the user's quiet window.

    Approximates user-local time via ``KIX_TZ_OFFSET`` (default SG=+8).
    Quiet window wraps midnight: 22:00..07:00 local.
    """
    ts = ts if ts is not None else _now()
    local = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(
        hours=_tz_offset_hours()
    )
    h = local.hour
    if QUIET_HOURS_START_LOCAL > QUIET_HOURS_END_LOCAL:
        # wraps midnight
        return h >= QUIET_HOURS_START_LOCAL or h < QUIET_HOURS_END_LOCAL
    return QUIET_HOURS_START_LOCAL <= h < QUIET_HOURS_END_LOCAL


# ── A/B arm sticky selection ──────────────────────────────────────────────


async def _pick_ab_arm(r, uid: str, trigger: str) -> str:
    """Sticky per-user A/B assignment for a trigger's copy variant."""
    variants = TRIGGER_VARIANTS.get(trigger) or ("default",)
    if len(variants) == 1:
        return variants[0]
    existing = await r.get(_k_user_ab(uid, trigger))
    if existing:
        arm = existing.decode() if isinstance(existing, bytes) else existing
        if arm in variants:
            return arm
    arm = variants[hash(f"{uid}:{trigger}") % len(variants)]
    await r.set(_k_user_ab(uid, trigger), arm, ex=COUNTER_TTL_SEC)
    return arm


# ── Fatigue / quota ───────────────────────────────────────────────────────


async def quota_remaining(r, uid: str) -> int:
    """Return number of additional viral prompts allowed today."""
    raw = await r.get(_k_user_quota(uid, _today_ymd()))
    used = int(raw or 0)
    return max(0, DAILY_USER_QUOTA - used)


async def _consume_quota(r, uid: str) -> bool:
    """Atomic increment+cap; True if quota was available."""
    key = _k_user_quota(uid, _today_ymd())
    new = await r.incr(key)
    if new == 1:
        await r.expire(key, COUNTER_TTL_SEC)
    if new > DAILY_USER_QUOTA:
        await r.decr(key)
        return False
    return True


# ── Audit log ─────────────────────────────────────────────────────────────


async def _audit(r, bid: str, payload: dict[str, Any]) -> None:
    """Append a JSON line into today's audit list (bounded)."""
    key = _k_audit(bid, _today_ymd())
    try:
        await r.rpush(key, json.dumps(payload, default=str))
        await r.expire(key, COUNTER_TTL_SEC)
        await r.ltrim(key, -5000, -1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("viral_amplifier audit failed: %s", exc)


# ── Compounding / chain bonus ────────────────────────────────────────────


async def record_chain_depth(r, uid: str, depth: int) -> dict[str, Any]:
    """Track maximum invite-chain depth seen for a user.

    When a chain hits ``DEPTH_BONUS_THRESHOLD`` for the first time the
    user gets a one-off depth-bonus voucher. Idempotent via the
    ``va:depth_bonus:{uid}`` flag.
    """
    key = _k_user_chain_depth(uid)
    raw = await r.get(key)
    prev = int(raw or 0)
    if depth > prev:
        await r.set(key, depth, ex=COUNTER_TTL_SEC)
    bonus_awarded = False
    if depth >= DEPTH_BONUS_THRESHOLD:
        set_ok = await r.set(
            _k_depth_bonus(uid), "1", nx=True, ex=COUNTER_TTL_SEC
        )
        if set_ok:
            bonus_awarded = True
    return {
        "user_id": uid,
        "depth": depth,
        "prev_max": prev,
        "depth_bonus_awarded": bonus_awarded,
        "depth_bonus_cents": (
            DEPTH_BONUS_VOUCHER_CENTS if bonus_awarded else 0
        ),
        "max_depth_cap": MAX_INHERITANCE_DEPTH,
    }


# ── Trigger emission ─────────────────────────────────────────────────────


def _share_url(token: str, bid: str) -> str:
    return f"https://play.kix.app/landing/play.html?brand={bid}&invite={token}"


def _share_text(trigger: str, arm: str, ctx: dict[str, Any]) -> str:
    """Deterministic copy generator — keeps tests stable. Personalised
    LLM copy is layered on by ``network_effect.personalized_invite_message``
    (already quota-guarded)."""
    if trigger == TRIGGER_GAME_COMPLETION:
        score = ctx.get("score", "?")
        if arm == "high_score_emoji":
            return f"I scored {score}. Think you can do better?"
        return f"Beat my score of {score}!"
    if trigger == TRIGGER_VOUCHER_WON:
        cents = int(ctx.get("voucher_cents", 500))
        if arm == "double_value":
            return f"Share with a friend — both get S${cents/100:.0f}"
        return f"Give a friend S${cents/100:.0f}, you get S${cents/100:.0f}"
    if trigger == TRIGGER_BRAND_DISCOVERY:
        brand_name = ctx.get("brand_name", "this brand")
        if arm == "double_with_friend":
            return f"Try {brand_name} with a friend — double the value."
        return f"Loving {brand_name}? Bring a friend, both win."
    if trigger == TRIGGER_ACHIEVEMENT_UNLOCK:
        ach = ctx.get("achievement_name", "an achievement")
        if arm == "humble_share":
            return f"Just unlocked {ach}. Worth a look?"
        return f"Just unlocked {ach} — beat that!"
    if trigger == TRIGGER_BIRTHDAY:
        if arm == "birthday_treat":
            return "Birthday treat — share with 3 friends, all get free."
        return "Get 4 friends to redeem together for the birthday bundle."
    if trigger == TRIGGER_RE_ENGAGEMENT:
        friend = ctx.get("friend_name", "Your friend")
        if arm == "friend_playing_now":
            return f"{friend} is playing right now — jump back in."
        return f"{friend} has been playing while you've been gone."
    if trigger == TRIGGER_GEOFENCE_FRIEND:
        friend = ctx.get("friend_name", "Your friend")
        place = ctx.get("place_name", "nearby")
        return f"{friend} is at {place} now — go win with them."
    return "Play KiX with me."


async def emit_trigger(
    r,
    *,
    user_id: str,
    brand_id: str,
    trigger: str,
    context: dict[str, Any] | None = None,
    bypass_quota: bool = False,
    inherited_depth: int = 0,
) -> dict[str, Any]:
    """Fire one viral trigger.

    Returns ``{ "sent": True, ... }`` or ``{ "sent": False, "reason": ...}``.
    Side effects:
      * Bumps per-trigger ``sent`` counters (lifetime + per-day).
      * Stores a small invite record under ``va:invite:{token}``.
      * Consumes one quota slot (unless ``bypass_quota``).
      * Appends an audit line.
    """
    if not user_id or not brand_id:
        raise ValueError("user_id and brand_id are required")
    if trigger not in ALL_AMP_TRIGGERS:
        raise ValueError(f"unknown trigger '{trigger}'")
    if inherited_depth >= MAX_INHERITANCE_DEPTH:
        return {
            "sent": False,
            "reason": "depth_cap_reached",
            "depth_cap": MAX_INHERITANCE_DEPTH,
        }

    if is_quiet_hours():
        return {"sent": False, "reason": "quiet_hours"}

    if not bypass_quota:
        if not await _consume_quota(r, user_id):
            return {"sent": False, "reason": "daily_quota_exhausted"}

    ctx = context or {}
    token = uuid4().hex[:16]
    arm = await _pick_ab_arm(r, user_id, trigger)
    text = _share_text(trigger, arm, ctx)
    share_url = _share_url(token, brand_id)
    ymd = _today_ymd()

    record = {
        "trigger": trigger,
        "ab_arm": arm,
        "from_user_id": user_id,
        "brand_id": brand_id,
        "context": ctx,
        "created_at": _now(),
        "depth": inherited_depth,
        "redeemed": False,
    }

    pipe = r.pipeline()
    pipe.set(_k_invite(token), json.dumps(record), ex=TOKEN_TTL_SEC)
    pipe.incr(_k_trigger_sent(brand_id, trigger))
    pipe.incr(_k_trigger_sent_day(brand_id, trigger, ymd))
    pipe.expire(_k_trigger_sent_day(brand_id, trigger, ymd), COUNTER_TTL_SEC)
    pipe.set(_k_user_last(user_id), _now(), ex=COUNTER_TTL_SEC)
    await pipe.execute()

    await _audit(
        r,
        brand_id,
        {
            "ev": "emit",
            "trigger": trigger,
            "arm": arm,
            "uid": user_id,
            "token": token,
            "depth": inherited_depth,
            "ts": _now(),
        },
    )

    return {
        "sent": True,
        "invite_token": token,
        "trigger": trigger,
        "ab_arm": arm,
        "share_text": text,
        "share_url": share_url,
        "depth": inherited_depth,
    }


async def record_redemption(
    r,
    *,
    invite_token: str,
    redeemer_user_id: str,
    brand_id: str,
) -> dict[str, Any]:
    """Mark a viral-amplifier invite redeemed + bump per-trigger K counters.

    Idempotent: second call on the same token returns ``redeemed=False
    reason=already``.
    """
    if not invite_token or not redeemer_user_id or not brand_id:
        raise ValueError("invite_token, redeemer_user_id, brand_id required")

    raw = await r.get(_k_invite(invite_token))
    if not raw:
        return {"redeemed": False, "reason": "unknown_token"}
    record = json.loads(raw)
    if record.get("brand_id") != brand_id:
        return {"redeemed": False, "reason": "brand_mismatch"}
    if record.get("from_user_id") == redeemer_user_id:
        return {"redeemed": False, "reason": "self_redeem"}
    if record.get("redeemed"):
        return {"redeemed": False, "reason": "already"}

    record["redeemed"] = True
    record["redeemed_by"] = redeemer_user_id
    record["redeemed_at"] = _now()

    trigger = record.get("trigger") or ""
    ymd = _today_ymd()
    pipe = r.pipeline()
    pipe.set(_k_invite(invite_token), json.dumps(record), ex=TOKEN_TTL_SEC)
    pipe.incr(_k_trigger_redeemed(brand_id, trigger))
    pipe.incr(_k_trigger_redeemed_day(brand_id, trigger, ymd))
    pipe.expire(
        _k_trigger_redeemed_day(brand_id, trigger, ymd), COUNTER_TTL_SEC
    )
    await pipe.execute()

    # Compounding: redeemer inherits a new chain depth.
    parent_depth = int(record.get("depth") or 0)
    new_depth = parent_depth + 1
    depth_info = await record_chain_depth(r, redeemer_user_id, new_depth)

    await _audit(
        r,
        brand_id,
        {
            "ev": "redeem",
            "trigger": trigger,
            "from": record.get("from_user_id"),
            "to": redeemer_user_id,
            "token": invite_token,
            "depth": new_depth,
            "ts": _now(),
        },
    )

    return {
        "redeemed": True,
        "trigger": trigger,
        "inviter_user_id": record.get("from_user_id"),
        "redeemer_user_id": redeemer_user_id,
        "new_depth": new_depth,
        "depth_bonus": depth_info,
    }


# ── K-factor (per-trigger + cumulative) ─────────────────────────────────


async def _sum_window(r, key_builder, brand_id: str, days: int) -> int:
    today = date.today()
    pipe = r.pipeline()
    for i in range(days):
        d = (today - timedelta(days=i)).isoformat()
        pipe.get(key_builder(brand_id, d))
    vals = await pipe.execute()
    return sum(int(v or 0) for v in vals)


async def kfactor_breakdown(
    r, brand_id: str, window_days: int = 7
) -> dict[str, Any]:
    """Per-trigger K-factor + cumulative + comparison to baseline."""
    breakdown: dict[str, dict[str, Any]] = {}
    total_sent = 0
    total_red = 0
    for trig in ALL_AMP_TRIGGERS:
        sent = await _sum_window(
            r,
            lambda b, d, t=trig: _k_trigger_sent_day(b, t, d),
            brand_id,
            window_days,
        )
        red = await _sum_window(
            r,
            lambda b, d, t=trig: _k_trigger_redeemed_day(b, t, d),
            brand_id,
            window_days,
        )
        total_sent += sent
        total_red += red
        k = (red / sent) if sent else 0.0
        breakdown[trig] = {
            "sent": sent,
            "redeemed": red,
            "k_factor": round(k, 4),
            "prior_k": TRIGGER_PRIOR_K.get(trig, 0.0),
        }
    cumulative_k = (total_red / total_sent) if total_sent else 0.0
    return {
        "brand_id": brand_id,
        "window_days": window_days,
        "cumulative_k": round(cumulative_k, 4),
        "total_sent": total_sent,
        "total_redeemed": total_red,
        "historical_baseline_k": HISTORICAL_BASELINE_K,
        "delta_vs_baseline": round(cumulative_k - HISTORICAL_BASELINE_K, 4),
        "target_self_sustaining_k": TARGET_K_SELF_SUSTAINING,
        "self_sustaining": cumulative_k >= TARGET_K_SELF_SUSTAINING,
        "per_trigger": breakdown,
    }


async def kfactor_trailing_per_trigger(
    r, brand_id: str, *, windows: tuple[int, ...] = (7, 30)
) -> dict[str, dict[str, Any]]:
    """Helper for the observability dashboard — returns 7d/30d per trig."""
    out: dict[str, dict[str, Any]] = {}
    for w in windows:
        out[f"{w}d"] = await kfactor_breakdown(r, brand_id, window_days=w)
    return out
