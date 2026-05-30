"""Bubble pop: tap bubbles for points."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#ff66c4")
    title = "Bubble Pop" if not locale.startswith("zh") else "泡泡爆破"
    body = """
<div id="field" style="position:relative; width:320px; height:420px; max-width:90vw; background: linear-gradient(180deg,#0e1a2b,#28386e); border-radius:14px; overflow:hidden;"></div>
<div style="margin-top:10px;">Score: <span class="score" id="score">0</span> · Time: <span id="time">25</span>s</div>
<button id="start" type="button" style="margin-top:8px;">Start</button>
<div class="hint">Pop as many bubbles as you can</div>
"""
    style_extra = """
.bubble { position:absolute; border-radius:50%; cursor:pointer; touch-action: manipulation;
  box-shadow: inset -6px -8px 16px rgba(255,255,255,.35); transition: transform .2s; }
.bubble.pop { transform: scale(1.4); opacity:0; pointer-events:none; }
"""
    script = f"""
const PRIMARY = {json.dumps(primary)};
const COLORS = [PRIMARY, '#ffd166', '#06d6a0', '#ef476f', '#118ab2'];
const field = document.getElementById('field'), scoreEl=document.getElementById('score'), timeEl=document.getElementById('time');
let score=0, t=25, running=false, spawn=null, tick=null;
function bubble(){{ const b = document.createElement('div'); b.className='bubble';
  const s = 36 + Math.random()*36; b.style.width=s+'px'; b.style.height=s+'px';
  b.style.left = Math.random()*(320-s)+'px'; b.style.top = '420px';
  b.style.background = 'radial-gradient(circle at 30% 30%, #fff, '+COLORS[Math.floor(Math.random()*COLORS.length)]+')';
  field.appendChild(b);
  let y=420; const speed=1+Math.random()*1.5;
  const it = setInterval(()=>{{ y -= speed; b.style.top = y+'px'; if (y < -s) {{ clearInterval(it); b.remove(); }} }}, 16);
  b.addEventListener('click', ()=>{{ if (!running) return; b.classList.add('pop'); score += Math.round(50 - s); scoreEl.textContent=score;
    setTimeout(()=>{{ clearInterval(it); b.remove(); }}, 200);
  }});
}}
document.getElementById('start').addEventListener('click', ()=>{{
  score=0; t=25; running=true; scoreEl.textContent=0; timeEl.textContent=t; field.innerHTML='';
  spawn = setInterval(bubble, 380); tick = setInterval(()=>{{ t--; timeEl.textContent=t; if(t<=0) end(); }}, 1000);
}});
function end(){{ running=false; clearInterval(spawn); clearInterval(tick);
  const won = score >= 200; window.kix.showResult(won?'You Win!':'Done','Score: '+score); }}
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, style_extra=style_extra, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="bubble_pop",
    display_name_en="Bubble Pop",
    display_name_zh="泡泡爆破",
    description_en="Tap floating bubbles for points.",
    description_zh="点击漂浮泡泡得分。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["bubble_skin", "sound_effects"]},
    scoring={"win_threshold": 200, "tiers": [{"min_score": 200, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["beauty", "fnb"],
    completion_seconds=28,
)
TEMPLATE._render = _render
