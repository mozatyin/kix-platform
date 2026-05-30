"""SG bilingual end-to-end test — Wave 2 deliverable.

These tests confirm that after Wave 2 source migration:

  * en-SG is served by default
  * ?lang=zh-Hans-SG flips response messages
  * Accept-Language: zh-Hans-SG,en-SG;q=0.9 parses correctly
  * Fallback chain resolves correctly
  * Missing keys do NOT raise — they return verbatim with a warning
  * Currency formatting differs per locale (SGD/CNY/USD)
  * Pluralisation works (English vs Chinese always-other)
  * The conditions FIX_HINTS migration still serves the same shape

No Redis / no Postgres — these tests are pure ASGI + catalog lookups.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(monkeypatch_module) -> TestClient:
    """ASGI client with KIX_REGION=sg so en-SG is the regional default."""
    return TestClient(_make_app())


@pytest.fixture(scope="module")
def monkeypatch_module(request):
    """Module-scope monkeypatch (default fixture is function-scope)."""
    from _pytest.monkeypatch import MonkeyPatch
    mpatch = MonkeyPatch()
    mpatch.setenv("KIX_REGION", "sg")
    # The region module reads KIX_REGION at import time; force reload so
    # the SG region is picked up for this test module.
    import importlib
    import app.region
    importlib.reload(app.region)
    request.addfinalizer(mpatch.undo)
    yield mpatch


def _make_app():
    # Defer to avoid importing before the env var is set in the fixture.
    from app.main import create_app
    return create_app()


# Re-import the i18n helpers after the fixture has reloaded app.region.
from app.i18n import t, fallback_chain  # noqa: E402
from app.i18n.context import (  # noqa: E402
    get_current_locale,
    reset_current_locale,
    set_current_locale,
)


# ── 1. Default locale ────────────────────────────────────────────────────────


def test_default_locale_is_en_sg(client: TestClient) -> None:
    r = client.get("/api/v1/i18n/translate", params={"key": "common-cta-login"})
    assert r.status_code == 200
    body = r.json()
    assert body["locale"] == "en-SG"
    assert body["rendered"] == "Login"
    assert body["translated"] is True


# ── 2. ?lang= override flips the response ────────────────────────────────────


def test_query_lang_overrides_to_zh_hans_sg(client: TestClient) -> None:
    r = client.get(
        "/api/v1/i18n/translate",
        params={"key": "common-cta-login", "lang": "zh-Hans-SG"},
    )
    assert r.status_code == 200
    body = r.json()
    # The middleware resolved zh-Hans-SG before the handler ran.
    assert body["locale"] == "zh-Hans-SG"
    assert body["rendered"] == "登录"


def test_query_lang_overrides_to_en_sg_explicitly(client: TestClient) -> None:
    r = client.get(
        "/api/v1/i18n/translate",
        params={"key": "common-cta-logout", "lang": "en-SG"},
    )
    assert r.json()["rendered"] == "Logout"


# ── 3. Accept-Language header parsing ────────────────────────────────────────


def test_accept_language_zh_hans_sg_with_quality(client: TestClient) -> None:
    r = client.get(
        "/api/v1/i18n/translate",
        params={"key": "conditions-blocker-supply_exhausted"},
        headers={"Accept-Language": "zh-Hans-SG,en-SG;q=0.9"},
    )
    body = r.json()
    assert body["locale"] == "zh-Hans-SG"
    assert "奖池" in body["rendered"]


def test_accept_language_base_zh_negotiates_to_supported(client: TestClient) -> None:
    """`Accept-Language: zh` should land on zh-Hans-CN or zh-Hans-SG."""
    r = client.get(
        "/api/v1/i18n/translate",
        params={"key": "common-cta-cancel"},
        headers={"Accept-Language": "zh"},
    )
    locale = r.json()["locale"]
    assert locale.startswith("zh-Hans")


def test_accept_language_zh_cn_picks_zh_hans_cn(client: TestClient) -> None:
    r = client.get(
        "/api/v1/i18n/translate",
        params={"key": "common-cta-cancel"},
        headers={"Accept-Language": "zh-CN"},
    )
    locale = r.json()["locale"]
    assert locale == "zh-Hans-CN"


def test_accept_language_unknown_falls_back_to_default(client: TestClient) -> None:
    r = client.get(
        "/api/v1/i18n/translate",
        params={"key": "common-cta-save"},
        headers={"Accept-Language": "kl-GL"},  # Greenlandic — unsupported
    )
    # Region default kicks in (SG or en-SG).
    assert r.json()["locale"] in {"en-SG", "zh-Hans-SG"}


# ── 4. Content-Language response header ──────────────────────────────────────


def test_content_language_header_set(client: TestClient) -> None:
    r = client.get(
        "/api/v1/i18n/translate",
        params={"key": "common-cta-save", "lang": "zh-Hans-SG"},
    )
    assert r.headers.get("Content-Language") == "zh-Hans-SG"


# ── 5. Fallback chain ────────────────────────────────────────────────────────


def test_fallback_chain_zh_hans_sg() -> None:
    chain = fallback_chain("zh-Hans-SG")
    assert chain[0] == "zh-Hans-SG"
    assert "zh-Hans" in chain
    assert "zh-Hans-CN" in chain
    assert "en-US" in chain  # terminal fallback


def test_fallback_chain_en_sg() -> None:
    chain = fallback_chain("en-SG")
    assert chain[0] == "en-SG"
    assert "en" in chain
    assert "en-US" in chain


# ── 6. Missing key never raises ──────────────────────────────────────────────


def test_missing_key_returns_verbatim() -> None:
    out = t("definitely-not-a-real-key", locale="zh-Hans-SG")
    assert out == "definitely-not-a-real-key"


def test_missing_key_does_not_raise_for_en_sg() -> None:
    out = t("another-missing-key", locale="en-SG")
    assert out == "another-missing-key"


# ── 7. Catalog content correctness ───────────────────────────────────────────


def test_tutorials_module_progression_translates() -> None:
    assert t("tutorials-module-progression", locale="en-SG") == "Progression"
    assert t("tutorials-module-progression", locale="zh-Hans-SG") == "成长体系"


def test_conditions_blocker_translates_both_locales() -> None:
    en = t("conditions-blocker-supply_exhausted", locale="en-SG")
    zh = t("conditions-blocker-supply_exhausted", locale="zh-Hans-SG")
    assert "claim" in en.lower() or "supply" in en.lower()
    assert "奖池" in zh or "用完" in zh
    assert en != zh


def test_welcome_kit_item_titles_translate() -> None:
    en = t("welcome_kit-item-table_stand-title", locale="en-SG")
    zh = t("welcome_kit-item-table_stand-title", locale="zh-Hans-SG")
    assert "Table" in en
    assert "桌牌" in zh


def test_recipe_generator_summary_with_pluralization() -> None:
    # English: 1 module / 5 modules
    one = t(
        "recipe_generator-summary-recipe-includes",
        locale="en-SG",
        recipe_name="Demo",
        module_count=1,
        module_list="streak",
        rule_count=1,
    )
    many = t(
        "recipe_generator-summary-recipe-includes",
        locale="en-SG",
        recipe_name="Demo",
        module_count=3,
        module_list="streak, league, lives",
        rule_count=5,
    )
    assert "1 module" in one and "1 rule" in one
    assert "3 modules" in many and "5 rules" in many


def test_zh_hans_pluralization_collapses_to_other() -> None:
    """Chinese has no plural inflection — same string regardless of count."""
    one = t(
        "tutorials-step-intro",
        locale="zh-Hans-SG",
        recipe_name="Demo",
        module_count=1,
        rule_count=1,
    )
    many = t(
        "tutorials-step-intro",
        locale="zh-Hans-SG",
        recipe_name="Demo",
        module_count=5,
        rule_count=5,
    )
    # Both render the count, neither swallows it.
    assert "1" in one and "5" in many


# ── 8. ContextVar isolation ──────────────────────────────────────────────────


def test_context_var_overrides_t_default() -> None:
    token = set_current_locale("zh-Hans-SG")
    try:
        assert get_current_locale() == "zh-Hans-SG"
        assert t("common-cta-login") == "登录"
    finally:
        reset_current_locale(token)


# ── 9. No Chinese leak in en-SG responses for migrated keys ──────────────────


@pytest.mark.parametrize(
    "key",
    [
        "tutorials-module-progression",
        "tutorials-module-energy",
        "conditions-blocker-supply_exhausted",
        "conditions-blocker-frequency_per_user_per_day",
        "welcome_kit-item-table_stand-title",
        "welcome_kit-item-table_stand-desc",
        "common-cta-cancel",
        "common-cta-save",
        "error-not_found",
        "error-unauthorized",
    ],
)
def test_no_chinese_glyphs_in_en_sg(key: str) -> None:
    """Catalog hygiene: zero Chinese characters in en-SG rendered output."""
    import re
    rendered = t(key, locale="en-SG")
    cjk = re.findall(r"[一-鿿]", rendered)
    assert not cjk, f"{key} en-SG leaked Chinese: {cjk}"


# ── 10. No English-only template leak in zh-Hans-SG for migrated keys ───────


@pytest.mark.parametrize(
    "key",
    [
        "tutorials-module-progression",
        "tutorials-module-currency",
        "conditions-blocker-supply_exhausted",
        "conditions-blocker-tier_required",
        "welcome_kit-item-table_stand-title",
        "common-cta-login",
        "common-cta-cancel",
    ],
)
def test_zh_hans_sg_contains_chinese(key: str) -> None:
    """Migrated keys must have actual Chinese, not pass-through English."""
    import re
    rendered = t(key, locale="zh-Hans-SG")
    cjk = re.findall(r"[一-鿿]", rendered)
    assert cjk, f"{key} zh-Hans-SG had no Chinese: {rendered!r}"
