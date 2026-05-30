"""Tests for app.services.game_library Wave-E2 expansion (35 new → 50 total).

Covers: registry size, rendering, localization, brand-asset injection,
recommendation, bulk-load performance, industry tagging, interface
completeness, and metadata shape.
"""

from __future__ import annotations

import re
import time

import pytest

from app.services.game_library import (
    GAME_LIBRARY,
    GameTemplate,
    get_template,
    list_templates,
    recommend_for_brand,
)


# Wave E2 — new 35 templates -----------------------------------------------
FNB_TYPES = [
    "coffee_brewing", "burger_builder", "pizza_topping", "dim_sum_match",
    "bubble_tea_mixer", "kopi_orders", "food_delivery_dash", "cake_decorating",
    "menu_quiz", "wok_tossing", "queue_jumper", "recipe_unlocker",
    "ingredient_sort", "dessert_combo", "spice_meter",
]
ENGAGEMENT_TYPES = [
    "trivia_avalanche", "word_search_brand", "crossword_mini", "picture_puzzle",
    "spot_difference", "word_anagram", "odd_one_out", "emoji_decoder",
    "word_chain", "sequence_predictor",
]
SKILL_TYPES = [
    "flappy_brand", "tap_speed", "swipe_direction", "balance_balance",
    "precision_target", "reaction_time", "drag_path", "timing_jump",
    "shake_to_win", "voice_shout",
]
NEW_E2 = FNB_TYPES + ENGAGEMENT_TYPES + SKILL_TYPES  # 35


# -------------------------------------------------------------------------
# 1. Catalog now contains 50 templates and all new types are present
# -------------------------------------------------------------------------
def test_total_templates_is_50():
    assert len(GAME_LIBRARY) == 50


def test_new_e2_types_count():
    assert len(NEW_E2) == 35


@pytest.mark.parametrize("type_name", NEW_E2)
def test_e2_template_registered(type_name):
    assert type_name in GAME_LIBRARY, f"{type_name} not registered"


# -------------------------------------------------------------------------
# 2. Each new template renders valid self-contained HTML
# -------------------------------------------------------------------------
@pytest.mark.parametrize("type_name", NEW_E2)
def test_e2_template_renders_valid_html(type_name):
    tpl = get_template(type_name)
    html = tpl.generate_html(
        brand_assets={"primary_color": "#ff00aa", "logo_url": "https://cdn/x.png"},
        prize_pool={"prizes": [{"label": "Gift"}, {"label": "Discount"}]},
        locale="en-SG",
    )
    assert html.startswith("<!doctype html>")
    assert "</html>" in html
    assert "<script>" in html and "</script>" in html
    assert "<style>" in html and "</style>" in html
    # mobile viewport
    assert 'name="viewport"' in html
    # WCAG focus indicators
    assert "focus-visible" in html
    # mobile-first responsive
    assert "100vh" in html or "100vw" in html


# -------------------------------------------------------------------------
# 3. Balanced tags + no python artefacts
# -------------------------------------------------------------------------
@pytest.mark.parametrize("type_name", NEW_E2)
def test_e2_html_balanced(type_name):
    tpl = get_template(type_name)
    html = tpl.generate_html({"primary_color": "#123456"}, {"prizes": []}, "en-SG")
    assert html.count("<script>") == html.count("</script>")
    assert html.count("<style>") == html.count("</style>")
    assert "{{" not in html  # no unrendered f-string artefacts
    assert "None" not in html.split("<body>")[0]


# -------------------------------------------------------------------------
# 4. Locale switching produces correct lang & RTL
# -------------------------------------------------------------------------
@pytest.mark.parametrize("type_name", NEW_E2[:5])
def test_e2_locale_lang(type_name):
    tpl = get_template(type_name)
    for locale in ("en-SG", "zh-Hans-SG"):
        html = tpl.generate_html({"primary_color": "#000000"}, {"prizes": []}, locale)
        assert f'lang="{locale}"' in html


@pytest.mark.parametrize("type_name", ["coffee_brewing", "tap_speed", "trivia_avalanche"])
def test_e2_rtl_locale(type_name):
    tpl = get_template(type_name)
    html = tpl.generate_html({"primary_color": "#000000"}, {"prizes": []}, "ar-SG")
    assert 'dir="rtl"' in html


@pytest.mark.parametrize("type_name", NEW_E2)
def test_e2_localizable_zh_title(type_name):
    """Every new template must surface a Chinese title under zh locale."""
    tpl = get_template(type_name)
    assert tpl.display_name_zh, f"{type_name} missing zh display name"
    # zh title appears in <title> for zh locale
    html = tpl.generate_html({"primary_color": "#000000"}, {"prizes": []}, "zh-Hans-SG")
    assert tpl.display_name_zh in html or any(
        0x4E00 <= ord(c) <= 0x9FFF for c in html
    )


# -------------------------------------------------------------------------
# 5. Brand asset injection — logo url + primary color
# -------------------------------------------------------------------------
@pytest.mark.parametrize("type_name", NEW_E2[:10])
def test_e2_brand_injection(type_name):
    tpl = get_template(type_name)
    html = tpl.generate_html(
        {"primary_color": "#aabbcc", "logo_url": "https://cdn.example.com/img.png"},
        {"prizes": []},
        "en-SG",
    )
    assert "https://cdn.example.com/img.png" in html
    assert "#aabbcc" in html


def test_e2_invalid_color_falls_back():
    tpl = get_template("coffee_brewing")
    html = tpl.generate_html({"primary_color": "javascript:alert(1)"}, {"prizes": []}, "en-SG")
    assert "javascript:alert" not in html  # rejected by validator
    assert "</html>" in html


# -------------------------------------------------------------------------
# 6. Recommendations leverage the new F&B templates
# -------------------------------------------------------------------------
def test_recommend_foodies_includes_new_fnb():
    # Across multiple brand ids, new F&B-tagged templates should show up
    seen = set()
    for bid in [f"brand-{i}" for i in range(20)]:
        seen.update(recommend_for_brand(bid, "foodies"))
    fnb_new = set(FNB_TYPES)
    assert seen & fnb_new, "no new F&B template ever recommended for foodies"


def test_recommend_shoppers_includes_new_retail():
    seen = set()
    for bid in [f"shop-{i}" for i in range(20)]:
        seen.update(recommend_for_brand(bid, "shoppers"))
    new_retail = {
        n for n in NEW_E2
        if "retail" in get_template(n).recommended_industries
    }
    assert seen & new_retail


def test_recommend_returns_exactly_three():
    assert len(recommend_for_brand("brand-X", "foodies")) == 3


def test_recommend_deterministic():
    a = recommend_for_brand("brand-XYZ", "shoppers")
    b = recommend_for_brand("brand-XYZ", "shoppers")
    assert a == b


# -------------------------------------------------------------------------
# 7. Interface completeness for every new template
# -------------------------------------------------------------------------
@pytest.mark.parametrize("type_name", NEW_E2)
def test_e2_interface(type_name):
    tpl = get_template(type_name)
    assert isinstance(tpl, GameTemplate)
    assert tpl.type_name == type_name
    assert tpl.display_name_en and tpl.display_name_zh
    assert tpl.description_en and tpl.description_zh
    assert isinstance(tpl.asset_requirements, dict)
    assert "required" in tpl.asset_requirements
    assert "brand_logo" in tpl.asset_requirements["required"]
    assert "primary_color" in tpl.asset_requirements["required"]
    assert isinstance(tpl.scoring, dict)
    assert tpl.recommended_industries, f"{type_name} missing industries"
    assert 5 <= tpl.estimate_completion_time() <= 60
    assert tpl.difficulty_levels()


# -------------------------------------------------------------------------
# 8. calculate_win remains deterministic for the new templates
# -------------------------------------------------------------------------
@pytest.mark.parametrize("type_name", NEW_E2[:8])
def test_e2_calculate_win_deterministic(type_name):
    tpl = get_template(type_name)
    pool = {"prizes": [{"label": "Big"}, {"label": "Small"}]}
    a = tpl.calculate_win(999, pool)
    b = tpl.calculate_win(999, pool)
    assert a == b


def test_e2_calculate_win_below_threshold():
    tpl = get_template("tap_speed")  # threshold 50
    res = tpl.calculate_win(0, {"prizes": [{"label": "P"}]})
    assert res["won"] is False


# -------------------------------------------------------------------------
# 9. Industry tags — F&B family well-represented
# -------------------------------------------------------------------------
def test_fnb_industry_well_represented():
    fnb_count = sum(
        1 for t in GAME_LIBRARY.values() if "fnb" in t.recommended_industries
    )
    # 15 new F&B + several legacy/D9 = healthy F&B catalog
    assert fnb_count >= 20, f"only {fnb_count} F&B templates"


def test_industries_cover_all_personas():
    industries = set()
    for t in GAME_LIBRARY.values():
        industries.update(t.recommended_industries)
    assert {"fnb", "retail", "beauty", "education", "fitness"} <= industries


@pytest.mark.parametrize("type_name", FNB_TYPES)
def test_fnb_templates_tagged_fnb(type_name):
    tpl = get_template(type_name)
    assert "fnb" in tpl.recommended_industries


# -------------------------------------------------------------------------
# 10. Bulk load performance — 50 templates should render in < 100ms
# -------------------------------------------------------------------------
def test_bulk_render_under_100ms():
    start = time.perf_counter()
    for tpl in GAME_LIBRARY.values():
        tpl.generate_html(
            {"primary_color": "#112233", "logo_url": "https://x/y.png"},
            {"prizes": [{"label": "A"}]},
            "en-SG",
        )
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 100, f"50-template bulk render took {elapsed_ms:.1f}ms"


def test_list_templates_includes_all_50():
    meta = list_templates()
    assert len(meta) == 50
    types = {m["type_name"] for m in meta}
    assert set(NEW_E2) <= types


# -------------------------------------------------------------------------
# 11. metadata shape is JSON-serialisable for catalog endpoint
# -------------------------------------------------------------------------
@pytest.mark.parametrize("type_name", NEW_E2[:5])
def test_e2_metadata_shape(type_name):
    import json
    m = get_template(type_name).metadata()
    s = json.dumps(m)  # must round-trip cleanly
    assert json.loads(s) == m
    assert {
        "type_name", "display_name", "description", "asset_requirements",
        "scoring", "recommended_industries", "completion_seconds", "difficulties",
    } <= set(m.keys())


# -------------------------------------------------------------------------
# 12. Type-name uniqueness
# -------------------------------------------------------------------------
def test_all_type_names_unique():
    names = [t.type_name for t in GAME_LIBRARY.values()]
    assert len(names) == len(set(names))
