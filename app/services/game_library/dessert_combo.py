"""Dessert combo: match desserts to prize tiers."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#ec4899")
    title = "Dessert Combo" if not locale.startswith("zh") else "甜品组合"
    body = """
<div class="hint">Tap 3 desserts to form a combo</div>
<div id="combo" style="font-size:42px; min-height:54px; margin:10px 0;">_ _ _</div>
<div style="display:flex; gap:10px; flex-wrap:wrap; justify-content:center; max-width:300px;">
  <button class="d" data-i="🍩" type="button">🍩</button>
  <button class="d" data-i="🍰" type="button">🍰</button>
  <button class="d" data-i="🍪" type="button">🍪</button>
  <button class="d" data-i="🍦" type="button">🍦</button>
  <button class="d" data-i="🧁" type="button">🧁</button>
  <button class="d" data-i="🍮" type="button">🍮</button>
</div>
<button id="reset" type="button" style="margin-top:10px; background:#444;">Reset</button>
"""
    style_extra = ".d{ font-size:30px; padding:8px 12px; }"
    script = f"""
const combo=document.getElementById('combo');
let picks=[];
function render(){{ combo.textContent = (picks[0]||'_') + ' ' + (picks[1]||'_') + ' ' + (picks[2]||'_'); }}
document.querySelectorAll('.d').forEach(b => b.addEventListener('click', ()=>{{
  if (picks.length>=3) return;
  picks.push(b.dataset.i); render();
  if (picks.length===3){{
    const unique = new Set(picks).size;
    const score = unique===1 ? 100 : (unique===2 ? 50 : 20);
    window.kix.showResult(score>=100?'Jackpot Combo!':(score>=50?'Nice!':'Try Again'),'Score: '+score);
  }}
}}));
document.getElementById('reset').addEventListener('click', ()=>{{ picks=[]; render(); }});
render();
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, style_extra=style_extra, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="dessert_combo",
    display_name_en="Dessert Combo",
    display_name_zh="甜品组合",
    description_en="Pick a 3-dessert combo, triples win jackpot.",
    description_zh="选3个甜品组合，三连=大奖。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["dessert_icons"]},
    scoring={"win_threshold": 50, "tiers": [
        {"min_score": 50, "prize_index": 1, "label": "pair"},
        {"min_score": 100, "prize_index": 0, "label": "jackpot"},
    ]},
    recommended_industries=["fnb"],
    completion_seconds=15,
)
TEMPLATE._render = _render
