"""Pizza topping: choose toppings before the timer runs out."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#ef4444")
    title = "Pizza Topping" if not locale.startswith("zh") else "披萨配料"
    body = """
<div id="pizza" style="position:relative; width:260px; height:260px; border-radius:50%; background:radial-gradient(circle,#f4a261 60%, #c1440e 100%); box-shadow: inset 0 0 14px #6b2a0a;"></div>
<div style="margin-top:10px; display:flex; gap:6px; flex-wrap:wrap; justify-content:center; max-width:320px;">
  <button class="top" data-e="🍄" type="button">🍄</button>
  <button class="top" data-e="🫑" type="button">🫑</button>
  <button class="top" data-e="🧀" type="button">🧀</button>
  <button class="top" data-e="🍖" type="button">🍖</button>
  <button class="top" data-e="🥓" type="button">🥓</button>
  <button class="top" data-e="🍍" type="button">🍍</button>
</div>
<div style="margin-top:10px;">Toppings: <span class="score" id="score">0</span> · Time: <span id="time">15</span>s</div>
"""
    style_extra = ".top { font-size:24px; padding:8px 12px; } .placed{position:absolute; font-size:28px; pointer-events:none;}"
    script = f"""
const pizza=document.getElementById('pizza'), scoreEl=document.getElementById('score'), timeEl=document.getElementById('time');
let score=0, t=15;
const it = setInterval(()=>{{ t--; timeEl.textContent=t; if(t<=0){{ clearInterval(it); end(); }} }}, 1000);
document.querySelectorAll('.top').forEach(b => b.addEventListener('click', ()=>{{
  if (t<=0) return;
  const x = 30+Math.random()*190, y = 30+Math.random()*190;
  const el=document.createElement('div'); el.className='placed';
  el.style.left=x+'px'; el.style.top=y+'px'; el.textContent=b.dataset.e;
  pizza.appendChild(el); score++; scoreEl.textContent=score;
}}));
function end(){{ window.kix.showResult(score>=12?'Delicioso!':'Done','Toppings: '+score); }}
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, style_extra=style_extra, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="pizza_topping",
    display_name_en="Pizza Topping",
    display_name_zh="披萨配料",
    description_en="Pile on toppings before time runs out.",
    description_zh="在时间结束前堆满配料。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["topping_skins"]},
    scoring={"win_threshold": 12, "tiers": [{"min_score": 12, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fnb"],
    completion_seconds=20,
)
TEMPLATE._render = _render
