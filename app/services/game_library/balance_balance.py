"""Balance balance: keep the ball centered via tilt or buttons."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#84cc16")
    title = "Balance!" if not locale.startswith("zh") else "平衡挑战"
    body = """
<div id="bar" style="position:relative; width:280px; height:24px; background:#222; border-radius:12px;">
  <div id="center" style="position:absolute; top:0; bottom:0; left:50%; width:2px; background:#fff; transform:translateX(-50%);"></div>
  <div id="ball" style="position:absolute; top:2px; width:20px; height:20px; border-radius:50%; background:#84cc16; left:130px;"></div>
</div>
<div style="margin-top:14px;">Hold: <span class="score" id="score">0</span>s</div>
<div style="margin-top:10px; display:flex; gap:8px;">
  <button id="left" type="button">◀ Tilt</button>
  <button id="right" type="button">Tilt ▶</button>
</div>
<div class="hint">Use gyro or tilt buttons to keep the ball centered</div>
"""
    script = f"""
let x=130, vx=0, score=0, lastTick=Date.now(), running=true;
const ball=document.getElementById('ball');
function tilt(dir){{ vx += dir*0.3; }}
document.getElementById('left').addEventListener('click', ()=>tilt(-1));
document.getElementById('right').addEventListener('click', ()=>tilt(1));
if (window.DeviceOrientationEvent){{ window.addEventListener('deviceorientation', e=>{{ if (e.gamma!=null) vx += e.gamma * 0.02; }}); }}
function loop(){{ if (!running) return;
  x += vx; vx *= 0.92;
  // gravity drift toward edges
  vx += (x-130) * 0.001;
  if (x<0){{ x=0; running=false; }}
  if (x>260){{ x=260; running=false; }}
  ball.style.left = x+'px';
  if (running){{
    const now = Date.now(); if (now-lastTick >= 1000){{ score++; lastTick = now; document.getElementById('score').textContent=score; }}
    if (score >= 10){{ running=false; window.kix.showResult('Steady!','Balanced '+score+'s'); return; }}
  }} else {{
    window.kix.showResult(score>=5?'Good Balance':'Lost it','Held '+score+'s');
    return;
  }}
  requestAnimationFrame(loop);
}}
loop();
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="balance_balance",
    display_name_en="Balance!",
    display_name_zh="平衡挑战",
    description_en="Keep the ball centered using tilt or buttons.",
    description_zh="使用倾斜或按钮保持球居中。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": []},
    scoring={"win_threshold": 10, "tiers": [{"min_score": 10, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fitness", "retail"],
    completion_seconds=20,
)
TEMPLATE._render = _render
