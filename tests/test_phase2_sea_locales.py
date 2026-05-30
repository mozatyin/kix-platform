"""Phase 2 SEA locale shipping — coverage tests.

Verifies the four new locales (id-ID, ms-MY, th-TH, vi-VN) are wired
into the catalog loader, region table, and locale switcher API, and
that their Fluent files satisfy basic structural invariants.

These tests intentionally do NOT enforce translation quality — the
Phase-2 stub may run without an LLM API key and emit deterministic
mocks (``[id-ID] Welcome { $name }!``). Quality gating happens in the
review queue, not in CI.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

# Make ``scripts.*`` importable from the repo root (mirrors test_i18n_translate.py).
REPO_ROOT_FOR_SCRIPTS = Path(__file__).resolve().parents[1]
if str(REPO_ROOT_FOR_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_FOR_SCRIPTS))

from app import i18n as kix_i18n
from app.i18n.currency import currency_decimals
from app.region import (
    REGION_CONFIG,
    get_region_config,
    get_supported_locales_for_region,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_DIR = REPO_ROOT / "app" / "i18n" / "catalogs"
LANDING_LOCALES_DIR = REPO_ROOT / "landing" / "i18n" / "locales"

NEW_LOCALES = ["id-ID", "ms-MY", "th-TH", "vi-VN"]
EN_SG_FTL = CATALOG_DIR / "en-SG" / "main.ftl"


# ---- Helpers ------------------------------------------------------------


def _ftl_message_keys(path: Path) -> set[str]:
    """Cheap key extractor — counts ``^key = ...`` lines, ignoring attrs.

    Matches what ``scripts.i18n_translate`` round-trips: every message
    or term ID at column 0.
    """
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([a-zA-Z][a-zA-Z0-9_-]*)\s*=", line)
        if m:
            keys.add(m.group(1))
    return keys


# ---- 1. Catalog load + key coverage ------------------------------------


@pytest.mark.parametrize("locale", NEW_LOCALES)
def test_new_locale_catalog_loads(locale: str) -> None:
    """The translator must have written a main.ftl for every new locale."""
    path = CATALOG_DIR / locale / "main.ftl"
    assert path.is_file(), f"Missing FTL for {locale}: {path}"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"Empty FTL for {locale}"
    # Loading must not raise.
    loc = kix_i18n.get_localization(locale)
    assert loc is not None


@pytest.mark.parametrize("locale", NEW_LOCALES)
def test_new_locale_key_coverage_matches_en_sg(locale: str) -> None:
    """Every en-SG message id must appear in the new locale catalog."""
    src_keys = _ftl_message_keys(EN_SG_FTL)
    new_keys = _ftl_message_keys(CATALOG_DIR / locale / "main.ftl")
    missing = src_keys - new_keys
    assert not missing, f"{locale} missing keys: {sorted(missing)[:5]}"


# ---- 2. Fallback chain --------------------------------------------------


def test_fallback_chain_id_id_includes_en_us() -> None:
    """id-ID must resolve through en-US so missing keys never crash."""
    chain = kix_i18n.fallback_chain("id-ID")
    assert chain[0] == "id-ID"
    assert "en-US" in chain, f"en-US not in id-ID fallback chain: {chain}"


@pytest.mark.parametrize("locale", NEW_LOCALES)
def test_fallback_chain_terminates_in_en(locale: str) -> None:
    chain = kix_i18n.fallback_chain(locale)
    assert chain[0] == locale
    # Some path to English exists.
    assert any(c.startswith("en") for c in chain), chain


# ---- 3. Region lookups --------------------------------------------------


@pytest.mark.parametrize(
    "region,expected_primary_locale,expected_currency",
    [
        ("id", "id-ID", "IDR"),
        ("my", "ms-MY", "MYR"),
        ("th", "th-TH", "THB"),
        ("vn", "vi-VN", "VND"),
    ],
)
def test_region_primary_locale_and_currency(
    region: str, expected_primary_locale: str, expected_currency: str
) -> None:
    cfg = get_region_config(region)
    assert cfg["primary_currency"] == expected_currency
    locales = get_supported_locales_for_region(region)
    assert locales[0] == expected_primary_locale, (
        f"{region} primary locale should be {expected_primary_locale}, got {locales}"
    )


def test_existing_regions_untouched() -> None:
    """Phase 2 must not regress cn/sg/us/eu region tables."""
    assert REGION_CONFIG["cn"]["primary_currency"] == "CNY"
    assert REGION_CONFIG["sg"]["primary_currency"] == "SGD"
    assert REGION_CONFIG["us"]["primary_currency"] == "USD"
    assert REGION_CONFIG["eu"]["primary_currency"] == "EUR"


# ---- 4. Currency decimals (per CLDR) -----------------------------------


def test_currency_decimals_per_cldr() -> None:
    """IDR/VND render with no decimals; MYR/THB use 2 (CLDR canonical).

    Note: the Phase-2 spec verbally bundled all four as "0-decimal" but
    CLDR (and Babel) say MYR and THB are 2-decimal currencies. We follow
    CLDR so format_currency() output matches local banking apps.
    """
    assert currency_decimals("IDR") == 0
    assert currency_decimals("VND") == 0
    assert currency_decimals("MYR") == 2
    assert currency_decimals("THB") == 2


# ---- 5. Plural rules (CLDR cardinal categories) -------------------------


@pytest.mark.parametrize(
    "locale,expected_categories",
    [
        # Indonesian, Malay, Thai, Vietnamese all use "other"-only.
        ("id-ID", {"other"}),
        ("ms-MY", {"other"}),
        ("th-TH", {"other"}),
        ("vi-VN", {"other"}),
    ],
)
def test_cldr_plural_rules_other_only(
    locale: str, expected_categories: set[str]
) -> None:
    from babel.plural import PluralRule
    from babel.core import Locale

    # Babel uses underscored locale ids.
    bcp = locale.replace("-", "_")
    rule: PluralRule = Locale.parse(bcp).plural_form
    categories = set(rule.tags) | {"other"}  # "other" is always implicit
    assert categories == expected_categories, (
        f"{locale} plural categories {categories} != {expected_categories}"
    )


# ---- 6. Locale switcher API (landing runtime) ---------------------------


def test_locale_switcher_includes_sea_locales() -> None:
    """SUPPORTED array in the landing i18next runtime advertises new tags."""
    js = (REPO_ROOT / "landing" / "i18n" / "i18next-runtime.js").read_text(
        encoding="utf-8"
    )
    for locale in NEW_LOCALES:
        assert f"'{locale}'" in js, f"{locale} missing from i18next-runtime.js"
    # Existing locales must still be there.
    for old in ("en-SG", "zh-Hans-SG", "en-US", "zh-Hans-CN"):
        assert f"'{old}'" in js, f"{old} accidentally removed"


# ---- 7. Landing JSON namespaces materialised ---------------------------


@pytest.mark.parametrize("locale", NEW_LOCALES)
def test_landing_json_namespaces_present(locale: str) -> None:
    """Every en-SG namespace must have a counterpart in the new locale."""
    en_ns = {p.name for p in (LANDING_LOCALES_DIR / "en-SG").glob("*.json")}
    new_ns = {
        p.name
        for p in (LANDING_LOCALES_DIR / locale).glob("*.json")
        if p.stem != "_translation_status"
    }
    missing = en_ns - new_ns
    assert not missing, f"{locale} missing namespaces: {missing}"
    # Spot-check that one namespace round-trips as a flat dict.
    common = json.loads(
        (LANDING_LOCALES_DIR / locale / "common.json").read_text(encoding="utf-8")
    )
    assert isinstance(common, dict) and common, f"{locale} common.json empty"


# ---- 8. Wave-B P1 real-translation pass (mock-mode hardening) -----------


WAVE_B_LOCALES = ["id-ID", "ms-MY", "th-TH", "vi-VN", "ar-EG", "ar-SA", "he-IL"]


@pytest.mark.parametrize("locale", WAVE_B_LOCALES)
def test_no_broken_locale_prefix_in_outputs(locale: str) -> None:
    """The old `[locale] English ...` placeholder pattern must not appear.

    Mock-mode previously emitted ``[id-ID] Welcome`` style strings that
    leaked into the UI; the Wave-B fallback echoes the English source
    verbatim instead, so this regex must find nothing.
    """
    prefix = f"[{locale}]"
    ftl = (CATALOG_DIR / locale / "main.ftl").read_text(encoding="utf-8")
    assert prefix not in ftl, f"FTL still has '{prefix}' placeholder"
    for ns in (LANDING_LOCALES_DIR / locale).glob("*.json"):
        if ns.stem == "_translation_status":
            continue
        body = ns.read_text(encoding="utf-8")
        assert prefix not in body, f"{ns.name} still has '{prefix}' placeholder"


def _load_translate_module():
    """Hand-load ``scripts/i18n_translate.py`` past sibling-repo collisions.

    Other repos on ``sys.path`` ship their own ``scripts`` package, so we
    mirror the trick used by ``tests/test_i18n_translate.py``.
    """
    import importlib.util as _ilu
    import types as _types

    pkg = _types.ModuleType("scripts")
    pkg.__path__ = [str(REPO_ROOT_FOR_SCRIPTS / "scripts")]
    sys.modules.setdefault("scripts", pkg)

    for sub in ("i18n_glossary", "i18n_translate"):
        spec = _ilu.spec_from_file_location(
            f"scripts.{sub}", REPO_ROOT_FOR_SCRIPTS / "scripts" / f"{sub}.py"
        )
        assert spec and spec.loader
        mod = _ilu.module_from_spec(spec)
        sys.modules[f"scripts.{sub}"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["scripts.i18n_translate"]


def test_mock_mode_returns_source_when_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_llm_translate_batch`` must echo the source (not garble it) when
    ``ANTHROPIC_API_KEY`` is absent, and tag the result so the review
    queue can detect it.
    """
    import asyncio

    mod = _load_translate_module()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    items = [mod.FluentString(key="welcome-message", text="Welcome { $name }!", pattern=None)]
    rows = asyncio.run(
        mod._llm_translate_batch(items, "id-ID", "", model="claude-haiku-4-5-20251001")
    )
    assert rows[0]["translation"] == "Welcome { $name }!"
    assert rows[0]["confidence"] == "needs_translation"
    assert rows[0].get("needs_translation") is True


@pytest.mark.parametrize("locale", WAVE_B_LOCALES)
def test_glossary_dnt_terms_preserved(locale: str) -> None:
    """KiX / Soul / ELTM / Toast Box must remain verbatim in every catalog."""
    _load_translate_module()  # also registers scripts.i18n_glossary
    from scripts.i18n_glossary import load_glossary  # type: ignore

    dnt = [t for t in load_glossary(locale) if t.do_not_translate]
    assert dnt, "Glossary appears empty — fixture regression"

    en = (CATALOG_DIR / "en-SG" / "main.ftl").read_text(encoding="utf-8")
    tgt = (CATALOG_DIR / locale / "main.ftl").read_text(encoding="utf-8")
    for term in dnt:
        if term.source_term in en:
            assert term.source_term in tgt, (
                f"{locale}: DNT term {term.source_term!r} dropped"
            )


@pytest.mark.parametrize("locale", WAVE_B_LOCALES)
def test_icu_placeholders_intact(locale: str) -> None:
    """Every `{name}` / `{count, plural, …}` in en-SG survives translation."""
    icu = re.compile(r"\{[^}]+\}")
    for ns in (LANDING_LOCALES_DIR / "en-SG").glob("*.json"):
        en = json.loads(ns.read_text(encoding="utf-8"))
        tgt_path = LANDING_LOCALES_DIR / locale / ns.name
        tgt = json.loads(tgt_path.read_text(encoding="utf-8"))
        for k, v in en.items():
            if not isinstance(v, str):
                continue
            en_phs = sorted(icu.findall(v))
            tgt_v = tgt.get(k, "")
            tgt_phs = sorted(icu.findall(tgt_v if isinstance(tgt_v, str) else ""))
            assert en_phs == tgt_phs, (
                f"{locale}/{ns.name}:{k}  en={en_phs}  tgt={tgt_phs}"
            )


@pytest.mark.parametrize("locale", WAVE_B_LOCALES)
def test_translation_status_sidecar(locale: str) -> None:
    """Stubbed locales must ship a `_translation_status.json` sidecar so
    the review queue can prioritise the first real LLM pass.

    Covers both the FTL catalog and the landing JSON catalog.
    """
    ftl_side = CATALOG_DIR / locale / "_translation_status.json"
    assert ftl_side.is_file(), f"Missing FTL sidecar for {locale}"
    data = json.loads(ftl_side.read_text(encoding="utf-8"))
    assert data["locale"] == locale
    assert data["total"] > 0
    assert data["needs_translation"] + data["auto_translated"] == data["total"]

    json_side = LANDING_LOCALES_DIR / locale / "_translation_status.json"
    assert json_side.is_file(), f"Missing JSON sidecar for {locale}"
    j = json.loads(json_side.read_text(encoding="utf-8"))
    assert j["locale"] == locale
    assert "namespaces" in j and j["namespaces"], j
    # Every en-SG namespace tracked.
    en_ns = {p.stem for p in (LANDING_LOCALES_DIR / "en-SG").glob("*.json")}
    assert en_ns.issubset(set(j["namespaces"].keys()))
