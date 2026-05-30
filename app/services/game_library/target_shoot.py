"""Target shoot: aim and tap to hit moving targets."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#d62828")
    title = "Target Shoot" if not locale.startswith("zh") else "射靶子"
    body = """
<canvas id="game" width="320" height="420" style="background:#1c2541; border-radius:14px; max-width:90vw;"></canvas>
<div style="margin-top:10px;">Score: <span class="score" id="score">0</span> · Time: <span id="time">20</span>s</div>
<button id="start" type="button" style="margin-top:8px;">Start</button>
<div class="hint">Tap targets quickly to hit them</div>
"""
    script = f"""
const PRIMARY = {json.dumps(primary)};
const c=document.getElementById('game'), ctx=c.getContext('2d');
let targets=[], score=0, t=20, running=false, tick=null, spawn=null;
function spawnTarget(){{ targets.push({{x:30+Math.random()*260, y:30+Math.random()*340, r:22, vx:(Math.random()-.5)*2, vy:(Math.random()-.5)*2, life: 2500, t0: performance.now()}}); }}
c.addEventListener('click', e => {{ if (!running) return; const r = c.getBoundingClientRect();
  const x = (e.clientX - r.left) * (c.width/r.width), y = (e.clientY - r.top) * (c.height/r.height);
  for (let i=targets.length-1;i>=0;i--) {{ const tg=targets[i]; const d = Math.hypot(tg.x-x, tg.y-y); if (d < tg.r) {{ score+=10; targets.splice(i,1); document.getElementById('score').textContent=score; break; }} }}
}});
function loop(){{ if(!running) return; ctx.fillStyle='#1c2541'; ctx.fillRect(0,0,c.width,c.height);
  const now = performance.now();
  targets = targets.filter(tg => {{ tg.x += tg.vx; tg.y += tg.vy; if (tg.x<20||tg.x>300) tg.vx=-tg.vx; if (tg.y<20||tg.y>400) tg.vy=-tg.vy;
    if (now - tg.t0 > tg.life) return false;
    ctx.beginPath(); ctx.fillStyle=PRIMARY; ctx.arc(tg.x,tg.y,tg.r,0,Math.PI*2); ctx.fill();
    ctx.beginPath(); ctx.fillStyle='#fff'; ctx.arc(tg.x,tg.y,tg.r*0.55,0,Math.PI*2); ctx.fill();
    ctx.beginPath(); ctx.fillStyle=PRIMARY; ctx.arc(tg.x,tg.y,tg.r*0.22,0,Math.PI*2); ctx.fill();
    return true;
  }}); requestAnimationFrame(loop);
}}
document.getElementById('start').addEventListener('click', ()=>{{
  score=0; t=20; running=true; targets=[]; document.getElementById('score').textContent=0; document.getElementById('time').textContent=t;
  spawn = setInterval(spawnTarget, 600); tick = setInterval(()=>{{ t--; document.getElementById('time').textContent=t; if(t<=0) end(); }}, 1000); loop();
}});
function end(){{ running=false; clearInterval(spawn); clearInterval(tick); const won = score>=120; window.kix.showResult(won?'Sharpshooter!':'Done', 'Score: '+score); }}
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="target_shoot",
    display_name_en="Target Shoot",
    display_name_zh="射靶子",
    description_en="Aim and tap targets within the time limit.",
    description_zh="时间内点击靶子。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["target_image", "sound_effects"]},
    scoring={"win_threshold": 120, "tiers": [{"min_score": 120, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fitness", "retail"],
    completion_seconds=22,
)
TEMPLATE._render = _render
