#!/usr/bin/env python3
"""Bible drift checker.

Compares numbers cited in ``KIX_GAMIFICATION_BIBLE.md`` (Appendix A) against the
real codebase. Fails (exit code 1) if any tracked metric drifts more than
``--threshold`` percent (default 5%).

Wire into pre-commit or CI to keep the Bible honest::

    python scripts/bible_check.py            # report only
    python scripts/bible_check.py --strict   # exit 1 on drift
    python scripts/bible_check.py --update   # rewrite Appendix A inline

Discipline rule (see ``BIBLE_CHANGELOG.md``): the Bible updates with every
major Wave. Drift > threshold = code shipped without doc update.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BIBLE = REPO / "KIX_GAMIFICATION_BIBLE.md"


@dataclass
class Metric:
    key: str
    actual: int
    label: str
    pattern: re.Pattern[str]


def _count_files(glob: str, exclude: list[str] | None = None) -> int:
    """Count files matching ``glob`` relative to repo root."""
    exclude = exclude or []
    return sum(
        1
        for p in REPO.glob(glob)
        if p.is_file() and not any(token in p.name for token in exclude)
    )


def _count_dirs(glob: str, exclude: list[str] | None = None) -> int:
    """Count directories matching ``glob`` relative to repo root."""
    exclude = exclude or []
    return sum(
        1
        for p in REPO.glob(glob)
        if p.is_dir() and not any(token in p.name for token in exclude)
    )


def _count_endpoints() -> int:
    """Count ``@router.<verb>`` decorators across ``app/routers/*.py``."""
    total = 0
    for p in (REPO / "app" / "routers").glob("*.py"):
        if p.name == "__init__.py":
            continue
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.lstrip().startswith("@router."):
                total += 1
    return total


def _count_test_functions() -> int:
    total = 0
    test_dir = REPO / "tests"
    if not test_dir.exists():
        return 0
    for p in test_dir.rglob("test_*.py"):
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.lstrip()
            if stripped.startswith("def test_") or stripped.startswith("async def test_"):
                total += 1
    return total


def _count_recipes() -> int:
    path = REPO / "app" / "data" / "recipes_seed.json"
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    return len(data) if isinstance(data, list) else 0


def _count_python_loc() -> int:
    total = 0
    for p in (REPO / "app").rglob("*.py"):
        try:
            total += sum(1 for _ in p.open("r", encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return total


def _git_head() -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


# Patterns match the "key  : NN" lines in Appendix A of the Bible.
METRICS = [
    Metric(
        key="routers",
        actual=_count_files("app/routers/*.py", exclude=["__init__"]),
        label="routers",
        pattern=re.compile(r"^\s*routers\s*:\s*(\d[\d,]*)"),
    ),
    Metric(
        key="endpoints",
        actual=_count_endpoints(),
        label="endpoints",
        pattern=re.compile(r"^\s*endpoints\s*:\s*(\d[\d,]*)"),
    ),
    Metric(
        key="workers",
        actual=_count_files("app/workers/*.py", exclude=["__init__"]),
        label="workers",
        pattern=re.compile(r"^\s*workers\s*:\s*(\d[\d,]*)"),
    ),
    Metric(
        key="services",
        actual=_count_files("app/services/*.py", exclude=["__init__"]),
        label="services",
        pattern=re.compile(r"^\s*services\s*:\s*(\d[\d,]*)"),
    ),
    Metric(
        key="migrations",
        actual=_count_files("migrations/versions/*.py"),
        label="migrations",
        pattern=re.compile(r"^\s*migrations\s*:\s*(\d[\d,]*)"),
    ),
    Metric(
        key="test_files",
        actual=_count_files("tests/test_*.py"),
        label="test files",
        pattern=re.compile(r"^\s*test files\s*:\s*(\d[\d,]*)"),
    ),
    Metric(
        key="test_functions",
        actual=_count_test_functions(),
        label="test functions",
        pattern=re.compile(r"^\s*test functions\s*:\s*(\d[\d,]*)"),
    ),
    Metric(
        key="recipes",
        actual=_count_recipes(),
        label="recipes",
        pattern=re.compile(r"^\s*recipes\s*:\s*(\d[\d,]*)"),
    ),
    Metric(
        key="industry_sims",
        actual=_count_files("scripts/sim_lao*.py"),
        label="industry sims",
        pattern=re.compile(r"^\s*industry sims\s*:\s*(\d[\d,]*)"),
    ),
    Metric(
        key="locales",
        actual=_count_dirs("app/i18n/catalogs/*"),
        label="locales",
        pattern=re.compile(r"^\s*locales\s*:\s*(\d[\d,]*)"),
    ),
    Metric(
        key="psps",
        actual=_count_files(
            "app/services/payment_psps/*.py", exclude=["__init__", "_common"]
        ),
        label="psp clients",
        pattern=re.compile(r"^\s*scaffolded clients\s*:\s*(\d[\d,]*)"),
    ),
]


def _parse_bible() -> dict[str, int]:
    """Extract the integer next to each metric label in the Bible."""
    if not BIBLE.exists():
        return {}
    text = BIBLE.read_text(encoding="utf-8")
    parsed: dict[str, int] = {}
    for metric in METRICS:
        # Re-compile with MULTILINE so ``^`` anchors each line.
        pattern = re.compile(metric.pattern.pattern, re.MULTILINE)
        match = pattern.search(text)
        if match:
            parsed[metric.key] = int(match.group(1).replace(",", ""))
    return parsed


def _drift_pct(actual: int, cited: int) -> float:
    if cited == 0:
        return 0.0 if actual == 0 else 100.0
    return abs(actual - cited) / cited * 100.0


def run(threshold: float, strict: bool) -> int:
    cited = _parse_bible()
    head = _git_head()
    print(f"Bible drift check — HEAD={head} threshold={threshold:.1f}%\n")

    rows: list[tuple[str, int, int | None, float, bool]] = []
    fail = False

    for metric in METRICS:
        cited_val = cited.get(metric.key)
        drift = (
            _drift_pct(metric.actual, cited_val) if cited_val is not None else None
        )
        over = drift is not None and drift > threshold
        rows.append(
            (
                metric.label,
                metric.actual,
                cited_val,
                drift if drift is not None else -1.0,
                over,
            )
        )
        if over:
            fail = True

    width_label = max(len(r[0]) for r in rows) + 2
    print(f"{'metric'.ljust(width_label)} {'actual':>8} {'bible':>8} {'drift':>8}")
    print("-" * (width_label + 30))
    for label, actual, cited_val, drift, over in rows:
        cited_disp = "—" if cited_val is None else f"{cited_val:,}"
        drift_disp = "—" if drift < 0 else f"{drift:5.1f}%"
        flag = " DRIFT" if over else (" missing" if cited_val is None else "")
        print(
            f"{label.ljust(width_label)} {actual:>8,} {cited_disp:>8} "
            f"{drift_disp:>8}{flag}"
        )

    if fail:
        print(
            f"\nFAIL · one or more metrics drifted > {threshold:.1f}%. "
            "Update KIX_GAMIFICATION_BIBLE.md (Appendix A) and BIBLE_CHANGELOG.md."
        )
        return 1 if strict else 0
    if any(r[2] is None for r in rows):
        print(
            "\nWARN · some metrics not found in Bible. Add them to Appendix A."
        )
        return 1 if strict else 0
    print("\nOK · Bible matches code reality.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--threshold",
        type=float,
        default=5.0,
        help="Max allowed drift %% before failure (default 5.0)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 on any drift > threshold (use in CI / pre-commit).",
    )
    args = parser.parse_args()
    return run(args.threshold, args.strict)


if __name__ == "__main__":
    sys.exit(main())
