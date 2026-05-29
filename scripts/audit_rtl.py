"""RTL (right-to-left) readiness audit for KiX landing/portal assets.

Walks `landing/**/*.{html,css,js}` and reports CSS / inline-style / JS patterns
that will break when the document is rendered with ``<html dir="rtl">``.

Severity model
--------------
P0 — breaks layout (float, absolute left/right, flex-direction:row without
     row-reverse, hardcoded ASCII box-drawing).
P1 — suboptimal but functional (margin-/padding-left/right, text-align:left|right,
     border-left/right, border-radius corner-specific, translateX positive).
P2 — cosmetic (directional unicode arrows, missing ``dir`` attribute,
     hardcoded ``direction: ltr``).

Patterns that are already logical (``margin-inline-start``, ``inset-inline-end``,
``text-align: start|end``, ``float: inline-start|inline-end``) are intentionally
ignored so the audit is idempotent on already-migrated files.

CLI
---
::

    python -m scripts.audit_rtl                # quiet, prints summary
    python -m scripts.audit_rtl --report       # detailed summary by file/severity
    python -m scripts.audit_rtl --csv out.csv  # write findings to CSV
    python -m scripts.audit_rtl --root path    # override search root

The module is import-safe: ``scan_directory`` / ``scan_text`` are pure helpers
with no I/O side effects (other than reading the supplied files).
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import io
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Iterator, Sequence

# ---------------------------------------------------------------------------
# Severity model
# ---------------------------------------------------------------------------

P0 = "P0"  # breaks layout
P1 = "P1"  # suboptimal but functional
P2 = "P2"  # cosmetic / advisory

# ---------------------------------------------------------------------------
# Pattern catalog
# ---------------------------------------------------------------------------
#
# Each entry is (severity, label, compiled regex, suggested-fix template).
# Regexes use VERBOSE mode and ignore-case where appropriate. They are
# deliberately conservative — false positives are worse than false negatives
# at this stage because we want trust in the report.
#
# We deliberately do NOT match the already-logical forms
# (``margin-inline-start``, ``inset-inline-start``, ``text-align:start`` etc).

_PATTERNS: list[tuple[str, str, re.Pattern[str], str]] = [
    # ---------------- P1 — directional spacing ----------------
    (
        P1,
        "margin-left",
        re.compile(r"(?<![\w-])margin-left\s*:", re.IGNORECASE),
        "margin-inline-start",
    ),
    (
        P1,
        "margin-right",
        re.compile(r"(?<![\w-])margin-right\s*:", re.IGNORECASE),
        "margin-inline-end",
    ),
    (
        P1,
        "padding-left",
        re.compile(r"(?<![\w-])padding-left\s*:", re.IGNORECASE),
        "padding-inline-start",
    ),
    (
        P1,
        "padding-right",
        re.compile(r"(?<![\w-])padding-right\s*:", re.IGNORECASE),
        "padding-inline-end",
    ),
    (
        P1,
        "border-left",
        re.compile(r"(?<![\w-])border-left(?:-(?:width|style|color))?\s*:", re.IGNORECASE),
        "border-inline-start*",
    ),
    (
        P1,
        "border-right",
        re.compile(r"(?<![\w-])border-right(?:-(?:width|style|color))?\s*:", re.IGNORECASE),
        "border-inline-end*",
    ),
    (
        P1,
        "border-radius-corner",
        re.compile(
            r"(?<![\w-])border-(?:top|bottom)-(?:left|right)-radius\s*:",
            re.IGNORECASE,
        ),
        "border-start-start-radius / border-start-end-radius (etc.)",
    ),
    (
        P1,
        "text-align-left",
        re.compile(r"text-align\s*:\s*left\b", re.IGNORECASE),
        "text-align: start",
    ),
    (
        P1,
        "text-align-right",
        re.compile(r"text-align\s*:\s*right\b", re.IGNORECASE),
        "text-align: end",
    ),
    # ---------------- P0 — layout-breaking ----------------
    (
        P0,
        "float-left",
        re.compile(r"float\s*:\s*left\b", re.IGNORECASE),
        "float: inline-start  (or convert to flexbox)",
    ),
    (
        P0,
        "float-right",
        re.compile(r"float\s*:\s*right\b", re.IGNORECASE),
        "float: inline-end  (or convert to flexbox)",
    ),
    (
        P0,
        "left-positioning",
        # Matches `left: <value>` but NOT `inset-inline-left` or `left:auto`
        # when the value would mirror harmlessly. We surface both — author decides.
        re.compile(r"(?<![\w-])left\s*:\s*(?!auto\b)[-+]?\d", re.IGNORECASE),
        "inset-inline-start",
    ),
    (
        P0,
        "right-positioning",
        re.compile(r"(?<![\w-])right\s*:\s*(?!auto\b)[-+]?\d", re.IGNORECASE),
        "inset-inline-end",
    ),
    (
        P0,
        "flex-direction-row",
        # Explicit `flex-direction: row` (the default) is suspicious only if
        # the author *might* have meant `row-reverse` for RTL. We surface it
        # so reviewers can decide; truly-symmetric layouts ignore the hint.
        re.compile(r"flex-direction\s*:\s*row\b(?!-reverse)", re.IGNORECASE),
        "consider row + dir-aware override, or use logical flex",
    ),
    (
        P0,
        "translateX-positive",
        # `translateX(0)` is harmless; `translateX(-)` flips naturally.
        # `translateX(<positive number/value>)` is the directional case.
        re.compile(
            r"translateX\s*\(\s*(?!0\b|-)\s*[+]?\d", re.IGNORECASE
        ),
        "translate with logical sign or use inset-inline-* ",
    ),
    # ---------------- JS-set styles ----------------
    (
        P0,
        "js-style-left",
        re.compile(r"\.style\.left\s*=", re.IGNORECASE),
        "use CSS class toggling inset-inline-start",
    ),
    (
        P0,
        "js-style-right",
        re.compile(r"\.style\.right\s*=", re.IGNORECASE),
        "use CSS class toggling inset-inline-end",
    ),
    (
        P1,
        "js-style-marginLeft",
        re.compile(r"\.style\.marginLeft\s*=", re.IGNORECASE),
        "style.marginInlineStart",
    ),
    (
        P1,
        "js-style-marginRight",
        re.compile(r"\.style\.marginRight\s*=", re.IGNORECASE),
        "style.marginInlineEnd",
    ),
    # ---------------- P2 — cosmetic / advisory ----------------
    (
        P2,
        "direction-ltr-hardcoded",
        re.compile(r"direction\s*:\s*ltr\b", re.IGNORECASE),
        "remove (let document dir win) or guard with dir-specific selector",
    ),
    (
        P2,
        "directional-arrow-unicode",
        # Right/left/triangle arrows commonly used as inline glyphs.
        re.compile(r"[←→▶◀⬅➡➔➜]"),
        "use icon class with .kix-icon-directional, or pair → with ← per dir",
    ),
    (
        P2,
        "ascii-box-drawing",
        # Common box-drawing characters used for inline ASCII art / tables.
        re.compile(r"[─-╿]{3,}"),
        "render as <table>/<svg> instead; ASCII art doesn't mirror",
    ),
]


# ---------------------------------------------------------------------------
# Already-logical patterns we explicitly want to ignore (for idempotency)
# ---------------------------------------------------------------------------

_LOGICAL_TOKENS = re.compile(
    r"(?:margin|padding|border|inset)-(?:inline|block)-(?:start|end)\b"
    r"|text-align\s*:\s*(?:start|end)\b"
    r"|float\s*:\s*inline-(?:start|end)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Finding:
    """One RTL-readiness issue."""

    file: str
    line: int
    column: int
    severity: str
    pattern: str
    snippet: str
    suggested_fix: str

    def as_csv_row(self) -> list[str]:
        return [
            self.file,
            str(self.line),
            str(self.column),
            self.severity,
            self.pattern,
            self.snippet,
            self.suggested_fix,
        ]


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def _iter_line_findings(line: str) -> Iterator[tuple[int, str, str, str]]:
    """Yield (column, severity, pattern, suggested_fix) for matches in *line*.

    Skips the entire line if it contains an already-logical CSS token at the
    same column position — keeps the audit idempotent on migrated files.
    """
    for severity, label, regex, fix in _PATTERNS:
        for m in regex.finditer(line):
            # Idempotency check: if the surrounding region (12 chars around the
            # match) already references a logical property, the author has
            # explicitly opted in — skip.
            window_start = max(0, m.start() - 12)
            window = line[window_start : m.end() + 12]
            if _LOGICAL_TOKENS.search(window):
                # The author already wrote the logical equivalent on the same
                # line — likely a fallback/override pair, don't double-report.
                if label not in {"directional-arrow-unicode", "ascii-box-drawing"}:
                    continue
            yield m.start() + 1, severity, label, fix


def scan_text(text: str, file_label: str) -> list[Finding]:
    """Scan an in-memory string for RTL-breaking patterns.

    Returns one :class:`Finding` per match. ``file_label`` is recorded as
    the source — pass the absolute or relative path the caller wants in the
    final report.
    """
    findings: list[Finding] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        for column, severity, pattern, fix in _iter_line_findings(raw_line):
            snippet = raw_line.strip()
            if len(snippet) > 200:
                snippet = snippet[:197] + "..."
            findings.append(
                Finding(
                    file=file_label,
                    line=line_no,
                    column=column,
                    severity=severity,
                    pattern=pattern,
                    snippet=snippet,
                    suggested_fix=fix,
                )
            )
    return findings


def scan_file(path: Path, root: Path | None = None) -> list[Finding]:
    """Read *path* and return its findings.

    ``root`` is used only to make ``file`` relative for readable reports.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # Surface as a single P2 entry rather than blowing up the run.
        return [
            Finding(
                file=str(path),
                line=0,
                column=0,
                severity=P2,
                pattern="io-error",
                snippet=str(exc),
                suggested_fix="check file readability",
            )
        ]
    label = str(path.relative_to(root)) if root else str(path)
    return scan_text(text, label)


_DEFAULT_EXTENSIONS = (".html", ".htm", ".css", ".js")
_DEFAULT_IGNORES = {"node_modules", "vendor", ".git", "__pycache__"}


def iter_audit_files(
    root: Path,
    extensions: Sequence[str] = _DEFAULT_EXTENSIONS,
    ignore_dirs: Iterable[str] = _DEFAULT_IGNORES,
) -> Iterator[Path]:
    """Yield files under *root* that should be audited.

    Walks the tree depth-first, skipping ``ignore_dirs`` and any file whose
    suffix is not in ``extensions``.
    """
    ignore_set = set(ignore_dirs)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore_set]
        for name in filenames:
            if name.lower().endswith(extensions):
                yield Path(dirpath) / name


def scan_directory(
    root: Path,
    extensions: Sequence[str] = _DEFAULT_EXTENSIONS,
    ignore_dirs: Iterable[str] = _DEFAULT_IGNORES,
) -> list[Finding]:
    """Scan all files under *root* matching *extensions*. Pure I/O, no LLM."""
    all_findings: list[Finding] = []
    for path in iter_audit_files(root, extensions=extensions, ignore_dirs=ignore_dirs):
        all_findings.extend(scan_file(path, root=root))
    return all_findings


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def write_csv(findings: Iterable[Finding], destination) -> None:
    """Write findings to *destination* (path str or file-like)."""
    if hasattr(destination, "write"):
        writer = csv.writer(destination)
        writer.writerow(
            ["file", "line", "column", "severity", "pattern", "snippet", "suggested_fix"]
        )
        for f in findings:
            writer.writerow(f.as_csv_row())
        return
    with open(destination, "w", encoding="utf-8", newline="") as fh:
        write_csv(findings, fh)


def summarize(findings: Sequence[Finding]) -> dict:
    """Produce a summary dict suitable for printing or JSON dump."""
    by_severity: Counter[str] = Counter(f.severity for f in findings)
    by_pattern: Counter[str] = Counter(f.pattern for f in findings)
    by_file: dict[str, Counter[str]] = defaultdict(Counter)
    for f in findings:
        by_file[f.file][f.severity] += 1
    return {
        "total": len(findings),
        "by_severity": dict(by_severity),
        "by_pattern": by_pattern.most_common(20),
        "by_file": {
            file: dict(counts)
            for file, counts in sorted(
                by_file.items(),
                key=lambda kv: -(kv[1][P0] * 100 + kv[1][P1] * 10 + kv[1][P2]),
            )
        },
    }


def format_report(summary: dict) -> str:
    """Render :func:`summarize` output as a plaintext report."""
    out = io.StringIO()
    out.write(f"RTL audit — {summary['total']} findings\n")
    out.write("=" * 60 + "\n")
    out.write("By severity:\n")
    for sev in (P0, P1, P2):
        out.write(f"  {sev}: {summary['by_severity'].get(sev, 0)}\n")
    out.write("\nTop patterns:\n")
    for pattern, count in summary["by_pattern"]:
        out.write(f"  {count:>5d}  {pattern}\n")
    out.write("\nBy file (worst first):\n")
    for file, sev_counts in summary["by_file"].items():
        p0 = sev_counts.get(P0, 0)
        p1 = sev_counts.get(P1, 0)
        p2 = sev_counts.get(P2, 0)
        out.write(f"  {file:<60s}  P0={p0:<4d} P1={p1:<4d} P2={p2:<4d}\n")
    return out.getvalue()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_root() -> Path:
    return Path(__file__).resolve().parents[1] / "landing"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit landing/portal assets for RTL-breaking CSS/JS patterns."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=_default_root(),
        help="Directory to scan (default: <repo>/landing)",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Write findings to CSV at this path.",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Print a detailed summary by file/severity.",
    )
    args = parser.parse_args(argv)

    if not args.root.exists():
        print(f"audit_rtl: root {args.root} does not exist", file=sys.stderr)
        return 2

    findings = scan_directory(args.root)
    summary = summarize(findings)

    if args.csv is not None:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        write_csv(findings, args.csv)
        print(f"audit_rtl: wrote {len(findings)} findings to {args.csv}")

    if args.report or args.csv is None:
        print(format_report(summary))

    # Exit code: 1 if any P0, else 0. Useful for CI gating.
    return 1 if summary["by_severity"].get(P0, 0) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
