"""Quiz: brand trivia, 3 questions."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#ef476f")
    title = "Brand Quiz" if not locale.startswith("zh") else "品牌问答"
    questions = brand.get("questions") or [
        {"q": "Which color is our brand?", "options": ["Red", "Blue", "Green"], "answer": 0},
        {"q": "When were we founded?", "options": ["2020", "2010", "2005"], "answer": 1},
        {"q": "Our flagship product is?", "options": ["A", "B", "C"], "answer": 2},
    ]
    body = """
<div id="qbox" style="max-width:90vw; width:340px; background:#1a1d27; border-radius:14px; padding:18px; margin-top:10px;"></div>
<div style="margin-top:14px;"><span class="score" id="score">0</span> / <span id="total">3</span></div>
"""
    script = f"""
const Q = {json.dumps(questions)};
const PRIMARY = {json.dumps(primary)};
const qbox = document.getElementById('qbox'); const scoreEl = document.getElementById('score');
document.getElementById('total').textContent = Q.length;
let i=0, score=0;
function render(){{
  if (i>=Q.length) {{
    const won = score >= Math.ceil(Q.length*0.66);
    window.kix.showResult(won?'You Win!':'Try Again', 'Score: '+score+'/'+Q.length); return;
  }}
  const q = Q[i]; qbox.innerHTML = '';
  const h = document.createElement('div'); h.style.cssText='font-weight:700;margin-bottom:12px;'; h.textContent = (i+1)+'. '+q.q; qbox.appendChild(h);
  q.options.forEach((opt, idx) => {{
    const b = document.createElement('button'); b.type='button'; b.textContent=opt;
    b.style.cssText='display:block;width:100%;margin:6px 0;padding:10px;border-radius:8px;border:none;background:'+PRIMARY+';color:#fff;font-weight:600;';
    b.addEventListener('click', () => {{ if (idx===q.answer) score++; scoreEl.textContent=score; i++; render(); }});
    qbox.appendChild(b);
  }});
}}
render();
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="quiz",
    display_name_en="Brand Quiz",
    display_name_zh="品牌问答",
    description_en="Multi-choice trivia about the brand.",
    description_zh="多选品牌知识问答。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["questions"]},
    scoring={"win_threshold": 2, "tiers": [{"min_score": 2, "prize_index": 0, "label": "smart"}]},
    recommended_industries=["education", "fnb", "retail"],
    completion_seconds=30,
)
TEMPLATE._render = _render
