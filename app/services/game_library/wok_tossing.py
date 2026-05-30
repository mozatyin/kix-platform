"""Wok tossing: tap with rhythm to toss food."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#dc2626")
    title = "Wok Tossing" if not locale.startswith("zh") else "颠勺挑战"
    body = """
<div id="wok" style="font-size:90px; transition:transform .15s; line-height:1;">🥘</div>
<div style="margin-top:14px;">Streak: <span class="score" id="streak">0</span> · Best: <span id="best">0</span></div>
<button id="toss" type="button" style="margin-top:14px;">Toss!</button>
<div class="hint">Tap on the beat (every ~700ms)</div>
"""
    script = f"""
const wok=document.getElementById('wok'), streakEl=document.getElementById('streak'), bestEl=document.getElementById('best');
let last=0, streak=0, best=0;
document.getElementById('toss').addEventListener('click', ()=>{{
  const now=Date.now(); const dt = last? now-last : 700;
  const ok = Math.abs(dt-700) < 150;
  if (ok){{ streak++; }} else {{ if (streak>best) best=streak; streak=0; }}
  streakEl.textContent=streak; bestEl.textContent=Math.max(best,streak);
  wok.style.transform='translateY(-30px) rotate(-15deg)';
  setTimeout(()=> wok.style.transform='', 150);
  last = now;
  if (streak>=10){{ window.kix.showResult('Wok Master!','Streak: '+streak); }}
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="wok_tossing",
    display_name_en="Wok Tossing",
    display_name_zh="颠勺挑战",
    description_en="Tap on the beat to toss food.",
    description_zh="按节奏点击颠勺。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["sizzle_sound"]},
    scoring={"win_threshold": 10, "tiers": [{"min_score": 10, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fnb"],
    completion_seconds=15,
)
TEMPLATE._render = _render
