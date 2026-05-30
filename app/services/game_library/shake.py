"""Shake: shake-the-phone game."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#f4a261")
    title = "Shake!" if not locale.startswith("zh") else "摇一摇"
    body = """
<div id="art" style="font-size:120px; margin: 24px 0;" aria-hidden="true">📱</div>
<div class="score"><span id="score">0</span> / 20</div>
<button id="tap" type="button" style="margin-top:18px;">Tap to shake (or shake phone)</button>
<div class="hint">Reach 20 shakes in 10s</div>
"""
    script = f"""
const PRIMARY = {json.dumps(primary)};
let count=0, started=false, last=0;
const scoreEl = document.getElementById('score'), art = document.getElementById('art');
function shake(){{ count++; scoreEl.textContent=count; art.style.transform = 'rotate('+(Math.random()*30-15)+'deg)';
  setTimeout(()=> art.style.transform='', 80);
  if (count >= 20) end(true);
}}
function end(win){{ if (!started) return; started=false; window.kix.showResult(win?'You Win!':'Time up', 'Shakes: '+count); }}
document.getElementById('tap').addEventListener('click', ()=>{{ if(!started){{started=true; setTimeout(()=>end(false),10000);}} shake(); }});
if (window.DeviceMotionEvent) window.addEventListener('devicemotion', e => {{
  const a = e.accelerationIncludingGravity || {{}}; const m = Math.abs(a.x||0)+Math.abs(a.y||0)+Math.abs(a.z||0);
  const now = Date.now();
  if (m > 25 && now-last > 200) {{ last=now; if(!started){{started=true; setTimeout(()=>end(false),10000);}} shake(); }}
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="shake",
    display_name_en="Shake to Win",
    display_name_zh="摇一摇",
    description_en="Shake the phone to score.",
    description_zh="摇晃手机得分。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": []},
    scoring={"win_threshold": 20, "tiers": [{"min_score": 20, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fnb", "retail", "fitness"],
    completion_seconds=12,
)
TEMPLATE._render = _render
