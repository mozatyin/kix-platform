"""Mini crossword: 3-word clue-based fill-in."""

from __future__ import annotations

import json

from .base import GameTemplate, _color, render_skeleton


def _render(brand: dict, prize_pool: dict, locale: str) -> str:
    primary = _color(brand.get("primary_color"), "#0f766e")
    title = "Mini Crossword" if not locale.startswith("zh") else "迷你填字"
    body = """
<div id="clues" class="hint" style="text-align:left; max-width:300px;">
  1A: Hot drink (3) · 2A: Sweet topping (5) · 3A: Loyalty perk (5)
</div>
<div style="display:flex; flex-direction:column; gap:6px; margin-top:10px;">
  <input class="cw" data-a="TEA" maxlength="3" placeholder="1A" style="text-transform:uppercase;" aria-label="1A" />
  <input class="cw" data-a="HONEY" maxlength="5" placeholder="2A" style="text-transform:uppercase;" aria-label="2A" />
  <input class="cw" data-a="POINT" maxlength="5" placeholder="3A" style="text-transform:uppercase;" aria-label="3A" />
</div>
<button id="check" type="button" style="margin-top:12px;">Check</button>
<div style="margin-top:10px;">Correct: <span class="score" id="score">0</span> / 3</div>
"""
    style_extra = ".cw{ padding:10px; font-size:18px; border-radius:8px; border:none; width:220px; }"
    script = f"""
document.getElementById('check').addEventListener('click', ()=>{{
  let ok=0;
  document.querySelectorAll('.cw').forEach(i=>{{ if (i.value.trim().toUpperCase() === i.dataset.a){{ ok++; i.style.background='#86efac'; }} else {{ i.style.background='#fecaca'; }} }});
  document.getElementById('score').textContent = ok;
  if (ok===3) window.kix.showResult('Crossword Champ!','3/3 correct');
}});
"""
    return render_skeleton(
        title=title, locale=locale, primary=primary,
        body_html=body, script=script, style_extra=style_extra, brand_logo=brand.get("logo_url"),
    )


TEMPLATE = GameTemplate(
    type_name="crossword_mini",
    display_name_en="Mini Crossword",
    display_name_zh="迷你填字",
    description_en="Solve a 3-clue mini crossword.",
    description_zh="解开3个迷你填字题。",
    asset_requirements={"required": ["brand_logo", "primary_color"], "optional": ["clue_pack"]},
    scoring={"win_threshold": 3, "tiers": [{"min_score": 3, "prize_index": 0, "label": "winner"}]},
    recommended_industries=["education", "retail"],
    completion_seconds=45,
)
TEMPLATE._render = _render
