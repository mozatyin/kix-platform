"""Wheel of fortune: multi-segment weighted wheel (richer than spin)."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#9d4edd")
    title = "Wheel of Fortune" if not locale.startswith("zh") else "幸运转盘"
    raw = prize_pool.get("prizes") or []
    if not raw:
        raw = [{"label": "5%", "weight": 30}, {"label": "10%", "weight": 25},
               {"label": "20%", "weight": 15}, {"label": "Free", "weight": 5},
               {"label": "Try again", "weight": 25}]
    body = """
<canvas id="wheel" width="320" height="320" aria-label="fortune wheel" role="img" style="max-width:90vw"></canvas>
<div style="margin-top:14px;"><button id="spin" type="button">Spin</button></div>
<div class="hint">Higher-value prizes are rarer</div>
"""
    script = f"""
const PRIZES = {json.dumps(raw)};
const PRIMARY = {json.dumps(primary)};
const COLORS = [PRIMARY, '#ffffff', '#222', '#ffd166', '#06d6a0', '#ef476f', '#118ab2'];
const cvs = document.getElementById('wheel'), ctx = cvs.getContext('2d');
const totalW = PRIZES.reduce((s,p)=> s + (p.weight||1), 0);
let angle=0, spinning=false;
function draw(){{
  const R = cvs.width/2; ctx.clearRect(0,0,cvs.width,cvs.height);
  let cur = angle;
  PRIZES.forEach((p,i) => {{
    const a = (p.weight||1)/totalW * Math.PI*2;
    ctx.beginPath(); ctx.moveTo(R,R); ctx.fillStyle = COLORS[i%COLORS.length];
    ctx.arc(R,R,R-4, cur, cur+a); ctx.fill();
    ctx.save(); ctx.translate(R,R); ctx.rotate(cur+a/2);
    ctx.fillStyle = (i%COLORS.length===1)?'#222':'#fff'; ctx.font='bold 13px sans-serif'; ctx.textAlign='right';
    ctx.fillText(p.label, R-12, 4); ctx.restore();
    cur += a;
  }});
  ctx.beginPath(); ctx.fillStyle='#222'; ctx.moveTo(R-10,4); ctx.lineTo(R+10,4); ctx.lineTo(R,24); ctx.fill();
}}
function pickWeighted(){{
  let r = Math.random()*totalW; for (let i=0;i<PRIZES.length;i++){{ r -= (PRIZES[i].weight||1); if (r<=0) return i; }} return PRIZES.length-1;
}}
draw();
document.getElementById('spin').addEventListener('click', ()=>{{
  if(spinning) return; spinning=true;
  const idx = pickWeighted();
  let off=0; for (let i=0;i<=idx;i++){{ off += (PRIZES[i].weight||1)/totalW * Math.PI*2; }}
  off -= (PRIZES[idx].weight||1)/totalW * Math.PI; // center
  const target = Math.PI*2*6 - off;
  const t0 = performance.now();
  function step(t){{ const k=Math.min(1,(t-t0)/3500); angle = target*(1-Math.pow(1-k,3)); draw();
    if(k<1) requestAnimationFrame(step); else {{ spinning=false; window.kix.showResult('Result', 'You got: ' + PRIZES[idx].label); }}
  }}
  requestAnimationFrame(step);
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="wheel_of_fortune",
    display_name_en="Wheel of Fortune",
    display_name_zh="幸运大转盘",
    description_en="Multi-segment weighted prize wheel.",
    description_zh="多段加权幸运大转盘。",
    asset_requirements={
        "required": ["brand_logo", "primary_color", "prize_pool"],
        "optional": ["sound_effects", "custom_skin"],
    },
    scoring={"win_threshold": 1, "mode": "weighted"},
    recommended_industries=["fnb", "retail", "beauty"],
    completion_seconds=12,
)
TEMPLATE._render = _render
