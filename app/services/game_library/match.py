"""Match-3 lite — simple line-match slot row variation."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#06d6a0")
    title = "Match 3" if not locale.startswith("zh") else "三消"
    symbols = brand.get("symbols") or ["A", "B", "C", "D", "E"]
    body = """
<div id="board" style="display:grid; grid-template-columns: repeat(5,56px); grid-gap:6px; margin-top:6px;"></div>
<div style="margin-top:14px;"><span class="score" id="score">0</span></div>
<button id="play" type="button" style="margin-top:10px;">Match!</button>
<div class="hint">Match 3+ adjacent symbols in 30s</div>
"""
    script = f"""
const SYM = {json.dumps(symbols)};
const PRIMARY = {json.dumps(primary)};
const board = document.getElementById('board'); const scoreEl = document.getElementById('score');
const ROWS=5, COLS=5; let grid=[], score=0, sel=null, t0=null, ended=false;
function rnd(){{ return Math.floor(Math.random()*SYM.length); }}
function init(){{ grid=[]; for(let r=0;r<ROWS;r++){{const row=[]; for(let c=0;c<COLS;c++) row.push(rnd()); grid.push(row);}}; render(); }}
function render(){{ board.innerHTML=''; for(let r=0;r<ROWS;r++) for(let c=0;c<COLS;c++){{
  const d=document.createElement('button'); d.type='button'; d.style.cssText='width:56px;height:56px;border-radius:10px;border:none;font-weight:800;font-size:18px;background:'+(sel&&sel[0]===r&&sel[1]===c?'#fff':PRIMARY)+';color:'+(sel&&sel[0]===r&&sel[1]===c?PRIMARY:'#fff');
  d.textContent=SYM[grid[r][c]]; d.setAttribute('aria-label','tile '+r+','+c);
  d.addEventListener('click', ()=> tap(r,c)); board.appendChild(d);
}} }}
function adj(a,b){{ return Math.abs(a[0]-b[0])+Math.abs(a[1]-b[1])===1; }}
function swap(a,b){{ const t=grid[a[0]][a[1]]; grid[a[0]][a[1]]=grid[b[0]][b[1]]; grid[b[0]][b[1]]=t; }}
function clearMatches(){{
  let cleared=0; const mark = Array.from({{length:ROWS}}, ()=>Array(COLS).fill(false));
  for(let r=0;r<ROWS;r++) for(let c=0;c<COLS-2;c++) if(grid[r][c]===grid[r][c+1] && grid[r][c]===grid[r][c+2]) {{ mark[r][c]=mark[r][c+1]=mark[r][c+2]=true; }}
  for(let c=0;c<COLS;c++) for(let r=0;r<ROWS-2;r++) if(grid[r][c]===grid[r+1][c] && grid[r][c]===grid[r+2][c]) {{ mark[r][c]=mark[r+1][c]=mark[r+2][c]=true; }}
  for(let r=0;r<ROWS;r++) for(let c=0;c<COLS;c++) if(mark[r][c]) {{ grid[r][c]=rnd(); cleared++; }}
  return cleared;
}}
function tap(r,c){{ if(ended)return; if(!sel) {{ sel=[r,c]; render(); return; }}
  if (adj(sel,[r,c])) {{ swap(sel,[r,c]); const cleared = clearMatches(); if (!cleared) swap(sel,[r,c]); else score += cleared*10; scoreEl.textContent=score;}}
  sel=null; render();
  while(clearMatches()) {{ score += 5; scoreEl.textContent=score; }}
}}
init();
document.getElementById('play').addEventListener('click', ()=>{{
  if (ended) {{ score=0; scoreEl.textContent=0; ended=false; init(); return; }}
  ended=true; setTimeout(()=>{{ window.kix.showResult('Time up!', 'Score: '+score+(score>=30?' — You Win!':' — Try Again')); }}, 30000);
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="match",
    display_name_en="Match 3",
    display_name_zh="三消",
    description_en="Swap adjacent symbols to make 3+ in a row.",
    description_zh="移动相邻符号，连成三个即得分。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["symbols", "sound_effects"]},
    scoring={"win_threshold": 30, "tiers": [{"min_score": 30, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fnb", "beauty"],
    completion_seconds=35,
)
TEMPLATE._render = _render
