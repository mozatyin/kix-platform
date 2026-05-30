"""Voice shout: mock voice activation by mic volume (falls back to tap)."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#e11d48")
    title = "Shout to Win!" if not locale.startswith("zh") else "大声呼喊"
    body = """
<div id="mic" style="font-size:100px;">🎤</div>
<div id="bar" style="width:280px; height:24px; background:#222; border-radius:12px; margin-top:14px; overflow:hidden;">
  <div id="fill" style="height:100%; background:linear-gradient(90deg,#22c55e,#facc15,#dc2626); width:0%; transition: width .1s;"></div>
</div>
<div style="margin-top:10px;">Level: <span class="score" id="lv">0</span></div>
<button id="start" type="button" style="margin-top:10px;">Start (or tap to simulate)</button>
<div class="hint">Tap to shout (mic optional); reach 80% to win</div>
"""
    script = f"""
let level=0, running=false;
function setLevel(v){{ level = Math.max(level, v); document.getElementById('fill').style.width=level+'%';
  document.getElementById('lv').textContent=Math.round(level);
  if (level>=80 && running){{ running=false; window.kix.showResult('Loud and Clear!','Peak: '+Math.round(level)+'%'); }} }}
document.getElementById('start').addEventListener('click', async ()=>{{
  running=true;
  document.body.addEventListener('click', ()=>{{ if (running) setLevel(level + 8 + Math.random()*10); }});
  try {{
    const stream = await navigator.mediaDevices.getUserMedia({{audio:true}});
    const ctx = new (window.AudioContext||window.webkitAudioContext)();
    const src = ctx.createMediaStreamSource(stream); const an = ctx.createAnalyser(); an.fftSize=256; src.connect(an);
    const data = new Uint8Array(an.frequencyBinCount);
    function tick(){{ if (!running) return; an.getByteFrequencyData(data);
      let sum=0; for (const v of data) sum += v; const avg = sum/data.length;
      setLevel(Math.min(100, avg*0.7));
      requestAnimationFrame(tick);
    }}
    tick();
  }} catch(e){{ /* mic denied; tap fallback */ }}
  setTimeout(()=>{{ if (running){{ running=false; window.kix.showResult(level>=80?'Loud and Clear!':'Done','Peak: '+Math.round(level)+'%'); }} }}, 10000);
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="voice_shout",
    display_name_en="Shout to Win",
    display_name_zh="大声呼喊",
    description_en="Shout loud (or tap) to win.",
    description_zh="大声呼喊（或点击）赢奖。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": []},
    scoring={"win_threshold": 80, "tiers": [{"min_score": 80, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fnb", "retail"],
    completion_seconds=15,
)
TEMPLATE._render = _render
