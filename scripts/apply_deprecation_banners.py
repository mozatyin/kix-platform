"""Apply DEPRECATED banner to legacy landing pages listed in
data/deprecation_registry.json.

The banner:
  - sits ABOVE the page's <body> opening content
  - states 'DEPRECATED · sunsetting on {sunset_at} · go to {successor}'
  - is idempotent: re-running this script doesn't double-stamp
  - is removable: search for the canonical comment marker and strip

Marker:  <!-- KIX-DEPRECATION-BANNER:START --> ... <!-- KIX-DEPRECATION-BANNER:END -->

Run:
  python -m scripts.apply_deprecation_banners               # apply
  python -m scripts.apply_deprecation_banners --dry-run     # show plan
  python -m scripts.apply_deprecation_banners --strip       # remove all banners

After running, the lint script reports time-since-deprecated for each
page so ops can decide when to flip to 302 redirects.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "data" / "deprecation_registry.json"

MARK_START = "<!-- KIX-DEPRECATION-BANNER:START -->"
MARK_END   = "<!-- KIX-DEPRECATION-BANNER:END -->"
MARK_RE = re.compile(re.escape(MARK_START) + r".*?" + re.escape(MARK_END), re.DOTALL)


def banner_html(deprecated_at: str, sunset_at: str, successor: str, reason: str) -> str:
    # R1 buyer-journey friction: "red DEPRECATED banner destroys credibility".
    # Softened to amber informational note that points to canonical successor
    # without screaming "this content is bad".
    return f'''{MARK_START}
<style>
  .kix-deprecation-banner{{
    position:fixed;top:0;left:0;right:0;z-index:9999;
    background:#FEF3C7;border-bottom:1px solid #FBBF24;
    padding:8px 16px;font:12.5px -apple-system,BlinkMacSystemFont,Inter,sans-serif;
    color:#78350F;text-align:center;display:flex;align-items:center;justify-content:center;gap:10px;flex-wrap:wrap;
  }}
  .kix-deprecation-banner strong{{font-weight:700}}
  .kix-deprecation-banner a{{color:#92400E;text-decoration:underline;font-weight:700}}
  body{{padding-top:38px !important}}
</style>
<div class="kix-deprecation-banner" role="status">
  <span>ℹ️ <strong>This page is moving</strong> to <a href="{successor}">{successor}</a> on {sunset_at}. Content here remains accurate until then.</span>
</div>
{MARK_END}
'''


def apply_to_file(path: Path, deprecated_at: str, sunset_at: str,
                  successor: str, reason: str, dry_run: bool) -> str:
    if not path.exists():
        return "missing"
    text = path.read_text()
    # Strip any prior banner first (idempotent)
    text_no_banner = MARK_RE.sub("", text).rstrip() + "\n"
    new_banner = banner_html(deprecated_at, sunset_at, successor, reason)
    # Insert just after <body> tag, else at top
    body_m = re.search(r"<body[^>]*>", text_no_banner, re.IGNORECASE)
    if body_m:
        idx = body_m.end()
        out = text_no_banner[:idx] + "\n" + new_banner + text_no_banner[idx:]
    else:
        out = new_banner + text_no_banner
    if out == text:
        return "no-change"
    if not dry_run:
        path.write_text(out)
    return "stamped"


def strip_from_file(path: Path, dry_run: bool) -> str:
    if not path.exists():
        return "missing"
    text = path.read_text()
    if MARK_START not in text:
        return "no-banner"
    out = MARK_RE.sub("", text)
    if not dry_run:
        path.write_text(out)
    return "stripped"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--strip", action="store_true")
    args = p.parse_args()

    reg = json.loads(REGISTRY.read_text())
    rows = reg["deprecations"]
    print(f"Loaded {len(rows)} deprecation entries from {REGISTRY.relative_to(ROOT)}")

    counts: dict[str, int] = {}
    for row in rows:
        page_path = ROOT / row["page"]
        if not row.get("deprecated_at"):
            counts["exempt"] = counts.get("exempt", 0) + 1
            continue

        if args.strip:
            r = strip_from_file(page_path, dry_run=args.dry_run)
        else:
            r = apply_to_file(
                page_path,
                deprecated_at=row["deprecated_at"],
                sunset_at=row["sunset_at"],
                successor=row["successor"],
                reason=row["reason"],
                dry_run=args.dry_run,
            )
        counts[r] = counts.get(r, 0) + 1
        prefix = "(dry) " if args.dry_run else ""
        print(f"  {prefix}{row['page']:<45s} {r}")

    print()
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
