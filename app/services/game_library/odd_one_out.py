"""Odd one out: pick the different item from the group."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#06b6d4")
    title = "Odd One Out" if not locale.startswith("zh") else "找出不同"
    body = """
<div id="grid" style="display:grid; grid-template-columns: repeat(3, 80px); gap:6px; margin-top:10px;"></div>
<div style="margin-top:10px;">Score: <span class="score" id="score">0</span> · Round: <span id="round">1</span>/5</div>
"""
    style_extra = ".item{ width:80px; height:80px; border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:30px; cursor:pointer; background:#1f2937; }"
    script = f"""
const SETS = [
  ['🍎','🍎','🍎','🍊','🍎','🍎','🍎','🍎','🍎'],
  ['⭐','⭐','⭐','⭐','⭐','✨','⭐','⭐','⭐'],
  ['🐶','🐶','🐶','🐶','🐱','🐶','🐶','🐶','🐶'],
  ['🟦','🟦','🟦','🟥','🟦','🟦','🟦','🟦','🟦'],
  ['🌹','🌹','🌷','🌹','🌹','🌹','🌹','🌹','🌹']
];
let r=0, score=0;
const grid=document.getElementById('grid');
function show(){{ if (r>=SETS.length){{ window.kix.showResult(score>=4?'Sharp Eye!':'Done','Score: '+score); return; }}
  document.getElementById('round').textContent = (r+1);
  const set = SETS[r];
  const counts = {{}}; set.forEach(s => counts[s] = (counts[s]||0)+1);
  const odd = Object.keys(counts).find(k => counts[k]===1);
  grid.innerHTML='';
  set.forEach(s=>{{ const d=document.createElement('div'); d.className='item'; d.textContent=s;
    d.addEventListener('click', ()=>{{ if (s===odd){{ score++; document.getElementById('score').textContent=score; }} r++; show(); }});
    grid.appendChild(d);
  }});
}}
show();
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, style_extra=style_extra, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="odd_one_out",
    display_name_en="Odd One Out",
    display_name_zh="找出不同",
    description_en="Pick the different item from each group.",
    description_zh="找出每组中不同的一项。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": []},
    scoring={"win_threshold": 4, "tiers": [{"min_score": 4, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["education", "retail", "beauty"],
    completion_seconds=25,
)
TEMPLATE._render = _render
