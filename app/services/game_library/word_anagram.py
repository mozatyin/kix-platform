"""Word anagram: unscramble a brand word."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#a855f7")
    title = "Anagram" if not locale.startswith("zh") else "字母重组"
    body = """
<div id="scrambled" style="font-size:32px; letter-spacing:8px; font-weight:800; margin:10px 0;">—</div>
<input id="guess" type="text" style="padding:10px; font-size:18px; border-radius:8px; border:none; width:220px; text-transform:uppercase;" aria-label="Guess" />
<button id="go" type="button" style="margin-top:10px;">Submit</button>
<div style="margin-top:10px;">Solved: <span class="score" id="score">0</span> / 3</div>
"""
    script = f"""
const WORDS = ['REWARD','BRAND','LOYAL'];
function scramble(w){{ const arr = w.split(''); for (let i=arr.length-1;i>0;i--){{ const j=Math.floor(Math.random()*(i+1)); [arr[i],arr[j]]=[arr[j],arr[i]]; }} return arr.join(''); }}
let i=0, score=0;
const scrambled=document.getElementById('scrambled'), guess=document.getElementById('guess'), scoreEl=document.getElementById('score');
function show(){{ if (i>=WORDS.length){{ window.kix.showResult(score>=3?'Word Wizard!':'Done','Score: '+score); return; }}
  let s = scramble(WORDS[i]); while (s === WORDS[i]) s = scramble(WORDS[i]); scrambled.textContent = s; guess.value='';
}}
document.getElementById('go').addEventListener('click', ()=>{{
  if (guess.value.trim().toUpperCase() === WORDS[i]){{ score++; scoreEl.textContent=score; }}
  i++; show();
}});
show();
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="word_anagram",
    display_name_en="Anagram",
    display_name_zh="字母重组",
    description_en="Unscramble brand-themed words.",
    description_zh="重组品牌词字母。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["word_list"]},
    scoring={"win_threshold": 3, "tiers": [{"min_score": 3, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["education", "retail"],
    completion_seconds=40,
)
TEMPLATE._render = _render
