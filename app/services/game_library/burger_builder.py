"""Burger builder: tap ingredients in the right order."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#e76f51")
    title = "Burger Builder" if not locale.startswith("zh") else "汉堡大师"
    body = """
<div id="recipe" class="hint" style="margin:6px 0 8px;">Order: 🍞 → 🥬 → 🍅 → 🧀 → 🍖 → 🍞</div>
<div id="stack" style="position:relative; width:240px; min-height:200px; display:flex; flex-direction:column-reverse; align-items:center; gap:2px;"></div>
<div style="margin-top:12px; display:flex; gap:6px; flex-wrap:wrap; justify-content:center; max-width:300px;">
  <button class="ing" data-i="🍞" type="button">🍞</button>
  <button class="ing" data-i="🥬" type="button">🥬</button>
  <button class="ing" data-i="🍅" type="button">🍅</button>
  <button class="ing" data-i="🧀" type="button">🧀</button>
  <button class="ing" data-i="🍖" type="button">🍖</button>
</div>
<div style="margin-top:10px;">Score: <span class="score" id="score">0</span></div>
"""
    script = f"""
const ORDER = ['🍞','🥬','🍅','🧀','🍖','🍞'];
let idx=0, score=0;
const stack=document.getElementById('stack'), scoreEl=document.getElementById('score');
document.querySelectorAll('.ing').forEach(b => b.addEventListener('click', ()=>{{
  const want = ORDER[idx];
  const got = b.dataset.i;
  const el = document.createElement('div');
  el.style.cssText='font-size:32px; line-height:1;';
  el.textContent = got;
  stack.appendChild(el);
  if (got === want){{ score += 20; idx++; }} else {{ score = Math.max(0, score-5); }}
  scoreEl.textContent = score;
  if (idx >= ORDER.length){{ window.kix.showResult(score>=100?'Top Chef!':'Done','Score: '+score); }}
}}));
"""
    style_extra = ".ing{ font-size:24px; padding:8px 12px; min-width:48px; }"
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, style_extra=style_extra, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="burger_builder",
    display_name_en="Burger Builder",
    display_name_zh="汉堡大师",
    description_en="Stack ingredients in the right order.",
    description_zh="按正确顺序堆叠汉堡食材。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["ingredient_skins"]},
    scoring={"win_threshold": 100, "tiers": [{"min_score": 100, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fnb", "retail"],
    completion_seconds=25,
)
TEMPLATE._render = _render
