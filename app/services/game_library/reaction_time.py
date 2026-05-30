"""Reaction time: tap as soon as the screen turns green."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#22c55e")
    title = "Reaction Time" if not locale.startswith("zh") else "反应速度"
    body = """
<div id="zone" style="width:280px; height:280px; border-radius:14px; background:#dc2626; display:flex; align-items:center; justify-content:center; color:#fff; font-weight:800; cursor:pointer;">Wait...</div>
<div style="margin-top:14px;">Best: <span class="score" id="best">-</span> ms · Avg: <span id="avg">-</span> ms</div>
"""
    script = f"""
const zone=document.getElementById('zone');
let phase='idle', startedAt=0, times=[];
function startRound(){{ phase='wait'; zone.style.background='#dc2626'; zone.textContent='Wait for green...';
  setTimeout(()=>{{ phase='go'; startedAt=Date.now(); zone.style.background='#16a34a'; zone.textContent='TAP!'; }}, 1000 + Math.random()*2000);
}}
zone.addEventListener('click', ()=>{{
  if (phase==='idle'){{ startRound(); return; }}
  if (phase==='wait'){{ phase='idle'; zone.style.background='#000'; zone.textContent='Too soon! Click to retry'; return; }}
  if (phase==='go'){{
    const dt = Date.now()-startedAt; times.push(dt);
    const best = Math.min(...times), avg = Math.round(times.reduce((a,b)=>a+b,0)/times.length);
    document.getElementById('best').textContent=best; document.getElementById('avg').textContent=avg;
    if (times.length>=3){{ window.kix.showResult(best<400?'Lightning!':'Done','Best: '+best+'ms'); phase='idle'; return; }}
    phase='idle'; zone.style.background='#000'; zone.textContent='Round '+(times.length+1)+'. Tap to start';
  }}
}});
zone.click(); // begin first round
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="reaction_time",
    display_name_en="Reaction Time",
    display_name_zh="反应速度",
    description_en="Tap as soon as the screen turns green.",
    description_zh="屏幕变绿立即点击。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": []},
    scoring={"win_threshold": 1, "mode": "instant"},
    recommended_industries=["fitness", "retail"],
    completion_seconds=20,
)
TEMPLATE._render = _render
