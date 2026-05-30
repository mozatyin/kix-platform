"""Queue jumper: pop bubbles representing waiting customers."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#0ea5e9")
    title = "Queue Jumper" if not locale.startswith("zh") else "排队冲刺"
    body = """
<div id="row" style="display:flex; gap:6px; padding:14px; background:#0f172a; border-radius:14px; overflow-x:auto;"></div>
<div style="margin-top:10px;">Served: <span class="score" id="score">0</span> · Time: <span id="time">15</span>s</div>
<div class="hint">Tap customers in the queue to serve them quickly</div>
"""
    style_extra = ".cust{ font-size:34px; cursor:pointer; transition:transform .2s; } .cust.gone{ transform:scale(0); opacity:0; }"
    script = f"""
const row=document.getElementById('row'), scoreEl=document.getElementById('score'), timeEl=document.getElementById('time');
let score=0, t=15;
const FACES = ['🧑','👩','🧓','👨','👶','👩‍🦳'];
function add(){{ const c=document.createElement('div'); c.className='cust'; c.textContent = FACES[Math.floor(Math.random()*FACES.length)];
  c.addEventListener('click', ()=>{{ c.classList.add('gone'); score++; scoreEl.textContent=score; setTimeout(()=>c.remove(), 250); }});
  row.appendChild(c);
}}
for (let i=0;i<8;i++) add();
const spawn=setInterval(add, 700);
const tick=setInterval(()=>{{ t--; timeEl.textContent=t; if (t<=0){{ clearInterval(spawn); clearInterval(tick);
  window.kix.showResult(score>=20?'Speedy Service!':'Done','Served: '+score); }} }}, 1000);
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, style_extra=style_extra, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="queue_jumper",
    display_name_en="Queue Jumper",
    display_name_zh="排队冲刺",
    description_en="Serve queue customers fast.",
    description_zh="快速服务排队顾客。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": []},
    scoring={"win_threshold": 20, "tiers": [{"min_score": 20, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fnb", "retail"],
    completion_seconds=18,
)
TEMPLATE._render = _render
