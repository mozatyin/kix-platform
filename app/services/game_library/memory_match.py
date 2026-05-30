"""Memory match: flip cards to find pairs."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#118ab2")
    title = "Memory Match" if not locale.startswith("zh") else "记忆翻牌"
    icons = brand.get("symbols") or ["★", "♥", "♣", "♦", "■", "●"]
    body = """
<div id="board" style="display:grid; grid-template-columns:repeat(4,68px); grid-gap:8px; margin-top:8px;"></div>
<div style="margin-top:14px;">Moves: <span class="score" id="score">0</span></div>
<div class="hint">Find all pairs in fewer than 18 moves</div>
"""
    style_extra = """
.cell { width:68px; height:68px; border-radius:10px; border:none; font-size:28px; font-weight:900;
  background:var(--brand); color:#fff; cursor:pointer; }
.cell.flip { background:#fff; color:#222; }
.cell.done { opacity:.4; cursor:default; }
"""
    script = f"""
const ICONS = {json.dumps(icons[:6])};
const PRIMARY = {json.dumps(primary)};
const deck = [];
ICONS.forEach(i => {{ deck.push(i); deck.push(i); }});
for (let i=deck.length-1;i>0;i--) {{ const j=Math.floor(Math.random()*(i+1)); [deck[i],deck[j]]=[deck[j],deck[i]]; }}
const board = document.getElementById('board'), scoreEl = document.getElementById('score');
let first=null, moves=0, matched=0, lock=false;
deck.forEach((sym, idx) => {{
  const b = document.createElement('button'); b.type='button'; b.className='cell'; b.dataset.sym=sym; b.dataset.idx=idx;
  b.setAttribute('aria-label', 'card '+(idx+1));
  b.addEventListener('click', () => {{
    if (lock || b.classList.contains('flip') || b.classList.contains('done')) return;
    b.classList.add('flip'); b.textContent = sym;
    if (!first) {{ first = b; return; }}
    moves++; scoreEl.textContent = moves;
    if (first.dataset.sym === sym) {{
      first.classList.add('done'); b.classList.add('done'); matched++; first=null;
      if (matched === ICONS.length) {{ const won = moves <= 18; setTimeout(()=> window.kix.showResult(won?'You Win!':'Done', 'Moves: '+moves), 300); }}
    }} else {{
      lock=true; const a=first; first=null;
      setTimeout(()=>{{ a.classList.remove('flip'); a.textContent=''; b.classList.remove('flip'); b.textContent=''; lock=false; }}, 700);
    }}
  }}); board.appendChild(b);
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, style_extra=style_extra, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="memory_match",
    display_name_en="Memory Match",
    display_name_zh="记忆翻牌",
    description_en="Flip cards to find matching pairs.",
    description_zh="翻牌寻找配对。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["symbols", "card_back"]},
    scoring={"win_threshold": 6, "tiers": [{"min_score": 6, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fnb", "beauty", "education"],
    completion_seconds=40,
)
TEMPLATE._render = _render
