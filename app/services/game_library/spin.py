"""Spin: classic spin-the-wheel (single-prize spin), existing type retained for catalog."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, _safe, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#FF4081")
    title = "Spin to Win" if not locale.startswith("zh") else "旋转赢奖"
    prizes = [p.get("label", f"Prize {i+1}") for i, p in enumerate(prize_pool.get("prizes", []))]
    if not prizes:
        prizes = ["10%", "Free Gift", "20%", "Try Again", "Bonus", "5%"]
    body = """
<canvas id="wheel" width="320" height="320" aria-label="prize wheel" role="img" style="max-width:90vw"></canvas>
<div style="margin-top:14px;"><button id="spin" type="button">Spin</button></div>
<div class="hint">Tap Spin to play</div>
"""
    script = f"""
const PRIZES = {json.dumps(prizes)};
const PRIMARY = {json.dumps(primary)};
const cvs = document.getElementById('wheel'), ctx = cvs.getContext('2d');
let angle = 0, spinning = false;
function draw(){{
  const N = PRIZES.length, arc = Math.PI*2/N, R = cvs.width/2;
  ctx.clearRect(0,0,cvs.width,cvs.height);
  for (let i=0;i<N;i++){{
    ctx.beginPath(); ctx.moveTo(R,R); ctx.fillStyle = i%2 ? PRIMARY : '#ffffff';
    ctx.arc(R,R,R-4, angle+i*arc, angle+(i+1)*arc); ctx.fill();
    ctx.save(); ctx.translate(R,R); ctx.rotate(angle+i*arc+arc/2);
    ctx.fillStyle = i%2 ? '#fff':'#222'; ctx.font='bold 14px sans-serif'; ctx.textAlign='right';
    ctx.fillText(PRIZES[i], R-12, 4); ctx.restore();
  }}
  ctx.beginPath(); ctx.fillStyle='#222'; ctx.moveTo(R-10,4); ctx.lineTo(R+10,4); ctx.lineTo(R,24); ctx.fill();
}}
draw();
document.getElementById('spin').addEventListener('click', () => {{
  if (spinning) return; spinning = true;
  const target = Math.PI*2*5 + Math.random()*Math.PI*2;
  const t0 = performance.now();
  function step(t){{
    const k = Math.min(1,(t-t0)/3000); angle = target * (1 - Math.pow(1-k,3)); draw();
    if (k<1) requestAnimationFrame(step); else {{
      const idx = Math.floor(((Math.PI*2 - (angle % (Math.PI*2))) / (Math.PI*2)) * PRIZES.length) % PRIZES.length;
      spinning = false;
      window.kix.showResult('Result', 'You got: ' + PRIZES[idx]);
    }}
  }}
  requestAnimationFrame(step);
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="spin",
    display_name_en="Spin the Wheel",
    display_name_zh="幸运转盘",
    description_en="Single-prize prize wheel; instant outcome.",
    description_zh="单次抽奖转盘，即时开奖。",
    asset_requirements={
        "required": ["brand_logo", "primary_color", "prize_labels"],
        "optional": ["background_music", "sound_effects", "custom_skin"],
    },
    scoring={"win_threshold": 1, "mode": "instant"},
    recommended_industries=["fnb", "retail", "beauty"],
    completion_seconds=8,
)
TEMPLATE._render = _render
