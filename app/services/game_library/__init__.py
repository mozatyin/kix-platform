"""Game library registry — 50 game templates.

Composition:
    5 legacy + 10 Wave D9 + 35 Wave E2 = 50 total.

Public API:
* ``GAME_LIBRARY`` — dict[str, GameTemplate]
* ``get_template(type_name)`` — fetch one
* ``list_templates()`` — list of metadata dicts (for UI)
* ``recommend_for_brand(brand_id, audience)`` — top 3 type_names

All templates conform to ``GameTemplate`` in ``base``.

Aligned with CataBoom (200+ templates) and BRAME directional benchmarks.
Focus: F&B / retail / SMB-relevant gameplay.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .base import GameTemplate

# Legacy (5)
from .match import TEMPLATE as _MATCH
from .quiz import TEMPLATE as _QUIZ
from .scratch import TEMPLATE as _SCRATCH
from .shake import TEMPLATE as _SHAKE
from .spin import TEMPLATE as _SPIN

# Wave D9 (10)
from .bubble_pop import TEMPLATE as _BUBBLE_POP
from .catch_falling import TEMPLATE as _CATCH_FALLING
from .lucky_dice import TEMPLATE as _LUCKY_DICE
from .memory_match import TEMPLATE as _MEMORY_MATCH
from .scratch_galaxy import TEMPLATE as _SCRATCH_GALAXY
from .slot_machine import TEMPLATE as _SLOT_MACHINE
from .stack_tower import TEMPLATE as _STACK_TOWER
from .target_shoot import TEMPLATE as _TARGET_SHOOT
from .whack_a_mole import TEMPLATE as _WHACK_A_MOLE
from .wheel_of_fortune import TEMPLATE as _WHEEL_OF_FORTUNE

# Wave E2 — F&B (15)
from .bubble_tea_mixer import TEMPLATE as _BUBBLE_TEA_MIXER
from .burger_builder import TEMPLATE as _BURGER_BUILDER
from .cake_decorating import TEMPLATE as _CAKE_DECORATING
from .coffee_brewing import TEMPLATE as _COFFEE_BREWING
from .dessert_combo import TEMPLATE as _DESSERT_COMBO
from .dim_sum_match import TEMPLATE as _DIM_SUM_MATCH
from .food_delivery_dash import TEMPLATE as _FOOD_DELIVERY_DASH
from .ingredient_sort import TEMPLATE as _INGREDIENT_SORT
from .kopi_orders import TEMPLATE as _KOPI_ORDERS
from .menu_quiz import TEMPLATE as _MENU_QUIZ
from .pizza_topping import TEMPLATE as _PIZZA_TOPPING
from .queue_jumper import TEMPLATE as _QUEUE_JUMPER
from .recipe_unlocker import TEMPLATE as _RECIPE_UNLOCKER
from .spice_meter import TEMPLATE as _SPICE_METER
from .wok_tossing import TEMPLATE as _WOK_TOSSING

# Wave E2 — Engagement (10)
from .crossword_mini import TEMPLATE as _CROSSWORD_MINI
from .emoji_decoder import TEMPLATE as _EMOJI_DECODER
from .odd_one_out import TEMPLATE as _ODD_ONE_OUT
from .picture_puzzle import TEMPLATE as _PICTURE_PUZZLE
from .sequence_predictor import TEMPLATE as _SEQUENCE_PREDICTOR
from .spot_difference import TEMPLATE as _SPOT_DIFFERENCE
from .trivia_avalanche import TEMPLATE as _TRIVIA_AVALANCHE
from .word_anagram import TEMPLATE as _WORD_ANAGRAM
from .word_chain import TEMPLATE as _WORD_CHAIN
from .word_search_brand import TEMPLATE as _WORD_SEARCH_BRAND

# Wave E2 — Skill (10)
from .balance_balance import TEMPLATE as _BALANCE_BALANCE
from .drag_path import TEMPLATE as _DRAG_PATH
from .flappy_brand import TEMPLATE as _FLAPPY_BRAND
from .precision_target import TEMPLATE as _PRECISION_TARGET
from .reaction_time import TEMPLATE as _REACTION_TIME
from .shake_to_win import TEMPLATE as _SHAKE_TO_WIN
from .swipe_direction import TEMPLATE as _SWIPE_DIRECTION
from .tap_speed import TEMPLATE as _TAP_SPEED
from .timing_jump import TEMPLATE as _TIMING_JUMP
from .voice_shout import TEMPLATE as _VOICE_SHOUT


GAME_LIBRARY: dict[str, GameTemplate] = {
    # --- Legacy (5) -------------------------------------------------------
    "spin": _SPIN,
    "scratch": _SCRATCH,
    "match": _MATCH,
    "quiz": _QUIZ,
    "shake": _SHAKE,
    # --- Wave D9 (10) -----------------------------------------------------
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
    # --- Wave E2 F&B (15) -------------------------------------------------
    "coffee_brewing": _COFFEE_BREWING,
    "burger_builder": _BURGER_BUILDER,
    "pizza_topping": _PIZZA_TOPPING,
    "dim_sum_match": _DIM_SUM_MATCH,
    "bubble_tea_mixer": _BUBBLE_TEA_MIXER,
    "kopi_orders": _KOPI_ORDERS,
    "food_delivery_dash": _FOOD_DELIVERY_DASH,
    "cake_decorating": _CAKE_DECORATING,
    "menu_quiz": _MENU_QUIZ,
    "wok_tossing": _WOK_TOSSING,
    "queue_jumper": _QUEUE_JUMPER,
    "recipe_unlocker": _RECIPE_UNLOCKER,
    "ingredient_sort": _INGREDIENT_SORT,
    "dessert_combo": _DESSERT_COMBO,
    "spice_meter": _SPICE_METER,
    # --- Wave E2 engagement (10) -----------------------------------------
    "trivia_avalanche": _TRIVIA_AVALANCHE,
    "word_search_brand": _WORD_SEARCH_BRAND,
    "crossword_mini": _CROSSWORD_MINI,
    "picture_puzzle": _PICTURE_PUZZLE,
    "spot_difference": _SPOT_DIFFERENCE,
    "word_anagram": _WORD_ANAGRAM,
    "odd_one_out": _ODD_ONE_OUT,
    "emoji_decoder": _EMOJI_DECODER,
    "word_chain": _WORD_CHAIN,
    "sequence_predictor": _SEQUENCE_PREDICTOR,
    # --- Wave E2 skill (10) -----------------------------------------------
    "flappy_brand": _FLAPPY_BRAND,
    "tap_speed": _TAP_SPEED,
    "swipe_direction": _SWIPE_DIRECTION,
    "balance_balance": _BALANCE_BALANCE,
    "precision_target": _PRECISION_TARGET,
    "reaction_time": _REACTION_TIME,
    "drag_path": _DRAG_PATH,
    "timing_jump": _TIMING_JUMP,
    "shake_to_win": _SHAKE_TO_WIN,
    "voice_shout": _VOICE_SHOUT,
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
