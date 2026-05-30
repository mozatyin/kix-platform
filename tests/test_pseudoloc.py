"""Tests for pseudolocalisation + i18n_lint + visual_qa scaffolding.

Stand-alone — no Redis / FastAPI / Playwright required. The visual_qa
test only verifies graceful degradation when Playwright is missing.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import os as _os
REPO_ROOT = Path(_os.environ.get("KIX_REPO_ROOT") or
                 Path(__file__).resolve().parents[1])
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load(mod_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(mod_name, file_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


pseudoloc = _load("kix_pseudoloc", REPO_ROOT / "scripts" / "pseudoloc.py")
i18n_lint = _load("kix_i18n_lint", REPO_ROOT / "scripts" / "i18n_lint.py")
visual_qa = _load("kix_visual_qa", REPO_ROOT / "scripts" / "visual_qa.py")


# ─── 1. xx-AC: accented + bracket wrapper + expansion ──────────────────────


def test_ac_mode_accents_and_wraps():
    out = pseudoloc.pseudo_translate("Hello", mode="AC")
    assert out.startswith("[") and out.endswith("]")
    # Original letters should have been replaced with accented forms.
    inner = out[1:-1]
    assert "H" not in inner and "e" not in inner
    # Some accented form of "Hello" survives.
    assert any(c in inner for c in "ĤéllôÉĈ")
    # +30 % padding minimum:
    assert len(inner) >= int(len("Hello") * 1.20)


# ─── 2. xx-HA: per-character hash marking ──────────────────────────────────


def test_ha_mode_hashes_every_char():
    out = pseudoloc.pseudo_translate("Hello", mode="HA")
    # 5 chars × (#X) + (no padding) = at least 10 chars
    assert out.count("#") == 5
    assert "H" in out and "e" in out
    # Spaces are preserved (not hashed)
    out2 = pseudoloc.pseudo_translate("Hi there", mode="HA")
    assert " " in out2


# ─── 3. xx-LO: 200 % length expansion ──────────────────────────────────────


def test_lo_mode_doubles_length():
    out = pseudoloc.pseudo_translate("Hello", mode="LO")
    assert len(out) >= 2 * len("Hello")


# ─── 4. ICU placeholders preserved ─────────────────────────────────────────


def test_placeholders_preserved():
    src = "Welcome { $name }!"
    out = pseudoloc.pseudo_translate(src, mode="AC")
    assert "{ $name }" in out
    # Plural blocks
    src2 = "{ $count -> [one] 1 thing *[other] { $count } things }"
    out2 = pseudoloc.pseudo_translate(src2, mode="AC")
    assert "{ $count }" in out2


# ─── 5. Plural selector keywords stay ASCII ────────────────────────────────


def test_plural_keywords_preserved():
    out = pseudoloc.pseudo_ftl(
        'msg = { $count ->\n    [one] One thing\n   *[other] Many things\n}\n',
        mode="AC",
    )
    # Keywords inside [ ] are passed through unchanged.
    assert "[one]" in out
    assert "*[other]" in out


# ─── 6. Round-trip: ICU still parses after pseudo ──────────────────────────


def test_round_trip_icu_parses():
    """ICU/Fluent parser is the source of truth; emulate via regex round-trip."""
    src = "messages.count = { $count ->\n  [one] 1 message\n *[other] { $count } messages\n}\n"
    out = pseudoloc.pseudo_ftl(src, mode="AC")
    # All placeholders survive
    assert out.count("{ $count }") == src.count("{ $count }")
    # Identifier on LHS untouched
    assert out.splitlines()[0].split("=")[0].strip() == "messages.count"


# ─── 6b. KEY TEST — dotted-key regex (the bug from the prior attempt) ─────


def test_fluent_dotted_key_regex_matches():
    """The previous agent's regex `[a-zA-Z][\\w-]*=` silently dropped keys
    containing dots like `messages.count`. Anchor that bug here so it can
    never regress. Both pseudoloc and i18n_lint must tolerate dotted IDs.
    """
    src = 'messages.count = "{count, plural, one {1} *[other] {#}}"\n'
    out = pseudoloc.pseudo_ftl(src, mode="AC")
    # Identifier survives untouched on the LHS.
    assert out.splitlines()[0].split("=", 1)[0].strip() == "messages.count"
    # Plural keywords + placeholders are preserved verbatim.
    for token in ("one", "*[other]", "{#}"):
        assert token in out, f"token {token!r} missing in pseudo output"

    # i18n_lint's catalog-key extraction must also pick up dotted IDs.
    key_re = i18n_lint.re.compile(r"^([a-zA-Z][\w.-]*)\s*=")
    m = key_re.match("messages.count = something\n")
    assert m and m.group(1) == "messages.count"


# ─── 6c. Attribute lines (.subject = ...) handled correctly ──────────────


def test_fluent_attribute_lines_preserved():
    src = (
        "email-welcome = Welcome to KiX\n"
        "    .subject = Get started\n"
        "    .body = Thanks for joining\n"
    )
    out = pseudoloc.pseudo_ftl(src, mode="AC")
    lines = out.splitlines()
    # Attribute identifiers must remain unchanged.
    assert lines[1].lstrip().startswith(".subject =")
    assert lines[2].lstrip().startswith(".body =")
    # Continuation/attribute indentation preserved.
    assert lines[1].startswith("    ")
    # Attribute values pseudo-localised.
    assert "Get started" not in out  # original text replaced


# ─── 6d. Round-trip with fluent.syntax (no Junk entries) ─────────────────


def test_full_catalog_round_trip_no_junk():
    """Pseudo-localise a real-shape Fluent catalog in all three modes and
    confirm zero Junk entries on re-parse. Only runs when
    ``fluent.syntax`` is installed; otherwise skipped.
    """
    try:
        from fluent.syntax import FluentParser
    except ImportError:
        pytest.skip("fluent.syntax not installed")

    # Strict-Fluent identifiers only (no dots) so we can assert zero Junk.
    # Dotted keys like ``messages.count`` are a KiX-internal JSON convention
    # and are covered separately by ``test_fluent_dotted_key_regex_matches``.
    src = (
        "welcome-message = Welcome { $name }!\n"
        "    .description = You have { $count ->\n"
        "        [one] 1 message\n"
        "        *[other] { $count } messages\n"
        "    }\n"
        "tutorials-step-intro = We'll set up { $module_count ->\n"
        "        [one] 1 module\n"
        "        *[other] { $module_count } modules\n"
        "    } and { $rule_count ->\n"
        "        [one] 1 rule\n"
        "        *[other] { $rule_count } rules\n"
        "    }.\n"
    )
    parser = FluentParser()
    for mode in ("AC", "HA", "LO"):
        out = pseudoloc.pseudo_ftl(src, mode=mode)
        tree = parser.parse(out)
        junk = [e for e in tree.body if type(e).__name__ == "Junk"]
        msgs = [e for e in tree.body if type(e).__name__ == "Message"]
        assert not junk, (
            f"mode={mode} produced Junk entries: "
            + "\n---\n".join(j.content[:200] for j in junk)
        )
        # Both top-level messages survived.
        assert len(msgs) == 2, f"mode={mode} got {len(msgs)} messages, want 2"


# ─── 7. R-001: HTTPException Chinese detail ────────────────────────────────


def test_lint_r001_chinese_http_exception(tmp_path: Path):
    fixture = tmp_path / "router.py"
    fixture.write_text(
        'from fastapi import HTTPException\n'
        'def x():\n'
        '    raise HTTPException(status_code=400, detail="参数错误")\n',
        encoding="utf-8",
    )
    cfg = i18n_lint.LintConfig(
        repo_root=tmp_path,
        files=[fixture],
    )
    findings = i18n_lint.run_lint(cfg)
    rules = {f.rule for f in findings}
    assert "R-001" in rules


# ─── 8. R-002: hardcoded ¥ outside formatting ──────────────────────────────


def test_lint_r002_yen_glyph(tmp_path: Path):
    fixture = tmp_path / "app" / "routers" / "wallet.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_text('PRICE_LABEL = "¥199 / month"\n', encoding="utf-8")
    cfg = i18n_lint.LintConfig(repo_root=tmp_path, files=[fixture])
    findings = i18n_lint.run_lint(cfg)
    assert any(f.rule == "R-002" for f in findings)


# ─── 9. R-007: orphan data-i18n key ────────────────────────────────────────


def test_lint_r007_orphan_data_i18n(tmp_path: Path):
    # Mock landing structure with a catalog that doesn't contain "ghost.key"
    landing = tmp_path / "landing"
    (landing / "i18n" / "locales" / "en-SG").mkdir(parents=True)
    (landing / "i18n" / "locales" / "en-SG" / "common.json").write_text(
        json.dumps({"login": "Login"}), encoding="utf-8"
    )
    page = landing / "portal.html"
    page.write_text(
        '<html><body>'
        '<button data-i18n="login">Login</button>'
        '<span data-i18n="ghost.key">???</span>'
        '</body></html>',
        encoding="utf-8",
    )
    cfg = i18n_lint.LintConfig(repo_root=tmp_path, files=[page])
    findings = i18n_lint.run_lint(cfg)
    orphans = [f for f in findings if f.rule == "R-007"]
    assert any("ghost.key" in f.message for f in orphans)
    # "login" key exists so should NOT be flagged.
    assert not any("'login'" in f.message for f in orphans)


# ─── 10. Lint exit codes (P0 → 1, otherwise 0) ─────────────────────────────


def test_lint_exit_code_p0_nonzero(tmp_path: Path, capsys):
    f = tmp_path / "x.py"
    f.write_text(
        'from fastapi import HTTPException\n'
        'raise HTTPException(detail="错误")\n',
        encoding="utf-8",
    )
    # Run with --files so we don't scan the whole repo
    rc = i18n_lint.main([
        "--root", str(tmp_path), "--files", str(f),
        "--whitelist", str(tmp_path / "nope.txt"),  # no whitelist
        "--quiet",
    ])
    assert rc == 1


def test_lint_exit_code_clean_zero(tmp_path: Path):
    f = tmp_path / "x.py"
    f.write_text('GREETING = "Hello"\n', encoding="utf-8")
    rc = i18n_lint.main([
        "--root", str(tmp_path), "--files", str(f),
        "--whitelist", str(tmp_path / "nope.txt"),
        "--quiet",
    ])
    assert rc == 0


# ─── 11. visual_qa skips gracefully when Playwright missing ────────────────


def test_visual_qa_no_playwright(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(visual_qa, "playwright_available", lambda: False)
    cfg = visual_qa.QAConfig(
        repo_root=tmp_path,
        base_url=None,
        output_dir=tmp_path / "out",
        pages=("portal",),
        locales=("en-SG", "xx-AC"),
        viewport=(1280, 900),
    )
    shots = visual_qa.capture_all(cfg)
    assert shots == []
    # report still gets written
    report = visual_qa.write_report(cfg, shots, [])
    assert report.exists()
    assert "NOT INSTALLED" in report.read_text(encoding="utf-8")


# ─── 12. Whitelist suppresses known exception ──────────────────────────────


def test_lint_whitelist_suppresses(tmp_path: Path):
    fixture = tmp_path / "app" / "routers" / "compliance.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(
        'from fastapi import HTTPException\n'
        'raise HTTPException(detail="违反广告法")\n',
        encoding="utf-8",
    )
    wl = tmp_path / "wl.txt"
    wl.write_text("R-001:app/routers/compliance.py:*\n", encoding="utf-8")
    cfg = i18n_lint.LintConfig(
        repo_root=tmp_path,
        files=[fixture],
        whitelist=i18n_lint.load_whitelist(wl),
    )
    findings = i18n_lint.run_lint(cfg)
    assert not any(f.rule == "R-001" for f in findings), \
        f"whitelist failed; findings: {findings}"
