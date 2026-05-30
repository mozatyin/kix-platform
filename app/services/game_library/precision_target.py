"""Precision target: tap the exact bullseye spot for max points."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#ef4444")
    title = "Precision Target" if not locale.startswith("zh") else "精准靶心"
    body = """
<div id="target" style="position:relative; width:240px; height:240px; border-radius:50%;
  background: radial-gradient(circle, #ef4444 0%, #ef4444 8%, #fff 8%, #fff 22%, #ef4444 22%, #ef4444 36%, #fff 36%, #fff 50%, #ef4444 50%); cursor:crosshair;">
</div>
<div style="margin-top:14px;">Score: <span class="score" id="score">0</span> · Shots: <span id="shots">5</span></div>
"""
    script = f"""
const target=document.getElementById('target');
let score=0, shots=5;
target.addEventListener('click', e=>{{
  if (shots<=0) return;
  const r=target.getBoundingClientRect(); const dx=e.clientX-r.left-120, dy=e.clientY-r.top-120;
  const dist=Math.sqrt(dx*dx+dy*dy);
  const pts = Math.max(0, 100 - Math.floor(dist*0.9));
  score += pts; shots--;
  document.getElementById('score').textContent=score; document.getElementById('shots').textContent=shots;
  if (shots<=0) window.kix.showResult(score>=300?'Sharpshooter!':'Done','Score: '+score);
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="precision_target",
    display_name_en="Precision Target",
    display_name_zh="精准靶心",
    description_en="Tap exact bullseye for max points.",
    description_zh="点击靶心获取最高分。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": []},
    scoring={"win_threshold": 300, "tiers": [{"min_score": 300, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fitness", "retail"],
    completion_seconds=18,
)
TEMPLATE._render = _render
