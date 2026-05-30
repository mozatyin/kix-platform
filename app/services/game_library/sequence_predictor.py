"""Sequence predictor: guess the next number/symbol in pattern."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#6366f1")
    title = "Predict the Next" if not locale.startswith("zh") else "猜下一个"
    body = """
<div id="seq" style="font-size:28px; letter-spacing:8px; font-weight:700; margin:10px 0;">—</div>
<div id="choices" style="display:flex; gap:8px; flex-wrap:wrap; justify-content:center;"></div>
<div style="margin-top:10px;">Score: <span class="score" id="score">0</span> / 4</div>
"""
    script = f"""
const ROUNDS = [
  {{seq:'2,4,6,8', a:'10', x:['11','12','9']}},
  {{seq:'A,C,E,G', a:'I', x:['H','J','K']}},
  {{seq:'1,1,2,3,5', a:'8', x:['7','9','6']}},
  {{seq:'🔴,🟡,🔴,🟡', a:'🔴', x:['🟢','🔵','🟡']}}
];
let i=0, score=0;
function show(){{ if (i>=ROUNDS.length){{ window.kix.showResult(score>=3?'Pattern Pro!':'Done','Score: '+score); return; }}
  const r=ROUNDS[i]; document.getElementById('seq').textContent = r.seq + ', ?';
  const opts = [r.a].concat(r.x).map(o=>({{o,k:Math.random()}})).sort((a,b)=>a.k-b.k).map(x=>x.o);
  const ch = document.getElementById('choices'); ch.innerHTML='';
  opts.forEach(o=>{{ const b=document.createElement('button'); b.type='button'; b.textContent=o;
    b.addEventListener('click', ()=>{{ if (o===r.a){{ score++; document.getElementById('score').textContent=score; }} i++; show(); }});
    ch.appendChild(b);
  }});
}}
show();
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="sequence_predictor",
    display_name_en="Predict the Next",
    display_name_zh="猜下一个",
    description_en="Guess the next item in the sequence.",
    description_zh="猜出序列中下一项。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": []},
    scoring={"win_threshold": 3, "tiers": [{"min_score": 3, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["education", "retail"],
    completion_seconds=30,
)
TEMPLATE._render = _render
