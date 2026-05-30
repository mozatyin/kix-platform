"""Tap speed: most taps in 10 seconds."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#facc15")
    title = "Tap Speed" if not locale.startswith("zh") else "极速点击"
    body = """
<div style="font-size:80px; margin: 12px 0;" aria-hidden="true">⚡</div>
<button id="tap" type="button" style="width:180px; height:180px; border-radius:50%; font-size:24px;">TAP!</button>
<div style="margin-top:14px;">Taps: <span class="score" id="score">0</span> · Time: <span id="time">10</span>s</div>
"""
    script = f"""
let taps=0, t=10, started=false, timer=null;
document.getElementById('tap').addEventListener('click', ()=>{{
  if (!started){{ started=true; timer=setInterval(()=>{{ t--; document.getElementById('time').textContent=t;
    if (t<=0){{ clearInterval(timer); window.kix.showResult(taps>=50?'Speedster!':'Done','Taps: '+taps); }} }}, 1000);
  }}
  if (t<=0) return;
  taps++; document.getElementById('score').textContent=taps;
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="tap_speed",
    display_name_en="Tap Speed",
    display_name_zh="极速点击",
    description_en="Most taps in 10 seconds wins.",
    description_zh="10秒内点击最多者胜。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": []},
    scoring={"win_threshold": 50, "tiers": [{"min_score": 50, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fitness", "retail", "fnb"],
    completion_seconds=12,
)
TEMPLATE._render = _render
