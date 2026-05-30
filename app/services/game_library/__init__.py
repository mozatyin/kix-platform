"""Game library registry — 15 game templates (5 legacy + 10 new).

Public API:
* ``GAME_LIBRARY`` — dict[str, GameTemplate]
* ``get_template(type_name)`` — fetch one
* ``list_templates()`` — list of metadata dicts (for UI)
* ``recommend_for_brand(brand_id, audience)`` — top 3 type_names

All templates conform to ``GameTemplate`` in ``base``.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .base import GameTemplate
from .bubble_pop import TEMPLATE as _BUBBLE_POP
from .catch_falling import TEMPLATE as _CATCH_FALLING
from .lucky_dice import TEMPLATE as _LUCKY_DICE
from .match import TEMPLATE as _MATCH
from .memory_match import TEMPLATE as _MEMORY_MATCH
from .quiz import TEMPLATE as _QUIZ
from .scratch import TEMPLATE as _SCRATCH
from .scratch_galaxy import TEMPLATE as _SCRATCH_GALAXY
from .shake import TEMPLATE as _SHAKE
from .slot_machine import TEMPLATE as _SLOT_MACHINE
from .spin import TEMPLATE as _SPIN
from .stack_tower import TEMPLATE as _STACK_TOWER
from .target_shoot import TEMPLATE as _TARGET_SHOOT
from .wheel_of_fortune import TEMPLATE as _WHEEL_OF_FORTUNE
from .whack_a_mole import TEMPLATE as _WHACK_A_MOLE


# Legacy (5) + new (10) = 15 game types
GAME_LIBRARY: dict[str, GameTemplate] = {
    # legacy
    "spin": _SPIN,
    "scratch": _SCRATCH,
    "match": _MATCH,
    "quiz": _QUIZ,
    "shake": _SHAKE,
    # new
    "slot_machine": _SLOT_MACHINE,
    "wheel_of_fortune": _WHEEL_OF_FORTUNE,
    "memory_match": _MEMORY_MATCH,
    "whack_a_mole": _WHACK_A_MOLE,
    "catch_falling": _CATCH_FALLING,
    "bubble_pop": _BUBBLE_POP,
    "target_shoot": _TARGET_SHOOT,
    "stack_tower": _STACK_TOWER,
    "lucky_dice": _LUCKY_DICE,
    "scratch_galaxy": _SCRATCH_GALAXY,
}


def get_template(type_name: str) -> GameTemplate:
    """Look up a template by name; raise KeyError if absent."""
    if type_name not in GAME_LIBRARY:
        raise KeyError(f"Unknown game type: {type_name!r}. "
                       f"Available: {sorted(GAME_LIBRARY)}")
    return GAME_LIBRARY[type_name]


def list_templates() -> list[dict]:
    """Return JSON-safe metadata for every template (UI dropdown)."""
    return [t.metadata() for t in GAME_LIBRARY.values()]


# --- recommendation ---------------------------------------------------------

# Audience → industry hint
_AUDIENCE_TO_INDUSTRY: dict[str, str] = {
    "foodies": "fnb",
    "diners": "fnb",
    "beauty_fans": "beauty",
    "shoppers": "retail",
    "students": "education",
    "fitness": "fitness",
    "athletes": "fitness",
}


def recommend_for_brand(brand_id: str, audience: str | None = None) -> list[str]:
    """Top-3 type_name recommendations.

    Strategy:
    * Map audience → industry hint
    * Score each template:
        +2 if industry in recommended_industries
        +1 if completion_seconds <= 20 (quick play)
        + deterministic tiebreaker from brand_id hash
    * Return top 3 type_names.
    """
    industry = _AUDIENCE_TO_INDUSTRY.get((audience or "").lower(), "")
    h = int(hashlib.sha256((brand_id or "").encode()).hexdigest()[:8], 16)

    def score(t: GameTemplate, i: int) -> tuple[int, int]:
        s = 0
        if industry and industry in t.recommended_industries:
            s += 2
        if t.completion_seconds <= 20:
            s += 1
        # deterministic tiebreaker
        return (s, (h + i * 17) % 991)

    ranked = sorted(
        enumerate(GAME_LIBRARY.values()),
        key=lambda pair: score(pair[1], pair[0]),
        reverse=True,
    )
    return [t.type_name for _, t in ranked[:3]]


__all__ = [
    "GAME_LIBRARY",
    "GameTemplate",
    "get_template",
    "list_templates",
    "recommend_for_brand",
]
