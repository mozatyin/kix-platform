"""Word chain: enter a word starting with the last letter of the previous."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#14b8a6")
    title = "Word Chain" if not locale.startswith("zh") else "词语接龙"
    body = """
<div id="prev" style="font-size:24px; font-weight:800;">CAT</div>
<div class="hint">Type a word starting with the last letter</div>
<input id="word" type="text" style="padding:10px; font-size:18px; border-radius:8px; border:none; width:240px; text-transform:uppercase;" aria-label="Word" />
<button id="go" type="button" style="margin-top:10px;">Add</button>
<div style="margin-top:10px;">Chain: <span class="score" id="score">1</span></div>
"""
    script = f"""
const used = new Set(['CAT']);
let prev = 'CAT', chain=1;
document.getElementById('go').addEventListener('click', ()=>{{
  const w = document.getElementById('word').value.trim().toUpperCase();
  if (w.length<3){{ alert('At least 3 letters'); return; }}
  if (used.has(w)){{ alert('Already used'); return; }}
  if (w[0] !== prev[prev.length-1]){{ alert('Must start with '+prev[prev.length-1]); return; }}
  used.add(w); prev=w; chain++;
  document.getElementById('prev').textContent = w;
  document.getElementById('score').textContent = chain;
  document.getElementById('word').value='';
  if (chain>=8) window.kix.showResult('Chain Master!','Length: '+chain);
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="word_chain",
    display_name_en="Word Chain",
    display_name_zh="词语接龙",
    description_en="Chain words by last letter.",
    description_zh="按末尾字母接龙。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": []},
    scoring={"win_threshold": 8, "tiers": [{"min_score": 8, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["education"],
    completion_seconds=45,
)
TEMPLATE._render = _render
