"""Shake to win: shake harder for bigger prize."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#f59e0b")
    title = "Shake to Win!" if not locale.startswith("zh") else "摇出大奖"
    body = """
<div id="art" style="font-size:120px; margin: 16px 0; transition:transform .1s;">🎁</div>
<div style="margin-top:6px;">Energy: <span class="score" id="energy">0</span></div>
<button id="tap" type="button" style="margin-top:14px;">Tap repeatedly (or shake)</button>
<div class="hint">Shake harder to win bigger prizes</div>
"""
    script = f"""
const art=document.getElementById('art'), energyEl=document.getElementById('energy');
let energy=0, started=false, last=0;
function shake(){{ energy += 5; energyEl.textContent=energy;
  art.style.transform = 'rotate('+(Math.random()*40-20)+'deg) scale('+(1+Math.min(.5, energy/200))+')';
  setTimeout(()=> art.style.transform='', 80);
  if (energy >= 200) end();
}}
function end(){{ if (!started) return; started=false;
  const tier = energy>=200?'Jackpot': energy>=100?'Big':'Small';
  window.kix.showResult(tier+' Prize!','Energy: '+energy);
}}
document.getElementById('tap').addEventListener('click', ()=>{{ if(!started){{started=true; setTimeout(end,10000);}} shake(); }});
if (window.DeviceMotionEvent) window.addEventListener('devicemotion', e => {{
  const a = e.accelerationIncludingGravity || {{}}; const m = Math.abs(a.x||0)+Math.abs(a.y||0)+Math.abs(a.z||0);
  const now = Date.now();
  if (m > 25 && now-last > 150) {{ last=now; if(!started){{started=true; setTimeout(end,10000);}} shake(); }}
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="shake_to_win",
    display_name_en="Shake to Win",
    display_name_zh="摇出大奖",
    description_en="Shake harder for bigger prizes.",
    description_zh="摇得越用力，奖励越大。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": []},
    scoring={"win_threshold": 100, "tiers": [
        {"min_score": 100, "prize_index": 1, "label": "big"},
        {"min_score": 200, "prize_index": 0, "label": "jackpot"},
    ]},
    recommended_industries=["fnb", "retail", "fitness"],
    completion_seconds=12,
)
TEMPLATE._render = _render
