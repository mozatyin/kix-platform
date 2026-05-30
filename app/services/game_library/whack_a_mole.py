"""Whack-a-mole: fast-paced tap game."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#8d99ae")
    title = "Whack-a-Mole" if not locale.startswith("zh") else "打地鼠"
    body = """
<div id="grid" style="display:grid; grid-template-columns:repeat(3,80px); grid-gap:8px; margin-top:8px;"></div>
<div style="margin-top:14px;">Score: <span class="score" id="score">0</span> · Time: <span id="time">20</span>s</div>
<button id="start" type="button" style="margin-top:10px;">Start</button>
<div class="hint">Tap moles, avoid bombs</div>
"""
    style_extra = """
.hole { width:80px; height:80px; background:#333; border-radius:50%; display:flex;
  align-items:center; justify-content:center; font-size:42px; cursor:pointer; user-select:none; }
.hole.mole { background:#a3531a; }
.hole.bomb { background:#2b2d42; }
"""
    script = f"""
const PRIMARY = {json.dumps(primary)};
const grid = document.getElementById('grid'), scoreEl=document.getElementById('score'), timeEl=document.getElementById('time');
const holes = []; for(let i=0;i<9;i++){{ const h=document.createElement('div'); h.className='hole'; h.setAttribute('role','button'); h.setAttribute('aria-label','hole '+(i+1));
  h.addEventListener('click', ()=>{{ if (!running) return; if(h.classList.contains('mole')){{ score+=10; h.textContent=''; h.className='hole'; }}
    else if (h.classList.contains('bomb')){{ score=Math.max(0, score-15); h.textContent=''; h.className='hole'; }} scoreEl.textContent=score; }});
  grid.appendChild(h); holes.push(h);
}}
let score=0, running=false, t=20, tick=null, spawn=null;
document.getElementById('start').addEventListener('click', ()=>{{
  score=0; t=20; running=true; scoreEl.textContent=0; timeEl.textContent=t;
  tick=setInterval(()=>{{ t--; timeEl.textContent=t; if (t<=0) end(); }}, 1000);
  spawn=setInterval(()=>{{ const i=Math.floor(Math.random()*9); const h=holes[i];
    if (h.classList.length>1) return; const bomb = Math.random()<0.2;
    h.className = 'hole '+(bomb?'bomb':'mole'); h.textContent = bomb?'💣':'🐹';
    setTimeout(()=>{{ if(h.classList.contains('mole')||h.classList.contains('bomb')){{h.className='hole'; h.textContent='';}} }}, 900);
  }}, 600);
}});
function end(){{ running=false; clearInterval(tick); clearInterval(spawn); holes.forEach(h=>{{h.className='hole';h.textContent='';}});
  const won = score >= 80; window.kix.showResult(won?'You Win!':'Game Over', 'Score: '+score); }}
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, style_extra=style_extra, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="whack_a_mole",
    display_name_en="Whack-a-Mole",
    display_name_zh="打地鼠",
    description_en="Tap moles, avoid bombs, in 20 seconds.",
    description_zh="20秒内点击地鼠，避开炸弹。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["mole_image", "bomb_image", "sound_effects"]},
    scoring={"win_threshold": 80, "tiers": [{"min_score": 80, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fnb", "fitness", "retail"],
    completion_seconds=22,
)
TEMPLATE._render = _render
