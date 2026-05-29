"""Tests for the i18n extraction tooling.

These tests are stand-alone — they do not touch Redis or the FastAPI
app, so they run in isolation from the rest of the test suite and add
zero risk to the existing 305 tests.
"""
from __future__ import annotations

import csv
import io
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Load i18n modules directly by file path to bypass any namespace-package
# pollution caused by other routers (some inject /Users/mozat/eltm into
# sys.path, which has a colliding ``scripts`` package).
import importlib.util as _ilu


def _load(mod_name: str, file_path: Path):
    spec = _ilu.spec_from_file_location(mod_name, file_path)
    assert spec and spec.loader
    mod = _ilu.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_load("kix_i18n_prompts", REPO_ROOT / "scripts" / "i18n_prompts.py")
# i18n_extract imports `from scripts.i18n_prompts import …` — pre-load
# a shim package so that import resolves without touching eltm.
import types as _types

_pkg = _types.ModuleType("scripts")
_pkg.__path__ = [str(REPO_ROOT / "scripts")]
sys.modules["scripts"] = _pkg
sys.modules["scripts.i18n_prompts"] = sys.modules["kix_i18n_prompts"]

i18n_extract = _load("kix_i18n_extract", REPO_ROOT / "scripts" / "i18n_extract.py")
sys.modules["scripts.i18n_extract"] = i18n_extract
i18n_extract_html = _load(
    "kix_i18n_extract_html", REPO_ROOT / "scripts" / "i18n_extract_html.py"
)
i18n_prompts = sys.modules["kix_i18n_prompts"]
Classification = i18n_prompts.Classification
heuristic_classify = i18n_prompts.heuristic_classify


# Fixture source — small Python file mimicking app/routers patterns.
PY_FIXTURE = textwrap.dedent(
    '''
    """Fixture module — not real code."""
    import logging

    logger = logging.getLogger(__name__)

    GREETING = "你好，欢迎来到 KiX"          # CJK literal → extract
    BUTTON   = "登录"                          # CJK button → extract
    INTERNAL = "user_wallet_balance"          # snake_case identifier → skip
    SQL      = "SELECT * FROM users WHERE id = $1"   # SQL → skip
    REGEX    = r"^[a-z]+\\d+$"                 # regex literal → skip

    def greet(name: str) -> str:
        msg = f"欢迎 {name}！"                # f-string with CJK → extract
        logger.info("user greet ok name=%s", name)  # log template → skip
        return msg

    DICT_LIKE = {
        "progression": {"label": "成长体系"}, # dict value CJK → extract; key skipped
    }

    IGNORED = "不要翻译我"  # i18n-ignore
    '''
).lstrip()


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ─── 1. extracts all CJK literals from fixture ─────────────────────────────


def test_extracts_cjk_literals(tmp_path: Path):
    f = _write(tmp_path, "fixture.py", PY_FIXTURE)
    raw = i18n_extract.extract_from_source(str(f), f.read_text())
    values = [v for _ln, v, _is_f in raw]
    assert "你好，欢迎来到 KiX" in values
    assert "登录" in values
    assert "成长体系" in values
    # f-string body reconstructed with {name} placeholder
    assert any("欢迎 {name}" in v for v in values)


# ─── 2. skips logs / comments / SQL / regex / identifiers ──────────────────


def test_skips_internal_strings(tmp_path: Path):
    f = _write(tmp_path, "fixture.py", PY_FIXTURE)
    raw = i18n_extract.extract_from_source(str(f), f.read_text())
    values = [v for _ln, v, _is_f in raw]
    assert "user_wallet_balance" not in values
    assert "SELECT * FROM users WHERE id = $1" not in values
    assert r"^[a-z]+\d+$" not in values
    # logger.info template skipped
    assert "user greet ok name=%s" not in values
    # i18n-ignore annotated line skipped
    assert "不要翻译我" not in values


# ─── 3. heuristic classifier (no LLM) ──────────────────────────────────────


def test_heuristic_classifier():
    c = heuristic_classify("余额不足，请充值", "app/routers/wallet.py L42")
    assert c.is_user_facing == "yes"
    assert c.category == "error"
    assert c.proposed_key.startswith("wallet.")

    c2 = heuristic_classify("user_wallet_balance", "app/x.py L1")
    assert c2.is_user_facing == "no"

    c3 = heuristic_classify("登录", "app/auth.py L9")
    assert c3.is_user_facing == "yes"
    assert c3.category == "button"


# ─── 4. mocked LLM classifier ──────────────────────────────────────────────


def test_mocked_llm_classifier(monkeypatch):
    fake = Classification(
        is_user_facing="yes",
        category="button",
        proposed_key="auth.login_button",
        comment="mock",
    )

    def fake_classify_sync(s, ctx="", use_llm=False):
        return fake

    monkeypatch.setattr(i18n_extract, "classify_sync", fake_classify_sync)
    rows = i18n_extract.classify_candidates(
        "app/x.py", [(1, "登录", False)], use_llm=True
    )
    assert len(rows) == 1
    assert rows[0].proposed_key == "auth.login_button"
    assert rows[0].is_user_facing == "yes"


# ─── 5. apply-mode is a Phase-2 stub — dry-run by default ─────────────────


def test_apply_flag_does_not_modify_source(tmp_path: Path, monkeypatch, capsys):
    f = _write(tmp_path, "fixture.py", PY_FIXTURE)
    original = f.read_text()
    out_csv = tmp_path / "out.csv"
    rc = i18n_extract.main(
        ["--target", str(f), "--output", str(out_csv), "--apply"]
    )
    assert rc == 0
    # source untouched
    assert f.read_text() == original
    # csv produced
    assert out_csv.exists()


# ─── 6. HTML extractor preserves attributes; pulls user text ──────────────


def test_html_extractor():
    html = """
        <html><body>
          <h1>欢迎来到 KiX</h1>
          <button title="点击登录">登录</button>
          <script>const X = "ignored CJK 不抓";</script>
          <style>.x{content:"也忽略";}</style>
          <p>This is a normal English description.</p>
          <span>icon</span>
        </body></html>
    """
    raw = i18n_extract_html.extract_html(html)
    values = [v for _ln, v in raw]
    assert "欢迎来到 KiX" in values
    assert "登录" in values
    assert "点击登录" in values  # attribute extracted
    assert "ignored CJK 不抓" not in values  # inside <script>
    assert "也忽略" not in values  # inside <style>
    assert "This is a normal English description." in values


# ─── 7. JS template literals + escaped quotes ──────────────────────────────


def test_js_extractor_template_literals():
    js = """
        const a = "用户登录";
        const b = `欢迎 ${userName}！`;
        const c = 'snake_case_id';   // should skip
        // 你好 — comment skipped
        /* 多行 注释 也跳过 */
        const url = "https://example.com/path";  // skip
        const msg = "Welcome to KiX!";
    """
    raw = i18n_extract_html.extract_js(js)
    values = [v for _ln, v in raw]
    assert "用户登录" in values
    assert any("欢迎 {userName}" in v for v in values)
    assert "snake_case_id" not in values
    assert "你好" not in values
    assert "多行 注释 也跳过" not in values
    assert "Welcome to KiX!" in values


# ─── 8. proposed_key uniqueness across files ───────────────────────────────


def test_proposed_key_uniqueness():
    rows = [
        i18n_extract.Candidate("a.py", 1, "登录", "button", "auth.login", "yes"),
        i18n_extract.Candidate("b.py", 1, "登录", "button", "auth.login", "yes"),
        i18n_extract.Candidate("c.py", 1, "登录", "button", "auth.login", "yes"),
    ]
    out = i18n_extract.unique_keys(rows)
    keys = [r.proposed_key for r in out]
    assert keys == ["auth.login", "auth.login_2", "auth.login_3"]


# ─── 9. CSV output format is well-formed ───────────────────────────────────


def test_csv_output_format(tmp_path: Path):
    f = _write(tmp_path, "fixture.py", PY_FIXTURE)
    out_csv = tmp_path / "out.csv"
    rc = i18n_extract.main(["--target", str(f), "--output", str(out_csv)])
    assert rc == 0
    with out_csv.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    assert rows, "CSV should not be empty"
    assert set(reader.fieldnames or []) == {
        "file",
        "line",
        "original",
        "category",
        "proposed_key",
        "is_user_facing",
    }
    user_facing = [r for r in rows if r["is_user_facing"] == "yes"]
    assert user_facing, "at least one row should be user-facing"


# ─── 10. idempotent: extracting twice on same file = same CSV ──────────────


def test_idempotent(tmp_path: Path):
    f = _write(tmp_path, "fixture.py", PY_FIXTURE)
    out1 = tmp_path / "r1.csv"
    out2 = tmp_path / "r2.csv"
    i18n_extract.main(["--target", str(f), "--output", str(out1)])
    i18n_extract.main(["--target", str(f), "--output", str(out2)])
    assert out1.read_text(encoding="utf-8") == out2.read_text(encoding="utf-8")
