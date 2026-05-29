"""Tests for the i18n_translate batch translator + glossary manager.

Stand-alone — no Redis, no DB, no live LLM. Mirrors the loading pattern
in tests/test_i18n_extract.py so the test files coexist regardless of
which other namespace packages are on sys.path.
"""
from __future__ import annotations

import asyncio
import importlib.util as _ilu
import json
import sys
import textwrap
import types as _types
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load(mod_name: str, file_path: Path):
    spec = _ilu.spec_from_file_location(mod_name, file_path)
    assert spec and spec.loader
    mod = _ilu.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# Pre-register a minimal `scripts` package so absolute imports resolve to
# this repo's scripts/ directory (some sibling repos collide on the name).
_pkg = _types.ModuleType("scripts")
_pkg.__path__ = [str(REPO_ROOT / "scripts")]
sys.modules["scripts"] = _pkg

i18n_glossary = _load("kix_i18n_glossary", REPO_ROOT / "scripts" / "i18n_glossary.py")
sys.modules["scripts.i18n_glossary"] = i18n_glossary

i18n_translate = _load(
    "kix_i18n_translate", REPO_ROOT / "scripts" / "i18n_translate.py"
)
sys.modules["scripts.i18n_translate"] = i18n_translate


# ── Fixture FTL ─────────────────────────────────────────────────────────

FIXTURE_FTL = textwrap.dedent(
    """\
    ### Fixture catalog
    welcome = Welcome to KiX!
    login = Login
    logout = Logout
    saved = Saved
    insufficient = Insufficient balance
    greet = Hello { $name }!
    voucher-info = Your voucher for Toast Box is ready.
    plural-msg = You have { $count ->
        [one] 1 message
        *[other] { $count } messages
    }
    """
)


# ─── 1. translates 5 strings to zh-Hans-SG (mocked LLM) ────────────────


def test_translates_batch(tmp_path: Path):
    src = tmp_path / "main.ftl"
    src.write_text(FIXTURE_FTL, encoding="utf-8")

    async def fake_batch(items, locale, gloss, *, model, timeout=30.0):
        # Look like real Claude JSON output.
        canned = {
            "welcome": "欢迎使用 KiX!",
            "login": "登录",
            "logout": "登出",
            "saved": "已保存",
            "insufficient": "余额不足",
            "greet": "你好 {$name}!",
            "voucher-info": "您来自 Toast Box 的优惠券已就绪。",
            "plural-msg": "你有 {$count} 条消息",
        }
        return [
            {"key": it.key, "translation": canned.get(it.key, it.text), "confidence": "high"}
            for it in items
        ]

    with patch.object(i18n_translate, "_llm_translate_batch", fake_batch):
        bundle = asyncio.run(
            i18n_translate.translate_catalog(src, "zh-Hans-SG", batch_size=20)
        )

    by_key = {r.key: r.translation for r in bundle["results"]}
    assert by_key["welcome"] == "欢迎使用 KiX!"
    assert by_key["login"] == "登录"
    assert by_key["logout"] == "登出"
    assert by_key["saved"] == "已保存"
    assert by_key["insufficient"] == "余额不足"
    assert bundle["total_strings"] >= 5


# ─── 2. glossary terms preserved (KiX stays KiX) ────────────────────────


def test_glossary_kix_dnt(tmp_path: Path):
    src = tmp_path / "main.ftl"
    src.write_text(FIXTURE_FTL, encoding="utf-8")

    captured_glossary = {}

    async def fake_batch(items, locale, gloss, *, model, timeout=30.0):
        captured_glossary["block"] = gloss
        return [
            {"key": it.key, "translation": it.text, "confidence": "high"} for it in items
        ]

    with patch.object(i18n_translate, "_llm_translate_batch", fake_batch):
        bundle = asyncio.run(
            i18n_translate.translate_catalog(src, "zh-Hans-SG", batch_size=20)
        )

    # Glossary block injected into the prompt must mention KiX as DNT.
    assert "KiX" in captured_glossary["block"]
    assert "KEEP AS-IS" in captured_glossary["block"]
    # And Toast Box too — appears in voucher-info.
    assert "Toast Box" in captured_glossary["block"]


# ─── 3. ICU placeholder syntax preserved ────────────────────────────────


def test_icu_placeholder_preserved(tmp_path: Path):
    src = tmp_path / "main.ftl"
    src.write_text(FIXTURE_FTL, encoding="utf-8")

    async def fake_batch(items, locale, gloss, *, model, timeout=30.0):
        # Pretend the LLM kept the placeholder.
        return [
            {
                "key": it.key,
                "translation": "你好 {$name}!" if it.key == "greet" else it.text,
                "confidence": "high",
            }
            for it in items
        ]

    with patch.object(i18n_translate, "_llm_translate_batch", fake_batch):
        bundle = asyncio.run(
            i18n_translate.translate_catalog(src, "zh-Hans-SG", batch_size=20)
        )
    out = i18n_translate.write_translated_ftl(
        src.read_text(), {r.key: r.translation for r in bundle["results"]}, locale="zh-Hans-SG"
    )
    # The Placeable should round-trip as a {$name} in the serialised FTL.
    assert "{ $name }" in out or "{$name}" in out


# ─── 4. quota-guard called before LLM ───────────────────────────────────


def test_quota_guard_called(tmp_path: Path):
    src = tmp_path / "main.ftl"
    src.write_text("hello = Hello\n", encoding="utf-8")

    called = {"n": 0}

    async def fake_wait(max_wait_seconds: int = 3600):
        called["n"] += 1
        return False

    async def fake_batch(items, locale, gloss, *, model, timeout=30.0):
        return [{"key": it.key, "translation": "你好", "confidence": "high"} for it in items]

    with patch.object(i18n_translate, "_wait_if_paused_async", fake_wait), patch.object(
        i18n_translate, "_llm_translate_batch", fake_batch
    ):
        asyncio.run(i18n_translate.translate_catalog(src, "zh-Hans-SG"))

    # fake_batch is mocked so the wait_if_paused inside the real
    # _llm_translate_batch never fires — verify the contract by calling
    # the real function with the patch in place.
    async def run_real():
        items = i18n_translate.extract_strings(src.read_text())
        # Bypass network: mock httpx as failing → real fn falls back to mock.
        return await i18n_translate._llm_translate_batch(
            items, "zh-Hans-SG", "", model=i18n_translate.DEFAULT_MODEL
        )

    with patch.object(i18n_translate, "_wait_if_paused_async", fake_wait):
        # Force no API key so the real branch exits before httpx.
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}, clear=False):
            asyncio.run(run_real())
    assert called["n"] >= 1


# ─── 5. batch of 20 = single LLM call ───────────────────────────────────


def test_batch_size_20_one_call(tmp_path: Path):
    lines = ["### header"] + [f"k{i} = String number {i}" for i in range(20)]
    src = tmp_path / "main.ftl"
    src.write_text("\n".join(lines) + "\n", encoding="utf-8")

    calls = {"n": 0}

    async def fake_batch(items, locale, gloss, *, model, timeout=30.0):
        calls["n"] += 1
        return [{"key": it.key, "translation": it.text, "confidence": "medium"} for it in items]

    with patch.object(i18n_translate, "_llm_translate_batch", fake_batch):
        bundle = asyncio.run(
            i18n_translate.translate_catalog(src, "zh-Hans-SG", batch_size=20)
        )
    assert bundle["total_strings"] == 20
    assert calls["n"] == 1


# ─── 6. TM cache hit avoids LLM call ────────────────────────────────────


def test_tm_cache_hit_skips_llm(tmp_path: Path):
    src = tmp_path / "main.ftl"
    src.write_text("hello = Hello\nbye = Goodbye\n", encoding="utf-8")

    tm = i18n_translate.TMCache()
    # Pre-warm cache for both strings.
    asyncio.run(tm.set("Hello", "zh-Hans-SG", "你好"))
    asyncio.run(tm.set("Goodbye", "zh-Hans-SG", "再见"))

    calls = {"n": 0}

    async def fake_batch(items, locale, gloss, *, model, timeout=30.0):
        calls["n"] += 1
        return [{"key": it.key, "translation": it.text, "confidence": "high"} for it in items]

    with patch.object(i18n_translate, "_llm_translate_batch", fake_batch):
        bundle = asyncio.run(
            i18n_translate.translate_catalog(src, "zh-Hans-SG", tm=tm, batch_size=20)
        )
    assert calls["n"] == 0
    assert bundle["cache_hits"] == 2
    by_key = {r.key: r.translation for r in bundle["results"]}
    assert by_key["hello"] == "你好"
    assert by_key["bye"] == "再见"


# ─── 7. review-mode HTML renders ────────────────────────────────────────


def test_review_html_renders(tmp_path: Path):
    src = tmp_path / "main.ftl"
    src.write_text("hello = Hello\nlogin = Login\n", encoding="utf-8")

    async def fake_batch(items, locale, gloss, *, model, timeout=30.0):
        return [
            {"key": it.key, "translation": it.text + "!", "confidence": "high"}
            for it in items
        ]

    with patch.object(i18n_translate, "_llm_translate_batch", fake_batch):
        bundle = asyncio.run(
            i18n_translate.translate_catalog(src, "zh-Hans-SG", batch_size=20)
        )
    html = i18n_translate.render_review_html(bundle)
    assert "<table" in html
    assert "hello" in html
    assert "Hello" in html
    assert "Hello!" in html
    assert "mark-reviewed" in html  # admin endpoint reference
    assert "zh-Hans-SG" in html


# ─── 8. cost estimate accurate (within 10%) ─────────────────────────────


def test_cost_estimate_reasonable(tmp_path: Path):
    src = tmp_path / "main.ftl"
    # 50 short strings, ~3 words each → ~150 words total
    lines = [f"k{i} = Quick brown fox" for i in range(50)]
    src.write_text("\n".join(lines) + "\n", encoding="utf-8")
    strings = i18n_translate.extract_strings(src.read_text())
    est = i18n_translate.estimate_cost(strings, ["zh-Hans-SG", "id-ID"])
    assert est["total_strings"] == 50
    # 3 words × 50 = 150 words; allow 10% slack.
    assert 135 <= est["total_words"] <= 165
    assert est["batches_per_locale"] == 3  # ceil(50/20)
    assert est["target_locales"] == ["zh-Hans-SG", "id-ID"]
    # 2 locales, cost > 0, total = per-locale × 2
    assert est["total_cost_usd"] == pytest.approx(
        est["cost_per_locale_usd"] * 2, rel=0.001
    )
    # And the render is non-empty.
    rendered = i18n_translate.render_estimate(est)
    assert "i18n_translate cost estimate" in rendered


# ─── 9. add glossary term endpoint (file-level upsert) ──────────────────


def test_glossary_upsert_term(tmp_path: Path):
    gd = tmp_path / "glossary"
    gd.mkdir()
    # Start with empty global
    term = i18n_glossary.upsert_term(
        "kix_pay",
        source_term="KiX Pay",
        do_not_translate=True,
        category="product_name",
        glossary_dir=gd,
    )
    assert term.term_id == "kix_pay"
    assert term.do_not_translate is True
    # File written
    data = json.loads((gd / "global.json").read_text())
    assert any(t["term_id"] == "kix_pay" for t in data["terms"])

    # Update is idempotent — same term_id, changed flag
    term2 = i18n_glossary.upsert_term(
        "kix_pay", do_not_translate=False, glossary_dir=gd
    )
    assert term2.do_not_translate is False
    data = json.loads((gd / "global.json").read_text())
    assert len([t for t in data["terms"] if t["term_id"] == "kix_pay"]) == 1


# ─── 10. per-locale glossary overrides global ───────────────────────────


def test_locale_glossary_override(tmp_path: Path):
    gd = tmp_path / "glossary"
    gd.mkdir()
    # Global says "voucher" is translatable
    i18n_glossary.upsert_term(
        "voucher", source_term="voucher", do_not_translate=False,
        category="technical", glossary_dir=gd,
    )
    # Locale override marks it DNT for this locale
    i18n_glossary.upsert_term(
        "voucher", source_term="voucher", do_not_translate=True,
        locale="zh-Hans-SG", glossary_dir=gd,
    )

    merged = i18n_glossary.load_glossary("zh-Hans-SG", glossary_dir=gd)
    voucher = next(t for t in merged if t.term_id == "voucher")
    assert voucher.do_not_translate is True
    assert voucher.locale == "zh-Hans-SG"

    # Without the locale, the global (translatable) version wins.
    global_only = i18n_glossary.load_glossary(None, glossary_dir=gd)
    g_voucher = next(t for t in global_only if t.term_id == "voucher")
    assert g_voucher.do_not_translate is False


# ─── 11. confidence levels assigned + normalised ────────────────────────


def test_confidence_levels(tmp_path: Path):
    src = tmp_path / "main.ftl"
    src.write_text("a = Alpha\nb = Beta\nc = Gamma\n", encoding="utf-8")

    async def fake_batch(items, locale, gloss, *, model, timeout=30.0):
        confs = ["high", "medium", "bogus"]  # last one must be normalised
        return [
            {"key": it.key, "translation": it.text + "!", "confidence": confs[i]}
            for i, it in enumerate(items)
        ]

    with patch.object(i18n_translate, "_llm_translate_batch", fake_batch):
        bundle = asyncio.run(
            i18n_translate.translate_catalog(src, "zh-Hans-SG", batch_size=20)
        )
    confs = {r.key: r.confidence for r in bundle["results"]}
    assert confs["a"] == "high"
    assert confs["b"] == "medium"
    # "bogus" → normalised to "medium"
    assert confs["c"] == "medium"


# ─── 12. empty source → empty target (no error) ─────────────────────────


def test_empty_source(tmp_path: Path):
    src = tmp_path / "main.ftl"
    src.write_text("### just a comment\n", encoding="utf-8")

    async def fake_batch(items, locale, gloss, *, model, timeout=30.0):
        return []

    with patch.object(i18n_translate, "_llm_translate_batch", fake_batch):
        bundle = asyncio.run(
            i18n_translate.translate_catalog(src, "zh-Hans-SG", batch_size=20)
        )
    assert bundle["total_strings"] == 0
    assert bundle["results"] == []

    out_dir = tmp_path / "catalogs"
    bundle_copy = dict(bundle)
    out_path = i18n_translate.persist_catalog(bundle_copy, catalog_dir=out_dir)
    assert out_path.exists()
    # Output FTL is just the comment header, no errors.
    assert "just a comment" in out_path.read_text()
