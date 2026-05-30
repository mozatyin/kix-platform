"""Bubble tea mixer: tap ingredients to mix the target recipe."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#d946ef")
    title = "Bubble Tea Mixer" if not locale.startswith("zh") else "珍珠奶茶调配"
    body = """
<div id="target" class="hint" style="margin-bottom:8px;">Target: tea x2 · milk x1 · pearls x2</div>
<div id="cup" style="width:120px; height:200px; border:3px solid #fff; border-radius:0 0 60px 60px / 0 0 30px 30px; display:flex; flex-direction:column-reverse; overflow:hidden; background:#222;"></div>
<div style="margin-top:14px; display:flex; gap:6px;">
  <button data-k="tea" type="button">+ Tea</button>
  <button data-k="milk" type="button">+ Milk</button>
  <button data-k="pearls" type="button">+ Pearls</button>
</div>
<button id="serve" type="button" style="margin-top:10px; background:#06d6a0;">Serve</button>
"""
    script = f"""
const TARGET = {{tea:2, milk:1, pearls:2}};
const COLORS = {{tea:'#7c2d12', milk:'#fef3c7', pearls:'#111'}};
const counts = {{tea:0, milk:0, pearls:0}};
const cup = document.getElementById('cup');
document.querySelectorAll('button[data-k]').forEach(b => b.addEventListener('click', ()=>{{
  const k=b.dataset.k; counts[k]++;
  const layer=document.createElement('div'); layer.style.cssText='height:24px; width:100%; background:'+COLORS[k]; cup.appendChild(layer);
}}));
document.getElementById('serve').addEventListener('click', ()=>{{
  let score=0; for (const k in TARGET){{ score += Math.max(0, 30 - 10*Math.abs(counts[k]-TARGET[k])); }}
  window.kix.showResult(score>=80?'Perfect Mix!':'Done','Score: '+score);
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="bubble_tea_mixer",
    display_name_en="Bubble Tea Mixer",
    display_name_zh="珍珠奶茶调配",
    description_en="Mix the target bubble tea recipe.",
    description_zh="调配目标珍珠奶茶配方。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["recipe_card"]},
    scoring={"win_threshold": 80, "tiers": [{"min_score": 80, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fnb"],
    completion_seconds=18,
)
TEMPLATE._render = _render
