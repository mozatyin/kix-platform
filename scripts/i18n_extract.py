"""i18n string extraction — Python AST walker.

Walks ``app/**/*.py`` (or a single file) and emits a CSV of every
candidate string that should become a translation key.

Usage:
    .venv/bin/python -m scripts.i18n_extract                              # all app/
    .venv/bin/python -m scripts.i18n_extract --target app/routers/x.py
    .venv/bin/python -m scripts.i18n_extract --target app/routers/x.py --llm
    .venv/bin/python -m scripts.i18n_extract --apply                      # NOT IMPLEMENTED yet — dry-run by default

Output CSV columns:
    file, line, original, category, proposed_key, is_user_facing

Skips:
    * Strings inside ``# noqa: i18n`` or ``# i18n-ignore`` annotated lines.
    * Pure SQL / regex / JSON-schema literals.
    * dict keys, log argument formats, identifier-shaped strings.

Design notes:
    * Single-pass AST walk; no source rewriting unless ``--apply`` is set
      (Phase 2 will add the rewriter — kept as a stub here).
    * Idempotent: re-running over the same file yields the same CSV.
    * LLM calls are quota-guarded via ``scripts.llm_quota_monitor``.
"""
from __future__ import annotations

import argparse
import ast
import csv
import logging
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.i18n_prompts import (  # noqa: E402
    Classification,
    classify_sync,
    heuristic_classify,
)

logger = logging.getLogger("i18n_extract")

CJK_RE = re.compile(r"[一-鿿　-〿]")
NL_SENT_RE = re.compile(r"^[A-Z][\w'’\- ]{2,}[\.!\?]$")
SQL_RE = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\b", re.I)
REGEX_HINT_RE = re.compile(r"[\\^$\[\]().*+?{}|]")
TEMPLATE_VAR_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
IGNORE_COMMENT_RE = re.compile(r"#\s*(noqa:\s*i18n|i18n-ignore)\b", re.I)


@dataclass
class Candidate:
    file: str
    line: int
    original: str
    category: str
    proposed_key: str
    is_user_facing: str

    def to_row(self) -> dict[str, str]:
        return {
            "file": self.file,
            "line": str(self.line),
            "original": self.original,
            "category": self.category,
            "proposed_key": self.proposed_key,
            "is_user_facing": self.is_user_facing,
        }


def _has_cjk(s: str) -> bool:
    return bool(CJK_RE.search(s))


def _looks_natural_lang(s: str) -> bool:
    s = s.strip()
    if not s:
        return False
    if _has_cjk(s):
        # Any CJK char counts — single char like "买" is a button label.
        return True
    if len(s) < 3:
        return False
    if NL_SENT_RE.match(s):
        return True
    # 2+ words with at least one space and starts with capital
    if " " in s and s[0:1].isupper() and len(s.split()) >= 2:
        return True
    return False


def _looks_internal(s: str) -> bool:
    s = s.strip()
    if not s:
        return True
    if _has_cjk(s):
        return False
    if SQL_RE.match(s):
        return True
    if re.match(r"^[A-Z_][A-Z0-9_]*$", s):  # SCREAMING_SNAKE
        return True
    if re.match(r"^[a-z_][a-z0-9_]*$", s):  # snake_case identifier
        return True
    # Regex-ish heuristic: short + heavy meta chars + no spaces
    if " " not in s and len(s) <= 40 and REGEX_HINT_RE.search(s):
        return True
    # Pure path / URL
    if re.match(r"^(https?://|/|\./)\S+$", s):
        return True
    return False


def _ignored_by_comment(source_lines: list[str], lineno: int) -> bool:
    """Return True if the line (or the line above) carries an i18n-ignore marker."""
    for li in (lineno - 1, lineno - 2):
        if 0 <= li < len(source_lines) and IGNORE_COMMENT_RE.search(source_lines[li]):
            return True
    return False


def _string_value_of_node(node: ast.AST) -> tuple[str, bool]:
    """Return (value, is_f_string).  Empty value means not a string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, False
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            elif isinstance(v, ast.FormattedValue):
                # Reconstruct {name} placeholder when possible
                name = ""
                if isinstance(v.value, ast.Name):
                    name = v.value.id
                elif isinstance(v.value, ast.Attribute):
                    name = v.value.attr
                parts.append("{" + (name or "var") + "}")
        return "".join(parts), True
    return "", False


class _Walker(ast.NodeVisitor):
    def __init__(self, file: str, source_lines: list[str]) -> None:
        self.file = file
        self.source_lines = source_lines
        self.candidates: list[tuple[int, str, bool]] = []  # (lineno, value, is_fstring)
        self._skip_keys: set[int] = set()  # id(node) of dict-key / docstring nodes

    def _mark_docstring(self, body: list[ast.stmt]) -> None:
        if not body:
            return
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            self._skip_keys.add(id(first.value))

    def visit_Module(self, node: ast.Module) -> None:
        self._mark_docstring(node.body)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._mark_docstring(node.body)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._mark_docstring(node.body)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._mark_docstring(node.body)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        # Mark dict keys for skipping — they are usually identifiers
        for k in node.keys:
            if k is not None:
                self._skip_keys.add(id(k))
        self.generic_visit(node)

    def visit_keyword(self, node: ast.keyword) -> None:  # kwargs in calls
        # Keep visiting values; arg names are not strings anyway
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # Skip logging.*("template", arg, arg) format-arg templates
        func = node.func
        log_fn = False
        if isinstance(func, ast.Attribute):
            if func.attr in {"debug", "info", "warning", "error", "exception", "critical"}:
                log_fn = True
        if log_fn and node.args:
            # Mark first arg (format template) as skip
            self._skip_keys.add(id(node.args[0]))
        self.generic_visit(node)

    def _record(self, node: ast.AST) -> None:
        if id(node) in self._skip_keys:
            return
        value, is_f = _string_value_of_node(node)
        if not value:
            return
        lineno = getattr(node, "lineno", 0)
        if _ignored_by_comment(self.source_lines, lineno):
            return
        if _looks_internal(value):
            return
        if not _looks_natural_lang(value):
            return
        self.candidates.append((lineno, value, is_f))

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self._record(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        self._record(node)
        # Don't descend — the constant parts would otherwise be re-visited
        # and produce duplicates.


def extract_from_source(file: str, source: str) -> list[tuple[int, str, bool]]:
    """Return raw candidates: (lineno, value, is_fstring)."""
    try:
        tree = ast.parse(source, filename=file)
    except SyntaxError as e:
        logger.warning("Skipping %s — SyntaxError: %s", file, e)
        return []
    lines = source.splitlines()
    walker = _Walker(file, lines)
    walker.visit(tree)
    # Dedup preserving order
    seen: set[tuple[int, str]] = set()
    out: list[tuple[int, str, bool]] = []
    for lineno, val, is_f in walker.candidates:
        key = (lineno, val)
        if key in seen:
            continue
        seen.add(key)
        out.append((lineno, val, is_f))
    return out


def classify_candidates(
    file: str, raw: Iterable[tuple[int, str, bool]], use_llm: bool = False
) -> list[Candidate]:
    rel = file
    out: list[Candidate] = []
    for lineno, val, _is_f in raw:
        ctx = f"{rel} L{lineno}"
        c = classify_sync(val, ctx, use_llm=use_llm)
        if c.is_user_facing != "yes":
            # We still record non-extractable ones for review traceability,
            # but flag them — Wave-2 agents can filter on is_user_facing=="yes".
            pass
        out.append(
            Candidate(
                file=rel,
                line=lineno,
                original=val,
                category=c.category,
                proposed_key=c.proposed_key,
                is_user_facing=c.is_user_facing,
            )
        )
    return out


def iter_py_files(target: Path) -> Iterator[Path]:
    if target.is_file():
        yield target
        return
    for p in target.rglob("*.py"):
        # Skip generated / vendor dirs
        if any(part in p.parts for part in ("__pycache__", "migrations", "alembic", ".venv")):
            continue
        yield p


def unique_keys(rows: list[Candidate]) -> list[Candidate]:
    """Disambiguate proposed_key collisions by appending _2, _3, …"""
    seen: dict[str, int] = {}
    out: list[Candidate] = []
    for r in rows:
        if not r.proposed_key:
            out.append(r)
            continue
        key = r.proposed_key
        n = seen.get(key, 0)
        if n:
            new_key = f"{key}_{n + 1}"
            r = Candidate(**{**asdict(r), "proposed_key": new_key})
        seen[key] = n + 1
        out.append(r)
    return out


def write_csv(rows: list[Candidate], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["file", "line", "original", "category", "proposed_key", "is_user_facing"],
        )
        w.writeheader()
        for r in rows:
            w.writerow(r.to_row())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Extract i18n candidate strings from Python source.")
    p.add_argument("--target", default="app", help="File or directory to walk (default: app)")
    p.add_argument("--output", default="i18n_extracted_report.csv", help="CSV output path")
    p.add_argument("--llm", action="store_true", help="Use LLM classifier (quota-guarded)")
    p.add_argument(
        "--apply",
        action="store_true",
        help="REWRITE source files (NOT IMPLEMENTED — Phase 2). Dry-run only.",
    )
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    target = Path(args.target)
    if not target.exists():
        # try relative to repo root
        target = REPO_ROOT / args.target
    if not target.exists():
        logger.error("Target not found: %s", args.target)
        return 2

    if args.apply:
        logger.warning("--apply is a Phase-2 deliverable; running in dry-run mode anyway.")

    all_rows: list[Candidate] = []
    file_count = 0
    for path in iter_py_files(target):
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
        raw = extract_from_source(rel, src)
        if not raw:
            continue
        rows = classify_candidates(rel, raw, use_llm=args.llm)
        all_rows.extend(rows)
        logger.info("%s — %d candidate strings", rel, len(rows))

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
