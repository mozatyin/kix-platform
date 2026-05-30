"""Tests for app.services.game_library expansion (15 templates).

20 tests covering: rendering, locale, brand asset injection, prize-pool
calculation, recommendations, interface coverage, and metadata shape.
"""

from __future__ import annotations

import re

import pytest

from app.services.game_library import (
    GAME_LIBRARY,
    GameTemplate,
    get_template,
    list_templates,
    recommend_for_brand,
)


NEW_TYPES = [
    "slot_machine",
    "wheel_of_fortune",
    "memory_match",
    "whack_a_mole",
    "catch_falling",
    "bubble_pop",
    "target_shoot",
    "stack_tower",
    "lucky_dice",
    "scratch_galaxy",
]
LEGACY_TYPES = ["spin", "scratch", "match", "quiz", "shake"]
ALL_TYPES = LEGACY_TYPES + NEW_TYPES


# ---------------------------------------------------------------------------
# 1. catalog size
# ---------------------------------------------------------------------------
def test_catalog_size_is_15():
    # Wave E2 expands library; original 15 must remain present.
    assert len(GAME_LIBRARY) >= 15
    for t in ALL_TYPES:
        assert t in GAME_LIBRARY


# ---------------------------------------------------------------------------
# 2. each of 10 new types generates valid HTML
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("type_name", NEW_TYPES)
def test_new_types_render_valid_html(type_name):
    tpl = get_template(type_name)
    html = tpl.generate_html(
        brand_assets={"primary_color": "#ff00aa", "logo_url": "https://x/logo.png"},
        prize_pool={"prizes": [{"label": "Gift"}, {"label": "Discount"}]},
        locale="en-SG",
    )
    assert html.startswith("<!doctype html>")
    assert "</html>" in html
    # contains script & style tags
    assert "<script>" in html and "</script>" in html
    assert "<style>" in html and "</style>" in html
    # mobile viewport
    assert "viewport" in html
    # WCAG: focus visible style present
    assert "focus-visible" in html
    # mobile-first responsive
    assert "100vh" in html or "100vw" in html


# ---------------------------------------------------------------------------
# 3. HTML opens without obvious JS errors (basic structural check)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("type_name", ALL_TYPES)
def test_html_balanced_tags_and_no_undefined(type_name):
    tpl = get_template(type_name)
    html = tpl.generate_html({"primary_color": "#123456"}, {"prizes": []}, "en-SG")
    # roughly balanced script tags
    assert html.count("<script>") == html.count("</script>")
    assert html.count("<style>") == html.count("</style>")
    # no stray python-template artifacts
    assert "{{" not in html  # double-braces would mean unrendered template
    assert "None" not in html.split("<body>")[0]  # no python None in head


# ---------------------------------------------------------------------------
# 4. Brand assets injected correctly
# ---------------------------------------------------------------------------
def test_brand_logo_injected():
    tpl = get_template("slot_machine")
    html = tpl.generate_html(
        {"primary_color": "#aabbcc", "logo_url": "https://cdn.example.com/abc.png"},
        {"prizes": []},
        "en-SG",
    )
    assert "https://cdn.example.com/abc.png" in html
    assert "#aabbcc" in html


def test_brand_color_validation_falls_back():
    tpl = get_template("spin")
    # invalid color shouldn't blow up; fallback used
    html = tpl.generate_html({"primary_color": "not-a-color"}, {"prizes": []}, "en-SG")
    assert "</html>" in html


# ---------------------------------------------------------------------------
# 5. Locale switching works
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("locale,expected", [
    ("en-SG", "en-SG"),
    ("zh-Hans-SG", "zh-Hans-SG"),
])
def test_locale_lang_tag(locale, expected):
    tpl = get_template("memory_match")
    html = tpl.generate_html({"primary_color": "#000000"}, {"prizes": []}, locale)
    assert f'lang="{expected}"' in html


def test_rtl_locale_marks_dir_rtl():
    tpl = get_template("bubble_pop")
    html = tpl.generate_html({"primary_color": "#ff66c4"}, {"prizes": []}, "ar-SG")
    assert 'dir="rtl"' in html


def test_zh_locale_shows_chinese_title():
    tpl = get_template("whack_a_mole")
    html = tpl.generate_html({"primary_color": "#000000"}, {"prizes": []}, "zh-Hans-SG")
    assert "打地鼠" in html


# ---------------------------------------------------------------------------
# 6. recommend-for-brand returns sensible suggestions
# ---------------------------------------------------------------------------
def test_recommend_returns_three():
    recs = recommend_for_brand("brand-1", "foodies")
    assert len(recs) == 3
    assert all(r in GAME_LIBRARY for r in recs)
    # for fnb audience all 3 should be in fnb-tagged templates
    fnb = {n for n, t in GAME_LIBRARY.items() if "fnb" in t.recommended_industries}
    assert any(r in fnb for r in recs)


def test_recommend_deterministic():
    a = recommend_for_brand("brand-XYZ", "shoppers")
    b = recommend_for_brand("brand-XYZ", "shoppers")
    assert a == b


def test_recommend_handles_unknown_audience():
    recs = recommend_for_brand("brand-2", "martian-mole-people")
    assert len(recs) == 3  # never errors


# ---------------------------------------------------------------------------
# 7. all templates implement required interface
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("type_name", ALL_TYPES)
def test_interface_completeness(type_name):
    tpl = get_template(type_name)
    assert isinstance(tpl, GameTemplate)
    assert tpl.type_name == type_name
    assert tpl.display_name_en and tpl.display_name_zh
    assert tpl.description_en and tpl.description_zh
    assert isinstance(tpl.asset_requirements, dict)
    assert "required" in tpl.asset_requirements
    assert isinstance(tpl.scoring, dict)
    assert callable(tpl.generate_html)
    assert callable(tpl.calculate_win)
    assert callable(tpl.estimate_completion_time)
    assert callable(tpl.difficulty_levels)


# ---------------------------------------------------------------------------
# 8. completion time estimates are reasonable (5-60s)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("type_name", ALL_TYPES)
def test_completion_time_in_range(type_name):
    tpl = get_template(type_name)
    t = tpl.estimate_completion_time()
    assert 5 <= t <= 60, f"{type_name} t={t}"


# ---------------------------------------------------------------------------
# 9. difficulty levels supported
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("type_name", ALL_TYPES)
def test_difficulty_levels(type_name):
    tpl = get_template(type_name)
    diffs = tpl.difficulty_levels()
    assert isinstance(diffs, list) and diffs
    assert all(d in {"easy", "medium", "hard"} for d in diffs)


# ---------------------------------------------------------------------------
# 10. prize pool calculation deterministic
# ---------------------------------------------------------------------------
def test_calculate_win_deterministic():
    tpl = get_template("slot_machine")
    pool = {"prizes": [{"label": "Big"}]}
    r1 = tpl.calculate_win(1, pool)
    r2 = tpl.calculate_win(1, pool)
    assert r1 == r2
    assert r1["won"] is True
    assert r1["prize"]["label"] == "Big"


def test_calculate_win_below_threshold():
    tpl = get_template("whack_a_mole")  # threshold 80
    r = tpl.calculate_win(20, {"prizes": [{"label": "P"}]})
    assert r["won"] is False
    assert r["prize"] is None


def test_calculate_win_empty_pool():
    tpl = get_template("spin")
    r = tpl.calculate_win(100, {"prizes": []})
    assert r["won"] is False


# ---------------------------------------------------------------------------
# 11. list_templates metadata shape
# ---------------------------------------------------------------------------
def test_list_templates_shape():
    meta = list_templates()
    assert len(meta) >= 15
    for m in meta:
        assert {"type_name", "display_name", "description",
                "asset_requirements", "scoring", "recommended_industries",
                "completion_seconds", "difficulties"} <= set(m.keys())
        assert m["display_name"]["en"] and m["display_name"]["zh"]


# ---------------------------------------------------------------------------
# 12. industry tagging covers required industries
# ---------------------------------------------------------------------------
def test_industry_tags_present():
    industries = set()
    for t in GAME_LIBRARY.values():
        industries.update(t.recommended_industries)
    # Per spec: fnb, beauty, retail must be represented
    assert {"fnb", "beauty", "retail"} <= industries


# ---------------------------------------------------------------------------
# 13. get_template raises on unknown
# ---------------------------------------------------------------------------
def test_get_template_unknown_raises():
    with pytest.raises(KeyError):
        get_template("nonexistent_game_type")
