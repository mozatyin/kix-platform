"""Kopi orders: memory game for Singaporean kopi types."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#92400e")
    title = "Kopi Orders" if not locale.startswith("zh") else "咖啡订单"
    body = """
<div id="show" style="font-size:22px; min-height:36px; margin:6px 0;">Watch the order...</div>
<div id="order" class="hint" style="margin-bottom:10px;">—</div>
<div style="display:grid; grid-template-columns: repeat(2, 130px); gap:8px;">
  <button class="kopi" data-k="Kopi-O" type="button">Kopi-O</button>
  <button class="kopi" data-k="Kopi-C" type="button">Kopi-C</button>
  <button class="kopi" data-k="Kopi Gao" type="button">Kopi Gao</button>
  <button class="kopi" data-k="Kopi Siew Dai" type="button">Kopi Siew Dai</button>
</div>
<div style="margin-top:10px;">Score: <span class="score" id="score">0</span></div>
"""
    style_extra = ".kopi{ background:#7c2d12; }"
    script = f"""
const KOPIS = ['Kopi-O','Kopi-C','Kopi Gao','Kopi Siew Dai'];
let target=[], idx=0, score=0, locked=true;
const show=document.getElementById('show'), order=document.getElementById('order'), scoreEl=document.getElementById('score');
function nextRound(){{ idx=0; target = []; const n = 3 + Math.min(3, Math.floor(score/30));
  for (let i=0;i<n;i++) target.push(KOPIS[Math.floor(Math.random()*KOPIS.length)]);
  order.textContent = target.join(' → '); show.textContent = 'Repeat the order!';
  setTimeout(()=>{{ order.textContent = '—'; locked=false; }}, 2000 + n*400);
}}
document.querySelectorAll('.kopi').forEach(b => b.addEventListener('click', ()=>{{
  if (locked) return;
  if (b.dataset.k === target[idx]){{ idx++; score+=10; scoreEl.textContent=score;
    if (idx>=target.length){{ locked=true; if (score>=60){{ window.kix.showResult('Tao Hwah Master!','Score: '+score); return; }} nextRound(); }}
  }} else {{ locked=true; window.kix.showResult(score>=60?'Tao Hwah Master!':'Wrong order','Score: '+score); }}
}}));
nextRound();
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, style_extra=style_extra, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="kopi_orders",
    display_name_en="Kopi Orders",
    display_name_zh="咖啡订单",
    description_en="Memorize and replay the kopi order.",
    description_zh="记住并重现咖啡订单。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["kopi_jargon_pack"]},
    scoring={"win_threshold": 60, "tiers": [{"min_score": 60, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fnb"],
    completion_seconds=30,
)
TEMPLATE._render = _render
