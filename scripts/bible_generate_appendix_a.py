"""B · Bible auto-generate Appendix A from code (replaces hand-edit drift).

Today: humans edit KIX_GAMIFICATION_BIBLE.md Appendix A to update counts.
Drift inevitable. bible_check catches it but cure is manual edit.

This: Appendix A becomes a GENERATED artifact (like Cargo.lock). Bible
source contains the narrative chapters; Appendix A is rewritten on every
deploy. Drift becomes structurally impossible.

Usage:
  python -m scripts.bible_generate_appendix_a                  # show
  python -m scripts.bible_generate_appendix_a --write          # write to Bible
  python -m scripts.bible_generate_appendix_a --check          # exit 1 if Bible doesn't match what would be generated

Wired into:
  - scripts/cron_nightly_refresh.sh Stage 5 (--check)
  - .github/workflows/bible-diff-bot.yml (--check)
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BIBLE = REPO / "KIX_GAMIFICATION_BIBLE.md"
APPENDIX_A_START = "<!-- BIBLE-APPENDIX-A:START -->"
APPENDIX_A_END = "<!-- BIBLE-APPENDIX-A:END -->"


def _count_files(glob: str, exclude=("__init__",)) -> int:
    return sum(
        1 for p in REPO.glob(glob)
        if p.is_file() and not any(e in p.name for e in exclude)
    )


def _count_endpoints() -> int:
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
    for p in (REPO / "tests").rglob("test_*.py"):
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.lstrip()
            if s.startswith("def test_") or s.startswith("async def test_"):
                total += 1
    return total


def _python_loc() -> int:
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
    except Exception:
        return "unknown"


def _last_commit_subject() -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(REPO), "log", "-1", "--pretty=%s"],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()[:90]
    except Exception:
        return "unknown"


def generate() -> str:
    """Return the canonical Appendix A markdown block. Pure function."""
    routers = _count_files("app/routers/*.py")
    endpoints = _count_endpoints()
    workers = _count_files("app/workers/*.py")
    services = _count_files("app/services/*.py")
    migrations = _count_files("migrations/versions/*.py")
    test_files = _count_files("tests/test_*.py")
    test_fns = _count_test_functions()
    recipes = (REPO / "app" / "data" / "recipes_seed.json")
    recipes_n = 0
    if recipes.exists():
        import json
        try:
            recipes_n = len(json.loads(recipes.read_text()))
        except Exception:
            pass
    industry_sims = _count_files("scripts/sim_lao*.py")
    locales = sum(1 for p in (REPO / "app" / "i18n" / "catalogs").glob("*") if p.is_dir())
    psps = _count_files("app/services/payment_psps/*.py", exclude=("__init__", "_common"))
    py_loc = _python_loc()
    head = _git_head()
    last_commit = _last_commit_subject()

    # Brand landings (Wave M generation surface)
    brand_landings = _count_files("landing/brands/*/index.html", exclude=())
    deprecated_pages = 0
    dep_reg = REPO / "data" / "deprecation_registry.json"
    if dep_reg.exists():
        import json
        try:
            data = json.loads(dep_reg.read_text())
            deprecated_pages = sum(
                1 for r in data.get("deprecations", []) if r.get("deprecated_at")
            )
        except Exception:
            pass

    return (
        f"{APPENDIX_A_START}\n"
        f"```\n"
        f"HEAD                : {head}\n"
        f"Last commit         : {last_commit}\n"
        f"Generated           : auto · run `python -m scripts.bible_generate_appendix_a --write`\n"
        f"\n"
        f"Code surface (excludes __init__.py)\n"
        f"  routers           : {routers}\n"
        f"  endpoints         : {endpoints:,}\n"
        f"  workers           : {workers}\n"
        f"  services          : {services}\n"
        f"  migrations        : {migrations}\n"
        f"  total Python LOC  : {py_loc:,}\n"
        f"\n"
        f"Test surface\n"
        f"  test files        : {test_files}\n"
        f"  test functions    : {test_fns:,}\n"
        f"\n"
        f"Data\n"
        f"  recipes           : {recipes_n}\n"
        f"  industries        : 26   (static)\n"
        f"  industry sims     : {industry_sims}\n"
        f"\n"
        f"i18n\n"
        f"  locales           : {locales}\n"
        f"  base locales done : 4    (en-SG, en-US, zh-Hans-SG, zh-Hans-CN)\n"
        f"  needs translation : 7 locales\n"
        f"\n"
        f"PSPs\n"
        f"  scaffolded clients: {psps}\n"
        f"  live in prod      : 0\n"
        f"\n"
        f"Landing-gen surface (Wave M)\n"
        f"  brand landings    : {brand_landings}\n"
        f"  deprecated pages  : {deprecated_pages}\n"
        f"```\n"
        f"{APPENDIX_A_END}"
    )


def _read_bible() -> str:
    return BIBLE.read_text(encoding="utf-8")


def _splice(text: str, new_block: str) -> str:
    """Replace the existing marked block with new_block. If markers absent,
    append the block at end of Appendix A header."""
    pattern = re.compile(
        re.escape(APPENDIX_A_START) + r".*?" + re.escape(APPENDIX_A_END),
        re.DOTALL,
    )
    if pattern.search(text):
        return pattern.sub(new_block, text)
    # markers absent — first run; insert AFTER the ```...``` block in Appendix A
    # find first ``` after "# Appendix A" and replace through closing ```
    m = re.search(r"(^# Appendix A.*?\n)(```\n.*?\n```)", text,
                  re.DOTALL | re.MULTILINE)
    if m:
        return text[:m.end(1)] + new_block + text[m.end(2):]
    return text + "\n\n" + new_block + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--write", action="store_true", help="write to Bible")
    p.add_argument("--check", action="store_true",
                   help="exit 1 if Bible would change")
    args = p.parse_args()

    block = generate()
    if args.check:
        text = _read_bible()
        spliced = _splice(text, block)
        if text == spliced:
            print("OK · Bible Appendix A matches generated output.")
            return 0
        print("DRIFT · Bible Appendix A is stale. Run with --write.")
        return 1

    if args.write:
        text = _read_bible()
        new_text = _splice(text, block)
        if text == new_text:
            print("No change.")
            return 0
        BIBLE.write_text(new_text)
        print(f"wrote {len(block)} chars to {BIBLE.relative_to(REPO)} Appendix A block")
        return 0

    print(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
