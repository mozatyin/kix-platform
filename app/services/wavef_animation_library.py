"""Animation library service — Wave F obvious-win #9.

Inspired by Flarie's "moment of truth" prize-reveal toolkit.

Spec 09 is primarily front-end (CSS keyframes + small JS helpers in
``landing/sdk/animations/`` and ``landing/sdk/kix-fx.js``). To keep the
server side honest we expose a tiny manifest service so the portal can
discover available primitives, advertise default durations, and check
visibility from the static-files mount.

Each animation primitive:
  - id            short slug (e.g. "confetti")
  - css_path      static-files URL of the CSS (under /sdk/animations/)
  - js_entry      JS entry on the global ``KiXFx`` object
  - default_ms    recommended duration
  - reduced_motion_safe  whether the primitive pauses under
                  prefers-reduced-motion

NEW file — no existing module touched.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass


_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
_ANIM_DIR = os.path.join(_REPO_ROOT, "landing", "sdk", "animations")


@dataclass(frozen=True)
class Primitive:
    id: str
    css_path: str
    js_entry: str
    default_ms: int
    reduced_motion_safe: bool
    description: str


_PRIMITIVES: tuple[Primitive, ...] = (
    Primitive(
        id="confetti",
        css_path="/sdk/animations/confetti.css",
        js_entry="confetti",
        default_ms=2400,
        reduced_motion_safe=True,
        description="Canvas-based burst, palette-driven, fades out.",
    ),
    Primitive(
        id="sparkle",
        css_path="/sdk/animations/sparkle.css",
        js_entry="sparkle",
        default_ms=1800,
        reduced_motion_safe=True,
        description="Twinkling trail overlay, density-tunable.",
    ),
    Primitive(
        id="jackpot",
        css_path="/sdk/animations/jackpot.css",
        js_entry="jackpot",
        default_ms=1500,
        reduced_motion_safe=True,
        description="Zoom-in + golden flash on a reward element.",
    ),
    Primitive(
        id="slot-roll",
        css_path="/sdk/animations/slot-roll.css",
        js_entry="slotRoll",
        default_ms=2000,
        reduced_motion_safe=True,
        description="Slot-machine roll terminating on a final label.",
    ),
)


def list_primitives() -> list[dict]:
    """All registered animation primitives as plain dicts."""
    return [asdict(p) for p in _PRIMITIVES]


def get_primitive(pid: str) -> dict | None:
    for p in _PRIMITIVES:
        if p.id == pid:
            return asdict(p)
    return None


def assets_dir() -> str:
    """Filesystem path of the CSS/JS assets shipped with the SDK."""
    return _ANIM_DIR


def asset_exists(pid: str) -> bool:
    """Check the corresponding .css file is actually shipped."""
    p = get_primitive(pid)
    if not p:
        return False
    return os.path.isfile(os.path.join(_ANIM_DIR, f"{pid}.css"))


def fx_js_exists() -> bool:
    """Check the small ``kix-fx.js`` shim file is present."""
    return os.path.isfile(
        os.path.join(_REPO_ROOT, "landing", "sdk", "kix-fx.js")
    )


def palette_for(brand_primary: str | None) -> list[str]:
    """Recommend a confetti palette derived from a brand-primary hex.

    Falls back to a generic 4-colour palette when no brand hex is given.
    Tiny helper so Spec 03 (brand colour) and Spec 09 (animations)
    compose cleanly per spec §31.
    """
    fallback = ["#ff6b6b", "#ffd93d", "#6bcf7f", "#4dabf7"]
    if not brand_primary or not brand_primary.startswith("#"):
        return fallback
    h = brand_primary.lstrip("#")
    if len(h) != 6 or any(c not in "0123456789abcdefABCDEF" for c in h):
        return fallback
    # Build a palette by lightening + darkening the brand colour.
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    def _clip(v: int) -> int:
        return max(0, min(255, v))

    def _hex(r: int, g: int, b: int) -> str:
        return f"#{r:02x}{g:02x}{b:02x}"

    return [
        brand_primary,
        _hex(_clip(r + 60), _clip(g + 60), _clip(b + 60)),
        _hex(_clip(r - 50), _clip(g - 50), _clip(b - 50)),
        "#ffffff",
    ]
