"""Flappy brand: simple flappy mechanic with brand mascot."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#22c55e")
    title = "Flappy Brand" if not locale.startswith("zh") else "飞翔挑战"
    body = """
<canvas id="cv" width="300" height="420" style="background:linear-gradient(180deg,#bae6fd,#fde68a); border-radius:12px; touch-action:none;"></canvas>
<div style="margin-top:10px;">Score: <span class="score" id="score">0</span></div>
<button id="tap" type="button" style="margin-top:8px;">Tap / Space to flap</button>
"""
    script = f"""
const cv=document.getElementById('cv'), ctx=cv.getContext('2d');
let y=200, vy=0, score=0, pipes=[], running=true, frame=0;
function flap(){{ if (!running){{ y=200; vy=0; score=0; pipes=[]; running=true; document.getElementById('score').textContent=0; return; }} vy = -6; }}
cv.addEventListener('click', flap);
document.getElementById('tap').addEventListener('click', flap);
window.addEventListener('keydown', e=>{{ if(e.code==='Space'){{ e.preventDefault(); flap(); }} }});
function loop(){{
  ctx.clearRect(0,0,300,420);
  vy += 0.35; y += vy;
  // bird
  ctx.fillStyle='#f59e0b'; ctx.beginPath(); ctx.arc(80,y,14,0,Math.PI*2); ctx.fill();
  // pipes
  if (frame % 90 === 0){{ const gap=120, top=40+Math.random()*180; pipes.push({{x:300, top, bot: top+gap, passed:false}}); }}
  ctx.fillStyle='#16a34a';
  for (const p of pipes){{ p.x -= 2; ctx.fillRect(p.x,0,40,p.top); ctx.fillRect(p.x,p.bot,40,420-p.bot);
    if (!p.passed && p.x+40 < 80){{ p.passed=true; score++; document.getElementById('score').textContent=score; }}
    if (80>p.x && 80<p.x+40 && (y<p.top || y>p.bot)){{ running=false; }}
  }}
  if (y>420 || y<0) running=false;
  if (!running){{ ctx.fillStyle='#000'; ctx.font='20px sans-serif'; ctx.fillText('Tap to restart', 80,210);
    if (score>=5){{ window.kix.showResult('Top Flyer!','Score: '+score); return; }}
  }}
  frame++;
  requestAnimationFrame(loop);
}}
loop();
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="flappy_brand",
    display_name_en="Flappy Brand",
    display_name_zh="飞翔挑战",
    description_en="Flap through gaps, brand mascot edition.",
    description_zh="穿越障碍——品牌飞翔版。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["mascot_sprite"]},
    scoring={"win_threshold": 5, "tiers": [{"min_score": 5, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["retail", "fitness", "fnb"],
    completion_seconds=40,
)
TEMPLATE._render = _render
