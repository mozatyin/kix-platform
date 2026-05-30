"""Menu quiz: which dish has ingredient X?"""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#d97706")
    title = "Menu Quiz" if not locale.startswith("zh") else "菜单问答"
    body = """
<div id="q" class="hint" style="font-size:16px; min-height:40px;">—</div>
<div id="choices" style="display:flex; flex-direction:column; gap:8px; width:260px; margin-top:10px;"></div>
<div style="margin-top:12px;">Score: <span class="score" id="score">0</span> / 5</div>
"""
    script = f"""
const QS = [
  {{q:'Which dish uses laksa leaves?', a:'Laksa', x:['Char Kway Teow','Hainanese Chicken Rice','Roti Prata']}},
  {{q:'Which dish features coconut milk?', a:'Nasi Lemak', x:['Bak Kut Teh','Yong Tau Foo','Wanton Mee']}},
  {{q:'Which dessert uses pandan?', a:'Pandan Cake', x:['Tiramisu','Mochi','Brownie']}},
  {{q:'Which dish has chili crab sauce?', a:'Chili Crab', x:['Pepper Crab','Salted Egg Squid','Sambal Stingray']}},
  {{q:'Which uses prawn paste?', a:'Har Cheong Gai', x:['Curry Puff','Carrot Cake','Popiah']}}
];
let i=0, score=0;
const qEl=document.getElementById('q'), choices=document.getElementById('choices'), scoreEl=document.getElementById('score');
function show(){{ if (i>=QS.length){{ window.kix.showResult(score>=4?'Foodie!':'Done','Score: '+score+'/5'); return; }}
  const q=QS[i]; qEl.textContent=q.q;
  const opts=[q.a].concat(q.x).map(o=>({{o,k:Math.random()}})).sort((a,b)=>a.k-b.k).map(x=>x.o);
  choices.innerHTML='';
  opts.forEach(o=>{{ const b=document.createElement('button'); b.type='button'; b.textContent=o;
    b.addEventListener('click', ()=>{{ if (o===q.a){{ score++; scoreEl.textContent=score; }} i++; show(); }});
    choices.appendChild(b);
  }});
}}
show();
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="menu_quiz",
    display_name_en="Menu Quiz",
    display_name_zh="菜单问答",
    description_en="Guess the dish from its ingredient.",
    description_zh="根据食材猜出菜名。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["question_pack"]},
    scoring={"win_threshold": 4, "tiers": [{"min_score": 4, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fnb", "education"],
    completion_seconds=30,
)
TEMPLATE._render = _render
