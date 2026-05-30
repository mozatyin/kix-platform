"""Emoji decoder: guess the product or phrase from emojis."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#f97316")
    title = "Emoji Decoder" if not locale.startswith("zh") else "表情解码"
    body = """
<div id="emoji" style="font-size:48px; min-height:60px; margin:10px 0;">—</div>
<input id="guess" type="text" style="padding:10px; font-size:18px; border-radius:8px; border:none; width:240px;" aria-label="Guess" placeholder="Your guess" />
<div style="margin-top:10px; display:flex; gap:8px;">
  <button id="go" type="button">Submit</button>
  <button id="skip" type="button" style="background:#444;">Skip</button>
</div>
<div style="margin-top:10px;">Solved: <span class="score" id="score">0</span> / 4</div>
"""
    script = f"""
const PUZZLES = [
  {{e:'☕🥐', a:'coffee breakfast'}},
  {{e:'🍔🍟', a:'burger fries'}},
  {{e:'🎂🎉', a:'birthday cake'}},
  {{e:'🍕🍕🍕', a:'pizza party'}}
];
let i=0, score=0;
function norm(s){{ return s.toLowerCase().replace(/[^a-z ]/g,'').trim(); }}
function show(){{ if (i>=PUZZLES.length){{ window.kix.showResult(score>=3?'Decoder Pro!':'Done','Score: '+score); return; }}
  document.getElementById('emoji').textContent = PUZZLES[i].e; document.getElementById('guess').value='';
}}
document.getElementById('go').addEventListener('click', ()=>{{
  if (norm(document.getElementById('guess').value) === norm(PUZZLES[i].a)){{ score++; document.getElementById('score').textContent=score; }}
  i++; show();
}});
document.getElementById('skip').addEventListener('click', ()=>{{ i++; show(); }});
show();
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="emoji_decoder",
    display_name_en="Emoji Decoder",
    display_name_zh="表情解码",
    description_en="Guess the phrase from emojis.",
    description_zh="根据表情符号猜短语。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["puzzle_pack"]},
    scoring={"win_threshold": 3, "tiers": [{"min_score": 3, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fnb", "retail", "education"],
    completion_seconds=40,
)
TEMPLATE._render = _render
