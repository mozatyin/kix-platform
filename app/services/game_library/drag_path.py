"""Drag path: trace a path shape with finger."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#a855f7")
    title = "Trace the Path" if not locale.startswith("zh") else "描摹路径"
    body = """
<canvas id="cv" width="280" height="280" style="background:#fff; border-radius:12px; touch-action:none;"></canvas>
<div style="margin-top:10px;">Coverage: <span class="score" id="score">0</span>%</div>
<button id="reset" type="button" style="margin-top:8px; background:#444;">Reset</button>
"""
    script = f"""
const cv=document.getElementById('cv'), ctx=cv.getContext('2d');
function drawPath(){{ ctx.clearRect(0,0,280,280); ctx.strokeStyle='#e5e7eb'; ctx.lineWidth=20; ctx.lineCap='round';
  ctx.beginPath(); ctx.moveTo(40,140); ctx.bezierCurveTo(80,40,200,40,240,140); ctx.bezierCurveTo(200,240,80,240,40,140); ctx.stroke(); }}
drawPath();
let painting=false, hits=0, total=0;
const checkpoints = [];
for (let a=0; a<360; a+=15){{ const r=100; const x=140+Math.cos(a*Math.PI/180)*r, y=140+Math.sin(a*Math.PI/180)*r; checkpoints.push({{x,y,hit:false}}); }}
total = checkpoints.length;
function pos(e){{ const r=cv.getBoundingClientRect(); const t=e.touches?e.touches[0]:e; return [t.clientX-r.left, t.clientY-r.top]; }}
function paint(e){{ if (!painting) return; const [x,y]=pos(e);
  ctx.strokeStyle = {json.dumps(primary)}; ctx.lineWidth=16; ctx.lineCap='round';
  ctx.lineTo(x,y); ctx.stroke();
  checkpoints.forEach(cp=>{{ if (!cp.hit && Math.hypot(cp.x-x, cp.y-y) < 15){{ cp.hit=true; hits++; }} }});
  const pct = Math.round((hits/total)*100); document.getElementById('score').textContent=pct;
  if (pct >= 80){{ painting=false; window.kix.showResult('Steady Hand!','Coverage: '+pct+'%'); }}
  e.preventDefault();
}}
cv.addEventListener('mousedown', e=>{{ const [x,y]=pos(e); ctx.beginPath(); ctx.moveTo(x,y); painting=true; }});
cv.addEventListener('mousemove', paint);
cv.addEventListener('mouseup', ()=>painting=false);
cv.addEventListener('touchstart', e=>{{ const [x,y]=pos(e); ctx.beginPath(); ctx.moveTo(x,y); painting=true; }}, {{passive:false}});
cv.addEventListener('touchmove', paint, {{passive:false}});
cv.addEventListener('touchend', ()=>painting=false);
document.getElementById('reset').addEventListener('click', ()=>{{ checkpoints.forEach(c=>c.hit=false); hits=0; document.getElementById('score').textContent=0; drawPath(); }});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="drag_path",
    display_name_en="Trace the Path",
    display_name_zh="描摹路径",
    description_en="Trace the shape with your finger.",
    description_zh="用手指描摹形状。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["path_shape"]},
    scoring={"win_threshold": 80, "tiers": [{"min_score": 80, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["beauty", "fitness", "education"],
    completion_seconds=25,
)
TEMPLATE._render = _render
