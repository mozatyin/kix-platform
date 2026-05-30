"""Spice meter: tap to stop the spice meter at the right level."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#dc2626")
    title = "Spice Meter" if not locale.startswith("zh") else "辣度挑战"
    body = """
<div id="target" class="hint" style="margin-bottom:6px;">Target spice: 🌶️🌶️🌶️</div>
<div style="position:relative; width:280px; height:36px; background:linear-gradient(90deg,#22c55e,#facc15,#dc2626); border-radius:18px; overflow:hidden;">
  <div id="needle" style="position:absolute; top:0; bottom:0; left:0; width:6px; background:#000;"></div>
</div>
<div style="margin-top:14px;">Last: <span id="last">-</span> · Score: <span class="score" id="score">0</span></div>
<button id="tap" type="button" style="margin-top:14px;">Stop!</button>
"""
    script = f"""
let x=0, dir=1, raf, score=0, attempts=0;
const needle=document.getElementById('needle'), lastEl=document.getElementById('last'), scoreEl=document.getElementById('score');
function loop(){{ x += dir*2; if (x>274){{x=274; dir=-1;}} if (x<0){{x=0; dir=1;}}
  needle.style.left = x+'px'; raf=requestAnimationFrame(loop);
}}
loop();
document.getElementById('tap').addEventListener('click', ()=>{{
  attempts++;
  const targetMid = 200; // upper-medium spice
  const dist = Math.abs(x-targetMid);
  const pts = Math.max(0, 50 - dist);
  score += pts; lastEl.textContent = pts; scoreEl.textContent=score;
  if (attempts>=3){{ cancelAnimationFrame(raf);
    window.kix.showResult(score>=100?'Spicy Master!':'Done','Score: '+score); }}
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="spice_meter",
    display_name_en="Spice Meter",
    display_name_zh="辣度挑战",
    description_en="Stop the spice meter at the target level.",
    description_zh="在目标辣度停止仪表。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": []},
    scoring={"win_threshold": 100, "tiers": [{"min_score": 100, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fnb"],
    completion_seconds=15,
)
TEMPLATE._render = _render
