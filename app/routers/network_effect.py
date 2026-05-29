"""Network Effect Engine — viral growth triggers for KiX gamification.

Turns every game/user event into a viral acquisition trigger. Six
mechanics, all brand-agnostic and brand-isolated via Redis key
namespacing:

    1. ShareToWin       — high score → shareable card
    2. EnergyInvite     — out-of-energy → invite for refill
    3. FriendChallenge  — badge/score → challenge friend
    4. LadderClimb      — near next tier → invite N to promote
    5. StreakRescue     — about to break streak → friend rescues
    6. AutoShare        — major milestone → auto-generate card

Each trigger has init + redeem endpoints; redeem grants symmetric
"pending_rewards" to both inviter and invitee (wired to real primitives
later). Viral coefficient = converted / invited, per brand per trigger.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import date, timedelta
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Constants ──────────────────────────────────────────────────────────────

INVITE_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days

TRIGGER_SHARE_TO_WIN = "share_to_win"
TRIGGER_ENERGY_INVITE = "energy_invite"
TRIGGER_FRIEND_CHALLENGE = "friend_challenge"
TRIGGER_LADDER_CLIMB = "ladder_climb"
TRIGGER_STREAK_RESCUE = "streak_rescue"
TRIGGER_AUTO_SHARE = "auto_share"

ALL_TRIGGERS = (
    TRIGGER_SHARE_TO_WIN,
    TRIGGER_ENERGY_INVITE,
    TRIGGER_FRIEND_CHALLENGE,
    TRIGGER_LADDER_CLIMB,
    TRIGGER_STREAK_RESCUE,
    TRIGGER_AUTO_SHARE,
)

# Default base URL for share landing — override via context.base_url
DEFAULT_LANDING_BASE = "https://play.kix.app"

# Per-trigger default reward template. Each side (inviter / invitee)
# gets a list of primitive ops to apply on redeem. Real wiring happens
# later — we just record what *should* be granted.
REWARD_TEMPLATES: dict[str, dict[str, list[dict[str, Any]]]] = {
    TRIGGER_SHARE_TO_WIN: {
        "inviter": [{"op": "xp_grant", "amount": 50}],
        "invitee": [{"op": "energy_grant", "amount": 10}],
    },
    TRIGGER_ENERGY_INVITE: {
        "inviter": [{"op": "energy_grant", "amount": 10}],
        "invitee": [{"op": "energy_grant", "amount": 10}],
    },
    TRIGGER_FRIEND_CHALLENGE: {
        "inviter": [{"op": "xp_grant", "amount": 30}],
        "invitee": [{"op": "xp_grant", "amount": 30}],
    },
    TRIGGER_LADDER_CLIMB: {
        "inviter": [{"op": "tier_promote", "tiers": 1}],
        "invitee": [{"op": "energy_grant", "amount": 15}],
    },
    TRIGGER_STREAK_RESCUE: {
        "inviter": [{"op": "streak_rescue"}, {"op": "xp_grant", "amount": 25}],
        "invitee": [{"op": "xp_grant", "amount": 25}],
    },
    TRIGGER_AUTO_SHARE: {
        "inviter": [{"op": "xp_grant", "amount": 20}],
        "invitee": [{"op": "energy_grant", "amount": 5}],
    },
}

# Ladder climb default: friends needed for instant promotion
LADDER_FRIENDS_NEEDED = 5

# Streak rescue cost (in energy) for the friend doing the rescuing
STREAK_RESCUE_COST = 5

# ── Viral compounding constants (P0 fix 2026-05-29) ───────────────────────
MAX_INHERITANCE_DEPTH = 5
DAILY_COUNTER_TTL_SECONDS = 35 * 24 * 60 * 60
K_EXPLOSION_THRESHOLD = 1.0
DEFAULT_K_WINDOW_DAYS = 7
AUTO_EMIT_TRIGGERS = (
    TRIGGER_SHARE_TO_WIN,
    TRIGGER_ENERGY_INVITE,
    TRIGGER_FRIEND_CHALLENGE,
    TRIGGER_LADDER_CLIMB,
    TRIGGER_STREAK_RESCUE,
)


# ── Pydantic models ────────────────────────────────────────────────────────


class ShareToWinRequest(BaseModel):
    user_id: str
    brand_id: str
    score: int
    game_slug: str
    base_url: str | None = None


class EnergyInviteRequest(BaseModel):
    user_id: str
    brand_id: str
    energy_short_by: int
    base_url: str | None = None


class FriendChallengeRequest(BaseModel):
    user_id: str
    brand_id: str
    badge_id: str | None = None
    score: int | None = None
    game_slug: str | None = None
    base_url: str | None = None


class LadderClimbRequest(BaseModel):
    user_id: str
    brand_id: str
    target_tier: str
    base_url: str | None = None


class StreakRescueRequest(BaseModel):
    user_id: str
    brand_id: str
    current_streak: int
    base_url: str | None = None


class AutoShareRequest(BaseModel):
    user_id: str
    brand_id: str
    event: str  # "level_up" | "badge_earned" | "highscore"
    context: dict[str, Any] = Field(default_factory=dict)
    base_url: str | None = None


class RedeemRequest(BaseModel):
    invite_token: str
    new_user_id: str
    brand_id: str


# ── Redis key helpers ──────────────────────────────────────────────────────


def _k_invite(token: str) -> str:
    return f"invite:{token}"


def _k_invited(brand_id: str, trigger: str) -> str:
    return f"brand:{brand_id}:viral:{trigger}:invited"


def _k_converted(brand_id: str, trigger: str) -> str:
    return f"brand:{brand_id}:viral:{trigger}:converted"


def _k_pending(user_id: str, brand_id: str) -> str:
    """List of pending reward ops for a given (user, brand)."""
    return f"brand:{brand_id}:user:{user_id}:pending_rewards"


def _k_ladder_progress(brand_id: str, user_id: str, target_tier: str) -> str:
    """Set of invite tokens this user has accumulated toward a tier."""
    return f"brand:{brand_id}:user:{user_id}:ladder:{target_tier}:invites"


def _today_str() -> str:
    return date.today().isoformat()


def _k_viral_issued_day(brand_id: str, day: str) -> str:
    return f"viral:brand:{brand_id}:invites_issued:day:{day}"


def _k_viral_redeemed_day(brand_id: str, day: str) -> str:
    return f"viral:brand:{brand_id}:invites_redeemed:day:{day}"


def _k_viral_issued_day_mech(brand_id: str, day: str, trigger: str) -> str:
    return f"viral:brand:{brand_id}:invites_issued:day:{day}:mech:{trigger}"


def _k_viral_redeemed_day_mech(brand_id: str, day: str, trigger: str) -> str:
    return f"viral:brand:{brand_id}:invites_redeemed:day:{day}:mech:{trigger}"


def _k_invite_tree_depth(user_id: str) -> str:
    return f"viral:user:{user_id}:invite_tree:depth"


def _k_explosion_warning(brand_id: str) -> str:
    return f"viral:brand:{brand_id}:explosion_warning"


def _k_ab_assignment(brand_id: str, user_id: str) -> str:
    return f"viral:brand:{brand_id}:user:{user_id}:invite_ab_arm"


def _k_ab_metric(brand_id: str, arm: str, metric: str) -> str:
    return f"viral:brand:{brand_id}:ab:{arm}:{metric}"


# ── Core helpers ───────────────────────────────────────────────────────────


def _now() -> int:
    return int(time.time())


def _new_token() -> str:
    return uuid4().hex[:12]


def _resolve_base_url(supplied: str | None) -> str:
    return (supplied or DEFAULT_LANDING_BASE).rstrip("/")


def _build_share_url(base_url: str | None, brand_id: str, token: str) -> str:
    base = _resolve_base_url(base_url)
    return f"{base}/landing/play.html?brand={brand_id}&invite={token}"


def _safe_color(color: str | None) -> str:
    """Return a CSS-safe hex color or a fallback."""
    if not color:
        return "#1a73e8"
    c = color.strip()
    if not c.startswith("#") or len(c) not in (4, 7):
        return "#1a73e8"
    # Basic hex validation
    for ch in c[1:]:
        if ch.lower() not in "0123456789abcdef":
            return "#1a73e8"
    return c


def _svg_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_card_svg(
    *,
    brand_color: str,
    headline: str,
    big_value: str,
    subline: str,
    cta: str,
) -> str:
    """Build a 240x320 SVG share card. Returns raw SVG string."""
    color = _safe_color(brand_color)
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="240" height="320" viewBox="0 0 240 320">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{color}"/>
      <stop offset="1" stop-color="#000000" stop-opacity="0.55"/>
    </linearGradient>
  </defs>
  <rect width="240" height="320" rx="18" ry="18" fill="url(#bg)"/>
  <text x="120" y="56" font-family="Helvetica,Arial,sans-serif" font-size="14" fill="#ffffff" text-anchor="middle" opacity="0.85">{_svg_escape(headline)}</text>
  <text x="120" y="160" font-family="Helvetica,Arial,sans-serif" font-size="56" font-weight="700" fill="#ffffff" text-anchor="middle">{_svg_escape(big_value)}</text>
  <text x="120" y="200" font-family="Helvetica,Arial,sans-serif" font-size="13" fill="#ffffff" text-anchor="middle" opacity="0.9">{_svg_escape(subline)}</text>
  <rect x="30" y="240" width="180" height="44" rx="22" ry="22" fill="#ffffff"/>
  <text x="120" y="268" font-family="Helvetica,Arial,sans-serif" font-size="14" font-weight="700" fill="{color}" text-anchor="middle">{_svg_escape(cta)}</text>
</svg>"""
    return svg


def _svg_to_data_uri(svg: str) -> str:
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


async def _get_brand_color(r: aioredis.Redis, brand_id: str) -> str:
    """Try to read brand color from cached config; fall back to default."""
    try:
        raw = await r.get(f"config:{brand_id}")
        if raw:
            cfg = json.loads(raw)
            for key in ("brand_color", "primary_color"):
                v = cfg.get(key)
                if v:
                    return v
    except Exception:  # noqa: BLE001
        pass
    return "#1a73e8"


async def _store_invite(
    r: aioredis.Redis,
    *,
    token: str,
    trigger: str,
    from_user_id: str,
    brand_id: str,
    extra: dict[str, Any] | None = None,
    inherited_from: str | None = None,
    depth: int = 0,
) -> dict[str, Any]:
    """Persist invite record + bump invited + daily K-factor counters."""
    now = _now()
    record: dict[str, Any] = {
        "trigger": trigger,
        "from_user_id": from_user_id,
        "brand_id": brand_id,
        "created_at": now,
        "expires_at": now + INVITE_TTL_SECONDS,
        "redeemed": False,
        "extra": extra or {},
        "inherited_from": inherited_from,
        "depth": depth,
    }
    day = _today_str()
    pipe = r.pipeline()
    pipe.set(_k_invite(token), json.dumps(record), ex=INVITE_TTL_SECONDS)
    pipe.incr(_k_invited(brand_id, trigger))
    pipe.incr(_k_viral_issued_day(brand_id, day))
    pipe.expire(_k_viral_issued_day(brand_id, day), DAILY_COUNTER_TTL_SECONDS)
    pipe.incr(_k_viral_issued_day_mech(brand_id, day, trigger))
    pipe.expire(
        _k_viral_issued_day_mech(brand_id, day, trigger),
        DAILY_COUNTER_TTL_SECONDS,
    )
    await pipe.execute()
    # Locale-aware viral-invite email (additive — best-effort).
    # Sends to the *inviter* as a confirmation that their invite is
    # live and that the invitee will see it. The actual invitee-side
    # delivery happens when they tap the share link (separate flow).
    try:
        from app.services.email_template_service import enqueue_email
        locale_raw = await r.get(f"brand:{brand_id}:locale")
        if isinstance(locale_raw, (bytes, bytearray)):
            locale_raw = locale_raw.decode()
        locale = locale_raw or "en-SG"
        await enqueue_email(
            r,
            brand_id=brand_id,
            template_id="viral_invite_received",
            locale=locale,
            user_name=from_user_id,
            inviter_name=from_user_id,
            invite_url=f"invite://{token}",
        )
    except Exception as _exc:  # pragma: no cover — never block invite
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "viral_invite_email enqueue failed brand=%s: %s", brand_id, _exc
        )
    return record


async def _push_pending(
    r: aioredis.Redis,
    *,
    user_id: str,
    brand_id: str,
    ops: list[dict[str, Any]],
    source: str,
    invite_token: str,
) -> None:
    """Append reward ops to the user's pending_rewards list."""
    if not ops:
        return
    pipe = r.pipeline()
    payload = {
        "source": source,
        "invite_token": invite_token,
        "granted_at": _now(),
        "ops": ops,
    }
    pipe.rpush(_k_pending(user_id, brand_id), json.dumps(payload))
    await pipe.execute()


# ── Shared init/redeem mechanics ───────────────────────────────────────────


async def _init_trigger(
    r: aioredis.Redis,
    *,
    trigger: str,
    user_id: str,
    brand_id: str,
    extra: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Allocate a token, persist invite, return (token, record)."""
    if not user_id or not brand_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="user_id and brand_id are required",
        )
    token = _new_token()
    record = await _store_invite(
        r,
        token=token,
        trigger=trigger,
        from_user_id=user_id,
        brand_id=brand_id,
        extra=extra,
    )
    return token, record


async def _redeem_token(
    r: aioredis.Redis,
    *,
    invite_token: str,
    new_user_id: str,
    brand_id: str,
) -> dict[str, Any]:
    """Atomically mark an invite redeemed and grant pending rewards to both sides."""
    if not invite_token or not new_user_id or not brand_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invite_token, new_user_id, brand_id are required",
        )

    raw = await r.get(_k_invite(invite_token))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invite token not found or expired",
        )
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Corrupt invite record",
        )

    if record.get("brand_id") != brand_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Brand mismatch for invite",
        )
    if record.get("redeemed"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Invite already redeemed",
        )
    if record.get("from_user_id") == new_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Self-redemption not allowed",
        )

    trigger = record.get("trigger", "")
    inviter_id = record.get("from_user_id", "")

    # Mark redeemed atomically. We rewrite the record preserving TTL approx
    # (Redis SET without EX would clear TTL — use KEEPTTL).
    record["redeemed"] = True
    record["redeemed_by"] = new_user_id
    record["redeemed_at"] = _now()

    try:
        await r.set(_k_invite(invite_token), json.dumps(record), keepttl=True)
    except TypeError:
        # Fallback for older redis-py versions w/o keepttl kwarg
        ttl = await r.ttl(_k_invite(invite_token))
        if ttl and ttl > 0:
            await r.set(_k_invite(invite_token), json.dumps(record), ex=ttl)
        else:
            await r.set(_k_invite(invite_token), json.dumps(record))

    # Bump conversion counter (lifetime)
    await r.incr(_k_converted(brand_id, trigger))

    # Bump daily K-factor "redeemed" counters
    day = _today_str()
    pipe2 = r.pipeline()
    pipe2.incr(_k_viral_redeemed_day(brand_id, day))
    pipe2.expire(_k_viral_redeemed_day(brand_id, day), DAILY_COUNTER_TTL_SECONDS)
    pipe2.incr(_k_viral_redeemed_day_mech(brand_id, day, trigger))
    pipe2.expire(
        _k_viral_redeemed_day_mech(brand_id, day, trigger),
        DAILY_COUNTER_TTL_SECONDS,
    )
    await pipe2.execute()

    issued_arm = record.get("extra", {}).get("ab_arm")
    if issued_arm in ("personalized", "template"):
        await r.incr(_k_ab_metric(brand_id, issued_arm, "redeemed"))

    # Resolve reward template
    template = REWARD_TEMPLATES.get(trigger, {})
    inviter_ops = list(template.get("inviter", []))
    invitee_ops = list(template.get("invitee", []))

    # Special handling: ladder climb requires N friends before promotion
    promoted = False
    if trigger == TRIGGER_LADDER_CLIMB:
        target_tier = record.get("extra", {}).get("target_tier", "next")
        # Track which invitee redeemed which ladder-tier slot
        ladder_key = _k_ladder_progress(brand_id, inviter_id, target_tier)
        await r.sadd(ladder_key, new_user_id)
        await r.expire(ladder_key, INVITE_TTL_SECONDS)
        count = await r.scard(ladder_key)
        if count >= LADDER_FRIENDS_NEEDED:
            promoted = True
            # Keep the tier_promote op only when the threshold is hit
        else:
            # Below threshold: drop the promote op, leave only tracking
            inviter_ops = [op for op in inviter_ops if op.get("op") != "tier_promote"]

    # Special handling: streak rescue charges energy from invitee
    if trigger == TRIGGER_STREAK_RESCUE:
        invitee_ops = [
            {"op": "energy_charge", "amount": STREAK_RESCUE_COST},
            *invitee_ops,
        ]

    # Persist pending rewards for both sides
    await _push_pending(
        r,
        user_id=inviter_id,
        brand_id=brand_id,
        ops=inviter_ops,
        source=trigger,
        invite_token=invite_token,
    )
    await _push_pending(
        r,
        user_id=new_user_id,
        brand_id=brand_id,
        ops=invitee_ops,
        source=trigger,
        invite_token=invite_token,
    )

    result: dict[str, Any] = {
        "trigger": trigger,
        "invite_token": invite_token,
        "inviter_id": inviter_id,
        "invitee_id": new_user_id,
        "brand_id": brand_id,
        "rewards_inviter": inviter_ops,
        "rewards_invitee": invitee_ops,
    }
    if trigger == TRIGGER_LADDER_CLIMB:
        result["ladder_promoted"] = promoted
        result["friends_invited_so_far"] = await r.scard(
            _k_ladder_progress(
                brand_id,
                inviter_id,
                record.get("extra", {}).get("target_tier", "next"),
            )
        )

    # ── P0 viral compounding: auto-emit fresh invite to the redeemer ──
    auto_emit_info: dict[str, Any] = {"emitted": False}
    if trigger in AUTO_EMIT_TRIGGERS:
        parent_depth = int(record.get("depth") or 0)
        child_depth = parent_depth + 1
        if child_depth > MAX_INHERITANCE_DEPTH:
            auto_emit_info = {
                "emitted": False,
                "reason": "depth_cap_reached",
                "depth_cap": MAX_INHERITANCE_DEPTH,
            }
        else:
            root_inviter = record.get("inherited_from") or inviter_id
            new_token = _new_token()
            await _store_invite(
                r,
                token=new_token,
                trigger=trigger,
                from_user_id=new_user_id,
                brand_id=brand_id,
                extra={
                    "auto_emitted": True,
                    "parent_invite_token": invite_token,
                    **(record.get("extra") or {}),
                },
                inherited_from=root_inviter,
                depth=child_depth,
            )
            await r.set(_k_invite_tree_depth(new_user_id), child_depth)
            share_url = _build_share_url(None, brand_id, new_token)
            auto_emit_info = {
                "emitted": True,
                "invite_token": new_token,
                "share_url": share_url,
                "depth": child_depth,
                "inherited_from": root_inviter,
            }
    result["auto_emitted_invite"] = auto_emit_info

    await _refresh_explosion_warning(r, brand_id)
    return result


async def _compute_k(
    r: aioredis.Redis,
    brand_id: str,
    window_days: int = DEFAULT_K_WINDOW_DAYS,
    trigger: str | None = None,
) -> tuple[float, int, int]:
    """Trailing N-day (K, issued, redeemed)."""
    today = date.today()
    issued_keys: list[str] = []
    redeemed_keys: list[str] = []
    for i in range(window_days):
        day = (today - timedelta(days=i)).isoformat()
        if trigger:
            issued_keys.append(_k_viral_issued_day_mech(brand_id, day, trigger))
            redeemed_keys.append(_k_viral_redeemed_day_mech(brand_id, day, trigger))
        else:
            issued_keys.append(_k_viral_issued_day(brand_id, day))
            redeemed_keys.append(_k_viral_redeemed_day(brand_id, day))
    pipe = r.pipeline()
    for k in issued_keys + redeemed_keys:
        pipe.get(k)
    vals = await pipe.execute()
    issued = sum(int(v or 0) for v in vals[: len(issued_keys)])
    redeemed = sum(int(v or 0) for v in vals[len(issued_keys) :])
    k_val = (redeemed / issued) if issued > 0 else 0.0
    return k_val, issued, redeemed


async def _refresh_explosion_warning(
    r: aioredis.Redis, brand_id: str, window_days: int = DEFAULT_K_WINDOW_DAYS
) -> float:
    k_val, _i, _r = await _compute_k(r, brand_id, window_days)
    if k_val > K_EXPLOSION_THRESHOLD:
        await r.set(
            _k_explosion_warning(brand_id), "1", ex=DAILY_COUNTER_TTL_SECONDS
        )
    else:
        await r.delete(_k_explosion_warning(brand_id))
    return k_val


# ── 1. ShareToWin ──────────────────────────────────────────────────────────


@router.post("/share-to-win")
async def share_to_win(
    body: ShareToWinRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    token, _record = await _init_trigger(
        r,
        trigger=TRIGGER_SHARE_TO_WIN,
        user_id=body.user_id,
        brand_id=body.brand_id,
        extra={"score": body.score, "game_slug": body.game_slug},
    )
    share_url = _build_share_url(body.base_url, body.brand_id, token)
    brand_color = await _get_brand_color(r, body.brand_id)
    svg = _build_card_svg(
        brand_color=brand_color,
        headline=f"{body.game_slug}",
        big_value=f"{body.score}",
        subline="Beat my score!",
        cta="Play Now",
    )
    return {
        "invite_token": token,
        "share_url": share_url,
        "share_text": f"I scored {body.score} on {body.game_slug}. Beat it!",
        "card_data_uri": _svg_to_data_uri(svg),
    }


# ── 2. EnergyInvite ────────────────────────────────────────────────────────


@router.post("/energy-invite")
async def energy_invite(
    body: EnergyInviteRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    token, _record = await _init_trigger(
        r,
        trigger=TRIGGER_ENERGY_INVITE,
        user_id=body.user_id,
        brand_id=body.brand_id,
        extra={"energy_short_by": body.energy_short_by},
    )
    share_url = _build_share_url(body.base_url, body.brand_id, token)
    inviter_reward = next(
        (
            op.get("amount", 0)
            for op in REWARD_TEMPLATES[TRIGGER_ENERGY_INVITE]["inviter"]
            if op.get("op") == "energy_grant"
        ),
        10,
    )
    return {
        "invite_token": token,
        "share_url": share_url,
        "share_text": "I'm out of energy — help me back in and we both get a boost!",
        "reward_when_friend_joins": {
            "energy": inviter_reward,
            "for_inviter": True,
            "for_invitee": True,
        },
    }


# ── 3. FriendChallenge ─────────────────────────────────────────────────────


@router.post("/friend-challenge")
async def friend_challenge(
    body: FriendChallengeRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    if not body.badge_id and body.score is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide badge_id or score",
        )
    challenge_target: dict[str, Any] = {}
    if body.badge_id:
        challenge_target["badge_id"] = body.badge_id
    if body.score is not None:
        challenge_target["score_to_beat"] = body.score
    if body.game_slug:
        challenge_target["game_slug"] = body.game_slug

    token, _record = await _init_trigger(
        r,
        trigger=TRIGGER_FRIEND_CHALLENGE,
        user_id=body.user_id,
        brand_id=body.brand_id,
        extra=challenge_target,
    )
    share_url = _build_share_url(body.base_url, body.brand_id, token)
    return {
        "invite_token": token,
        "share_url": share_url,
        "challenge_target": challenge_target,
        "share_text": "Think you can beat me? Challenge accepted?",
    }


# ── 4. LadderClimb ─────────────────────────────────────────────────────────


@router.post("/ladder-climb")
async def ladder_climb(
    body: LadderClimbRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    token, _record = await _init_trigger(
        r,
        trigger=TRIGGER_LADDER_CLIMB,
        user_id=body.user_id,
        brand_id=body.brand_id,
        extra={"target_tier": body.target_tier},
    )
    share_url = _build_share_url(body.base_url, body.brand_id, token)
    invited_so_far = await r.scard(
        _k_ladder_progress(body.brand_id, body.user_id, body.target_tier)
    )
    return {
        "invite_token": token,
        "share_url": share_url,
        "friends_needed": LADDER_FRIENDS_NEEDED,
        "friends_invited_so_far": int(invited_so_far or 0),
        "target_tier": body.target_tier,
        "share_text": f"Help me reach {body.target_tier} — invite {LADDER_FRIENDS_NEEDED} friends and we all win.",
    }


# ── 5. StreakRescue ────────────────────────────────────────────────────────


@router.post("/streak-rescue")
async def streak_rescue(
    body: StreakRescueRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    token, _record = await _init_trigger(
        r,
        trigger=TRIGGER_STREAK_RESCUE,
        user_id=body.user_id,
        brand_id=body.brand_id,
        extra={"current_streak": body.current_streak},
    )
    share_url = _build_share_url(body.base_url, body.brand_id, token)
    return {
        "invite_token": token,
        "share_url": share_url,
        "current_streak": body.current_streak,
        "rescue_cost": STREAK_RESCUE_COST,
        "share_text": f"My {body.current_streak}-day streak is about to break — rescue me with {STREAK_RESCUE_COST} energy!",
    }


# ── 6. AutoShare ───────────────────────────────────────────────────────────


@router.post("/auto-share")
async def auto_share(
    body: AutoShareRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    allowed_events = {"level_up", "badge_earned", "highscore"}
    if body.event not in allowed_events:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"event must be one of {sorted(allowed_events)}",
        )
    token, _record = await _init_trigger(
        r,
        trigger=TRIGGER_AUTO_SHARE,
        user_id=body.user_id,
        brand_id=body.brand_id,
        extra={"event": body.event, "context": body.context},
    )
    share_url = _build_share_url(body.base_url, body.brand_id, token)
    brand_color = await _get_brand_color(r, body.brand_id)

    # Pick reasonable headline / big_value from context
    if body.event == "level_up":
        headline = "LEVEL UP"
        big_value = str(body.context.get("level", "★"))
        subline = "Join me in the game"
        social_text = f"Just hit level {big_value}! Join me."
    elif body.event == "badge_earned":
        headline = "NEW BADGE"
        big_value = str(body.context.get("badge_name", "★"))
        subline = "Can you earn it too?"
        social_text = f"Just earned the {big_value} badge!"
    else:  # highscore
        headline = "HIGH SCORE"
        big_value = str(body.context.get("score", "?"))
        subline = "Beat my score!"
        social_text = f"New high score: {big_value}. Beat it!"

    svg = _build_card_svg(
        brand_color=brand_color,
        headline=headline,
        big_value=big_value,
        subline=subline,
        cta="Play Now",
    )
    return {
        "invite_token": token,
        "share_url": share_url,
        "card_data_uri": _svg_to_data_uri(svg),
        "social_text": social_text,
        "event": body.event,
    }


# ── Generic trigger init/redeem (per spec: /trigger/{name}/init etc.) ──────


@router.post("/trigger/{trigger_name}/init")
async def trigger_init(
    trigger_name: str,
    body: dict[str, Any],
    r: aioredis.Redis = Depends(get_redis),
):
    """Generic init endpoint — accepts any trigger by name."""
    if trigger_name not in ALL_TRIGGERS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown trigger '{trigger_name}'",
        )
    user_id = body.get("user_id")
    brand_id = body.get("brand_id")
    context = body.get("context") or {}
    if not user_id or not brand_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="user_id and brand_id are required",
        )

    token, _record = await _init_trigger(
        r,
        trigger=trigger_name,
        user_id=user_id,
        brand_id=brand_id,
        extra=context,
    )
    share_url = _build_share_url(context.get("base_url"), brand_id, token)
    response: dict[str, Any] = {
        "invite_token": token,
        "share_url": share_url,
    }
    # Add an image for visual triggers
    if trigger_name in (TRIGGER_SHARE_TO_WIN, TRIGGER_AUTO_SHARE):
        brand_color = await _get_brand_color(r, brand_id)
        svg = _build_card_svg(
            brand_color=brand_color,
            headline=trigger_name.replace("_", " ").upper(),
            big_value=str(context.get("score") or context.get("badge_name") or "★"),
            subline="Beat my score!",
            cta="Play Now",
        )
        response["card_data_uri"] = _svg_to_data_uri(svg)
    return response


@router.post("/trigger/{trigger_name}/redeem")
async def trigger_redeem(
    trigger_name: str,
    body: dict[str, Any],
    r: aioredis.Redis = Depends(get_redis),
):
    """Generic redeem endpoint — validates trigger name matches stored record."""
    if trigger_name not in ALL_TRIGGERS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown trigger '{trigger_name}'",
        )
    invite_token = body.get("invite_token", "")
    new_user_id = body.get("new_user_id", "")
    brand_id = body.get("brand_id", "")

    # Cross-check trigger name matches the stored record before redemption
    raw = await r.get(_k_invite(invite_token))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invite token not found or expired",
        )
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Corrupt invite record",
        )
    if record.get("trigger") != trigger_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Trigger mismatch for this invite token",
        )

    return await _redeem_token(
        r,
        invite_token=invite_token,
        new_user_id=new_user_id,
        brand_id=brand_id,
    )


# ── Unified redeem ─────────────────────────────────────────────────────────


@router.post("/redeem")
async def redeem(
    body: RedeemRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    """Trigger-agnostic redeem. Looks up the invite, applies rewards to both."""
    return await _redeem_token(
        r,
        invite_token=body.invite_token,
        new_user_id=body.new_user_id,
        brand_id=body.brand_id,
    )


# ── Viral stats ────────────────────────────────────────────────────────────


@router.get("/{brand_id}/viral-stats")
async def viral_stats(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Per-brand viral coefficient per trigger."""
    pipe = r.pipeline()
    for trig in ALL_TRIGGERS:
        pipe.get(_k_invited(brand_id, trig))
        pipe.get(_k_converted(brand_id, trig))
    results = await pipe.execute()

    triggers: dict[str, dict[str, Any]] = {}
    overall_invited = 0
    overall_converted = 0
    for idx, trig in enumerate(ALL_TRIGGERS):
        invited = int(results[idx * 2] or 0)
        converted = int(results[idx * 2 + 1] or 0)
        overall_invited += invited
        overall_converted += converted
        coeff = (converted / invited) if invited > 0 else 0.0
        triggers[trig] = {
            "invited": invited,
            "converted": converted,
            "coefficient": round(coeff, 4),
        }

    overall_coeff = (
        (overall_converted / overall_invited) if overall_invited > 0 else 0.0
    )
    return {
        "brand_id": brand_id,
        "triggers": triggers,
        "overall": {
            "invited": overall_invited,
            "converted": overall_converted,
            "coefficient": round(overall_coeff, 4),
        },
    }


# ── K-factor (trailing-window viral coefficient) ───────────────────────────


@router.get("/k-factor/{brand_id}")
async def k_factor(
    brand_id: str,
    window_days: int = DEFAULT_K_WINDOW_DAYS,
    r: aioredis.Redis = Depends(get_redis),
):
    """Trailing K-factor for a brand + per-mechanic breakdown."""
    if window_days < 1 or window_days > 35:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="window_days must be in [1,35]",
        )
    overall_k, overall_issued, overall_redeemed = await _compute_k(
        r, brand_id, window_days
    )
    breakdown: dict[str, dict[str, Any]] = {}
    for trig in ALL_TRIGGERS:
        k_t, issued_t, redeemed_t = await _compute_k(
            r, brand_id, window_days, trigger=trig
        )
        breakdown[trig] = {
            "issued": issued_t,
            "redeemed": redeemed_t,
            "k_factor": round(k_t, 4),
        }
    await _refresh_explosion_warning(r, brand_id, window_days)
    explosion = bool(await r.get(_k_explosion_warning(brand_id)))
    return {
        "brand_id": brand_id,
        "window_days": window_days,
        "k_factor": round(overall_k, 4),
        "invites_issued": overall_issued,
        "invites_redeemed": overall_redeemed,
        "explosion_warning": explosion,
        "explosion_threshold": K_EXPLOSION_THRESHOLD,
        "target_band": {"min": 0.3, "max": 1.2},
        "per_mechanic": breakdown,
    }


# ── Personalized invite copy (LLM, quota-guarded) ──────────────────────────


_TEMPLATE_INVITES: dict[str, str] = {
    "default": "Hey — I'm playing on KiX. Join me and grab a bonus.",
    "competitive": "Think you can beat me? Step up. Free entry on me.",
    "casual": "Found a fun little game — wanna try? Takes 2 min.",
    "social": "Doing this with friends. Join — first round on me.",
    "deal_hunter": "Free reward when you sign up via this link. No catch.",
    "foodie": "F&B perk inside — only takes a tap. Grab it before it expires.",
}


def _fallback_invite(persona_tag: str | None, inviter: str) -> str:
    tag = (persona_tag or "default").lower()
    return _TEMPLATE_INVITES.get(tag, _TEMPLATE_INVITES["default"])


async def _llm_quota_paused() -> bool:
    """Quota-pause check per CLAUDE.md feedback_llm_quota_guard.

    Returns False when the monitor module isn't importable so production
    is never blocked by missing optional infra. Patchable from tests.
    """
    try:
        import importlib.util
        import sys
        from pathlib import Path

        if "scripts.llm_quota_monitor" in sys.modules:
            mod = sys.modules["scripts.llm_quota_monitor"]
        else:
            root = Path(__file__).resolve().parents[2]
            path = root / "scripts" / "llm_quota_monitor.py"
            if not path.exists():
                return False
            spec = importlib.util.spec_from_file_location(
                "scripts.llm_quota_monitor", str(path)
            )
            if spec is None or spec.loader is None:
                return False
            mod = importlib.util.module_from_spec(spec)
            sys.modules["scripts.llm_quota_monitor"] = mod
            spec.loader.exec_module(mod)
        check = getattr(mod, "is_paused", None)
        if check is None:
            return False
        return bool(await check())
    except Exception:  # noqa: BLE001
        return False


async def _llm_personalize(
    *, inviter: str, persona_tag: str, brand_id: str
) -> str | None:
    """Best-effort LLM personalization. None on any failure → template."""
    if await _llm_quota_paused():
        logger.info("LLM quota paused — fallback to template invite")
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    system = (
        "You write 1-line viral invite messages (max 22 words). "
        "Friendly, no emojis, no hashtags. Personalize to the invitee "
        "persona tag. Output the message only — nothing else."
    )
    user = (
        f"Inviter user_id: {inviter}\n"
        f"Brand: {brand_id}\n"
        f"Invitee persona tag: {persona_tag}\n"
        "Write the invite message now."
    )
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            resp = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-3-5-haiku-latest",
                    "max_tokens": 80,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
            )
        if resp.status_code != 200:
            return None
        data = resp.json()
        for ch in data.get("content") or []:
            if ch.get("type") == "text":
                text = (ch.get("text") or "").strip()
                if text:
                    return text.splitlines()[0].strip()[:280]
        return None
    except Exception:  # noqa: BLE001
        return None


@router.get("/share-to-win/personalized-message")
async def personalized_invite_message(
    inviter: str,
    invitee_persona: str = "default",
    brand_id: str = "default",
    r: aioredis.Redis = Depends(get_redis),
):
    """Personalized invite copy. LLM with template fallback. Sticky A/B."""
    if not inviter:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="inviter is required",
        )
    arm_key = _k_ab_assignment(brand_id, inviter)
    existing_arm = await r.get(arm_key)
    if existing_arm in ("personalized", "template"):
        arm = existing_arm
    else:
        arm = "personalized" if (hash(inviter) & 1) == 0 else "template"
        await r.set(arm_key, arm, ex=DAILY_COUNTER_TTL_SECONDS)
    message: str | None = None
    source = "template"
    if arm == "personalized":
        message = await _llm_personalize(
            inviter=inviter, persona_tag=invitee_persona, brand_id=brand_id
        )
        if message is not None:
            source = "llm"
    if message is None:
        message = _fallback_invite(invitee_persona, inviter)
    await r.incr(_k_ab_metric(brand_id, arm, "issued"))
    return {
        "inviter": inviter,
        "brand_id": brand_id,
        "invitee_persona": invitee_persona,
        "ab_arm": arm,
        "source": source,
        "message": message,
    }


@router.get("/share-to-win/ab-stats/{brand_id}")
async def invite_ab_stats(
    brand_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Personalized vs template invite A/B stats."""
    pipe = r.pipeline()
    for arm in ("personalized", "template"):
        pipe.get(_k_ab_metric(brand_id, arm, "issued"))
        pipe.get(_k_ab_metric(brand_id, arm, "redeemed"))
    vals = await pipe.execute()
    arms: dict[str, dict[str, Any]] = {}
    for idx, arm in enumerate(("personalized", "template")):
        issued = int(vals[idx * 2] or 0)
        redeemed = int(vals[idx * 2 + 1] or 0)
        arms[arm] = {
            "issued": issued,
            "redeemed": redeemed,
            "rate": round((redeemed / issued) if issued else 0.0, 4),
        }
    return {
        "brand_id": brand_id,
        "arms": arms,
        "uplift_personalized_vs_template": round(
            arms["personalized"]["rate"] - arms["template"]["rate"], 4
        ),
    }


# ── Pending rewards inspector (helpful for the future wiring) ──────────────


@router.get("/{brand_id}/users/{user_id}/pending-rewards")
async def list_pending_rewards(
    brand_id: str,
    user_id: str,
    r: aioredis.Redis = Depends(get_redis),
):
    """Return the queued (un-applied) reward ops for this user/brand."""
    raw_items = await r.lrange(_k_pending(user_id, brand_id), 0, -1)
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        try:
            items.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return {
        "brand_id": brand_id,
        "user_id": user_id,
        "count": len(items),
        "pending": items,
    }
