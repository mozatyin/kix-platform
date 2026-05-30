"""Coffee brewing: slide brew bar to perfect zone."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#6f4e37")
    title = "Coffee Brewing" if not locale.startswith("zh") else "咖啡冲泡"
    body = """
<div style="position:relative; width:280px; max-width:90vw; height:48px; background:#222; border-radius:24px; overflow:hidden;">
  <div style="position:absolute; top:0; bottom:0; left:55%; width:18%; background:#06d6a0;" aria-hidden="true"></div>
  <div id="mark" style="position:absolute; top:0; bottom:0; left:0; width:6px; background:#fff;"></div>
</div>
<div style="margin-top:14px;">Score: <span class="score" id="score">0</span> · Shots: <span id="shots">3</span></div>
<button id="tap" type="button" style="margin-top:14px;">Brew!</button>
<div class="hint">Tap when the marker hits the green zone</div>
"""
    script = f"""
const PRIMARY = {json.dumps(primary)};
let pos=0, dir=1, score=0, shots=3, running=true, raf=null;
const mark=document.getElementById('mark'), scoreEl=document.getElementById('score'), shotsEl=document.getElementById('shots');
function loop(){{ pos+=dir*1.6; if(pos>274){{pos=274;dir=-1;}} if(pos<0){{pos=0;dir=1;}}
  mark.style.left=pos+'px'; if(running) raf=requestAnimationFrame(loop); }}
loop();
document.getElementById('tap').addEventListener('click', ()=>{{
  if(!running) return;
  const inZone = pos >= 154 && pos <= 204;
  if (inZone) score += 50; else score += 5;
  scoreEl.textContent = score; shots--; shotsEl.textContent = shots;
  if (shots<=0){{ running=false; cancelAnimationFrame(raf);
    window.kix.showResult(score>=100?'Perfect Brew!':'Done','Score: '+score); }}
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="coffee_brewing",
    display_name_en="Coffee Brewing",
    display_name_zh="咖啡冲泡",
    description_en="Tap when the marker hits the perfect-brew zone.",
    description_zh="冲泡咖啡——掌握最佳时机点击。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["cup_skin"]},
    scoring={"win_threshold": 100, "tiers": [{"min_score": 100, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fnb", "retail"],
    completion_seconds=18,
)
TEMPLATE._render = _render
