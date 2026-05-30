"""Recipe unlocker: solve riddles to reveal recipe steps."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#9333ea")
    title = "Recipe Unlocker" if not locale.startswith("zh") else "解锁食谱"
    body = """
<div id="lock" style="font-size:80px;">🔒</div>
<div id="riddle" class="hint" style="margin-top:10px; min-height:40px;">—</div>
<input id="ans" type="text" placeholder="Answer" style="margin-top:10px; padding:10px; font-size:16px; border-radius:8px; border:none; width:220px;" aria-label="Answer" />
<button id="go" type="button" style="margin-top:10px;">Unlock</button>
<div style="margin-top:10px;">Steps: <span class="score" id="step">0</span> / 3</div>
"""
    script = f"""
const STEPS = [
  {{r:'Yellow, salty, served at breakfast.', a:'egg'}},
  {{r:'Black, hot, energizing morning drink.', a:'coffee'}},
  {{r:'Long, thin, served in soup.', a:'noodle'}}
];
let i=0;
const lock=document.getElementById('lock'), riddle=document.getElementById('riddle'), ans=document.getElementById('ans'), step=document.getElementById('step');
function show(){{ if (i>=STEPS.length){{ window.kix.showResult('Recipe Unlocked!','All '+STEPS.length+' steps'); return; }}
  riddle.textContent = STEPS[i].r; ans.value='';
}}
document.getElementById('go').addEventListener('click', ()=>{{
  if (ans.value.trim().toLowerCase() === STEPS[i].a){{ i++; step.textContent=i; lock.textContent = i>=STEPS.length?'🔓':'🔒'; show(); }}
  else {{ ans.style.outline='2px solid #ef4444'; setTimeout(()=> ans.style.outline='', 600); }}
}});
show();
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="recipe_unlocker",
    display_name_en="Recipe Unlocker",
    display_name_zh="解锁食谱",
    description_en="Solve riddles to reveal a recipe.",
    description_zh="解谜揭示食谱。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["recipe_pack"]},
    scoring={"win_threshold": 3, "tiers": [{"min_score": 3, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["fnb", "education"],
    completion_seconds=40,
)
TEMPLATE._render = _render
