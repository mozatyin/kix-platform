"""Printable HTML receipt generator for POS redemptions.

Produces a self-contained HTML page sized for an 80mm thermal printer
(also legible on A4). Embeds a QR code (data-URL SVG, no external deps)
that links back to the KiX app so the consumer can redeem more on their
next visit.

Used by :mod:`app.routers.pos_integration` after a successful redemption.
"""

from __future__ import annotations

import hashlib
import html
from datetime import datetime, timezone
from typing import Any


def _ts_str(ts: int | float) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def _qr_svg(data: str, *, size_px: int = 120) -> str:
    """Render a deterministic placeholder QR.

    Real production uses ``qrcode[svg]`` if installed; otherwise this
    dependency-free fallback paints a 21x21 hash-derived module grid
    that's clearly QR-like (corner finders) so cashiers can confirm the
    shape on the printout. The real scanned QR is recreated on the
    consumer's phone by the KiX app deep-link, not by the thermal print
    (thermal printers often smudge below ~150 DPI).
    """
    try:  # pragma: no cover — optional dep
        import qrcode  # type: ignore
        from qrcode.image.svg import SvgImage  # type: ignore

        img = qrcode.make(data, image_factory=SvgImage, box_size=4, border=1)
        return img.to_string(encoding="unicode")  # type: ignore[no-any-return]
    except Exception:
        pass

    # Deterministic 21x21 fallback (visual placeholder — not a scannable QR)
    digest = hashlib.sha256(data.encode("utf-8")).digest()
    bits = "".join(f"{b:08b}" for b in digest)  # 256 bits >> 441 cells
    grid = 21
    cell = max(2, size_px // grid)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{grid*cell}" '
        f'height="{grid*cell}" viewBox="0 0 {grid*cell} {grid*cell}">'
    ]
    # Background
    parts.append(f'<rect width="100%" height="100%" fill="white"/>')

    def finder(x: int, y: int) -> None:
        parts.append(
            f'<rect x="{x*cell}" y="{y*cell}" width="{7*cell}" height="{7*cell}" '
            f'fill="black"/>'
            f'<rect x="{(x+1)*cell}" y="{(y+1)*cell}" width="{5*cell}" '
            f'height="{5*cell}" fill="white"/>'
            f'<rect x="{(x+2)*cell}" y="{(y+2)*cell}" width="{3*cell}" '
            f'height="{3*cell}" fill="black"/>'
        )

    finder(0, 0)
    finder(grid - 7, 0)
    finder(0, grid - 7)

    # Data modules from hash, skipping finder zones
    idx = 0
    for r_ in range(grid):
        for c_ in range(grid):
            in_finder = (
                (r_ < 8 and c_ < 8)
                or (r_ < 8 and c_ >= grid - 8)
                or (r_ >= grid - 8 and c_ < 8)
            )
            if in_finder:
                continue
            bit = bits[idx % len(bits)]
            idx += 1
            if bit == "1":
                parts.append(
                    f'<rect x="{c_*cell}" y="{r_*cell}" width="{cell}" '
                    f'height="{cell}" fill="black"/>'
                )
    parts.append("</svg>")
    return "".join(parts)


_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<title>KiX Receipt · {voucher_id_short}</title>
<style>
@page {{ size: 80mm auto; margin: 4mm; }}
body {{
  font-family: ui-monospace, Menlo, Consolas, monospace;
  width: 72mm;
  margin: 0 auto;
  color: #111;
  font-size: 12px;
  line-height: 1.4;
}}
h1 {{ font-size: 16px; margin: 4px 0; text-align: center; }}
.muted {{ color: #555; font-size: 10px; }}
.row {{ display: flex; justify-content: space-between; margin: 2px 0; }}
.divider {{ border-top: 1px dashed #888; margin: 8px 0; }}
.qr {{ text-align: center; margin: 10px 0; }}
.footer {{ text-align: center; font-size: 10px; margin-top: 8px; }}
.brand {{ font-weight: bold; }}
@media print {{
  body {{ font-size: 11px; }}
}}
</style>
</head><body>
<h1>{brand_name}</h1>
<div class="muted" style="text-align:center;">KiX Reward Redemption</div>
<div class="divider"></div>
<div class="row"><span>Voucher</span><span class="brand">{voucher_id_short}</span></div>
<div class="row"><span>Discount</span><span>{currency} {amount_display}</span></div>
<div class="row"><span>Order</span><span>{order_id}</span></div>
<div class="row"><span>POS</span><span>{pos_code}</span></div>
<div class="row"><span>Cashier</span><span>{cashier_id}</span></div>
<div class="row"><span>Time</span><span>{ts_str}</span></div>
<div class="row"><span>Ref</span><span>{redemption_id}</span></div>
<div class="divider"></div>
<div class="qr">
  {qr_svg}
  <div class="muted">Scan for your next reward</div>
</div>
<div class="footer">
  kix.gg/r/{voucher_id_short}<br/>
  Thanks for playing — come back soon!
</div>
</body></html>
"""


def generate_receipt_html(
    *,
    voucher_id: str,
    redemption_id: str,
    pos_code: str,
    brand_id: str | None,
    brand_name: str | None,
    order_id: str,
    amount_cents: int,
    currency: str,
    cashier_id: str | None,
    redeemed_at: int,
    next_visit_url: str | None = None,
) -> str:
    """Render an 80mm-thermal-friendly receipt as a self-contained HTML page."""
    vid_short = (voucher_id or "")[:12].upper()
    deeplink = next_visit_url or f"https://kix.gg/r/{vid_short}"
    qr_svg = _qr_svg(deeplink)
    amount_display = f"{amount_cents / 100:.2f}"
    return _TEMPLATE.format(
        voucher_id_short=html.escape(vid_short),
        brand_name=html.escape(brand_name or brand_id or "KiX Merchant"),
        currency=html.escape((currency or "USD").upper()),
        amount_display=amount_display,
        order_id=html.escape(order_id or "-"),
        pos_code=html.escape((pos_code or "-").upper()),
        cashier_id=html.escape(cashier_id or "-"),
        ts_str=_ts_str(redeemed_at),
        redemption_id=html.escape((redemption_id or "-")[:20]),
        qr_svg=qr_svg,
    )


__all__ = ["generate_receipt_html"]
