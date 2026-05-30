"""Base interface for game library templates.

Every game type module in `app.services.game_library` exports a
`GameTemplate` instance that conforms to this contract. The renderer
produces self-contained HTML5 (no external JS deps) that:

* respects locale (en-SG, zh-Hans-SG, ar-SG, ...)
* injects brand assets (logo, primary color, prize images)
* runs offline (mobile-first, WCAG AA, 5-60s playtime)
* exposes deterministic win calculation given prize pool

This module is consumed by `app.services.game_library.__init__`
(`GAME_LIBRARY`, `get_template`, `list_templates`,
`recommend_for_brand`) and rendered by demo/landing pages or the
Recipe Generator.

Pure-Python; no LLM calls, no I/O.
"""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Locale helpers
# ---------------------------------------------------------------------------

RTL_LOCALES = {"ar", "ar-SG", "he", "fa", "ur"}


def _is_rtl(locale: str) -> bool:
    if not locale:
        return False
    return locale.split("-")[0] in {l.split("-")[0] for l in RTL_LOCALES}


def _lang_tag(locale: str | None) -> str:
    return (locale or "en-SG").strip() or "en-SG"


def _safe(text: Any) -> str:
    """HTML-escape arbitrary text."""
    return html.escape(str(text), quote=True)


def _color(value: str | None, default: str = "#FF4081") -> str:
    """Validate a CSS color (hex only, fallback)."""
    if not value:
        return default
    v = value.strip()
    if v.startswith("#") and len(v) in (4, 7) and all(c in "0123456789abcdefABCDEF" for c in v[1:]):
        return v
    return default


def _icu(text: str, params: dict[str, Any] | None = None) -> str:
    """Minimal ICU placeholder replacement: {name} -> value."""
    if not params:
        return text
    out = text
    for k, v in params.items():
        out = out.replace("{" + str(k) + "}", _safe(v))
    return out


def _stable_seed(brand_id: str, salt: str = "") -> int:
    """Deterministic int seed from brand_id."""
    raw = f"{brand_id}|{salt}".encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:8], 16)


# ---------------------------------------------------------------------------
# Game template
# ---------------------------------------------------------------------------

@dataclass
class GameTemplate:
    """Declarative game template.

    Stateless – `generate_html` is a pure function of inputs.
    """

    type_name: str
    display_name_en: str
    display_name_zh: str
    description_en: str
    description_zh: str
    asset_requirements: dict = field(default_factory=dict)
    scoring: dict = field(default_factory=dict)
    recommended_industries: list[str] = field(default_factory=list)
    completion_seconds: int = 20  # midpoint of 5-60
    difficulties: list[str] = field(default_factory=lambda: ["easy", "medium", "hard"])

    # injected renderer – takes (brand_assets, prize_pool, locale)
    _render: Callable[[dict, dict, str], str] | None = None

    # ------------------------------------------------------------------
    # Interface
    # ------------------------------------------------------------------
    def generate_html(
        self,
        brand_assets: dict | None = None,
        prize_pool: dict | None = None,
        locale: str = "en-SG",
    ) -> str:
        brand_assets = brand_assets or {}
        prize_pool = prize_pool or {"prizes": []}
        if self._render is None:
            raise NotImplementedError(f"renderer missing for {self.type_name}")
        return self._render(brand_assets, prize_pool, locale)

    def calculate_win(self, score: int | float, prize_pool: dict | None = None) -> dict:
        """Deterministic win calculation.

        Strategy: score >= win_threshold -> highest available prize.
        Tier-based: scoring.tiers = [{min_score, prize_index, label}].
        """
        prize_pool = prize_pool or {"prizes": []}
        prizes = prize_pool.get("prizes") or []
        tiers = self.scoring.get("tiers") or []
        # default tier: score>=win_threshold
        threshold = self.scoring.get("win_threshold", 1)
        if tiers:
            # pick highest tier whose min_score <= score
            best = None
            for tier in sorted(tiers, key=lambda t: t.get("min_score", 0)):
                if score >= tier.get("min_score", 0):
                    best = tier
            if best is None:
                return {"won": False, "score": score, "prize": None, "tier": None}
            idx = best.get("prize_index", 0)
            prize = prizes[idx] if 0 <= idx < len(prizes) else None
            return {
                "won": prize is not None,
                "score": score,
                "prize": prize,
                "tier": best.get("label", f"tier_{idx}"),
            }
        if score >= threshold and prizes:
            return {"won": True, "score": score, "prize": prizes[0], "tier": "win"}
        return {"won": False, "score": score, "prize": None, "tier": None}

    def estimate_completion_time(self) -> int:
        return int(self.completion_seconds)

    def difficulty_levels(self) -> list[str]:
        return list(self.difficulties)

    # ------------------------------------------------------------------
    def metadata(self) -> dict:
        """JSON-safe metadata for catalog endpoints."""
        return {
            "type_name": self.type_name,
            "display_name": {
                "en": self.display_name_en,
                "zh": self.display_name_zh,
            },
            "description": {
                "en": self.description_en,
                "zh": self.description_zh,
            },
            "asset_requirements": self.asset_requirements,
            "scoring": self.scoring,
            "recommended_industries": self.recommended_industries,
            "completion_seconds": self.completion_seconds,
            "difficulties": self.difficulties,
        }


# ---------------------------------------------------------------------------
# HTML skeleton helper (shared)
# ---------------------------------------------------------------------------

def render_skeleton(
    *,
    title: str,
    locale: str,
    primary: str,
    body_html: str,
    script: str,
    style_extra: str = "",
    brand_logo: str | None = None,
) -> str:
    """Produce a complete, self-contained, mobile-first HTML5 page.

    No external dependencies; WCAG AA color contrast & focus styles.
    """
    rtl = _is_rtl(locale)
    dir_attr = "rtl" if rtl else "ltr"
    lang = _lang_tag(locale)
    logo_html = (
        f'<img class="brand-logo" src="{_safe(brand_logo)}" alt="logo" />'
        if brand_logo
        else ""
    )
    return f"""<!doctype html>
<html lang="{_safe(lang)}" dir="{dir_attr}">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no" />
<meta name="theme-color" content="{primary}" />
<title>{_safe(title)}</title>
<style>
:root {{ --brand: {primary}; --bg: #0b0d12; --fg: #fff; --muted: #c9c9d1; }}
* {{ box-sizing: border-box; }}
html, body {{ margin:0; padding:0; height:100%; background:var(--bg); color:var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans SC", sans-serif;
  overflow:hidden; touch-action: manipulation; }}
.app {{ position:relative; width:100vw; height:100vh; display:flex; flex-direction:column;
  align-items:center; justify-content:flex-start; padding: 12px; }}
.brand-logo {{ max-height:40px; margin: 6px 0 12px; }}
.title {{ font-size:18px; font-weight:700; margin: 4px 0 8px; text-align:center; }}
.hint {{ font-size:13px; color: var(--muted); margin-bottom: 6px; text-align:center; }}
.score {{ font-weight:700; font-size: 16px; color: var(--brand); }}
.cta, button {{ background: var(--brand); color:#fff; border:none; border-radius:999px;
  padding: 12px 22px; font-size:16px; font-weight:700; cursor:pointer; min-width:44px; min-height:44px; }}
.cta:focus-visible, button:focus-visible {{ outline: 3px solid #fff; outline-offset: 2px; }}
.overlay {{ position: fixed; inset: 0; background: rgba(0,0,0,.6); display:none;
  align-items:center; justify-content:center; flex-direction:column; padding:24px; z-index: 99; }}
.overlay.show {{ display:flex; }}
.card {{ background:#fff; color:#222; border-radius:16px; padding:20px; max-width:320px; text-align:center; }}
@media (prefers-reduced-motion: reduce) {{ * {{ animation: none !important; transition: none !important; }} }}
{style_extra}
</style>
</head>
<body>
<div class="app" id="app">
  {logo_html}
  <div class="title">{_safe(title)}</div>
  {body_html}
</div>
<div class="overlay" id="overlay" role="dialog" aria-modal="true" aria-live="polite">
  <div class="card">
    <div id="result-title" style="font-size:20px; font-weight:800; margin-bottom:8px;">—</div>
    <div id="result-body" style="margin-bottom:16px;">—</div>
    <button id="result-close" class="cta" type="button">OK</button>
  </div>
</div>
<script>
(function(){{
  const RTL = {str(rtl).lower()};
  const LOCALE = {json.dumps(lang)};
  function showResult(title, body){{
    document.getElementById('result-title').textContent = title;
    document.getElementById('result-body').textContent = body;
    document.getElementById('overlay').classList.add('show');
  }}
  document.getElementById('result-close').addEventListener('click', () => {{
    document.getElementById('overlay').classList.remove('show');
  }});
  window.kix = {{ showResult, RTL, LOCALE }};
}})();
{script}
</script>
</body>
</html>"""
