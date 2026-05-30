# Game Library — 15 Templates

`app.services.game_library` exposes 15 brand-customizable HTML5 mini-game
templates. Every template is a `GameTemplate` (declared in `base.py`) and
renders a self-contained, mobile-first, WCAG-AA HTML page from
`(brand_assets, prize_pool, locale)`.

## All 15 game types

| # | type_name | Name (EN / 中文) | Time | Industries | When to use |
|---|---|---|---|---|---|
| 1 | `spin` | Spin the Wheel / 幸运转盘 | 8s | fnb, retail, beauty | Quick instant-win, generic. |
| 2 | `scratch` | Scratch Card / 刮刮乐 | 10s | fnb, retail | Tactile instant-win. |
| 3 | `match` | Match 3 / 三消 | 35s | fnb, beauty | Skill-gated reward. |
| 4 | `quiz` | Brand Quiz / 品牌问答 | 30s | education, fnb, retail | Brand education + reward. |
| 5 | `shake` | Shake to Win / 摇一摇 | 12s | fnb, retail, fitness | Energy-burst engagement. |
| 6 | `slot_machine` | Slot Machine / 老虎机 | 10s | retail, fnb | Lottery / probability play. |
| 7 | `wheel_of_fortune` | Wheel of Fortune / 幸运大转盘 | 12s | fnb, retail, beauty | Weighted, multi-tier prizes. |
| 8 | `memory_match` | Memory Match / 记忆翻牌 | 40s | fnb, beauty, education | Cognitive skill, longer sessions. |
| 9 | `whack_a_mole` | Whack-a-Mole / 打地鼠 | 22s | fnb, fitness, retail | Reflex, high-intensity. |
| 10 | `catch_falling` | Catch & Win / 接掉落 | 30s | beauty, retail, fnb | Sustained engagement, product placement. |
| 11 | `bubble_pop` | Bubble Pop / 泡泡爆破 | 28s | beauty, fnb | Stress-relief / satisfying haptic. |
| 12 | `target_shoot` | Target Shoot / 射靶子 | 22s | fitness, retail | Skill-based, precision. |
| 13 | `stack_tower` | Stack Tower / 叠叠高 | 30s | retail, fitness | Reflex-skill, viral potential. |
| 14 | `lucky_dice` | Lucky Dice / 幸运骰子 | 8s | retail, fnb | Quick chance, triple-jackpot. |
| 15 | `scratch_galaxy` | Scratch Galaxy / 刮刮星河 | 45s | fnb, retail | Multi-card scratch session. |

## Choosing a template

| Goal | Recommended types |
|---|---|
| **Maximize daily-active** | `spin`, `lucky_dice`, `slot_machine` (≤10s) |
| **Brand education** | `quiz`, `memory_match` |
| **Product showcase** | `catch_falling`, `bubble_pop` (assets become game items) |
| **Skill-gated rewards** | `match`, `whack_a_mole`, `target_shoot`, `stack_tower` |
| **Multi-tier prize pool** | `wheel_of_fortune`, `scratch_galaxy` |
| **Mobile-only / device-native** | `shake` (uses devicemotion) |

## Industry tagging

```
F&B:        scratch, wheel_of_fortune, memory_match, slot_machine, spin, whack_a_mole, catch_falling, bubble_pop, lucky_dice, scratch_galaxy, quiz, shake
Beauty:     bubble_pop, catch_falling, memory_match, match, wheel_of_fortune, spin
Retail:     slot_machine, stack_tower, lucky_dice, target_shoot, scratch, whack_a_mole, catch_falling, wheel_of_fortune, spin, scratch_galaxy, shake, quiz
Education:  quiz, memory_match
Fitness:    shake, whack_a_mole, target_shoot, stack_tower
```

## Adding a new template

1. Create `app/services/game_library/<type_name>.py`.
2. Define a `_render(brand_assets, prize_pool, locale) -> str` that returns
   HTML produced via `base.render_skeleton(...)`. Stay self-contained
   (no external JS), use `var(--brand)` for the brand color, and respect
   `LOCALE` for any text.
3. Instantiate a module-level `TEMPLATE = GameTemplate(...)` and set
   `TEMPLATE._render = _render`.
4. Register in `app/services/game_library/__init__.py` (`GAME_LIBRARY`).
5. Update this doc + the demo page (`landing/game-catalog-demo.html`).
6. Add tests in `tests/test_game_library_expansion.py`.

## Asset requirements

Every template documents `asset_requirements = {"required": [...], "optional": [...]}`:

- **Always required**: `brand_logo`, `primary_color`.
- **Often required**: `prize_pool` (label + optional image per prize) and/or
  `prize_labels` for instant-win games.
- **Optional / adaptive**: `background_music`, `sound_effects`, `custom_skin`,
  `symbols` (per-game icons). Templates degrade gracefully when omitted.

## Public API

```python
from app.services.game_library import (
    GAME_LIBRARY,           # dict[str, GameTemplate]
    get_template,           # (type_name) -> GameTemplate
    list_templates,         # -> list[metadata dict]  (UI dropdown)
    recommend_for_brand,    # (brand_id, audience) -> [type_name, type_name, type_name]
)

tpl = get_template("slot_machine")
html = tpl.generate_html(
    brand_assets={"primary_color": "#e63946", "logo_url": "https://cdn/logo.png"},
    prize_pool={"prizes": [{"label": "10% OFF"}, {"label": "Free Drink"}]},
    locale="zh-Hans-SG",
)
result = tpl.calculate_win(score=1, prize_pool={"prizes": [{"label": "JACKPOT"}]})
# {"won": True, "score": 1, "prize": {"label": "JACKPOT"}, "tier": "jackpot"}
```

## Locale & accessibility

- `locale` accepts BCP-47 (e.g. `en-SG`, `zh-Hans-SG`, `ar-SG`). The
  skeleton sets `<html lang="…" dir="rtl|ltr">` automatically.
- ICU-style `{placeholder}` substitution is available via `base._icu`.
- WCAG AA: visible focus ring, ≥44×44 px tap targets, prefers-reduced-motion
  honored.

## Demo page

`landing/game-catalog-demo.html` previews all 15 templates in iframes with
filters by industry, time-bucket, and difficulty. Requires a backend
endpoint serving `generate_html` per type (e.g. `GET /api/v1/game-library/preview?type=…&locale=…`).
