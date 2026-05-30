"""Adjustable game speed / difficulty service — Wave F obvious-win #10.

Inspired by Playable. A single per-campaign ``difficulty`` integer in
{1..5} (default 3) drives a deterministic win-rate and per-template
parameter set. Marketers want easier win-rates to boost engagement;
KPIs want harder ones for grand prizes.

Spec § per-template interpretation:

    | template       | difficulty 1 | difficulty 5 |
    |----------------|--------------|--------------|
    | spin_wheel     | win 60%      | win 8%       |
    | scratch_card   | win 50%      | win 4%       |
    | memory_match   | 4×4 grid     | 6×6 grid     |
    | reaction_time  | window 800ms | window 250ms |
    | trivia         | 3 options    | 5 + timer    |

Redis schema (additive — does not touch existing campaign hashes):
    wavef:game_speed:{campaign_id}   HASH {difficulty}

NEW file — no existing campaigns module touched.
"""

from __future__ import annotations

from typing import Any

import redis.asyncio as aioredis


DIFFICULTY_MIN = 1
DIFFICULTY_MAX = 5
DIFFICULTY_DEFAULT = 3


# Spec §32 — single source of truth for win-rate.
DIFFICULTY_WIN_RATE: dict[int, float] = {
    1: 0.60,
    2: 0.40,
    3: 0.25,
    4: 0.15,
    5: 0.08,
}


# Per-template parameter sets. Each entry returns the full template
# config dict for that difficulty. Adding new templates is additive.
_TEMPLATE_PARAMS: dict[str, dict[int, dict[str, Any]]] = {
    "spin_wheel": {
        1: {"win_rate": 0.60},
        2: {"win_rate": 0.40},
        3: {"win_rate": 0.25},
        4: {"win_rate": 0.15},
        5: {"win_rate": 0.08},
    },
    "scratch_card": {
        1: {"win_rate": 0.50},
        2: {"win_rate": 0.35},
        3: {"win_rate": 0.22},
        4: {"win_rate": 0.12},
        5: {"win_rate": 0.04},
    },
    "memory_match": {
        1: {"grid": [4, 4]},
        2: {"grid": [4, 5]},
        3: {"grid": [5, 5]},
        4: {"grid": [5, 6]},
        5: {"grid": [6, 6]},
    },
    "reaction_time": {
        1: {"window_ms": 800},
        2: {"window_ms": 600},
        3: {"window_ms": 450},
        4: {"window_ms": 350},
        5: {"window_ms": 250},
    },
    "trivia": {
        1: {"options": 3, "timer_s": None},
        2: {"options": 3, "timer_s": 30},
        3: {"options": 4, "timer_s": 20},
        4: {"options": 4, "timer_s": 15},
        5: {"options": 5, "timer_s": 10},
    },
}


# ── Keys ─────────────────────────────────────────────────────────────────


def _k(campaign_id: str) -> str:
    return f"wavef:game_speed:{campaign_id}"


# ── Helpers ──────────────────────────────────────────────────────────────


def clamp_difficulty(d: Any) -> int:
    """Coerce any input to a valid difficulty (1..5). Defaults to 3."""
    try:
        di = int(d)
    except (TypeError, ValueError):
        return DIFFICULTY_DEFAULT
    if di < DIFFICULTY_MIN:
        return DIFFICULTY_MIN
    if di > DIFFICULTY_MAX:
        return DIFFICULTY_MAX
    return di


def win_probability(template_id: str, difficulty: Any) -> float:
    """Spec skeleton helper. Returns a probability in [0, 1].

    Falls back to the canonical DIFFICULTY_WIN_RATE table if a template
    doesn't specify its own ``win_rate``.
    """
    d = clamp_difficulty(difficulty)
    tpl = _TEMPLATE_PARAMS.get(template_id, {})
    entry = tpl.get(d, {})
    if "win_rate" in entry:
        return float(entry["win_rate"])
    return DIFFICULTY_WIN_RATE.get(d, 0.25)


def template_params(template_id: str, difficulty: Any) -> dict[str, Any]:
    """Return the per-template parameter dict for a given difficulty.

    Returns {} for unknown template ids — caller decides whether to
    fail open or hard.
    """
    d = clamp_difficulty(difficulty)
    return dict(_TEMPLATE_PARAMS.get(template_id, {}).get(d, {}))


def supported_templates() -> list[str]:
    return sorted(_TEMPLATE_PARAMS.keys())


# ── Persistence ──────────────────────────────────────────────────────────


async def set_difficulty(
    r: aioredis.Redis, campaign_id: str, difficulty: Any
) -> int:
    """Persist a campaign's difficulty. Returns the clamped int stored."""
    d = clamp_difficulty(difficulty)
    await r.hset(_k(campaign_id), mapping={"difficulty": str(d)})
    return d


async def get_difficulty(r: aioredis.Redis, campaign_id: str) -> int:
    """Read difficulty for a campaign, defaulting to 3 if unset."""
    raw = await r.hget(_k(campaign_id), "difficulty")
    if raw is None:
        return DIFFICULTY_DEFAULT
    return clamp_difficulty(raw)


async def resolve_session(
    r: aioredis.Redis,
    campaign_id: str,
    template_id: str,
) -> dict[str, Any]:
    """One-shot helper for a game template at session-init time per spec §15.

    Returns ``{difficulty, template, params, win_probability}``.
    """
    d = await get_difficulty(r, campaign_id)
    return {
        "campaign_id": campaign_id,
        "template": template_id,
        "difficulty": d,
        "params": template_params(template_id, d),
        "win_probability": win_probability(template_id, d),
    }
