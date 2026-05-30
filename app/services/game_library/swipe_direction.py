"""Swipe direction: swipe matching arrow before timer."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#06b6d4")
    title = "Swipe!" if not locale.startswith("zh") else "滑动方向"
    body = """
<div id="arrow" style="font-size:120px; line-height:1; user-select:none; touch-action:none; margin:14px 0;">▲</div>
<div class="hint">Swipe in the shown direction</div>
<div style="margin-top:14px;">Score: <span class="score" id="score">0</span> · Time: <span id="time">15</span>s</div>
"""
    script = f"""
const DIRS = [['▲','up'],['▼','down'],['◀','left'],['▶','right']];
let cur=null, score=0, t=15, running=true;
function next(){{ cur = DIRS[Math.floor(Math.random()*DIRS.length)]; document.getElementById('arrow').textContent = cur[0]; }}
next();
const tick = setInterval(()=>{{ t--; document.getElementById('time').textContent=t; if (t<=0){{ running=false; clearInterval(tick);
  window.kix.showResult(score>=10?'Swiper!':'Done','Score: '+score); }} }}, 1000);

let sx=0, sy=0;
const target = document.getElementById('arrow');
target.addEventListener('touchstart', e=>{{ const t0=e.touches[0]; sx=t0.clientX; sy=t0.clientY; }}, {{passive:true}});
target.addEventListener('touchend', e=>{{ const t0=e.changedTouches[0]; check(t0.clientX-sx, t0.clientY-sy); }}, {{passive:true}});
target.addEventListener('mousedown', e=>{{ sx=e.clientX; sy=e.clientY; }});
target.addEventListener('mouseup', e=>{{ check(e.clientX-sx, e.clientY-sy); }});
function check(dx, dy){{ if (!running) return; if (Math.abs(dx)<20 && Math.abs(dy)<20) return;
  let dir; if (Math.abs(dx)>Math.abs(dy)) dir = dx>0?'right':'left'; else dir = dy>0?'down':'up';
  if (dir === cur[1]){{ score++; document.getElementById('score').textContent=score; }} else score = Math.max(0, score-1);
  next();
}}
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="swipe_direction",
    display_name_en="Swipe Direction",
    display_name_zh="滑动方向",
    description_en="Swipe matching the arrow direction.",
    description_zh="按照箭头方向滑动。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": []},
    scoring={"win_threshold": 10, "tiers": [{"min_score": 10, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fitness", "retail"],
    completion_seconds=18,
)
TEMPLATE._render = _render
