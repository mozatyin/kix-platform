"""Catch falling: catch good items, avoid bombs."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#ff8500")
    title = "Catch & Win" if not locale.startswith("zh") else "接掉落"
    good = brand.get("good_item") or "🎁"
    bad = brand.get("bad_item") or "💣"
    body = """
<canvas id="game" width="320" height="420" style="background:#1a1d27; border-radius:14px; max-width:90vw; touch-action:none;"></canvas>
<div style="margin-top:10px;">Score: <span class="score" id="score">0</span> · Lives: <span id="lives">3</span></div>
<button id="start" type="button" style="margin-top:8px;">Start</button>
<div class="hint">Drag the basket to catch gifts</div>
"""
    script = f"""
const PRIMARY = {json.dumps(primary)};
const GOOD = {json.dumps(good)}, BAD = {json.dumps(bad)};
const c=document.getElementById('game'), ctx=c.getContext('2d');
let basketX=140, score=0, lives=3, items=[], running=false, last=0;
function reset(){{ basketX=140; score=0; lives=3; items=[]; document.getElementById('score').textContent=0; document.getElementById('lives').textContent=3; }}
c.addEventListener('touchmove', e=>{{ const r=c.getBoundingClientRect(); basketX = (e.touches[0].clientX - r.left) - 30; e.preventDefault(); }}, {{passive:false}});
c.addEventListener('mousemove', e=>{{ const r=c.getBoundingClientRect(); basketX = (e.clientX - r.left) - 30; }});
function loop(t){{ if(!running) return; const dt=(t-last)/16.7; last=t;
  ctx.fillStyle='#1a1d27'; ctx.fillRect(0,0,c.width,c.height);
  if (Math.random()<0.03) items.push({{x:Math.random()*280, y:-30, bad:Math.random()<0.25}});
  items.forEach(it => {{ it.y += 3*dt; ctx.font='28px sans-serif'; ctx.fillText(it.bad?BAD:GOOD, it.x, it.y); }});
  ctx.fillStyle=PRIMARY; ctx.fillRect(basketX, 380, 60, 18);
  items = items.filter(it => {{
    if (it.y > 380 && it.y < 410 && it.x > basketX-15 && it.x < basketX+60) {{
      if (it.bad) {{ lives--; document.getElementById('lives').textContent=lives; if(lives<=0) end(); }}
      else {{ score+=10; document.getElementById('score').textContent=score; }} return false;
    }} return it.y < c.height;
  }});
  requestAnimationFrame(loop);
}}
function end(){{ running=false; const won=score>=100; window.kix.showResult(won?'You Win!':'Game Over','Score: '+score); }}
document.getElementById('start').addEventListener('click', ()=>{{ reset(); running=true; last=performance.now(); requestAnimationFrame(loop);
  setTimeout(()=>{{ if(running) end(); }}, 30000); }});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="catch_falling",
    display_name_en="Catch & Win",
    display_name_zh="接掉落",
    description_en="Catch falling items in a basket, avoid bombs.",
    description_zh="接住掉落奖品，避开炸弹。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["good_item", "bad_item", "basket_image"]},
    scoring={"win_threshold": 100, "tiers": [{"min_score": 100, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["beauty", "retail", "fnb"],
    completion_seconds=30,
)
TEMPLATE._render = _render
