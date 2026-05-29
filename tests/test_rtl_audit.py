"""Tests for the RTL-readiness audit tool.

These tests load ``scripts/audit_rtl.py`` directly by file path to avoid the
``scripts`` namespace-package collision flagged in ``tests/test_i18n_extract.py``
— other repos inject their own ``scripts`` package into ``sys.path``.
"""
from __future__ import annotations

import csv
import io
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Load by file path — see tests/test_i18n_extract.py for the rationale.
import importlib.util as _ilu


def _load(mod_name: str, file_path: Path):
    spec = _ilu.spec_from_file_location(mod_name, file_path)
    assert spec and spec.loader
    mod = _ilu.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


audit_rtl = _load("kix_audit_rtl", REPO_ROOT / "scripts" / "audit_rtl.py")


# ---------------------------------------------------------------------------
# 1. Detects margin-left
# ---------------------------------------------------------------------------


def test_detects_margin_left_as_p1():
    css = ".btn { margin-left: 12px; }"
    findings = audit_rtl.scan_text(css, "fake.css")
    labels = {f.pattern for f in findings}
    assert "margin-left" in labels
    margin_findings = [f for f in findings if f.pattern == "margin-left"]
    assert margin_findings[0].severity == audit_rtl.P1
    assert "margin-inline-start" in margin_findings[0].suggested_fix


# ---------------------------------------------------------------------------
# 2. Detects float:left as P0
# ---------------------------------------------------------------------------


def test_detects_float_left_as_p0():
    css = ".sidebar { float: left; width: 200px; }"
    findings = audit_rtl.scan_text(css, "fake.css")
    floats = [f for f in findings if f.pattern == "float-left"]
    assert len(floats) == 1
    assert floats[0].severity == audit_rtl.P0


# ---------------------------------------------------------------------------
# 3. Idempotent: ignores already-logical margin-inline-start
# ---------------------------------------------------------------------------


def test_ignores_logical_properties():
    css = textwrap.dedent(
        """\
        .a { margin-inline-start: 12px; }
        .b { padding-inline-end: 8px; }
        .c { border-inline-start: 1px solid #000; }
        .d { text-align: start; }
        .e { float: inline-start; }
        .f { inset-inline-start: 0; }
        """
    )
    findings = audit_rtl.scan_text(css, "logical.css")
    # No legacy pattern should fire for this file.
    legacy_labels = {
        "margin-left",
        "margin-right",
        "padding-left",
        "padding-right",
        "border-left",
        "border-right",
        "text-align-left",
        "text-align-right",
        "float-left",
        "float-right",
        "left-positioning",
        "right-positioning",
    }
    hits = [f for f in findings if f.pattern in legacy_labels]
    assert hits == [], f"unexpected legacy hits on logical-only file: {hits}"


# ---------------------------------------------------------------------------
# 4. Handles CSS-in-HTML — both <style> blocks and style="" attrs
# ---------------------------------------------------------------------------


def test_handles_css_in_html_style_block_and_attribute():
    html = textwrap.dedent(
        """\
        <!doctype html>
        <html>
        <head><style>
          .hero { padding-left: 20px; }
        </style></head>
        <body>
          <div style="margin-right: 8px; float: right;">x</div>
        </body>
        </html>
        """
    )
    findings = audit_rtl.scan_text(html, "page.html")
    labels = {f.pattern for f in findings}
    # Style block hit:
    assert "padding-left" in labels
    # Inline style attribute hits:
    assert "margin-right" in labels
    assert "float-right" in labels


# ---------------------------------------------------------------------------
# 5. Handles JS-set style assignments
# ---------------------------------------------------------------------------


def test_handles_js_style_left_assignment():
    js = textwrap.dedent(
        """\
        function place(el, x) {
            el.style.left = x + 'px';
            el.style.marginLeft = '4px';
        }
        """
    )
    findings = audit_rtl.scan_text(js, "place.js")
    labels = {f.pattern for f in findings}
    assert "js-style-left" in labels
    assert "js-style-marginLeft" in labels
    # js-style-left is P0 (positioning), marginLeft is P1.
    by_pattern = {f.pattern: f for f in findings}
    assert by_pattern["js-style-left"].severity == audit_rtl.P0
    assert by_pattern["js-style-marginLeft"].severity == audit_rtl.P1


# ---------------------------------------------------------------------------
# 6. CSV output schema
# ---------------------------------------------------------------------------


def test_csv_output_schema_and_round_trip():
    findings = [
        audit_rtl.Finding(
            file="x.css",
            line=3,
            column=5,
            severity=audit_rtl.P0,
            pattern="float-left",
            snippet=".x { float: left; }",
            suggested_fix="float: inline-start",
        )
    ]
    buf = io.StringIO()
    audit_rtl.write_csv(findings, buf)
    buf.seek(0)
    rows = list(csv.reader(buf))
    assert rows[0] == [
        "file",
        "line",
        "column",
        "severity",
        "pattern",
        "snippet",
        "suggested_fix",
    ]
    assert rows[1] == [
        "x.css",
        "3",
        "5",
        "P0",
        "float-left",
        ".x { float: left; }",
        "float: inline-start",
    ]


# ---------------------------------------------------------------------------
# 7. Severity classification correct across P0/P1/P2
# ---------------------------------------------------------------------------


def test_severity_classification_across_patterns():
    css = textwrap.dedent(
        """\
        .a { float: left; }                /* P0 */
        .b { margin-left: 8px; }           /* P1 */
        .c { direction: ltr; }             /* P2 */
        .d { left: 10px; }                 /* P0 */
        .e { text-align: right; }          /* P1 */
        """
    )
    findings = audit_rtl.scan_text(css, "mix.css")
    sevs = {f.pattern: f.severity for f in findings}
    assert sevs["float-left"] == audit_rtl.P0
    assert sevs["margin-left"] == audit_rtl.P1
    assert sevs["direction-ltr-hardcoded"] == audit_rtl.P2
    assert sevs["left-positioning"] == audit_rtl.P0
    assert sevs["text-align-right"] == audit_rtl.P1


# ---------------------------------------------------------------------------
# 8. Real audit on landing/ directory produces meaningful counts
# ---------------------------------------------------------------------------


def test_landing_audit_returns_nonempty_with_p0_findings():
    root = REPO_ROOT / "landing"
    if not root.exists():
        return  # nothing to audit on a stripped checkout
    findings = audit_rtl.scan_directory(root)
    summary = audit_rtl.summarize(findings)
    # Sanity bounds — we expect somewhere between 100 and 5000.
    assert 100 <= summary["total"] <= 5000, summary["total"]
    # Portal page is the densest; expect at least 1 P0 finding overall.
    assert summary["by_severity"].get(audit_rtl.P0, 0) >= 1
    # Idempotency check: rtl.css should NOT trip any P1 margin-left findings
    rtl_css_findings = [
        f for f in findings if f.file.endswith("rtl.css") and f.pattern == "margin-left"
    ]
    assert rtl_css_findings == []
