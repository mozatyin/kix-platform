"""Brand-color extraction — Wave F obvious-win #3.

Inspired by Flarie / Playable "upload logo → site auto-themes" pattern.

Strategy (no heavy ML, no scikit dep):
  1. Decode image bytes via Pillow.
  2. Resize to small thumbnail (max 64x64) for speed.
  3. Histogram-quantize to N=8 dominant colors using Pillow's
     ``Image.quantize`` (median-cut), which is built-in.
  4. Sort by pixel-count, drop near-white / near-black backgrounds.
  5. Return the top-K (default 3) as #RRGGBB strings + a recommended
     palette mapping (primary / accent / text-on-primary).

NEW file. No new heavy dependency: Pillow is already in requirements.
"""

from __future__ import annotations

import io
from typing import Sequence

from PIL import Image


def _is_near_white(rgb: tuple[int, int, int], threshold: int = 240) -> bool:
    return all(c >= threshold for c in rgb)


def _is_near_black(rgb: tuple[int, int, int], threshold: int = 20) -> bool:
    return all(c <= threshold for c in rgb)


def _luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    # Perceived brightness (Rec. 709 weights, 0..1)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def extract_palette(
    image_bytes: bytes,
    k: int = 3,
    drop_neutrals: bool = True,
) -> dict:
    """Extract dominant colors from raw image bytes.

    Returns dict::

        {
            "dominant": ["#RRGGBB", ...],   # k colors, ordered by frequency
            "palette": {
                "primary":   "#RRGGBB",
                "accent":    "#RRGGBB",
                "text_on_primary": "#FFFFFF" or "#000000",
            }
        }

    Raises ValueError if the image is invalid.
    """
    if not image_bytes:
        raise ValueError("empty image bytes")
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except Exception as exc:
        raise ValueError(f"could not decode image: {exc}") from exc

    img = img.convert("RGB")
    img.thumbnail((64, 64))

    # Quantize to 8 dominant colors via Pillow's median-cut.
    palette_img = img.quantize(colors=8, method=Image.MEDIANCUT)
    palette_bytes = palette_img.getpalette() or []
    # getcolors() over the palette index image gives (count, idx) pairs.
    counts = palette_img.getcolors(maxcolors=8) or []
    counts.sort(key=lambda x: x[0], reverse=True)

    candidates: list[tuple[int, tuple[int, int, int]]] = []
    for count, idx in counts:
        r = palette_bytes[idx * 3]
        g = palette_bytes[idx * 3 + 1]
        b = palette_bytes[idx * 3 + 2]
        rgb = (r, g, b)
        if drop_neutrals and (_is_near_white(rgb) or _is_near_black(rgb)):
            continue
        candidates.append((count, rgb))

    # Fallback: if we dropped everything, use raw counts.
    if not candidates:
        for count, idx in counts:
            r = palette_bytes[idx * 3]
            g = palette_bytes[idx * 3 + 1]
            b = palette_bytes[idx * 3 + 2]
            candidates.append((count, (r, g, b)))

    top = [rgb for _, rgb in candidates[:k]]
    hex_top = [_hex(rgb) for rgb in top]
    primary = top[0]
    accent = top[1] if len(top) > 1 else top[0]
    text_on_primary = "#FFFFFF" if _luminance(primary) < 0.55 else "#000000"

    return {
        "dominant": hex_top,
        "palette": {
            "primary": _hex(primary),
            "accent": _hex(accent),
            "text_on_primary": text_on_primary,
        },
    }


def palette_for_test_colors(rgbs: Sequence[tuple[int, int, int]]) -> dict:
    """Convenience for unit tests: build palette directly from RGB list.

    Generates a tiny synthetic image of the colors in horizontal stripes
    so the same extract_palette code path runs.
    """
    img = Image.new("RGB", (len(rgbs) * 8, 8))
    pixels = []
    for y in range(8):
        for rgb in rgbs:
            for _ in range(8):
                pixels.append(rgb)
    img.putdata(pixels)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return extract_palette(buf.getvalue(), k=min(3, len(rgbs)))
