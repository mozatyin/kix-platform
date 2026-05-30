"""i18n CI linter — flag hardcoded user-facing strings.

Detects eight classes of i18n hygiene violations across ``app/`` and
``landing/``. Designed for CI: emits JUnit XML and GitHub Actions
annotation lines; exits non-zero on P0 violations.

Rules
-----
==========  =========================================  =====
ID          Description                                Sev.
==========  =========================================  =====
R-001       ``HTTPException(detail="<non-ASCII>")``     P0
R-002       Hardcoded currency glyph (¥/$) outside      P0
            ``app/i18n/`` and formatting helpers
R-003       ``name_en`` / ``name_cn`` dual-key dict     P1
            pattern (migrate to Fluent catalog)
R-004       f-string concatenation of user-facing       P1
            text (likely needs ``t()`` wrapper)
R-005       Hardcoded date format strings outside       P2
            formatting modules
R-006       Comparison ``x == "<non-ASCII literal>"``    P1
            (use enum/code, not localised string)
R-007       ``data-i18n="key"`` whose key is missing    P0
            from every locale JSON catalog
R-008       Catalog key declared but no translation     P2
            in at least one non-source locale
==========  =========================================  =====

Whitelist
---------
``scripts/i18n_lint_ignore.txt`` — one ``<rule>:<path>:<lineno>`` per line
(``lineno`` optional, ``*`` allowed for any).

Usage
-----
::

    python -m scripts.i18n_lint                       # report all rules
    python -m scripts.i18n_lint --severity p0         # only P0 (CI gate)
    python -m scripts.i18n_lint --files a.py b.py     # pre-commit subset
    python -m scripts.i18n_lint --junit lint.xml      # write JUnit
    python -m scripts.i18n_lint --github-annotations  # emit ::error lines
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

# --------------------------------------------------------------------------- #
# Severity
# --------------------------------------------------------------------------- #

P0_RULES = {"R-001", "R-002", "R-007"}
P1_RULES = {"R-003", "R-004", "R-006"}
P2_RULES = {"R-005", "R-008"}
ALL_RULES = P0_RULES | P1_RULES | P2_RULES


def severity_of(rule: str) -> str:
    if rule in P0_RULES:
        return "p0"
    if rule in P1_RULES:
        return "p1"
    return "p2"


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #


@dataclass
class Finding:
    rule: str
    path: Path
    line: int
    message: str
    snippet: str = ""

    @property
    def severity(self) -> str:
        return severity_of(self.rule)


@dataclass
class LintConfig:
    repo_root: Path
    files: list[Path] | None = None
    whitelist: set[tuple[str, str, str]] = field(default_factory=set)
    # paths *allowed* to contain currency glyphs / date formats:
    formatting_modules: tuple[str, ...] = (
        "app/i18n/",
        "app/region.py",
    )


# --------------------------------------------------------------------------- #
# Whitelist
# --------------------------------------------------------------------------- #


def load_whitelist(path: Path) -> set[tuple[str, str, str]]:
    """Parse ``scripts/i18n_lint_ignore.txt``."""
    out: set[tuple[str, str, str]] = set()
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.split("#", 1)[0].strip()
        if not s:
            continue
        parts = s.split(":")
        if len(parts) == 2:
            rule, file_glob = parts
            line = "*"
        elif len(parts) == 3:
            rule, file_glob, line = parts
        else:
            continue
        out.add((rule.strip(), file_glob.strip(), line.strip()))
    return out


def _is_whitelisted(finding: Finding, wl: set[tuple[str, str, str]],
                    root: Path) -> bool:
    rel = str(finding.path.relative_to(root)) if finding.path.is_absolute() else str(finding.path)
    for rule, pat, line in wl:
        if rule != finding.rule and rule != "*":
            continue
        if not _glob_match(pat, rel):
            continue
        if line in ("*", "", str(finding.line)):
            return True
    return False


def _glob_match(pattern: str, path: str) -> bool:
    import fnmatch
    return fnmatch.fnmatch(path, pattern)


# --------------------------------------------------------------------------- #
# Catalog loading
# --------------------------------------------------------------------------- #


def _load_landing_catalog_keys(root: Path) -> dict[str, set[str]]:
    """Return ``{locale -> set(keys)}`` for the landing JSON catalogs."""
    out: dict[str, set[str]] = {}
    base = root / "landing" / "i18n" / "locales"
    if not base.is_dir():
        return out
    for locale_dir in base.iterdir():
        if not locale_dir.is_dir():
            continue
        keys: set[str] = set()
        for jf in locale_dir.glob("*.json"):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            for k in _flatten_json_keys(data):
                keys.add(k)
        out[locale_dir.name] = keys
    return out


def _flatten_json_keys(node, prefix: str = "") -> Iterable[str]:
    if isinstance(node, dict):
        for k, v in node.items():
            full = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)):
                yield from _flatten_json_keys(v, full)
            else:
                yield full
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _flatten_json_keys(v, f"{prefix}[{i}]")


def _load_ftl_catalog_keys(root: Path) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    base = root / "app" / "i18n" / "catalogs"
    if not base.is_dir():
        return out
    # Fluent identifiers may contain dots in KiX (e.g. ``messages.count``);
    # the canonical ``[a-zA-Z][\w-]*`` form would silently drop those keys.
    key_re = re.compile(r"^([a-zA-Z][\w.-]*)\s*=")
    for locale_dir in base.iterdir():
        if not locale_dir.is_dir():
            continue
        keys: set[str] = set()
        for ftl in locale_dir.glob("*.ftl"):
            for line in ftl.read_text(encoding="utf-8").splitlines():
                m = key_re.match(line)
                if m:
                    keys.add(m.group(1))
        out[locale_dir.name] = keys
    return out


# --------------------------------------------------------------------------- #
# AST-based python rules
# --------------------------------------------------------------------------- #


_CJK_RE = re.compile(r"[　-鿿＀-￯]")
_DATE_FMT_RE = re.compile(r"%[YymdHMSjB]")


def _has_cjk(s: str) -> bool:
    return bool(_CJK_RE.search(s))


def _scan_python(path: Path, cfg: LintConfig) -> list[Finding]:
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    findings: list[Finding] = []
    rel = str(path.relative_to(cfg.repo_root)) if path.is_absolute() else str(path)
    in_formatting = any(rel.startswith(mod.rstrip("/")) for mod in cfg.formatting_modules)

    for node in ast.walk(tree):
        # R-001: HTTPException(detail="<non-ASCII>")
        if isinstance(node, ast.Call):
            func_name = _call_name(node.func)
            if func_name == "HTTPException":
                for kw in node.keywords:
                    if kw.arg == "detail" and isinstance(kw.value, ast.Constant) \
                            and isinstance(kw.value.value, str) and _has_cjk(kw.value.value):
                        findings.append(Finding(
                            rule="R-001",
                            path=path,
                            line=kw.value.lineno,
                            message=f"HTTPException detail contains localised string; use error code instead: {kw.value.value!r}",
                            snippet=kw.value.value[:60],
                        ))

        # R-006: comparison to non-ASCII string literal
        if isinstance(node, ast.Compare):
            for cmp in node.comparators + [node.left]:
                if isinstance(cmp, ast.Constant) and isinstance(cmp.value, str) \
                        and _has_cjk(cmp.value):
                    findings.append(Finding(
                        rule="R-006",
                        path=path,
                        line=cmp.lineno,
                        message=f"Comparison against localised string {cmp.value!r}; use enum/code",
                        snippet=cmp.value[:60],
                    ))

        # R-003: dict with name_en / name_cn keys
        if isinstance(node, ast.Dict):
            string_keys = {
                k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            if {"name_en", "name_cn"} <= string_keys or \
               {"description_en", "description_cn"} <= string_keys:
                findings.append(Finding(
                    rule="R-003",
                    path=path,
                    line=node.lineno,
                    message="Dual-key (_en/_cn) dict pattern — migrate to Fluent catalog",
                ))

        # R-002 / R-005 string-literal scans
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            val = node.value
            if not in_formatting:
                if ("¥" in val or _is_bare_currency_template(val)):
                    findings.append(Finding(
                        rule="R-002",
                        path=path,
                        line=node.lineno,
                        message=f"Hardcoded currency glyph in string literal: {val!r}",
                        snippet=val[:60],
                    ))
                if _DATE_FMT_RE.search(val) and any(tok in val for tok in ("%Y", "%m", "%d")):
                    findings.append(Finding(
                        rule="R-005",
                        path=path,
                        line=node.lineno,
                        message=f"Hardcoded date format {val!r}; use formatting helper",
                        snippet=val[:60],
                    ))

        # R-004: f-string with user-facing CJK content + interpolation
        if isinstance(node, ast.JoinedStr):
            has_cjk = any(
                isinstance(v, ast.Constant) and isinstance(v.value, str)
                and _has_cjk(v.value)
                for v in node.values
            )
            has_interp = any(isinstance(v, ast.FormattedValue) for v in node.values)
            if has_cjk and has_interp:
                findings.append(Finding(
                    rule="R-004",
                    path=path,
                    line=node.lineno,
                    message="f-string concatenates localised text; use t() with placeholders",
                ))

    return findings


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_bare_currency_template(val: str) -> bool:
    """e.g. ``"$ {amount}"`` — ``$`` followed by space/brace, not regex/identifier."""
    return bool(re.search(r"(?:^|[\s\(\[])\$\s*\{", val))


# --------------------------------------------------------------------------- #
# HTML rule R-007
# --------------------------------------------------------------------------- #


_DATA_I18N_RE = re.compile(r'data-i18n="([^"]+)"')


def _scan_html(path: Path, cfg: LintConfig,
               landing_keys_union: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return findings
    for i, line in enumerate(text.splitlines(), start=1):
        for m in _DATA_I18N_RE.finditer(line):
            key = m.group(1).strip()
            if key and key not in landing_keys_union:
                findings.append(Finding(
                    rule="R-007",
                    path=path,
                    line=i,
                    message=f"data-i18n key {key!r} not declared in any locale catalog",
                    snippet=key,
                ))
    return findings


# --------------------------------------------------------------------------- #
# Catalog rule R-008
# --------------------------------------------------------------------------- #


def _check_missing_translations(catalogs: dict[str, set[str]],
                                source_locale: str,
                                cfg: LintConfig) -> list[Finding]:
    findings: list[Finding] = []
    if source_locale not in catalogs:
        return findings
    src_keys = catalogs[source_locale]
    for locale, keys in catalogs.items():
        if locale == source_locale or locale.startswith("xx-"):
            continue
        missing = src_keys - keys
        for k in sorted(missing):
            findings.append(Finding(
                rule="R-008",
                path=cfg.repo_root / "app" / "i18n" / "catalogs" / locale,
                line=0,
                message=f"key {k!r} present in {source_locale} but missing in {locale}",
                snippet=k,
            ))
    return findings


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def _iter_python_files(root: Path) -> Iterable[Path]:
    for base in ("app", "scripts"):
        b = root / base
        if not b.is_dir():
            continue
        for p in b.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            yield p


def _iter_html_files(root: Path) -> Iterable[Path]:
    landing = root / "landing"
    if not landing.is_dir():
        return
    for p in landing.glob("*.html"):
        yield p


def run_lint(cfg: LintConfig) -> list[Finding]:
    findings: list[Finding] = []

    landing_catalogs = _load_landing_catalog_keys(cfg.repo_root)
    ftl_catalogs = _load_ftl_catalog_keys(cfg.repo_root)
    landing_union: set[str] = set().union(*landing_catalogs.values()) if landing_catalogs else set()

    py_files = list(cfg.files) if cfg.files else list(_iter_python_files(cfg.repo_root))
    html_files = []
    if cfg.files:
        html_files = [f for f in cfg.files if f.suffix == ".html"]
    else:
        html_files = list(_iter_html_files(cfg.repo_root))

    for f in py_files:
        if f.suffix != ".py":
            continue
        findings.extend(_scan_python(f, cfg))

    for f in html_files:
        findings.extend(_scan_html(f, cfg, landing_union))

    # Catalog parity (FTL): check zh-Hans-SG vs en-SG (the source).
    findings.extend(_check_missing_translations(ftl_catalogs, "en-SG", cfg))

    # Whitelist filter.
    findings = [f for f in findings if not _is_whitelisted(f, cfg.whitelist, cfg.repo_root)]
    findings.sort(key=lambda f: (f.rule, str(f.path), f.line))
    return findings


# --------------------------------------------------------------------------- #
# Reporters
# --------------------------------------------------------------------------- #


def report_text(findings: list[Finding]) -> str:
    if not findings:
        return "i18n_lint: 0 findings"
    lines = []
    for f in findings:
        lines.append(f"[{f.severity.upper()}] {f.rule} {f.path}:{f.line}  {f.message}")
    lines.append(f"--\n{len(findings)} findings")
    return "\n".join(lines)


def report_github(findings: list[Finding]) -> str:
    """One GitHub Actions ``::error|::warning`` line per finding."""
    out = []
    for f in findings:
        level = "error" if f.severity == "p0" else "warning"
        msg = f.message.replace("\n", " ").replace("%", "%25")
        out.append(f"::{level} file={f.path},line={f.line},title={f.rule}::{msg}")
    return "\n".join(out)


def report_junit(findings: list[Finding]) -> str:
    suite = ET.Element("testsuite", attrib={
        "name": "i18n_lint",
        "tests": str(max(len(findings), 1)),
        "failures": str(len(findings)),
    })
    if not findings:
        ET.SubElement(suite, "testcase", attrib={"classname": "i18n", "name": "ok"})
    for i, f in enumerate(findings):
        tc = ET.SubElement(suite, "testcase", attrib={
            "classname": f.rule,
            "name": f"{f.path}:{f.line}",
        })
        fail = ET.SubElement(tc, "failure", attrib={
            "type": f.rule,
            "message": f.message,
        })
        fail.text = f.snippet
    return ET.tostring(suite, encoding="unicode")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="i18n CI linter for KiX platform.")
    p.add_argument("--severity", choices=("p0", "p1", "p2", "all"), default="all")
    p.add_argument("--files", nargs="*", type=Path,
                   help="explicit file list (e.g. from pre-commit staged set)")
    p.add_argument("--root", type=Path, default=Path.cwd(),
                   help="repo root (default: cwd)")
    p.add_argument("--whitelist", type=Path,
                   default=Path("scripts/i18n_lint_ignore.txt"))
    p.add_argument("--junit", type=Path,
                   help="write JUnit XML to this path")
    p.add_argument("--github-annotations", action="store_true",
                   help="emit GitHub Actions ::error|::warning lines on stdout")
    p.add_argument("--quiet", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    root = args.root.resolve()
    cfg = LintConfig(
        repo_root=root,
        files=[p.resolve() for p in args.files] if args.files else None,
        whitelist=load_whitelist(args.whitelist if args.whitelist.is_absolute()
                                 else root / args.whitelist),
    )
    findings = run_lint(cfg)

    if args.severity != "all":
        wanted = {"p0": P0_RULES, "p1": P0_RULES | P1_RULES, "p2": ALL_RULES}[args.severity]
        findings = [f for f in findings if f.rule in wanted]

    if not args.quiet:
        print(report_text(findings))
    if args.github_annotations:
        gh = report_github(findings)
        if gh:
            print(gh)
    if args.junit:
        args.junit.parent.mkdir(parents=True, exist_ok=True)
        args.junit.write_text(report_junit(findings), encoding="utf-8")

    # Exit-code policy: 1 if any P0 violation, else 0.
    has_p0 = any(f.severity == "p0" for f in findings)
    return 1 if has_p0 else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
