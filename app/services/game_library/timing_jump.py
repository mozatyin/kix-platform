"""Timing jump: jump over obstacles by tapping at the right moment."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#f97316")
    title = "Timing Jump" if not locale.startswith("zh") else "完美跳跃"
    body = """
<div id="track" style="position:relative; width:300px; height:120px; background:linear-gradient(180deg,#fde68a,#fb923c); border-radius:10px; overflow:hidden;">
  <div id="player" style="position:absolute; bottom:0; left:40px; font-size:40px;">🏃</div>
</div>
<div style="margin-top:10px;">Score: <span class="score" id="score">0</span></div>
<button id="jump" type="button" style="margin-top:8px;">Jump (tap or Space)</button>
"""
    style_extra = ".obs{ position:absolute; bottom:0; font-size:30px; }"
    script = f"""
const track=document.getElementById('track'), player=document.getElementById('player');
let jumping=false, score=0, running=true, obs=[];
function jump(){{ if (jumping||!running) return; jumping=true;
  player.style.transition='bottom .25s'; player.style.bottom='60px';
  setTimeout(()=>{{ player.style.bottom='0px'; setTimeout(()=>jumping=false, 250); }}, 250);
}}
document.getElementById('jump').addEventListener('click', jump);
window.addEventListener('keydown', e=>{{ if(e.code==='Space'){{ e.preventDefault(); jump(); }} }});
function spawn(){{ if(!running) return;
  const e=document.createElement('div'); e.className='obs'; e.textContent='🌵'; e.style.left='300px'; track.appendChild(e);
  obs.push({{el:e, x:300}});
}}
function loop(){{ if(!running) return;
  for (let i=obs.length-1;i>=0;i--){{ const o=obs[i]; o.x-=3; o.el.style.left=o.x+'px';
    if (o.x < 70 && o.x > 30 && !jumping){{ running=false; window.kix.showResult(score>=5?'Jumper!':'Crashed','Score: '+score); return; }}
    if (o.x < -30){{ o.el.remove(); obs.splice(i,1); score++; document.getElementById('score').textContent=score; }}
  }}
  requestAnimationFrame(loop);
}}
setInterval(spawn, 1200); loop();
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, style_extra=style_extra, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="timing_jump",
    display_name_en="Timing Jump",
    display_name_zh="完美跳跃",
    description_en="Jump over obstacles, perfect timing.",
    description_zh="完美时机跳过障碍。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["runner_skin"]},
    scoring={"win_threshold": 5, "tiers": [{"min_score": 5, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fitness", "retail"],
    completion_seconds=30,
)
TEMPLATE._render = _render
