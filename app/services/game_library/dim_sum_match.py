"""Dim sum match: tap matching pairs on a steamer basket."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#f59e0b")
    title = "Dim Sum Match" if not locale.startswith("zh") else "点心配对"
    body = """
<div id="board" style="display:grid; grid-template-columns: repeat(4, 60px); gap:6px; margin-top:8px;"></div>
<div style="margin-top:12px;">Pairs: <span class="score" id="score">0</span> / 6</div>
<div class="hint">Tap two dim sum to find matching pairs</div>
"""
    style_extra = (
        ".cell{ width:60px; height:60px; border-radius:10px; background:#7c2d12; "
        "display:flex; align-items:center; justify-content:center; font-size:30px; cursor:pointer; }"
        ".cell.flipped{ background:#fff7ed; } .cell.matched{ opacity:0.4; pointer-events:none; }"
    )
    script = f"""
const ICONS = ['🥟','🥠','🍡','🍤','🥬','🍵'];
const deck = ICONS.concat(ICONS).map(v => ({{ v, k: Math.random() }})).sort((a,b)=>a.k-b.k).map(x=>x.v);
const board=document.getElementById('board'), scoreEl=document.getElementById('score');
let first=null, lock=false, pairs=0;
deck.forEach((v,i)=>{{
  const c=document.createElement('div'); c.className='cell'; c.dataset.v=v;
  c.addEventListener('click', ()=>{{
    if (lock || c.classList.contains('matched') || c.classList.contains('flipped')) return;
    c.classList.add('flipped'); c.textContent = v;
    if (!first){{ first=c; return; }}
    if (first.dataset.v === c.dataset.v){{
      first.classList.add('matched'); c.classList.add('matched'); first=null; pairs++; scoreEl.textContent=pairs;
      if (pairs===6) window.kix.showResult('Yum Cha Master!','All matched');
    }} else {{
      lock=true;
      setTimeout(()=>{{ first.classList.remove('flipped'); first.textContent=''; c.classList.remove('flipped'); c.textContent=''; first=null; lock=false; }}, 700);
    }}
  }});
  board.appendChild(c);
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, style_extra=style_extra, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="dim_sum_match",
    display_name_en="Dim Sum Match",
    display_name_zh="点心配对",
    description_en="Find matching dim sum pairs.",
    description_zh="找到相同的点心配对。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["dim_sum_icons"]},
    scoring={"win_threshold": 6, "tiers": [{"min_score": 6, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fnb"],
    completion_seconds=35,
)
TEMPLATE._render = _render
