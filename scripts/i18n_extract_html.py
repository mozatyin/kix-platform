"""i18n string extraction — HTML / JavaScript.

Walks ``landing/*.html`` and ``landing/**/*.js`` (or a single target),
extracts user-facing text nodes from HTML and string literals from JS,
classifies each candidate, and emits a CSV.

For HTML the recommended rewrite target is a ``data-i18n="<key>"``
attribute on the element containing the text node — this is the
convention used by i18next, vue-i18n, and Phoenix LiveView.

Usage:
    .venv/bin/python -m scripts.i18n_extract_html
    .venv/bin/python -m scripts.i18n_extract_html --target landing/portal.html --llm

Output CSV columns are identical to ``i18n_extract.py``.
"""
from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from dataclasses import asdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.i18n_extract import (  # noqa: E402
    Candidate,
    _looks_internal,
    _looks_natural_lang,
    unique_keys,
    write_csv,
)
from scripts.i18n_prompts import classify_sync  # noqa: E402

logger = logging.getLogger("i18n_extract_html")


# ─── HTML extractor ──────────────────────────────────────────────────────────


SKIP_TAGS = {"script", "style", "noscript", "code", "pre", "template"}


class _HTMLTextExtractor(HTMLParser):
    """Collect text-node candidates with their line numbers.

    The stdlib html.parser preserves source line numbers via
    ``self.getpos()``. We also track the depth of skip-tags so we don't
    pull text out of ``<script>`` blocks (those are handled by the JS
    extractor against the file content directly).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.candidates: list[tuple[int, str]] = []  # (lineno, text)
        self._skip_depth = 0
        self._tag_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs):  # type: ignore[override]
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        self._tag_stack.append(tag)
        # extract user-facing attributes too
        for k, v in attrs:
            if not v:
                continue
            if k in {"title", "placeholder", "alt", "aria-label"} and _is_extractable_text(v):
                line, _ = self.getpos()
                self.candidates.append((line, v.strip()))

    def handle_startendtag(self, tag: str, attrs):  # type: ignore[override]
        self.handle_starttag(tag, attrs)
        # auto-close — no skip-depth change because start handled both

    def handle_endtag(self, tag: str):  # type: ignore[override]
        if tag in SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()

    def handle_data(self, data: str):  # type: ignore[override]
        if self._skip_depth > 0:
            return
        text = data.strip()
        if not _is_extractable_text(text):
            return
        line, _ = self.getpos()
        self.candidates.append((line, text))


def _is_extractable_text(text: str) -> bool:
    text = text.strip()
    if not text:
        return False
    if len(text) < 2 and not re.search(r"[一-鿿]", text):
        return False
    if _looks_internal(text):
        return False
    if not _looks_natural_lang(text):
        return False
    return True


def extract_html(source: str) -> list[tuple[int, str]]:
    p = _HTMLTextExtractor()
    p.feed(source)
    # dedup preserving order
    seen: set[tuple[int, str]] = set()
    out: list[tuple[int, str]] = []
    for ln, txt in p.candidates:
        key = (ln, txt)
        if key in seen:
            continue
        seen.add(key)
        out.append((ln, txt))
    return out


# ─── JS extractor ────────────────────────────────────────────────────────────

# Regex captures plain strings, template literals; allows escaped quotes.
_JS_STRING_RE = re.compile(
    r"""
    (?P<q>['"`])                       # opening quote
    (?P<body>(?:\\.|(?!(?P=q)).)*)     # body — no unescaped match of quote
    (?P=q)                             # closing quote
    """,
    re.VERBOSE | re.DOTALL,
)

# Strip /* */ and // comments before scanning
_JS_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_JS_LINE_COMMENT_RE = re.compile(r"(^|[^:])//.*?$", re.MULTILINE)


def _strip_js_comments(src: str) -> str:
    src = _JS_BLOCK_COMMENT_RE.sub(lambda m: " " * len(m.group(0)), src)
    src = _JS_LINE_COMMENT_RE.sub(
        lambda m: m.group(1) + " " * (len(m.group(0)) - len(m.group(1))), src
    )
    return src


def extract_js(source: str) -> list[tuple[int, str]]:
    stripped = _strip_js_comments(source)
    out: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for m in _JS_STRING_RE.finditer(stripped):
        body = m.group("body")
        # un-escape minimally — sufficient for classification
        body = body.replace("\\n", "\n").replace("\\t", "\t").replace("\\'", "'").replace('\\"', '"')
        # collapse template ${expr} into {expr}
        body = re.sub(r"\$\{\s*([a-zA-Z_][\w.]*)\s*\}", r"{\1}", body)
        if not _is_extractable_text(body):
            continue
        line = source.count("\n", 0, m.start()) + 1
        key = (line, body)
        if key in seen:
            continue
        seen.add(key)
        out.append((line, body))
    return out


# ─── Driver ──────────────────────────────────────────────────────────────────


def iter_targets(target: Path) -> Iterator[Path]:
    if target.is_file():
        yield target
        return
    for ext in ("*.html", "*.js"):
        for p in target.rglob(ext):
            if any(seg in p.parts for seg in ("vendor", "node_modules", "assets/gdpr-exports")):
                continue
            yield p


def classify_rows(file: str, raw: list[tuple[int, str]], use_llm: bool) -> list[Candidate]:
    out: list[Candidate] = []
    for ln, txt in raw:
        ctx = f"{file} L{ln}"
        c = classify_sync(txt, ctx, use_llm=use_llm)
        out.append(
            Candidate(
                file=file,
                line=ln,
                original=txt,
                category=c.category,
                proposed_key=c.proposed_key,
                is_user_facing=c.is_user_facing,
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Extract i18n candidate strings from HTML / JS.")
    p.add_argument("--target", default="landing", help="File or directory (default: landing)")
    p.add_argument("--output", default="i18n_extracted_html_report.csv")
    p.add_argument("--llm", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    target = Path(args.target)
    if not target.exists():
        target = REPO_ROOT / args.target
    if not target.exists():
        logger.error("Target not found: %s", args.target)
        return 2

    all_rows: list[Candidate] = []
    file_count = 0
    for path in iter_targets(target):
        file_count += 1
        try:
            src = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as e:
            logger.warning("Skip %s: %s", path, e)
            continue
        try:
            rel = str(path.relative_to(REPO_ROOT)) if path.is_absolute() else str(path)
        except ValueError:
            rel = str(path)
        if path.suffix == ".html":
            raw = extract_html(src)
        elif path.suffix == ".js":
            raw = extract_js(src)
        else:
            continue
        if not raw:
            continue
        rows = classify_rows(rel, raw, use_llm=args.llm)
        all_rows.extend(rows)
        logger.info("%s — %d candidates", rel, len(rows))

    all_rows = unique_keys(all_rows)
    out_path = Path(args.output)
    if not out_path.is_absolute():
        out_path = REPO_ROOT / out_path
    write_csv(all_rows, out_path)
    user_facing = sum(1 for r in all_rows if r.is_user_facing == "yes")
    logger.info(
        "Scanned %d file(s); %d candidates (%d user-facing) → %s",
        file_count,
        len(all_rows),
        user_facing,
        out_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
