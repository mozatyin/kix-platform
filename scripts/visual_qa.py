"""Visual QA orchestrator — screenshot landing pages per locale.

Runs Playwright (when available) against the landing pages, captures
full-page screenshots per locale, and emits an HTML report with
side-by-side comparisons. Used to catch:

  * **Layout overflow** under +30 % / +200 % string expansion
    (compare ``en-SG`` to ``xx-AC`` / ``xx-LO``).
  * **Clickable-element survival** under pseudo-translation — accented
    text in ``xx-AC`` should not break button hit-targets.
  * **Mojibake / font fallback** on CJK locales.

Playwright is *optional*. When the ``playwright`` package isn't
installed the tool skips screenshot capture and only generates the
report skeleton (so CI doesn't fail on dev machines without
browsers).

Usage::

    python -m scripts.visual_qa
    python -m scripts.visual_qa --locales en-SG,zh-Hans-SG,xx-AC
    python -m scripts.visual_qa --pages portal,storefront --base-url http://localhost:8000
    python -m scripts.visual_qa --output qa_screenshots
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_PAGES = ("portal", "storefront", "play")
DEFAULT_LOCALES = ("en-SG", "zh-Hans-SG", "xx-AC", "xx-LO")
DEFAULT_VIEWPORT = (1280, 900)


@dataclass
class QAConfig:
    repo_root: Path
    base_url: Optional[str]           # http://host:port serving landing/
    output_dir: Path
    pages: tuple[str, ...]
    locales: tuple[str, ...]
    viewport: tuple[int, int]
    timeout_ms: int = 15_000


def playwright_available() -> bool:
    """Return True iff the ``playwright`` package is importable and
    its sync browser API works on this machine."""
    if importlib.util.find_spec("playwright") is None:
        return False
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except Exception:  # pragma: no cover
        return False


# --------------------------------------------------------------------------- #
# URL builder
# --------------------------------------------------------------------------- #


def page_url(cfg: QAConfig, page: str, locale: str) -> str:
    """Build the URL for ``page`` in ``locale``.

    If ``--base-url`` is set, it's used as the origin.  Otherwise we
    return a ``file://`` URL pointing at the static HTML so the tool
    works without a running server.
    """
    if cfg.base_url:
        return f"{cfg.base_url.rstrip('/')}/{page}.html?lang={locale}"
    p = (cfg.repo_root / "landing" / f"{page}.html").resolve()
    return f"file://{p}?lang={locale}"


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #


@dataclass
class Shot:
    page: str
    locale: str
    path: Path                 # png file on disk
    width: int
    height: int
    overflow: bool = False     # heuristic: page width > viewport width


def capture_all(cfg: QAConfig) -> list[Shot]:
    """Capture screenshots for every (page, locale) combination."""
    if not playwright_available():
        print("playwright not installed; skipping screenshot capture", file=sys.stderr)
        return []

    from playwright.sync_api import sync_playwright  # local import

    shots: list[Shot] = []
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={
                "width": cfg.viewport[0],
                "height": cfg.viewport[1],
            })
            for page in cfg.pages:
                for locale in cfg.locales:
                    p = context.new_page()
                    url = page_url(cfg, page, locale)
                    out = cfg.output_dir / f"{page}_{locale}.png"
                    try:
                        p.goto(url, timeout=cfg.timeout_ms, wait_until="load")
                        p.screenshot(path=str(out), full_page=True)
                        body_w = p.evaluate("document.documentElement.scrollWidth")
                        body_h = p.evaluate("document.documentElement.scrollHeight")
                        shots.append(Shot(
                            page=page, locale=locale, path=out,
                            width=body_w, height=body_h,
                            overflow=body_w > cfg.viewport[0] + 4,
                        ))
                    except Exception as exc:  # pragma: no cover
                        print(f"capture failed {page}/{locale}: {exc}", file=sys.stderr)
                    finally:
                        p.close()
        finally:
            browser.close()
    return shots


# --------------------------------------------------------------------------- #
# Diff heuristics
# --------------------------------------------------------------------------- #


@dataclass
class DiffFinding:
    kind: str          # "overflow_lo" / "missing_shot" / "size_jump"
    page: str
    locale: str
    detail: str


def diff_check(shots: list[Shot], cfg: QAConfig) -> list[DiffFinding]:
    """Compare en-SG baseline to xx-AC / xx-LO and flag layout issues."""
    findings: list[DiffFinding] = []
    by_key = {(s.page, s.locale): s for s in shots}

    for page in cfg.pages:
        base = by_key.get((page, "en-SG"))
        if base is None:
            # No baseline → nothing to compare.
            continue

        # en-SG vs xx-LO: width must not blow past viewport.
        lo = by_key.get((page, "xx-LO"))
        if lo and lo.overflow:
            findings.append(DiffFinding(
                kind="overflow_lo",
                page=page,
                locale="xx-LO",
                detail=f"xx-LO page width {lo.width}px exceeds viewport "
                       f"{cfg.viewport[0]}px (en-SG was {base.width}px)",
            ))

        # en-SG vs xx-AC: similar size envelope (within 50 %).
        ac = by_key.get((page, "xx-AC"))
        if ac and base.height and ac.height > base.height * 1.50:
            findings.append(DiffFinding(
                kind="size_jump",
                page=page,
                locale="xx-AC",
                detail=f"xx-AC height {ac.height}px is >150% of en-SG ({base.height}px)",
            ))

        for locale in cfg.locales:
            if (page, locale) not in by_key:
                findings.append(DiffFinding(
                    kind="missing_shot",
                    page=page,
                    locale=locale,
                    detail="no screenshot captured",
                ))
    return findings


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


_REPORT_CSS = """
body { font-family: system-ui, sans-serif; margin: 24px; }
h1 { margin-bottom: 4px; }
table { border-collapse: collapse; margin-top: 16px; }
td, th { border: 1px solid #ccc; padding: 8px; vertical-align: top; }
.shot { max-width: 360px; max-height: 240px; border: 1px solid #888; }
.findings li.overflow_lo, .findings li.size_jump { color: #b00; }
.findings li.missing_shot { color: #888; }
code { background: #f4f4f4; padding: 1px 4px; border-radius: 3px; }
"""


def write_report(cfg: QAConfig, shots: list[Shot],
                 findings: list[DiffFinding]) -> Path:
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.output_dir / "report.html"

    rows = ["<tr><th>page</th>"]
    for loc in cfg.locales:
        rows.append(f"<th>{loc}</th>")
    rows.append("</tr>")

    by_key = {(s.page, s.locale): s for s in shots}
    for page in cfg.pages:
        rows.append(f"<tr><td><b>{page}</b></td>")
        for loc in cfg.locales:
            shot = by_key.get((page, loc))
            if shot:
                rel = os.path.relpath(shot.path, cfg.output_dir)
                rows.append(
                    f"<td><img class=shot src='{rel}' alt='{page}/{loc}'>"
                    f"<br><small>{shot.width}×{shot.height}</small></td>"
                )
            else:
                rows.append("<td><em>(no shot)</em></td>")
        rows.append("</tr>")

    findings_html = "".join(
        f"<li class='{f.kind}'><b>{f.kind}</b> {f.page} / {f.locale} — {f.detail}</li>"
        for f in findings
    ) or "<li>no layout regressions detected</li>"

    pw_status = "available" if playwright_available() else (
        "<b>NOT INSTALLED</b> — install with <code>pip install playwright && playwright install chromium</code>"
    )

    body = (
        f"<h1>i18n visual QA report</h1>"
        f"<div>playwright: {pw_status}</div>"
        f"<div>pages: <code>{', '.join(cfg.pages)}</code> &nbsp; "
        f"locales: <code>{', '.join(cfg.locales)}</code></div>"
        f"<h2>Side-by-side</h2>"
        f"<table>{''.join(rows)}</table>"
        f"<h2>Findings</h2><ul class='findings'>{findings_html}</ul>"
    )
    out.write_text(
        f"<!doctype html><meta charset='utf-8'>"
        f"<title>i18n visual QA</title><style>{_REPORT_CSS}</style>"
        f"{body}",
        encoding="utf-8",
    )
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--root", type=Path, default=Path.cwd())
    p.add_argument("--base-url", default=None,
                   help="origin serving landing/ HTML (default: file://)")
    p.add_argument("--output", type=Path, default=Path("qa_screenshots"))
    p.add_argument("--pages", default=",".join(DEFAULT_PAGES),
                   help="comma-separated page slugs")
    p.add_argument("--locales", default=",".join(DEFAULT_LOCALES),
                   help="comma-separated locale codes")
    p.add_argument("--viewport", default="1280x900",
                   help="WxH default viewport")
    p.add_argument("--timeout-ms", type=int, default=15_000)
    p.add_argument("--json", type=Path,
                   help="write findings as JSON to this path")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    w, _, h = args.viewport.partition("x")
    cfg = QAConfig(
        repo_root=args.root.resolve(),
        base_url=args.base_url,
        output_dir=(args.output if args.output.is_absolute()
                    else args.root.resolve() / args.output),
        pages=tuple(s.strip() for s in args.pages.split(",") if s.strip()),
        locales=tuple(s.strip() for s in args.locales.split(",") if s.strip()),
        viewport=(int(w), int(h)),
        timeout_ms=args.timeout_ms,
    )

    shots = capture_all(cfg)
    findings = diff_check(shots, cfg)
    report = write_report(cfg, shots, findings)
    print(f"report: {report}")
    print(f"shots:  {len(shots)}  findings: {len(findings)}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({
            "shots": [{"page": s.page, "locale": s.locale, "path": str(s.path),
                       "width": s.width, "height": s.height,
                       "overflow": s.overflow} for s in shots],
            "findings": [vars(f) for f in findings],
        }, indent=2), encoding="utf-8")

    # Exit-code: 1 if any non-cosmetic findings (overflow/size_jump).
    bad = [f for f in findings if f.kind in {"overflow_lo", "size_jump"}]
    return 1 if bad else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
