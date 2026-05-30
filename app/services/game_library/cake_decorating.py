"""Cake decorating: paint a cake with brand colors."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#fb7185")
    title = "Cake Decorating" if not locale.startswith("zh") else "蛋糕装饰"
    body = """
<canvas id="cake" width="300" height="220" style="background:#fff7ed; border-radius:14px; touch-action:none;"></canvas>
<div style="margin-top:10px; display:flex; gap:8px;">
  <button class="cl" data-c="#ef476f" type="button" style="background:#ef476f;">Pink</button>
  <button class="cl" data-c="#ffd166" type="button" style="background:#ffd166; color:#222;">Yellow</button>
  <button class="cl" data-c="#06d6a0" type="button" style="background:#06d6a0;">Green</button>
  <button id="finish" type="button">Finish</button>
</div>
<div class="hint" style="margin-top:8px;">Decorate the cake</div>
"""
    script = f"""
const cvs=document.getElementById('cake'), ctx=cvs.getContext('2d');
ctx.fillStyle='#fde68a'; ctx.fillRect(20,60,260,140); // cake body
ctx.fillStyle='#fff'; ctx.fillRect(20,40,260,30); // frosting
let color='#ef476f', painting=false, painted=0;
function pos(e){{ const r=cvs.getBoundingClientRect(); const t=e.touches?e.touches[0]:e; return [t.clientX-r.left, t.clientY-r.top]; }}
function paint(e){{ if(!painting) return; const [x,y]=pos(e);
  ctx.fillStyle=color; ctx.beginPath(); ctx.arc(x,y,10,0,Math.PI*2); ctx.fill(); painted++;
  e.preventDefault();
}}
document.querySelectorAll('.cl').forEach(b=> b.addEventListener('click', ()=> color=b.dataset.c));
cvs.addEventListener('mousedown', e=>{{painting=true; paint(e);}});
cvs.addEventListener('mousemove', paint);
cvs.addEventListener('mouseup', ()=>painting=false);
cvs.addEventListener('touchstart', e=>{{painting=true; paint(e);}}, {{passive:false}});
cvs.addEventListener('touchmove', paint, {{passive:false}});
cvs.addEventListener('touchend', ()=>painting=false);
document.getElementById('finish').addEventListener('click', ()=>{{
  window.kix.showResult(painted>=40?'Beautiful Cake!':'Done','Strokes: '+painted);
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="cake_decorating",
    display_name_en="Cake Decorating",
    display_name_zh="蛋糕装饰",
    description_en="Paint and decorate the cake.",
    description_zh="绘制并装饰蛋糕。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["sprinkle_pack"]},
    scoring={"win_threshold": 40, "tiers": [{"min_score": 40, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fnb", "beauty"],
    completion_seconds=35,
)
TEMPLATE._render = _render
