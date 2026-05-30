"""Spot the difference: tap the diffs between two images."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#0ea5e9")
    title = "Spot the Difference" if not locale.startswith("zh") else "找不同"
    body = """
<div style="display:flex; flex-direction:column; gap:10px;">
  <div id="left" class="pane"></div>
  <div id="right" class="pane"></div>
</div>
<div style="margin-top:10px;">Found: <span class="score" id="score">0</span> / 3 · Time: <span id="time">30</span>s</div>
"""
    style_extra = (
        ".pane{ position:relative; width:260px; height:140px; background:linear-gradient(180deg,#e0f2fe,#f0fdf4); border-radius:10px; overflow:hidden; }"
        ".dot{ position:absolute; width:30px; height:30px; border-radius:50%; }"
        ".mark{ position:absolute; width:30px; height:30px; border:3px solid #ef4444; border-radius:50%; box-sizing:border-box; pointer-events:none; }"
    )
    script = f"""
const DIFFS = [
  {{x:50, y:30, color:'#ef4444'}},
  {{x:150, y:80, color:'#10b981'}},
  {{x:200, y:40, color:'#f59e0b'}}
];
const left=document.getElementById('left'), right=document.getElementById('right');
const DECOY = [{{x:20,y:90,color:'#3b82f6'}},{{x:100,y:50,color:'#a855f7'}},{{x:230,y:100,color:'#06b6d4'}}];
function dot(p, container){{ const d=document.createElement('div'); d.className='dot'; d.style.left=p.x+'px'; d.style.top=p.y+'px'; d.style.background=p.color; container.appendChild(d); }}
DECOY.forEach(p=>{{ dot(p,left); dot(p,right); }});
DIFFS.forEach(p=>{{ dot(p,left); }}); // only on left
let found=0, t=30, running=true;
right.addEventListener('click', e=>{{ if (!running) return;
  const r=right.getBoundingClientRect(); const cx=e.clientX-r.left, cy=e.clientY-r.top;
  for (const p of DIFFS){{ if (Math.abs(p.x+15-cx)<25 && Math.abs(p.y+15-cy)<25){{
    if (right.querySelector('.mark[data-x="'+p.x+'"]')) return;
    const m=document.createElement('div'); m.className='mark'; m.dataset.x=p.x; m.style.left=p.x+'px'; m.style.top=p.y+'px'; right.appendChild(m);
    found++; document.getElementById('score').textContent=found;
    if (found>=DIFFS.length){{ running=false; window.kix.showResult('Eagle Eye!','All spotted'); }}
    return;
  }} }}
}});
const tick = setInterval(()=>{{ t--; document.getElementById('time').textContent=t; if (t<=0){{ running=false; clearInterval(tick);
  window.kix.showResult(found>=DIFFS.length?'Eagle Eye!':'Done','Found: '+found); }} }}, 1000);
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, style_extra=style_extra, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="spot_difference",
    display_name_en="Spot the Difference",
    display_name_zh="找不同",
    description_en="Find the differences between two images.",
    description_zh="找出两张图片的不同之处。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["image_pair"]},
    scoring={"win_threshold": 3, "tiers": [{"min_score": 3, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["retail", "beauty", "education"],
    completion_seconds=35,
)
TEMPLATE._render = _render
