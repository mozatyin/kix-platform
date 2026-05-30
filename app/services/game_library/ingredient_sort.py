"""Ingredient sort: quickly categorize items as veg/meat/dairy."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#16a34a")
    title = "Ingredient Sort" if not locale.startswith("zh") else "食材分类"
    body = """
<div id="item" style="font-size:80px; min-height:90px;">—</div>
<div style="display:flex; gap:8px; margin-top:10px;">
  <button data-c="veg" type="button">🥦 Veg</button>
  <button data-c="meat" type="button">🥩 Meat</button>
  <button data-c="dairy" type="button">🧀 Dairy</button>
</div>
<div style="margin-top:10px;">Score: <span class="score" id="score">0</span> · Time: <span id="time">20</span>s</div>
"""
    script = f"""
const ITEMS = [
  ['🥬','veg'],['🍖','meat'],['🥛','dairy'],['🍅','veg'],['🐔','meat'],['🧀','dairy'],
  ['🥕','veg'],['🥩','meat'],['🍦','dairy'],['🥒','veg'],['🍗','meat'],['🧈','dairy']
];
const item=document.getElementById('item'), scoreEl=document.getElementById('score'), timeEl=document.getElementById('time');
let cur=null, score=0, t=20, running=true;
function next(){{ cur = ITEMS[Math.floor(Math.random()*ITEMS.length)]; item.textContent = cur[0]; }}
document.querySelectorAll('button[data-c]').forEach(b=> b.addEventListener('click', ()=>{{
  if (!running) return;
  if (b.dataset.c === cur[1]) score++; else score = Math.max(0,score-1);
  scoreEl.textContent=score; next();
}}));
next();
const tick=setInterval(()=>{{ t--; timeEl.textContent=t; if (t<=0){{ running=false; clearInterval(tick);
  window.kix.showResult(score>=15?'Sorted!':'Done','Score: '+score); }} }}, 1000);
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="ingredient_sort",
    display_name_en="Ingredient Sort",
    display_name_zh="食材分类",
    description_en="Categorize ingredients quickly.",
    description_zh="快速分类食材。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": []},
    scoring={"win_threshold": 15, "tiers": [{"min_score": 15, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fnb", "education"],
    completion_seconds=22,
)
TEMPLATE._render = _render
