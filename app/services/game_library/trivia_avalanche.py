"""Trivia avalanche: fast-paced trivia rounds."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#3b82f6")
    title = "Trivia Avalanche" if not locale.startswith("zh") else "知识雪崩"
    body = """
<div id="q" class="hint" style="font-size:16px; min-height:48px;">—</div>
<div id="bar" style="width:280px; height:8px; background:#222; border-radius:4px; overflow:hidden;"><div id="fill" style="height:100%; background:#ef4444; width:100%; transition:width .1s linear;"></div></div>
<div id="choices" style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:12px; width:280px;"></div>
<div style="margin-top:10px;">Correct: <span class="score" id="score">0</span> / 5</div>
"""
    script = f"""
const QS = [
  {{q:'Capital of Singapore?', a:'Singapore', x:['Kuala Lumpur','Bangkok','Jakarta']}},
  {{q:'2+2*3 = ?', a:'8', x:['12','10','6']}},
  {{q:'Fastest land animal?', a:'Cheetah', x:['Lion','Horse','Eagle']}},
  {{q:'Currency of Japan?', a:'Yen', x:['Won','Yuan','Baht']}},
  {{q:'Largest ocean?', a:'Pacific', x:['Atlantic','Indian','Arctic']}}
];
let i=0, score=0, timer=null;
const qEl=document.getElementById('q'), choices=document.getElementById('choices'), scoreEl=document.getElementById('score'), fill=document.getElementById('fill');
function show(){{ if (i>=QS.length){{ window.kix.showResult(score>=4?'Brain Champ!':'Done','Score: '+score); return; }}
  const q=QS[i]; qEl.textContent=q.q;
  const opts=[q.a].concat(q.x).map(o=>({{o,k:Math.random()}})).sort((a,b)=>a.k-b.k).map(x=>x.o);
  choices.innerHTML='';
  opts.forEach(o=>{{ const b=document.createElement('button'); b.type='button'; b.textContent=o;
    b.addEventListener('click', ()=>{{ clearInterval(timer); if (o===q.a){{ score++; scoreEl.textContent=score; }} i++; show(); }});
    choices.appendChild(b);
  }});
  let t=100;
  timer = setInterval(()=>{{ t-=2; fill.style.width=t+'%'; if (t<=0){{ clearInterval(timer); i++; show(); }} }}, 100);
}}
show();
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="trivia_avalanche",
    display_name_en="Trivia Avalanche",
    display_name_zh="知识雪崩",
    description_en="Fast-fire trivia with a 5-second timer.",
    description_zh="5秒倒计时快速问答。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["question_pack"]},
    scoring={"win_threshold": 4, "tiers": [{"min_score": 4, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["education", "fnb", "retail"],
    completion_seconds=30,
)
TEMPLATE._render = _render
