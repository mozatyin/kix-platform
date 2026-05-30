"""Scratch galaxy: 3 scratch-cards in succession."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#5e60ce")
    title = "Scratch Galaxy" if not locale.startswith("zh") else "刮刮星河"
    prizes = [p.get("label", f"P{i+1}") for i, p in enumerate(prize_pool.get("prizes", []))] or ["5%", "10%", "Free"]
    body = """
<div id="cards" style="display:flex; gap:10px; margin-top:8px;"></div>
<div style="margin-top:10px;">Won: <span class="score" id="score">0</span> / 3</div>
<div class="hint">Match 3 same labels to win the jackpot</div>
"""
    style_extra = """
.gcard { position:relative; width:96px; height:128px; border-radius:10px; overflow:hidden; background:#fff; color:#222; }
.gcard .lab { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-weight:800; font-size:18px; }
.gcard canvas { position:absolute; inset:0; touch-action:none; }
"""
    script = f"""
const PRIZES = {json.dumps(prizes)};
const PRIMARY = {json.dumps(primary)};
const cards = document.getElementById('cards');
const won = []; let openedCount=0; const labels=[];
for (let i=0;i<3;i++){{
  const card = document.createElement('div'); card.className='gcard';
  const lab = document.createElement('div'); lab.className='lab';
  const pick = PRIZES[Math.floor(Math.random()*PRIZES.length)]; lab.textContent = pick; labels.push(pick);
  const cvs = document.createElement('canvas'); cvs.width=96; cvs.height=128;
  card.appendChild(lab); card.appendChild(cvs); cards.appendChild(card);
  const ctx = cvs.getContext('2d'); ctx.fillStyle=PRIMARY; ctx.fillRect(0,0,96,128);
  ctx.fillStyle='#fff'; ctx.font='bold 14px sans-serif'; ctx.textAlign='center'; ctx.fillText('SCRATCH', 48, 70);
  let painting=false, area=0, opened=false;
  function pos(e){{ const r=cvs.getBoundingClientRect(); const tt=e.touches?e.touches[0]:e; return [tt.clientX-r.left, tt.clientY-r.top]; }}
  function paint(e){{ if(!painting) return; const [x,y]=pos(e); ctx.globalCompositeOperation='destination-out';
    ctx.beginPath(); ctx.arc(x,y,14,0,Math.PI*2); ctx.fill(); area += 600;
    if (!opened && area > 96*128*0.4) {{ opened=true; openedCount++; document.getElementById('score').textContent=openedCount;
      if (openedCount===3) finish();
    }} e.preventDefault();
  }}
  cvs.addEventListener('mousedown', e=>{{painting=true;paint(e);}});
  cvs.addEventListener('mousemove', paint); cvs.addEventListener('mouseup', ()=>painting=false);
  cvs.addEventListener('touchstart', e=>{{painting=true;paint(e);}}, {{passive:false}});
  cvs.addEventListener('touchmove', paint, {{passive:false}});
  cvs.addEventListener('touchend', ()=>painting=false);
}}
function finish(){{ const jackpot = labels[0]===labels[1] && labels[1]===labels[2];
  setTimeout(()=> window.kix.showResult(jackpot?'JACKPOT!':'Done', jackpot?('3x '+labels[0]+' — top prize!'):('You revealed: '+labels.join(', '))), 300);
}}
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, style_extra=style_extra, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="scratch_galaxy",
    display_name_en="Scratch Galaxy",
    display_name_zh="刮刮星河",
    description_en="Scratch three cards in succession; matching trio = jackpot.",
    description_zh="连刮三卡，三同得大奖。",
    asset_requirements={"required": ["brand_logo", "primary_color", "prize_pool"], "optional": ["card_skin"]},
    scoring={"win_threshold": 1, "tiers": [{"min_score": 1, "prize_index": 0, "label": "win"}, {"min_score": 3, "prize_index": 0, "label": "jackpot"}]},
    recommended_industries=["fnb", "retail"],
    completion_seconds=45,
)
TEMPLATE._render = _render
