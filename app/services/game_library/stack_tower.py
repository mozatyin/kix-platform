"""Stack tower: stack blocks higher and higher."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#fcbf49")
    title = "Stack Tower" if not locale.startswith("zh") else "叠叠高"
    body = """
<canvas id="game" width="320" height="480" style="background:#0e1a2b; border-radius:14px; max-width:90vw;"></canvas>
<div style="margin-top:10px;">Height: <span class="score" id="score">0</span></div>
<button id="drop" type="button" style="margin-top:8px;">DROP</button>
<div class="hint">Tap DROP to stack — match the previous block</div>
"""
    script = f"""
const PRIMARY = {json.dumps(primary)};
const c = document.getElementById('game'), ctx = c.getContext('2d');
const W=c.width, H=c.height, BH=24;
let stack=[{{x:80,w:160,color:PRIMARY}}], moving=null, dir=1, dropped=0, ended=false;
function newMover(){{ const top=stack[stack.length-1]; moving={{x:0,w:top.w,color:PRIMARY,y:H - (stack.length+1)*BH - 8}}; dir=1; }}
function step(){{ if (ended) return; if (moving){{ moving.x += dir*3; if(moving.x<0||moving.x+moving.w>W) dir=-dir; }}
  ctx.fillStyle='#0e1a2b'; ctx.fillRect(0,0,W,H);
  stack.forEach((b,i)=>{{ ctx.fillStyle=b.color; ctx.fillRect(b.x, H-(i+1)*BH-8, b.w, BH-2); }});
  if (moving) {{ ctx.fillStyle=moving.color; ctx.fillRect(moving.x, moving.y, moving.w, BH-2); }}
  requestAnimationFrame(step);
}}
newMover(); step();
document.getElementById('drop').addEventListener('click', ()=>{{
  if (ended || !moving) return;
  const top = stack[stack.length-1];
  const left = Math.max(top.x, moving.x), right = Math.min(top.x+top.w, moving.x+moving.w);
  const overlap = right - left;
  if (overlap <= 0) {{ ended=true; window.kix.showResult('Game Over', 'Height: '+(stack.length-1)); return; }}
  stack.push({{x:left, w:overlap, color: dropped%2 ? '#fff' : PRIMARY}});
  dropped++;
  document.getElementById('score').textContent = stack.length - 1;
  if (stack.length >= 12) {{ ended=true; window.kix.showResult('You Win!', 'Height: '+(stack.length-1)); return; }}
  newMover();
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="stack_tower",
    display_name_en="Stack Tower",
    display_name_zh="叠叠高",
    description_en="Stack blocks accurately to build the tallest tower.",
    description_zh="精准叠加方块，建最高塔。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["block_skin"]},
    scoring={"win_threshold": 10, "tiers": [{"min_score": 10, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["retail", "fitness"],
    completion_seconds=30,
)
TEMPLATE._render = _render
