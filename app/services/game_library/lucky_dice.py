"""Lucky dice: roll dice for combinations."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#2a9d8f")
    title = "Lucky Dice" if not locale.startswith("zh") else "幸运骰子"
    body = """
<div id="dice" style="display:flex; gap:14px; margin-top:14px;"></div>
<div style="margin-top:14px;">Total: <span class="score" id="score">—</span></div>
<button id="roll" type="button" style="margin-top:10px;">Roll</button>
<div class="hint">Roll three dice; total 13+ to win, all-same triples = jackpot</div>
"""
    style_extra = """
.die { width:72px; height:72px; background:#fff; color:#222; border-radius:12px; display:flex;
  align-items:center; justify-content:center; font-size:36px; font-weight:900; box-shadow: 0 4px 0 rgba(0,0,0,.3); }
.die.rolling { animation: roll .12s linear infinite; }
@keyframes roll { 0%{transform:rotate(-6deg)} 50%{transform:rotate(6deg)} 100%{transform:rotate(-6deg)} }
"""
    script = f"""
const PRIMARY = {json.dumps(primary)};
const DOTS = ['⚀','⚁','⚂','⚃','⚄','⚅'];
const cont = document.getElementById('dice');
const dice = []; for(let i=0;i<3;i++){{ const d=document.createElement('div'); d.className='die'; d.textContent='?'; cont.appendChild(d); dice.push(d); }}
document.getElementById('roll').addEventListener('click', ()=>{{
  dice.forEach(d => d.classList.add('rolling'));
  const result = [Math.floor(Math.random()*6), Math.floor(Math.random()*6), Math.floor(Math.random()*6)];
  const fl = dice.map(d => setInterval(()=>{{ d.textContent = DOTS[Math.floor(Math.random()*6)]; }}, 80));
  dice.forEach((d,i)=> setTimeout(()=>{{ clearInterval(fl[i]); d.classList.remove('rolling'); d.textContent = DOTS[result[i]];
    if (i===2) {{
      const sum = result[0]+result[1]+result[2]+3;
      document.getElementById('score').textContent = sum;
      const triple = result[0]===result[1] && result[1]===result[2];
      const won = triple || sum>=13;
      window.kix.showResult(triple?'JACKPOT!':(won?'You Win!':'No luck'), 'Total: '+sum);
    }}
  }}, 800 + i*400));
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, style_extra=style_extra, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="lucky_dice",
    display_name_en="Lucky Dice",
    display_name_zh="幸运骰子",
    description_en="Roll three dice for combinations; triples = jackpot.",
    description_zh="掷三颗骰子，三同点为大奖。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["dice_skin", "sound_effects"]},
    scoring={"win_threshold": 13, "tiers": [{"min_score": 13, "prize_index": 0, "label": "win"}, {"min_score": 18, "prize_index": 0, "label": "jackpot"}]},
    recommended_industries=["retail", "fnb"],
    completion_seconds=8,
)
TEMPLATE._render = _render
