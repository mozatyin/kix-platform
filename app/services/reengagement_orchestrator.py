"""Re-engagement orchestrator — Wave E Step 5 (Return).

Final step of the 5-step funnel:

    Acquire → Activate → Retain → Refer → **Return**

When a user goes quiet (no visit for N days), this module brings them
back via a timed, multi-channel cadence (WhatsApp > push > email).
Personalisation is done via the optional TriSoul integration (Wave C);
if TriSoul isn't loaded for this user we fall back to generic copy.

Design principles
-----------------
* **Channel cascade.** Try the richest channel first (WhatsApp), then
  drop down (push, email). ``recommend_channel`` picks per-user based
  on consent + reachability + last successful channel.
* **Cadence shapes.** Four cascade archetypes (Light/Medium/Heavy/
  Win-back). The trigger conditions (§3) decide which cascade a user
  enters; the cascade itself is just a schedule of (offset_days,
  channel, template, offer_pct) tuples that ``reengagement_worker``
  reads from Redis when it's time to send a step.
* **Suppression always wins.** Quiet hours (10pm-7am SGT), per-user
  frequency cap (max 1 reengagement msg / 7 days), opt-out, and
  "just-redeemed" all hard-block sends. We never punish users for
  doing the thing we wanted them to do.
* **Idempotent.** Re-entering a cascade for the same (user, brand) is
  a no-op until the in-flight cascade either completes, expires, or
  is cancelled. Redis key: ``reengagement:active:{brand}:{kid}``.
* **Audit trail.** Every state change (start, send, suppress, cancel)
  writes a JSON line to ``reengagement:audit:{brand}:{kid}`` (capped
  list) so downstream attribution / GDPR export can reconstruct the
  full re-engagement history per user.

Redis schema (mirrors the Wave-A/B/C conventions)
-------------------------------------------------
::

    reengagement:active:{brand}:{kid}   HASH  cascade_id, started_at,
                                              cascade_type, step_idx,
                                              next_due_ts, status
    reengagement:lastsend:{brand}:{kid} STRING epoch ts of last send
                                               (used by 7-day frequency cap)
    reengagement:audit:{brand}:{kid}    LIST   newest-first JSON envelopes
                                               (capped at AUDIT_LOG_MAX)
    reengagement:cohort:atrisk:{brand}  SET    kids currently in any cascade
    reengagement:stats:{brand}          HASH   started/sent/suppressed/redeemed
    reengagement:optout:{brand}:{kid}   STRING "1" if user has opted out
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

__all__ = [
    "CascadeType",
    "CascadeStep",
    "CASCADE_BLUEPRINTS",
    "ChannelPref",
    "compute_lapse_score",
    "recommend_channel",
    "craft_message",
    "select_cascade",
    "is_suppressed",
    "in_quiet_hours_sgt",
    "frequency_capped",
    "start_cascade",
    "send_cascade_step",
    "send_cascade",
    "evaluate_users",
    "cascade_stats",
    "at_risk_cohort",
    "active_cascade_key",
    "last_send_key",
    "audit_log_key",
    "atrisk_cohort_key",
    "stats_key",
    "optout_key",
]

# ── Tunables ──────────────────────────────────────────────────────────────

QUIET_HOURS_START_LOCAL = 22  # 10pm SGT (inclusive)
QUIET_HOURS_END_LOCAL = 7     # 7am SGT (exclusive)
SGT_UTC_OFFSET_HOURS = 8

FREQUENCY_CAP_SECONDS = 7 * 86_400  # max 1 reengagement msg / 7 days
AUDIT_LOG_MAX = 999                  # cap per (brand, kid)

# Lapse-score weighting — combines days-since-last-visit and unused
# voucher inventory into a single 0..1 score the cohort scanner can
# threshold on.
LAPSE_FULL_DAYS = 30.0  # ≥30d silence → 1.0 from time alone

# ── Cascade definitions ──────────────────────────────────────────────────


class CascadeType:
    """Enum-like namespace (kept as plain strings for Redis hash storage)."""

    LIGHT = "light"
    MEDIUM = "medium"
    HEAVY = "heavy"
    WIN_BACK = "win_back"


@dataclass(frozen=True)
class CascadeStep:
    """One scheduled touch within a cascade."""

    offset_days: float           # days since cascade start when this step fires
    template_id: str             # email or push template id
    channel_pref: tuple[str, ...]  # preferred channels, in order
    offer_pct: int = 0           # % discount embedded in the offer (0 = no offer)
    subject_key: str = ""        # subject line copy slot
    body_key: str = ""           # body copy slot

    def envelope_kind(self) -> str:
        return "email" if self.template_id.startswith("email_") else "push"


@dataclass(frozen=True)
class CascadeBlueprint:
    """A cascade is an ordered list of CascadeSteps plus metadata."""

    cascade_type: str
    description: str
    steps: tuple[CascadeStep, ...]


#: Light: 1 message after 7d of silence.
_LIGHT = CascadeBlueprint(
    cascade_type=CascadeType.LIGHT,
    description="Gentle nudge after 7 days of silence",
    steps=(
        CascadeStep(
            offset_days=0.0,
            template_id="push_streak_break",
            channel_pref=("whatsapp", "push", "email"),
            subject_key="light_d0_subject",
            body_key="light_d0_body",
        ),
    ),
)


#: Medium: 3 messages over 14d (D0, D3, D7 within the cascade — i.e. D7,
#: D10, D14 from last visit since the cascade kicks off at D7 silence).
_MEDIUM = CascadeBlueprint(
    cascade_type=CascadeType.MEDIUM,
    description="3-touch over 14 days when voucher unused",
    steps=(
        CascadeStep(
            offset_days=0.0,
            template_id="push_streak_break",
            channel_pref=("push", "whatsapp", "email"),
            offer_pct=10,
            subject_key="medium_d0_subject",
            body_key="medium_d0_body",
        ),
        CascadeStep(
            offset_days=3.0,
            template_id="push_voucher_nearby",
            channel_pref=("whatsapp", "push", "email"),
            offer_pct=15,
            subject_key="medium_d3_subject",
            body_key="medium_d3_body",
        ),
        CascadeStep(
            offset_days=7.0,
            template_id="email_alpha_day3_checkin",
            channel_pref=("email", "whatsapp", "push"),
            offer_pct=20,
            subject_key="medium_d7_subject",
            body_key="medium_d7_body",
        ),
    ),
)


#: Heavy: 5 messages over 30d with escalating offers. Cadence days
#: from cascade-start: 0, 5, 12, 21, 30.
_HEAVY = CascadeBlueprint(
    cascade_type=CascadeType.HEAVY,
    description="5-touch over 30 days, escalating offers, high-LTV users",
    steps=(
        CascadeStep(
            offset_days=0.0,
            template_id="push_streak_break",
            channel_pref=("whatsapp", "push", "email"),
            offer_pct=15,
            subject_key="heavy_d0_subject",
            body_key="heavy_d0_body",
        ),
        CascadeStep(
            offset_days=5.0,
            template_id="push_campaign_match",
            channel_pref=("push", "whatsapp", "email"),
            offer_pct=20,
            subject_key="heavy_d5_subject",
            body_key="heavy_d5_body",
        ),
        CascadeStep(
            offset_days=12.0,
            template_id="push_voucher_nearby",
            channel_pref=("whatsapp", "email", "push"),
            offer_pct=25,
            subject_key="heavy_d12_subject",
            body_key="heavy_d12_body",
        ),
        CascadeStep(
            offset_days=21.0,
            template_id="email_alpha_week1_summary",
            channel_pref=("email", "whatsapp", "push"),
            offer_pct=30,
            subject_key="heavy_d21_subject",
            body_key="heavy_d21_body",
        ),
        CascadeStep(
            offset_days=30.0,
            template_id="email_alpha_monthly_survey",
            channel_pref=("email", "whatsapp"),
            offer_pct=40,
            subject_key="heavy_d30_subject",
            body_key="heavy_d30_body",
        ),
    ),
)


#: Win-back: single final-offer message at D60 silence (50% off).
_WIN_BACK = CascadeBlueprint(
    cascade_type=CascadeType.WIN_BACK,
    description="Final 50%-off win-back at 60 days silence",
    steps=(
        CascadeStep(
            offset_days=0.0,
            template_id="email_alpha_monthly_survey",
            channel_pref=("email", "whatsapp", "push"),
            offer_pct=50,
            subject_key="winback_subject",
            body_key="winback_body",
        ),
    ),
)


CASCADE_BLUEPRINTS: dict[str, CascadeBlueprint] = {
    CascadeType.LIGHT: _LIGHT,
    CascadeType.MEDIUM: _MEDIUM,
    CascadeType.HEAVY: _HEAVY,
    CascadeType.WIN_BACK: _WIN_BACK,
}


# ── Redis key helpers ────────────────────────────────────────────────────


def active_cascade_key(brand_id: str, kid: str) -> str:
    return f"reengagement:active:{brand_id}:{kid}"


def last_send_key(brand_id: str, kid: str) -> str:
    return f"reengagement:lastsend:{brand_id}:{kid}"


def audit_log_key(brand_id: str, kid: str) -> str:
    return f"reengagement:audit:{brand_id}:{kid}"


def atrisk_cohort_key(brand_id: str) -> str:
    return f"reengagement:cohort:atrisk:{brand_id}"


def stats_key(brand_id: str) -> str:
    return f"reengagement:stats:{brand_id}"


def optout_key(brand_id: str, kid: str) -> str:
    return f"reengagement:optout:{brand_id}:{kid}"


# ── Channel preference ──────────────────────────────────────────────────


@dataclass
class ChannelPref:
    """Resolved channel reachability for a (brand, user) pair."""

    has_whatsapp: bool = False
    has_push: bool = False
    has_email: bool = False
    last_success_channel: str = ""

    def best(self, ordering: Iterable[str]) -> str:
        """Pick the highest-pref channel the user can actually be reached on."""
        for ch in ordering:
            if ch == "whatsapp" and self.has_whatsapp:
                return ch
            if ch == "push" and self.has_push:
                return ch
            if ch == "email" and self.has_email:
                return ch
        return ""


async def _decode_hash(raw: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not raw:
        return out
    for k, v in raw.items():
        sk = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
        sv = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
        out[sk] = sv
    return out


async def _resolve_channels(redis: Any, brand_id: str, kid: str) -> ChannelPref:
    """Inspect Redis to figure out which channels we can reach the user on.

    Conventions match what other Wave-A/B/C modules write:
      * push devices       — ``kid:{kid}:push_devices`` non-empty
      * email on file      — ``user_profile:{kid}`` hash has ``email``
      * WhatsApp opted-in  — ``whatsapp:opt:{brand_id}:{kid}`` is "1"
                             (writers: C6 WhatsApp consent flow)
    """
    pref = ChannelPref()

    push_devices = await redis.scard(f"kid:{kid}:push_devices")
    pref.has_push = bool(push_devices)

    profile_raw = await redis.hgetall(f"user_profile:{kid}")
    profile = await _decode_hash(profile_raw)
    pref.has_email = bool(profile.get("email"))

    wa_raw = await redis.get(f"whatsapp:opt:{brand_id}:{kid}")
    if isinstance(wa_raw, (bytes, bytearray)):
        wa_raw = wa_raw.decode()
    pref.has_whatsapp = wa_raw == "1"

    last_raw = await redis.get(f"reengagement:lastchannel:{brand_id}:{kid}")
    if isinstance(last_raw, (bytes, bytearray)):
        last_raw = last_raw.decode()
    pref.last_success_channel = last_raw or ""

    return pref


# ── Lapse scoring & cascade selection ───────────────────────────────────


async def compute_lapse_score(
    redis: Any,
    user_id: str,
    brand_id: str,
    *,
    now: float | None = None,
) -> float:
    """0..1 score: how lapsed is this (user, brand)?

    Pulls:
      * ``user:{kid}:last_visit:{brand_id}`` (epoch ts written on each
        in-store/in-app session by the engagement layer)
      * ``voucher:user:{kid}:brand:{brand}:unused`` (count)
      * ``ltv:{brand_id}:{kid}`` (lifetime value in SGD)

    Returns 0 if active (≤1d), saturates to 1.0 at ``LAPSE_FULL_DAYS``.
    """
    now = now if now is not None else time.time()

    last_visit_raw = await redis.get(f"user:{user_id}:last_visit:{brand_id}")
    if isinstance(last_visit_raw, (bytes, bytearray)):
        last_visit_raw = last_visit_raw.decode()
    try:
        last_visit = float(last_visit_raw) if last_visit_raw else 0.0
    except ValueError:
        last_visit = 0.0

    if last_visit <= 0:
        # Never visited → treat as fully lapsed.
        days_since = LAPSE_FULL_DAYS
    else:
        days_since = max(0.0, (now - last_visit) / 86_400.0)

    base = min(1.0, days_since / LAPSE_FULL_DAYS)

    # Lift score slightly if the user has unused vouchers (sunk cost
    # they aren't redeeming → strong lapse signal).
    unused_raw = await redis.get(
        f"voucher:user:{user_id}:brand:{brand_id}:unused"
    )
    if isinstance(unused_raw, (bytes, bytearray)):
        unused_raw = unused_raw.decode()
    try:
        unused = int(unused_raw or 0)
    except ValueError:
        unused = 0
    if unused > 0:
        base = min(1.0, base + 0.05 * min(unused, 4))

    return round(base, 4)


async def select_cascade(
    redis: Any,
    user_id: str,
    brand_id: str,
    *,
    now: float | None = None,
) -> str:
    """Pick the cascade type for this (user, brand) using the spec triggers.

    Returns one of:
      * ``CascadeType.LIGHT``    — 7d silence
      * ``CascadeType.MEDIUM``   — 14d silence + ≥1 unused voucher
      * ``CascadeType.HEAVY``    — 30d silence + LTV ≥ $20
      * ``CascadeType.WIN_BACK`` — 60d silence
      * ``""`` (empty)           — no cascade should fire
    """
    now = now if now is not None else time.time()

    last_visit_raw = await redis.get(f"user:{user_id}:last_visit:{brand_id}")
    if isinstance(last_visit_raw, (bytes, bytearray)):
        last_visit_raw = last_visit_raw.decode()
    try:
        last_visit = float(last_visit_raw) if last_visit_raw else 0.0
    except ValueError:
        last_visit = 0.0
    if last_visit <= 0:
        return ""  # we don't blast users who never visited
    days_since = (now - last_visit) / 86_400.0
    if days_since < 7:
        return ""

    unused_raw = await redis.get(
        f"voucher:user:{user_id}:brand:{brand_id}:unused"
    )
    if isinstance(unused_raw, (bytes, bytearray)):
        unused_raw = unused_raw.decode()
    try:
        unused = int(unused_raw or 0)
    except ValueError:
        unused = 0

    ltv_raw = await redis.get(f"ltv:{brand_id}:{user_id}")
    if isinstance(ltv_raw, (bytes, bytearray)):
        ltv_raw = ltv_raw.decode()
    try:
        ltv = float(ltv_raw or 0)
    except ValueError:
        ltv = 0.0

    # Most-specific first.
    if days_since >= 60:
        return CascadeType.WIN_BACK
    if days_since >= 30 and ltv >= 20:
        return CascadeType.HEAVY
    if days_since >= 14 and unused >= 1:
        return CascadeType.MEDIUM
    if days_since >= 7:
        return CascadeType.LIGHT
    return ""


async def recommend_channel(
    redis: Any,
    user_id: str,
    brand_id: str,
    *,
    ordering: tuple[str, ...] = ("whatsapp", "push", "email"),
) -> str:
    """Best channel for this user. Empty string if unreachable."""
    pref = await _resolve_channels(redis, brand_id, user_id)
    # If the user has previously engaged via a channel, lift it to the top.
    if pref.last_success_channel and pref.last_success_channel in ordering:
        promoted = (pref.last_success_channel,) + tuple(
            c for c in ordering if c != pref.last_success_channel
        )
        ordering = promoted  # type: ignore[assignment]
    return pref.best(ordering)


# ── TriSoul-aware copy crafting ────────────────────────────────────────


_COPY_GENERIC: dict[str, dict[str, str]] = {
    "light_d0_subject": {
        "en-SG": "We miss you at {brand_name}",
    },
    "light_d0_body": {
        "en-SG": "It's been a week — come back for your usual.",
    },
    "medium_d0_subject": {
        "en-SG": "Your voucher's waiting at {brand_name}",
    },
    "medium_d0_body": {
        "en-SG": "Save {offer_pct}% on your next visit — voucher still active.",
    },
    "medium_d3_subject": {
        "en-SG": "Quick reminder from {brand_name}",
    },
    "medium_d3_body": {
        "en-SG": "Your {offer_pct}% off won't last forever.",
    },
    "medium_d7_subject": {
        "en-SG": "Last chance at {brand_name}",
    },
    "medium_d7_body": {
        "en-SG": "{offer_pct}% off expires soon. Treat yourself.",
    },
    "heavy_d0_subject": {
        "en-SG": "Still your favourite, {brand_name}?",
    },
    "heavy_d0_body": {
        "en-SG": "We've kept {offer_pct}% off your next round.",
    },
    "heavy_d5_subject": {
        "en-SG": "{brand_name} has a new pick for you",
    },
    "heavy_d5_body": {
        "en-SG": "Try it with {offer_pct}% off.",
    },
    "heavy_d12_subject": {
        "en-SG": "On us — {offer_pct}% off at {brand_name}",
    },
    "heavy_d12_body": {
        "en-SG": "Just a tap away. Voucher inside.",
    },
    "heavy_d21_subject": {
        "en-SG": "{brand_name} — a token of thanks",
    },
    "heavy_d21_body": {
        "en-SG": "Loyal customers get loyal rewards: {offer_pct}% off.",
    },
    "heavy_d30_subject": {
        "en-SG": "One more time? {offer_pct}% from {brand_name}",
    },
    "heavy_d30_body": {
        "en-SG": "Biggest offer yet — {offer_pct}% off, any item.",
    },
    "winback_subject": {
        "en-SG": "We want you back: {offer_pct}% off at {brand_name}",
    },
    "winback_body": {
        "en-SG": "Final offer. {offer_pct}% — anything, any time this month.",
    },
}


async def _trisoul_features(redis: Any, user_id: str) -> dict[str, float]:
    """Best-effort TriSoul fetch. Degrades silently if Wave-C isn't loaded."""
    try:
        from app.routers import trisoul_integration as _ts  # local import
        return await _ts.get_features(user_id, redis)
    except Exception as exc:  # noqa: BLE001 — optional integration
        logger.debug("trisoul unavailable: %s", exc)
        return {}


def _tone_from_trisoul(features: dict[str, float]) -> str:
    """Project TriSoul features → a discrete copy-tone slot.

    Cheap heuristic: high ``urgency`` → "urgent", high ``social`` →
    "social", otherwise default "warm". This is the only place we
    interpret TriSoul axes; if the production model changes its
    feature names we only patch here.
    """
    if not features:
        return "warm"
    urgency = float(features.get("urgency", 0.0))
    social = float(features.get("social", 0.0))
    if urgency >= 0.7:
        return "urgent"
    if social >= 0.7:
        return "social"
    return "warm"


def _tone_prefix(tone: str) -> str:
    return {
        "urgent": "[Today only] ",
        "social": "[For you] ",
        "warm": "",
    }.get(tone, "")


async def craft_message(
    redis: Any,
    user_id: str,
    brand_id: str,
    lapse_days: int | float,
    *,
    cascade_type: str | None = None,
    step_idx: int = 0,
    locale: str = "en-SG",
    brand_name: str | None = None,
) -> dict[str, Any]:
    """Build a personalised message envelope for one cascade step.

    Pulls TriSoul features (if available) to nudge the tone, fills
    ``brand_name``/``offer_pct`` into the copy slot, and returns a dict
    the worker can hand to the appropriate transport (push enqueue,
    email enqueue, or WhatsApp client).

    Falls back gracefully when the cascade is unknown — we still return
    a sensible generic message so the caller doesn't have to special-case.
    """
    cascade = CASCADE_BLUEPRINTS.get(cascade_type or "")
    if cascade and 0 <= step_idx < len(cascade.steps):
        step = cascade.steps[step_idx]
    else:
        step = _LIGHT.steps[0]

    features = await _trisoul_features(redis, user_id)
    tone = _tone_from_trisoul(features)

    brand_label = brand_name or brand_id

    subject_pool = _COPY_GENERIC.get(step.subject_key, {})
    body_pool = _COPY_GENERIC.get(step.body_key, {})
    subject_tmpl = subject_pool.get(locale) or subject_pool.get("en-SG", "")
    body_tmpl = body_pool.get(locale) or body_pool.get("en-SG", "")

    ctx = {
        "brand_name": brand_label,
        "offer_pct": step.offer_pct,
        "lapse_days": int(lapse_days),
    }
    subject = _tone_prefix(tone) + _interpolate(subject_tmpl, ctx)
    body = _interpolate(body_tmpl, ctx)

    return {
        "subject": subject,
        "title": subject,  # push envelopes use ``title``
        "body": body,
        "offer_pct": step.offer_pct,
        "template_id": step.template_id,
        "channel_pref": list(step.channel_pref),
        "tone": tone,
        "personalised": bool(features),
        "lapse_days": int(lapse_days),
        "step_idx": step_idx,
        "cascade_type": cascade.cascade_type if cascade else "",
    }


def _interpolate(template: str, ctx: dict[str, Any]) -> str:
    """Minimal {key} interpolation — Jinja is overkill for these snippets."""
    if not template:
        return ""
    out = template
    for k, v in ctx.items():
        out = out.replace("{" + k + "}", str(v))
    return out


# ── Suppression ─────────────────────────────────────────────────────────


def in_quiet_hours_sgt(now_utc: datetime | None = None) -> bool:
    """True iff SGT time is within 22:00..07:00 (quiet hours)."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    local_hour = (now_utc.hour + SGT_UTC_OFFSET_HOURS) % 24
    if QUIET_HOURS_START_LOCAL > QUIET_HOURS_END_LOCAL:
        return (
            local_hour >= QUIET_HOURS_START_LOCAL
            or local_hour < QUIET_HOURS_END_LOCAL
        )
    return QUIET_HOURS_START_LOCAL <= local_hour < QUIET_HOURS_END_LOCAL


async def frequency_capped(
    redis: Any, brand_id: str, user_id: str, *, now: float | None = None,
) -> bool:
    """True iff the user got a reengagement msg within the last 7 days."""
    last_raw = await redis.get(last_send_key(brand_id, user_id))
    if not last_raw:
        return False
    if isinstance(last_raw, (bytes, bytearray)):
        last_raw = last_raw.decode()
    try:
        last_ts = float(last_raw)
    except ValueError:
        return False
    now = now if now is not None else time.time()
    return (now - last_ts) < FREQUENCY_CAP_SECONDS


async def _just_redeemed(
    redis: Any, brand_id: str, user_id: str, *, now: float | None = None,
) -> bool:
    """Suppress sends if the user redeemed in the last 6 hours.

    Sometimes a visit happens between cascade start and send — we
    don't want to look tone-deaf by chasing someone who just walked in.
    """
    raw = await redis.get(f"user:{user_id}:last_redeem:{brand_id}")
    if not raw:
        return False
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    try:
        ts = float(raw)
    except ValueError:
        return False
    now = now if now is not None else time.time()
    return (now - ts) < 6 * 3600


async def is_opted_out(redis: Any, brand_id: str, user_id: str) -> bool:
    raw = await redis.get(optout_key(brand_id, user_id))
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    return raw == "1"


async def is_suppressed(
    redis: Any,
    brand_id: str,
    user_id: str,
    *,
    now: float | None = None,
    now_utc: datetime | None = None,
) -> tuple[bool, str]:
    """Run the full suppression cascade. Returns (suppressed, reason).

    Order matters — quiet hours suppress *all* channels, opt-out is
    absolute, just-redeemed beats frequency-cap (more informative reason).
    """
    if await is_opted_out(redis, brand_id, user_id):
        return True, "opted_out"
    if in_quiet_hours_sgt(now_utc):
        return True, "quiet_hours"
    if await _just_redeemed(redis, brand_id, user_id, now=now):
        return True, "just_redeemed"
    if await frequency_capped(redis, brand_id, user_id, now=now):
        return True, "frequency_cap"
    return False, ""


# ── Audit ───────────────────────────────────────────────────────────────


async def _audit(
    redis: Any,
    brand_id: str,
    user_id: str,
    event: str,
    **extra: Any,
) -> None:
    """Append an envelope to the per-user audit log (capped)."""
    envelope = {
        "ts": time.time(),
        "event": event,
        "brand_id": brand_id,
        "user_id": user_id,
        **extra,
    }
    try:
        await redis.lpush(audit_log_key(brand_id, user_id), json.dumps(envelope))
        await redis.ltrim(audit_log_key(brand_id, user_id), 0, AUDIT_LOG_MAX)
    except Exception as exc:  # noqa: BLE001 — audit is best-effort
        logger.debug("audit write failed: %s", exc)


# ── Cascade lifecycle ──────────────────────────────────────────────────


async def start_cascade(
    redis: Any,
    *,
    brand_id: str,
    user_id: str,
    cascade_type: str,
    now: float | None = None,
    cascade_id: str | None = None,
) -> dict[str, Any]:
    """Open a cascade for (brand, user). Idempotent — returns existing one."""
    if cascade_type not in CASCADE_BLUEPRINTS:
        raise ValueError(f"unknown cascade_type: {cascade_type!r}")
    now = now if now is not None else time.time()
    key = active_cascade_key(brand_id, user_id)
    existing = await redis.hgetall(key)
    if existing:
        decoded = await _decode_hash(existing)
        if decoded.get("status") == "active":
            return {
                "status": "already_active",
                "cascade_id": decoded.get("cascade_id", ""),
                "cascade_type": decoded.get("cascade_type", ""),
                "started_at": decoded.get("started_at", ""),
            }

    cid = cascade_id or f"casc_{int(now)}_{user_id[-6:]}"
    blueprint = CASCADE_BLUEPRINTS[cascade_type]
    first_step_due = now + blueprint.steps[0].offset_days * 86_400

    mapping = {
        "cascade_id": cid,
        "cascade_type": cascade_type,
        "started_at": str(now),
        "step_idx": "0",
        "next_due_ts": str(first_step_due),
        "status": "active",
        "steps_total": str(len(blueprint.steps)),
    }
    await redis.hset(key, mapping=mapping)
    await redis.sadd(atrisk_cohort_key(brand_id), user_id)
    await redis.hincrby(stats_key(brand_id), "started", 1)
    await redis.hincrby(stats_key(brand_id), f"started_{cascade_type}", 1)
    await _audit(
        redis, brand_id, user_id, "cascade_start",
        cascade_id=cid, cascade_type=cascade_type,
    )

    return {
        "status": "started",
        "cascade_id": cid,
        "cascade_type": cascade_type,
        "started_at": now,
        "steps_total": len(blueprint.steps),
        "first_step_due_ts": first_step_due,
    }


async def cancel_cascade(
    redis: Any,
    *,
    brand_id: str,
    user_id: str,
    reason: str = "user_returned",
) -> bool:
    """Mark an active cascade as completed (e.g. user just visited)."""
    key = active_cascade_key(brand_id, user_id)
    existing = await redis.hgetall(key)
    if not existing:
        return False
    decoded = await _decode_hash(existing)
    if decoded.get("status") != "active":
        return False
    await redis.hset(key, mapping={"status": "cancelled", "cancelled_reason": reason})
    await redis.srem(atrisk_cohort_key(brand_id), user_id)
    await redis.hincrby(stats_key(brand_id), "cancelled", 1)
    await _audit(
        redis, brand_id, user_id, "cascade_cancel",
        cascade_id=decoded.get("cascade_id", ""), reason=reason,
    )
    return True


async def send_cascade_step(
    redis: Any,
    *,
    brand_id: str,
    user_id: str,
    locale: str = "en-SG",
    brand_name: str | None = None,
    now: float | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Fire the next due step in this user's active cascade.

    Honours all suppression rules (quiet hours, freq cap, opt-out,
    just-redeemed) unless ``force=True``. ``force`` is used by the
    admin "test cascade" endpoint and the dry-run paths.

    Returns a structured envelope describing what happened. The worker
    persists this; the router echoes it back in API responses.
    """
    now = now if now is not None else time.time()
    key = active_cascade_key(brand_id, user_id)
    raw = await redis.hgetall(key)
    if not raw:
        return {"status": "no_active_cascade"}
    state = await _decode_hash(raw)
    if state.get("status") != "active":
        return {"status": "not_active", "cascade_status": state.get("status", "")}

    step_idx = int(state.get("step_idx") or 0)
    cascade_type = state.get("cascade_type", "")
    blueprint = CASCADE_BLUEPRINTS.get(cascade_type)
    if not blueprint or step_idx >= len(blueprint.steps):
        await redis.hset(key, mapping={"status": "completed"})
        await redis.srem(atrisk_cohort_key(brand_id), user_id)
        await _audit(
            redis, brand_id, user_id, "cascade_complete",
            cascade_id=state.get("cascade_id", ""),
        )
        return {"status": "completed"}

    next_due_ts = float(state.get("next_due_ts") or 0)
    if now < next_due_ts and not force:
        return {
            "status": "not_due",
            "next_due_ts": next_due_ts,
            "step_idx": step_idx,
        }

    # Suppression gates.
    if not force:
        suppressed, reason = await is_suppressed(
            redis, brand_id, user_id, now=now,
        )
        if suppressed:
            await redis.hincrby(stats_key(brand_id), "suppressed", 1)
            await redis.hincrby(stats_key(brand_id), f"suppress_{reason}", 1)
            await _audit(
                redis, brand_id, user_id, "send_suppressed",
                cascade_id=state.get("cascade_id", ""),
                step_idx=step_idx, reason=reason,
            )
            # quiet_hours / just_redeemed → retry on next cron pass
            # (don't advance step_idx). frequency_cap / opt-out are sticky.
            if reason in ("quiet_hours", "just_redeemed"):
                # Push the next-due-ts forward 1h so we re-evaluate.
                await redis.hset(
                    key, mapping={"next_due_ts": str(now + 3600)},
                )
            return {
                "status": "suppressed",
                "reason": reason,
                "step_idx": step_idx,
            }

    # Resolve channel + craft message.
    step = blueprint.steps[step_idx]
    pref = await _resolve_channels(redis, brand_id, user_id)
    channel = pref.best(step.channel_pref) or pref.best(("whatsapp", "push", "email"))
    if not channel:
        await redis.hincrby(stats_key(brand_id), "unreachable", 1)
        await _audit(
            redis, brand_id, user_id, "send_unreachable",
            cascade_id=state.get("cascade_id", ""), step_idx=step_idx,
        )
        # No way to reach this user — advance the step so we don't
        # spin forever. They'll get the next step if reachability is
        # restored later.
        _advance(state, blueprint, now)
        await redis.hset(key, mapping=state)
        return {"status": "unreachable", "step_idx": step_idx}

    days_since_last = 0
    last_visit_raw = await redis.get(f"user:{user_id}:last_visit:{brand_id}")
    if isinstance(last_visit_raw, (bytes, bytearray)):
        last_visit_raw = last_visit_raw.decode()
    try:
        last_visit = float(last_visit_raw) if last_visit_raw else 0.0
        if last_visit > 0:
            days_since_last = int((now - last_visit) / 86_400)
    except ValueError:
        pass

    msg = await craft_message(
        redis,
        user_id,
        brand_id,
        days_since_last,
        cascade_type=cascade_type,
        step_idx=step_idx,
        locale=locale,
        brand_name=brand_name,
    )

    # Hand-off to the right transport. Errors here are absorbed into
    # the audit log so a single send glitch doesn't poison the
    # cascade-stepper.
    delivery: dict[str, Any] = {"channel": channel}
    try:
        if channel == "whatsapp":
            delivery.update(
                await _send_whatsapp(redis, brand_id, user_id, msg)
            )
        elif channel == "push":
            delivery.update(
                await _send_push(redis, brand_id, user_id, msg, locale=locale)
            )
        elif channel == "email":
            delivery.update(
                await _send_email(redis, brand_id, user_id, msg, locale=locale)
            )
        else:
            delivery.update({"delivered": False, "error": "unknown_channel"})
    except Exception as exc:  # noqa: BLE001
        logger.warning("cascade send failed: %s", exc)
        delivery.update({"delivered": False, "error": f"exception:{exc}"})

    # Persist last-send + advance step + stats.
    await redis.set(last_send_key(brand_id, user_id), str(now))
    await redis.set(
        f"reengagement:lastchannel:{brand_id}:{user_id}", channel, ex=90 * 86_400,
    )
    await redis.hincrby(stats_key(brand_id), "sent", 1)
    await redis.hincrby(stats_key(brand_id), f"sent_{channel}", 1)
    if msg.get("personalised"):
        await redis.hincrby(stats_key(brand_id), "personalised_sent", 1)

    _advance(state, blueprint, now)
    await redis.hset(key, mapping=state)

    await _audit(
        redis, brand_id, user_id, "send",
        cascade_id=state.get("cascade_id", ""),
        step_idx=step_idx,
        channel=channel,
        template_id=msg.get("template_id"),
        offer_pct=msg.get("offer_pct", 0),
        tone=msg.get("tone"),
        personalised=msg.get("personalised", False),
        delivery=delivery,
    )

    return {
        "status": "sent",
        "channel": channel,
        "step_idx": step_idx,
        "next_step_idx": int(state.get("step_idx") or 0),
        "next_due_ts": float(state.get("next_due_ts") or 0),
        "message": msg,
        "delivery": delivery,
    }


def _advance(state: dict[str, str], blueprint: CascadeBlueprint, now: float) -> None:
    """Bump step_idx and recompute next_due_ts; mark completed if last step."""
    next_idx = int(state.get("step_idx") or 0) + 1
    state["step_idx"] = str(next_idx)
    if next_idx >= len(blueprint.steps):
        state["status"] = "completed"
        state["completed_at"] = str(now)
        return
    started_at = float(state.get("started_at") or now)
    next_step = blueprint.steps[next_idx]
    state["next_due_ts"] = str(started_at + next_step.offset_days * 86_400)


async def send_cascade(
    redis: Any,
    *,
    brand_id: str,
    user_id: str,
    cascade_id: str | None = None,
    locale: str = "en-SG",
    brand_name: str | None = None,
    max_steps: int = 1,
    now: float | None = None,
) -> dict[str, Any]:
    """Orchestrate the timed sends — usually called by the worker.

    Walks the user's active cascade firing as many due steps as it can
    (bounded by ``max_steps`` to prevent runaway loops in pathological
    state). Returns a list of per-step envelopes.

    NB: in steady state ``max_steps=1`` is correct because each step
    has a deliberate calendar gap. ``max_steps>1`` is used by the
    admin test-cascade endpoint to fast-forward through the whole flow.
    """
    sent: list[dict[str, Any]] = []
    suppressions: list[dict[str, Any]] = []
    for _ in range(max_steps):
        result = await send_cascade_step(
            redis,
            brand_id=brand_id,
            user_id=user_id,
            locale=locale,
            brand_name=brand_name,
            now=now,
        )
        if result.get("status") == "sent":
            sent.append(result)
            # Subsequent steps in the same call only fire if forced — in
            # steady state they're not due yet.
            if max_steps > 1:
                # Bump now forward by 1d so the next step becomes due in
                # test runs.
                now = (now or time.time()) + 86_400
                continue
            break
        elif result.get("status") in ("suppressed", "not_due", "unreachable"):
            suppressions.append(result)
            break
        else:
            break

    return {
        "brand_id": brand_id,
        "user_id": user_id,
        "cascade_id": cascade_id,
        "sent_count": len(sent),
        "sent": sent,
        "suppressions": suppressions,
    }


# ── Transport wrappers ─────────────────────────────────────────────────


async def _send_push(
    redis: Any, brand_id: str, user_id: str, msg: dict[str, Any], *, locale: str,
) -> dict[str, Any]:
    """Enqueue a push via the same path as Wave-A push templates."""
    try:
        from app.services.email_template_service import enqueue_push
        from app.email_templates import PUSH_TEMPLATES
        template_id = msg.get("template_id") or "push_streak_break"
        if template_id not in PUSH_TEMPLATES:
            template_id = "push_streak_break"
        # The Wave-A templates accept variable maps — we feed the
        # crafted copy values into the standard slots. Fields that
        # the template doesn't need are silently ignored by the renderer.
        defaults = {
            "brand_name": msg.get("subject", "").split("at ")[-1],
            "campaign_title": msg.get("subject", ""),
            "friend_name": "",
            "voucher_name": f"{msg.get('offer_pct', 0)}% off",
            "area_name": "your area",
        }
        try:
            await enqueue_push(
                redis,
                brand_id=brand_id,
                template_id=template_id,
                locale=locale,
                recipient_kid=user_id,
                **defaults,
            )
        except Exception:
            # Some templates have stricter required_vars — fall back to
            # the streak_break template which only needs brand_name.
            await enqueue_push(
                redis,
                brand_id=brand_id,
                template_id="push_streak_break",
                locale=locale,
                recipient_kid=user_id,
                brand_name=defaults["brand_name"],
            )
        return {"delivered": True, "transport": "push_queue"}
    except Exception as exc:  # noqa: BLE001
        return {"delivered": False, "error": f"push_enqueue:{exc}"}


async def _send_email(
    redis: Any, brand_id: str, user_id: str, msg: dict[str, Any], *, locale: str,
) -> dict[str, Any]:
    """Enqueue an email envelope. Templates are picked from Wave-A registry."""
    try:
        # Minimal envelope — bypass template rendering when we already
        # have crafted copy (the message is itself the rendered payload).
        envelope = {
            "template_id": msg.get("template_id", ""),
            "locale": locale,
            "recipient": user_id,  # the worker resolves email by user_id
            "subject": msg.get("subject", ""),
            "body_text": msg.get("body", ""),
            "body_html": (
                "<html><body><p>"
                + msg.get("body", "").replace("\n", "</p><p>")
                + "</p></body></html>"
            ),
            "kind": "reengagement",
        }
        await redis.rpush(
            f"email_queue:brand:{brand_id}", json.dumps(envelope),
        )
        return {"delivered": True, "transport": "email_queue"}
    except Exception as exc:  # noqa: BLE001
        return {"delivered": False, "error": f"email_enqueue:{exc}"}


async def _send_whatsapp(
    redis: Any, brand_id: str, user_id: str, msg: dict[str, Any],
) -> dict[str, Any]:
    """Stub WhatsApp send — pushes onto ``whatsapp_queue:brand:{brand}``.

    The actual WhatsApp transport (C6) reads this same queue.
    """
    try:
        envelope = {
            "to": user_id,
            "kind": "reengagement",
            "subject": msg.get("subject", ""),
            "body": msg.get("body", ""),
            "offer_pct": msg.get("offer_pct", 0),
            "tone": msg.get("tone"),
            "personalised": msg.get("personalised", False),
        }
        await redis.rpush(
            f"whatsapp_queue:brand:{brand_id}", json.dumps(envelope),
        )
        return {"delivered": True, "transport": "whatsapp_queue"}
    except Exception as exc:  # noqa: BLE001
        return {"delivered": False, "error": f"whatsapp_enqueue:{exc}"}


# ── Cohort scan + stats ─────────────────────────────────────────────────


async def evaluate_users(
    redis: Any,
    *,
    brand_id: str,
    cohort: Iterable[str] | None = None,
    now: float | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Daily scan: open cascades for newly-lapsed users; tick existing ones.

    Returns a structured report (started / advanced / suppressed / errors).
    Cohort may be passed explicitly (e.g. by the worker from a known
    user index) or omitted to default to the brand's user index
    ``brand:{brand}:users`` set.
    """
    now = now if now is not None else time.time()

    if cohort is None:
        raw = await redis.smembers(f"brand:{brand_id}:users")
        cohort = sorted(
            m.decode() if isinstance(m, (bytes, bytearray)) else str(m)
            for m in (raw or set())
        )

    started: list[dict[str, Any]] = []
    advanced: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for uid in cohort:
        if await is_opted_out(redis, brand_id, uid):
            skipped.append({"user_id": uid, "reason": "opted_out"})
            continue

        # Currently-active cascade → tick it.
        existing = await redis.hgetall(active_cascade_key(brand_id, uid))
        if existing:
            decoded = await _decode_hash(existing)
            if decoded.get("status") == "active":
                if dry_run:
                    advanced.append({"user_id": uid, "would_tick": True})
                else:
                    advanced.append(
                        {
                            "user_id": uid,
                            **(
                                await send_cascade_step(
                                    redis,
                                    brand_id=brand_id,
                                    user_id=uid,
                                    now=now,
                                )
                            ),
                        }
                    )
                continue

        # No active cascade — see if one should start.
        cascade_type = await select_cascade(redis, uid, brand_id, now=now)
        if not cascade_type:
            skipped.append({"user_id": uid, "reason": "not_lapsed_enough"})
            continue
        if dry_run:
            started.append({"user_id": uid, "cascade_type": cascade_type, "dry_run": True})
            continue
        started.append(
            {
                "user_id": uid,
                **(
                    await start_cascade(
                        redis,
                        brand_id=brand_id,
                        user_id=uid,
                        cascade_type=cascade_type,
                        now=now,
                    )
                ),
            }
        )

    return {
        "brand_id": brand_id,
        "scanned": len(list(cohort)) if not isinstance(cohort, list) else len(cohort),
        "started_count": len(started),
        "advanced_count": len(advanced),
        "skipped_count": len(skipped),
        "started": started,
        "advanced": advanced,
        "skipped": skipped[:50],  # cap echoed payload
        "ran_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
        "dry_run": dry_run,
    }


async def cascade_stats(redis: Any, brand_id: str) -> dict[str, Any]:
    """Return aggregate counters + at-risk cohort size."""
    raw = await redis.hgetall(stats_key(brand_id))
    stats = await _decode_hash(raw)
    typed: dict[str, Any] = {}
    for k, v in stats.items():
        try:
            typed[k] = int(v)
        except (TypeError, ValueError):
            typed[k] = v
    cohort_size = await redis.scard(atrisk_cohort_key(brand_id))
    return {
        "brand_id": brand_id,
        "stats": typed,
        "at_risk_count": cohort_size,
    }


async def at_risk_cohort(
    redis: Any, brand_id: str, *, limit: int = 200,
) -> dict[str, Any]:
    """Return the currently-in-cascade user ids + their cascade types."""
    raw = await redis.smembers(atrisk_cohort_key(brand_id))
    uids = sorted(
        m.decode() if isinstance(m, (bytes, bytearray)) else str(m)
        for m in (raw or set())
    )[:limit]

    rows: list[dict[str, Any]] = []
    for uid in uids:
        existing = await redis.hgetall(active_cascade_key(brand_id, uid))
        decoded = await _decode_hash(existing)
        if decoded.get("status") == "active":
            rows.append(
                {
                    "user_id": uid,
                    "cascade_id": decoded.get("cascade_id", ""),
                    "cascade_type": decoded.get("cascade_type", ""),
                    "step_idx": int(decoded.get("step_idx") or 0),
                    "next_due_ts": float(decoded.get("next_due_ts") or 0),
                    "started_at": float(decoded.get("started_at") or 0),
                }
            )

    return {
        "brand_id": brand_id,
        "count": len(rows),
        "cohort": rows,
    }
