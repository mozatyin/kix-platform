"""Scratch: single scratch-off card."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#5e60ce")
    title = "Scratch to Win" if not locale.startswith("zh") else "刮刮乐"
    prizes = [p.get("label", f"Prize {i+1}") for i, p in enumerate(prize_pool.get("prizes", []))] or ["10% OFF"]
    body = """
<div style="position:relative; width:300px; max-width:90vw; height:200px; border-radius:18px; overflow:hidden; background:#fff; color:#222;">
  <div id="prize" style="position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-size:24px; font-weight:800;"></div>
  <canvas id="scratch" width="300" height="200" style="position:absolute; inset:0; touch-action:none;"></canvas>
</div>
<div class="hint" style="margin-top:14px;">Scratch the card to reveal your prize</div>
"""
    script = f"""
const PRIZES = {json.dumps(prizes)};
const PRIMARY = {json.dumps(primary)};
const prizeEl = document.getElementById('prize');
const chosen = PRIZES[Math.floor(Math.random()*PRIZES.length)];
prizeEl.textContent = chosen;
const cvs = document.getElementById('scratch'), ctx = cvs.getContext('2d');
ctx.fillStyle = PRIMARY; ctx.fillRect(0,0,cvs.width,cvs.height);
ctx.fillStyle = '#fff'; ctx.font = 'bold 22px sans-serif'; ctx.textAlign='center';
ctx.fillText('SCRATCH HERE', cvs.width/2, cvs.height/2+8);
let painting=false, scratched=0, total=cvs.width*cvs.height, opened=false;
function pos(e){{ const r=cvs.getBoundingClientRect(); const t=e.touches?e.touches[0]:e; return [t.clientX-r.left, t.clientY-r.top]; }}
function scratch(e){{ if (!painting) return; const [x,y] = pos(e);
  ctx.globalCompositeOperation='destination-out'; ctx.beginPath(); ctx.arc(x,y,18,0,Math.PI*2); ctx.fill();
  scratched += Math.PI*324;
  if (!opened && scratched/total > 0.45) {{ opened = true;
    setTimeout(()=> window.kix.showResult('Congrats!', 'You won: ' + chosen), 300);
  }}
  e.preventDefault();
}}
cvs.addEventListener('mousedown', e => {{ painting=true; scratch(e); }});
cvs.addEventListener('mousemove', scratch);
cvs.addEventListener('mouseup', ()=> painting=false);
cvs.addEventListener('touchstart', e => {{ painting=true; scratch(e); }}, {{passive:false}});
cvs.addEventListener('touchmove', scratch, {{passive:false}});
cvs.addEventListener('touchend', ()=> painting=false);
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="scratch",
    display_name_en="Scratch Card",
    display_name_zh="刮刮乐",
    description_en="Single scratch-off reveal.",
    description_zh="单张刮刮卡。",
    asset_requirements={"required": ["brand_logo", "primary_color", "prize_labels"], "optional": []},
    scoring={"win_threshold": 1, "mode": "instant"},
    recommended_industries=["fnb", "retail"],
    completion_seconds=10,
)
TEMPLATE._render = _render
