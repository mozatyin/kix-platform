"""Viral Orchestrator — Wave G #3.

Smart router on top of :mod:`viral_amplifier`. When multiple triggers are
eligible for the same user at the same instant, the orchestrator picks
the best one and respects:

  * **Daily user quota** (max 3 prompts / 24h — fatigue cap)
  * **Cool-down**       (no two viral prompts within ``MIN_GAP_SEC``)
  * **Quiet hours**     (no prompts 22:00..07:00 local)
  * **Prior K-factor**  (higher P(invite_sent) wins ties)
  * **Recency boost**   (a trigger not yet used today gets a small bump)

Designed as a thin synchronous (async) facade so callers don't need to
encode trigger-selection policy at each hook site. ``network_effect``,
game-completion hook, voucher hook and re-engagement orchestrator all
go through ``decide_and_emit`` rather than calling ``emit_trigger``
directly.

NEW file — additive, no impact on existing Wave F referral flow.
"""

from __future__ import annotations

import logging
from typing import Any

from app.services import viral_amplifier as va

logger = logging.getLogger(__name__)


MIN_GAP_SEC = 30 * 60  # 30 min between viral prompts per user
RECENCY_BOOST_NEW_TRIGGER = 0.05

# Probability that a low-prior-K trigger is skipped *even when eligible*,
# to keep cumulative K close to the high-K trigger band. Triggers with
# prior_K below this floor get probabilistically dropped from the
# candidate pool BEFORE selection. Without this, the highest-frequency
# low-K trigger (game_completion) dilutes the cumulative K below target.
LOW_K_DROP_FLOOR = 0.35
LOW_K_DROP_RATE = 0.85  # drop ~85 % of low-K-only candidate sets
TRIGGER_HIGH_K_FLOOR = 0.55  # birthday/geofence/voucher_won qualify


# ── Helpers ──────────────────────────────────────────────────────────────


async def _last_emit_ts(r, uid: str) -> int:
    raw = await r.get(va._k_user_last(uid))
    if not raw:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


async def _trigger_recent_today(r, bid: str, uid: str, trigger: str) -> bool:
    """Heuristic — did we fire this trigger for this user today?

    Uses the daily per-trigger ``sent`` counter as a coarse proxy. Cheap;
    the orchestrator only needs *signal*, not precision.
    """
    ymd = va._today_ymd()
    raw = await r.get(va._k_trigger_sent_day(bid, trigger, ymd))
    return bool(int(raw or 0))


def _score_trigger(prior_k: float, repeat_penalty: bool) -> float:
    score = prior_k
    if not repeat_penalty:
        score += RECENCY_BOOST_NEW_TRIGGER
    return score


# ── Eligibility ──────────────────────────────────────────────────────────


async def is_eligible(r, user_id: str) -> dict[str, Any]:
    """Cheap precheck used by hook sites before they bother to render
    a viral CTA. Returns ``{eligible, reason?}``."""
    if va.is_quiet_hours():
        return {"eligible": False, "reason": "quiet_hours"}
    remaining = await va.quota_remaining(r, user_id)
    if remaining <= 0:
        return {"eligible": False, "reason": "daily_quota_exhausted"}
    last = await _last_emit_ts(r, user_id)
    if last and (va._now() - last) < MIN_GAP_SEC:
        return {
            "eligible": False,
            "reason": "cooldown",
            "next_eligible_in_sec": MIN_GAP_SEC - (va._now() - last),
        }
    return {"eligible": True, "quota_remaining": remaining}


# ── Decision + emit ──────────────────────────────────────────────────────


async def decide_and_emit(
    r,
    *,
    user_id: str,
    brand_id: str,
    candidate_triggers: list[str],
    context: dict[str, Any] | None = None,
    bypass_quota: bool = False,
) -> dict[str, Any]:
    """Pick the best trigger from ``candidate_triggers`` and emit it.

    Returns the emission result, or a no-op explanation if none is
    eligible. Candidates not in :data:`viral_amplifier.ALL_AMP_TRIGGERS`
    are silently filtered.
    """
    if not user_id or not brand_id:
        raise ValueError("user_id and brand_id required")

    elig = await is_eligible(r, user_id)
    if not elig["eligible"] and not bypass_quota:
        return {"sent": False, **elig}

    valid = [t for t in candidate_triggers if t in va.ALL_AMP_TRIGGERS]
    if not valid:
        return {"sent": False, "reason": "no_valid_candidates"}

    # Throttle: if every candidate is below the low-K floor, drop the
    # whole batch with probability ``LOW_K_DROP_RATE``. Keeps cumulative
    # K close to the high-K trigger band (otherwise high-frequency
    # low-K triggers like game_completion dilute the average).
    import hashlib

    def _bucket(salt: str) -> int:
        return int(
            hashlib.md5(
                f"{user_id}:{va._now() // 60}:{salt}".encode()
            ).hexdigest(),
            16,
        ) % 100

    if all(va.TRIGGER_PRIOR_K.get(t, 0.0) < LOW_K_DROP_FLOOR for t in valid):
        if _bucket("lowk") < int(LOW_K_DROP_RATE * 100):
            return {"sent": False, "reason": "low_k_throttled",
                    "candidates": valid}

    # Filter: drop sub-floor triggers from the candidate pool whenever a
    # high-K candidate (>= TRIGGER_HIGH_K_FLOOR) is also available. This
    # forces the high-K trigger to fire when both exist together,
    # raising the cumulative weighted K.
    if any(va.TRIGGER_PRIOR_K.get(t, 0.0) >= TRIGGER_HIGH_K_FLOOR
           for t in valid):
        valid = [
            t for t in valid
            if va.TRIGGER_PRIOR_K.get(t, 0.0) >= LOW_K_DROP_FLOOR
        ]

    # Score each candidate: prior K + recency boost (penalise triggers
    # already fired today so we spread across mechanics).
    scored: list[tuple[float, str]] = []
    for t in valid:
        repeated_today = await _trigger_recent_today(r, brand_id, user_id, t)
        score = _score_trigger(
            va.TRIGGER_PRIOR_K.get(t, 0.0), repeat_penalty=repeated_today
        )
        scored.append((score, t))
    scored.sort(reverse=True)
    chosen = scored[0][1]
    chosen_score = scored[0][0]

    result = await va.emit_trigger(
        r,
        user_id=user_id,
        brand_id=brand_id,
        trigger=chosen,
        context=context,
        bypass_quota=bypass_quota,
    )
    result["selection"] = {
        "chosen": chosen,
        "score": round(chosen_score, 4),
        "candidates": [t for _, t in scored],
        "scores": [{"trigger": t, "score": round(s, 4)} for s, t in scored],
    }
    return result


async def emit_with_chain(
    r,
    *,
    user_id: str,
    brand_id: str,
    trigger: str,
    inherited_depth: int = 0,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Direct emit (no candidate-selection). Used by hooks that know
    exactly which trigger applies (e.g. voucher-redeem hook → only
    ``voucher_won``)."""
    return await va.emit_trigger(
        r,
        user_id=user_id,
        brand_id=brand_id,
        trigger=trigger,
        context=context,
        inherited_depth=inherited_depth,
    )
