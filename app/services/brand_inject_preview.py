"""Gap F — client-side brand-preview overlay (Wave M-3 / "fix the machine").

PURE function: takes a generated game HTML + brand assets, returns the HTML
with brand assets injected via CSS variables + DOM replacements. No backend
round-trip — designed to be called both server-side (to write final HTML to
disk) AND client-side (via the same code transliterated to JS for instant
preview).

Why this matters: today a merchant iterates "change my logo / voucher copy
/ primary color" by triggering a full creative_gen round-trip (~60s).
With this preview overlay, the iterate loop drops to <1s — they tweak,
hit "preview", instant.

This module is the SERVER-side / Python implementation. The client-side
TS twin lives in landing/sdk/brand-preview-overlay.ts (built next).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ── Brand asset model ──

@dataclass
class BrandAssets:
    """Slim brand-asset bundle used by the preview overlay.

    All fields optional; only the ones provided are injected.
    """
    brand_id: str
    logo_url: Optional[str] = None
    logo_alt: Optional[str] = None
    primary_color: Optional[str] = None      # e.g. "#7C2D12"
    accent_color: Optional[str] = None       # e.g. "#FBBF24"
    voucher_copy: Optional[str] = None       # e.g. "Free Kopi-O"
    voucher_code: Optional[str] = None       # e.g. "DEMO-2026"
    brand_name: Optional[str] = None
    background_image_url: Optional[str] = None
    # Extra custom slots: {"slot_id": "html-fragment"}
    custom_slots: dict[str, str] = field(default_factory=dict)


# ── CSS variable injection ──

_CSS_VAR_TEMPLATE = """
<style id="kix-brand-vars">
:root {
__VARS__
}
.kix-brand-logo {
  background-image: var(--kix-brand-logo);
}
.kix-brand-bg {
  background-image: var(--kix-brand-bg);
}
</style>
"""


def _build_css_vars(assets: BrandAssets) -> str:
    """Build the :root CSS variable block. Returns "" if no vars to inject."""
    vars_list: list[str] = []
    if assets.primary_color:
        vars_list.append(f"  --kix-brand-primary: {_sanitize_color(assets.primary_color)};")
    if assets.accent_color:
        vars_list.append(f"  --kix-brand-accent: {_sanitize_color(assets.accent_color)};")
    if assets.logo_url:
        vars_list.append(f"  --kix-brand-logo: url('{_sanitize_url(assets.logo_url)}');")
    if assets.background_image_url:
        vars_list.append(f"  --kix-brand-bg: url('{_sanitize_url(assets.background_image_url)}');")
    if not vars_list:
        return ""
    return _CSS_VAR_TEMPLATE.replace("__VARS__", "\n".join(vars_list))


def _sanitize_color(c: str) -> str:
    """Allow only #rgb / #rrggbb / common color keywords. Strip everything else."""
    c = c.strip()
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}", c):
        return c
    if re.fullmatch(r"[a-zA-Z]+", c) and len(c) <= 24:
        return c.lower()
    return "currentColor"   # safe fallback


def _sanitize_url(u: str) -> str:
    """Allow http(s):/data: URLs only; strip quotes + parens to block CSS escape."""
    u = u.strip().replace('"', '').replace("'", '').replace("(", "").replace(")", "")
    if u.startswith(("https://", "http://", "data:image/", "/")):
        return u
    return ""


# ── DOM-level replacements ──

def _inject_brand_name(html: str, assets: BrandAssets) -> str:
    if not assets.brand_name:
        return html
    # Replace common placeholders. Order matters (longest first).
    for placeholder in ("{{brand_name}}", "{{BRAND_NAME}}", "__BRAND_NAME__"):
        html = html.replace(placeholder, assets.brand_name)
    return html


def _inject_voucher(html: str, assets: BrandAssets) -> str:
    if assets.voucher_copy:
        for p in ("{{voucher_copy}}", "{{VOUCHER_COPY}}", "__VOUCHER__"):
            html = html.replace(p, assets.voucher_copy)
    if assets.voucher_code:
        for p in ("{{voucher_code}}", "{{VOUCHER_CODE}}", "__CODE__"):
            html = html.replace(p, assets.voucher_code)
    return html


def _inject_custom_slots(html: str, assets: BrandAssets) -> str:
    for slot_id, content in assets.custom_slots.items():
        if not slot_id or not isinstance(slot_id, str):
            continue
        # Slots are <div data-kix-slot="X"></div> — replace content
        pattern = rf'(<[^>]+data-kix-slot="{re.escape(slot_id)}"[^>]*>).*?(</[^>]+>)'
        html = re.sub(pattern, lambda m: m.group(1) + content + m.group(2),
                      html, flags=re.DOTALL)
    return html


def _inject_css_vars(html: str, assets: BrandAssets) -> str:
    """Insert the CSS-variable block just before </head>. If no </head>, prepend."""
    block = _build_css_vars(assets)
    if not block:
        return html
    if "</head>" in html:
        return html.replace("</head>", block + "\n</head>", 1)
    return block + html


# ── Public API ──

def inject_brand(html: str, assets: BrandAssets) -> str:
    """Inject brand assets into a generated game HTML. Pure: input → output."""
    if not isinstance(html, str):
        raise TypeError("html must be a string")
    if not isinstance(assets, BrandAssets):
        raise TypeError("assets must be BrandAssets instance")
    out = html
    out = _inject_css_vars(out, assets)
    out = _inject_brand_name(out, assets)
    out = _inject_voucher(out, assets)
    out = _inject_custom_slots(out, assets)
    return out


def diff_summary(original: str, preview: str) -> dict[str, int]:
    """Return a small summary of changes between original and preview.
    Useful for the portal to show "you changed: 3 colors, 1 logo, 2 strings".
    """
    return {
        "css_vars_added": 1 if "kix-brand-vars" in preview and "kix-brand-vars" not in original else 0,
        "char_delta": len(preview) - len(original),
        "lines_delta": preview.count("\n") - original.count("\n"),
    }
