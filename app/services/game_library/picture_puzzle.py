"""Picture puzzle: 3x3 slide puzzle of brand image."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, _safe, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#f59e0b")
    title = "Picture Puzzle" if not locale.startswith("zh") else "图片拼图"
    img = brand.get("logo_url") or ""
    body = f"""
<div id="board" style="display:grid; grid-template-columns: repeat(3, 80px); gap:2px; background:#222; padding:2px; border-radius:8px;"></div>
<div style="margin-top:10px;">Moves: <span class="score" id="moves">0</span></div>
<div class="hint">Tap a tile next to the empty space to slide it</div>
"""
    style_extra = (
        ".tile{ width:80px; height:80px; background:#eee; color:#000; display:flex; align-items:center; justify-content:center; "
        "font-weight:800; font-size:18px; cursor:pointer; border-radius:4px; }"
        ".tile.empty{ background:transparent; cursor:default; }"
    )
    script = f"""
const IMG = {json.dumps(img)};
const board=document.getElementById('board'), movesEl=document.getElementById('moves');
let order = [1,2,3,4,5,6,7,8,0];
// shuffle by valid moves (keep solvable)
function adj(i){{ const r=Math.floor(i/3), c=i%3; const a=[];
  if (r>0) a.push(i-3); if (r<2) a.push(i+3); if (c>0) a.push(i-1); if (c<2) a.push(i+1); return a; }}
for (let s=0;s<60;s++){{ const z=order.indexOf(0); const opts=adj(z); const pick=opts[Math.floor(Math.random()*opts.length)];
  [order[z],order[pick]]=[order[pick],order[z]]; }}
let moves=0;
function render(){{ board.innerHTML='';
  order.forEach((v,i)=>{{ const d=document.createElement('div'); d.className='tile'+(v===0?' empty':''); d.textContent = v===0?'':v;
    d.addEventListener('click', ()=>{{ const z=order.indexOf(0); if (adj(z).includes(i)){{ [order[z],order[i]]=[order[i],order[z]]; moves++; movesEl.textContent=moves; render(); check(); }} }});
    board.appendChild(d);
  }});
}}
function check(){{ const sorted = order.every((v,i)=> v === (i===8?0:i+1));
  if (sorted) window.kix.showResult('Solved!','Moves: '+moves);
}}
render();
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, style_extra=style_extra, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="picture_puzzle",
    display_name_en="Picture Puzzle",
    display_name_zh="图片拼图",
    description_en="3x3 slide puzzle, solve to win.",
    description_zh="3x3滑块拼图，复原即胜。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["puzzle_image"]},
    scoring={"win_threshold": 1, "tiers": [{"min_score": 1, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["retail", "education", "beauty"],
    completion_seconds=50,
)
TEMPLATE._render = _render
