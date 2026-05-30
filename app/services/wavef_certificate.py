"""Game-completion certificate — Wave F obvious-win #5.

Inspired by Flarie's creative-content layer: a sharable SVG that proves
"I won at <BrandName> game on <Date> with score N". Zero binary deps —
SVG is hand-built XML that any browser renders crisply at any size.

Use case:
  - User finishes a campaign game with score 1500.
  - Endpoint returns an inline SVG (image/svg+xml) with the brand color,
    player name, score, date, and a short verification code.
  - The verification code is a deterministic short hash so a 3rd party
    can later look up the record server-side.

Inputs come from the request body, not implicit state, so this is fully
testable and stateless.

NEW file.
"""

from __future__ import annotations

import hashlib
import html
from datetime import datetime, timezone, timedelta


_SGT = timezone(timedelta(hours=8))


def _verification_code(*parts: str) -> str:
    """Short, deterministic, URL-safe code derived from inputs."""
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return h[:10].upper()


def render_svg(
    *,
    player_name: str,
    brand_name: str,
    game_name: str,
    score: int,
    primary_color: str = "#1F6FEB",
    accent_color: str = "#FFD23F",
    issued_at: datetime | None = None,
) -> tuple[str, str]:
    """Return (svg_xml, verification_code).

    SVG is 800x500, fits inside a square share card. All inputs are
    HTML-escaped to prevent injection into the XML.
    """
    issued_at = issued_at or datetime.now(_SGT)
    iso = issued_at.strftime("%Y-%m-%d %H:%M")
    code = _verification_code(player_name, brand_name, game_name, str(score), iso)

    # Defensive escaping for XML content.
    e_player = html.escape(player_name, quote=True)
    e_brand = html.escape(brand_name, quote=True)
    e_game = html.escape(game_name, quote=True)
    e_primary = html.escape(primary_color, quote=True)
    e_accent = html.escape(accent_color, quote=True)

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 500" role="img" aria-label="Game completion certificate">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{e_primary}" stop-opacity="0.95"/>
      <stop offset="100%" stop-color="{e_primary}" stop-opacity="0.65"/>
    </linearGradient>
  </defs>
  <rect width="800" height="500" rx="24" fill="url(#bg)"/>
  <rect x="20" y="20" width="760" height="460" rx="16" fill="none" stroke="{e_accent}" stroke-width="3"/>
  <text x="400" y="100" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="36" font-weight="700" fill="#FFFFFF">CERTIFICATE OF VICTORY</text>
  <text x="400" y="140" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="18" fill="#FFFFFF" opacity="0.85">{e_brand}</text>
  <text x="400" y="220" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="22" fill="#FFFFFF" opacity="0.9">awarded to</text>
  <text x="400" y="280" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="48" font-weight="700" fill="{e_accent}">{e_player}</text>
  <text x="400" y="330" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="20" fill="#FFFFFF" opacity="0.9">for completing</text>
  <text x="400" y="370" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="28" font-weight="600" fill="#FFFFFF">{e_game}</text>
  <text x="400" y="420" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="22" fill="{e_accent}">score: {score}</text>
  <text x="400" y="460" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="14" fill="#FFFFFF" opacity="0.7">issued {iso} SGT  ·  verification {code}</text>
</svg>"""
    return svg, code
