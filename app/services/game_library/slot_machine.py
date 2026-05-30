"""Slot machine: 3-reel slot, win on matching symbols."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#e63946")
    title = "Lucky Slots" if not locale.startswith("zh") else "幸运老虎机"
    symbols = brand.get("symbols") or ["7", "★", "♦", "♣", "♥", "$"]
    body = """
<div id="reels" style="display:flex; gap:8px; margin-top:8px; background:#1a1d27; padding:14px; border-radius:14px;">
  <div class="reel" id="r0">?</div><div class="reel" id="r1">?</div><div class="reel" id="r2">?</div>
</div>
<div style="margin-top:14px;"><button id="spin" type="button">SPIN</button></div>
<div class="hint">Match 3 symbols to win</div>
"""
    style_extra = """
.reel { width:72px; height:96px; background:#fff; color:#222; border-radius:10px; display:flex; align-items:center;
  justify-content:center; font-size:42px; font-weight:900; font-family: monospace; }
.reel.spin { animation: rspin .08s linear infinite; }
@keyframes rspin { 0%{transform:translateY(0)} 100%{transform:translateY(-6px)} }
"""
    script = f"""
const SYM = {json.dumps(symbols)};
const PRIMARY = {json.dumps(primary)};
const reels = [document.getElementById('r0'), document.getElementById('r1'), document.getElementById('r2')];
let spinning=false;
document.getElementById('spin').addEventListener('click', ()=>{{
  if (spinning) return; spinning=true;
  reels.forEach(r => r.classList.add('spin'));
  const result = reels.map(()=> SYM[Math.floor(Math.random()*SYM.length)]);
  const flickers = [];
  reels.forEach((r,i)=>{{ flickers.push(setInterval(()=>{{ r.textContent = SYM[Math.floor(Math.random()*SYM.length)]; }}, 60)); }});
  reels.forEach((r,i)=> setTimeout(()=>{{ clearInterval(flickers[i]); r.classList.remove('spin'); r.textContent = result[i];
    if (i===2) {{ spinning=false; const win = (result[0]===result[1] && result[1]===result[2]);
      window.kix.showResult(win?'JACKPOT!':'So close!', win?'You won the prize!':'Try again');
    }} }}, 1000 + i*600));
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, style_extra=style_extra, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="slot_machine",
    display_name_en="Slot Machine",
    display_name_zh="老虎机",
    description_en="3-reel slot machine; win on three matching symbols.",
    description_zh="三轴老虎机，三个相同符号获奖。",
    asset_requirements={
        "required": ["brand_logo", "primary_color", "prize_pool"],
        "optional": ["symbols", "sound_effects", "background_music"],
    },
    scoring={"win_threshold": 1, "tiers": [{"min_score": 1, "prize_index": 0, "label": "jackpot"}]},
    recommended_industries=["retail", "fnb"],
    completion_seconds=10,
)
TEMPLATE._render = _render
