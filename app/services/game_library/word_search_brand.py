"""Word search: find brand-themed words in a letter grid."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#7c3aed")
    title = "Word Search" if not locale.startswith("zh") else "找单词"
    body = """
<div id="words" class="hint">Find: BRAND · GIFT · WIN</div>
<div id="grid" style="display:grid; grid-template-columns: repeat(8, 32px); gap:2px; margin-top:10px;"></div>
<div style="margin-top:10px;">Found: <span class="score" id="score">0</span> / 3</div>
"""
    style_extra = (
        ".cell{ width:32px; height:32px; background:#1f2937; display:flex; align-items:center; justify-content:center; "
        "font-weight:700; cursor:pointer; user-select:none; border-radius:4px; font-size:14px; }"
        ".cell.sel{ background:#7c3aed; } .cell.found{ background:#10b981; }"
    )
    script = f"""
const WORDS = ['BRAND','GIFT','WIN'];
const COLS=8, ROWS=8;
const grid = Array.from({{length:ROWS}}, ()=> Array.from({{length:COLS}}, ()=> String.fromCharCode(65+Math.floor(Math.random()*26))));
// place words horizontally
WORDS.forEach((w,wi)=>{{ const r = wi*2+1, c = 1; for (let i=0;i<w.length;i++) grid[r][c+i] = w[i]; }});
const board = document.getElementById('grid');
const cells = [];
for (let r=0;r<ROWS;r++) for (let c=0;c<COLS;c++){{
  const d=document.createElement('div'); d.className='cell'; d.textContent=grid[r][c]; d.dataset.r=r; d.dataset.c=c;
  board.appendChild(d); cells.push(d);
}}
let selecting=false, sel=[];
function clearSel(){{ sel.forEach(d=>d.classList.remove('sel')); sel=[]; }}
function selWord(){{ return sel.map(d=>d.textContent).join(''); }}
function startSel(d){{ selecting=true; clearSel(); d.classList.add('sel'); sel.push(d); }}
function extendSel(d){{ if (!selecting) return; if (sel.includes(d)) return; const last=sel[sel.length-1];
  if (+d.dataset.r === +last.dataset.r){{ d.classList.add('sel'); sel.push(d); }} }}
function endSel(){{ selecting=false; const w = selWord(); if (WORDS.includes(w) && !sel[0].classList.contains('found')){{
    sel.forEach(d=>d.classList.add('found')); const sc = document.getElementById('score'); sc.textContent = (+sc.textContent)+1;
    if (+sc.textContent >= WORDS.length) window.kix.showResult('All Found!','Words: '+WORDS.length);
  }}
  clearSel();
}}
cells.forEach(d=>{{
  d.addEventListener('mousedown', ()=>startSel(d));
  d.addEventListener('mouseenter', ()=>extendSel(d));
  d.addEventListener('mouseup', endSel);
  d.addEventListener('touchstart', ()=>startSel(d), {{passive:true}});
  d.addEventListener('touchmove', e=>{{ const t=e.touches[0]; const el=document.elementFromPoint(t.clientX,t.clientY); if (el && el.classList.contains('cell')) extendSel(el); }}, {{passive:true}});
  d.addEventListener('touchend', endSel);
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, style_extra=style_extra, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="word_search_brand",
    display_name_en="Word Search",
    display_name_zh="找单词",
    description_en="Find brand-themed words in the grid.",
    description_zh="在字母矩阵中找到品牌词。",
    asset_requirements={"required": ["brand_logo", "primary_color", "word_list"], "optional": []},
    scoring={"win_threshold": 3, "tiers": [{"min_score": 3, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["retail", "education"],
    completion_seconds=45,
)
TEMPLATE._render = _render
