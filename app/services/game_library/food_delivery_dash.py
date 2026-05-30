"""Food delivery dash: dodge obstacles, deliver food."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#10b981")
    title = "Delivery Dash" if not locale.startswith("zh") else "外卖冲刺"
    body = """
<div id="field" style="position:relative; width:300px; height:420px; max-width:90vw; background:linear-gradient(180deg,#0f172a,#1e293b); border-radius:14px; overflow:hidden;">
  <div id="player" style="position:absolute; bottom:20px; left:130px; font-size:32px;">🛵</div>
</div>
<div style="margin-top:10px;">Score: <span class="score" id="score">0</span></div>
<div style="margin-top:8px; display:flex; gap:8px;">
  <button id="left" type="button">◀</button>
  <button id="right" type="button">▶</button>
</div>
"""
    style_extra = ".obs{ position:absolute; font-size:28px; }"
    script = f"""
const field=document.getElementById('field'), player=document.getElementById('player'), scoreEl=document.getElementById('score');
let px=130, score=0, running=true, obs=[];
document.getElementById('left').addEventListener('click', ()=>{{ px=Math.max(10, px-50); player.style.left=px+'px'; }});
document.getElementById('right').addEventListener('click', ()=>{{ px=Math.min(258, px+50); player.style.left=px+'px'; }});
function spawn(){{ if(!running) return; const e=document.createElement('div'); e.className='obs';
  e.textContent = Math.random()<0.7 ? '🚧' : '🍔';
  e.style.left = (Math.random()*270)+'px'; e.style.top = '-30px'; field.appendChild(e);
  obs.push({{el:e, y:-30, kind: e.textContent}});
}}
function step(){{ if(!running) return;
  for (let i=obs.length-1;i>=0;i--){{ const o=obs[i]; o.y+=4; o.el.style.top=o.y+'px';
    if (o.y > 380 && o.y < 420){{
      const ex = parseInt(o.el.style.left)||0;
      if (Math.abs(ex - px) < 30){{
        if (o.kind==='🍔'){{ score+=10; scoreEl.textContent=score; o.el.remove(); obs.splice(i,1); continue; }}
        else {{ running=false; window.kix.showResult(score>=50?'Delivered!':'Crashed','Score: '+score); return; }}
      }}
    }}
    if (o.y > 440){{ o.el.remove(); obs.splice(i,1); }}
  }}
  requestAnimationFrame(step);
}}
setInterval(spawn, 600); step();
setTimeout(()=>{{ running=false; window.kix.showResult(score>=50?'Delivered!':'Time up','Score: '+score); }}, 25000);
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, style_extra=style_extra, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="food_delivery_dash",
    display_name_en="Delivery Dash",
    display_name_zh="外卖冲刺",
    description_en="Dodge traffic, collect food drops.",
    description_zh="躲开障碍，收集食物。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["scooter_skin"]},
    scoring={"win_threshold": 50, "tiers": [{"min_score": 50, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fnb", "retail"],
    completion_seconds=30,
)
TEMPLATE._render = _render
