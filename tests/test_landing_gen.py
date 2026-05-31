"""Tests for app/services/landing_gen.py — Gap B."""
import pytest

from app.services.landing_gen import (
    BrandConfig, CaseStudy, WhatYouGetItem,
    from_dict, generate_landing,
)


def test_generate_minimal_landing():
    cfg = BrandConfig(brand_id="b1", brand_name="Toast Box",
                      hero_tagline="Pay only for verified new customers",
                      hero_sub="Free SaaS.")
    html = generate_landing(cfg)
    assert "<!DOCTYPE html>" in html
    assert "Toast Box" in html
    assert "Pay only for verified new customers" in html
    assert "</body></html>" in html


def test_generated_html_has_locale_slot():
    cfg = BrandConfig(brand_id="b1", brand_name="X", hero_tagline="T", hero_sub="S")
    html = generate_landing(cfg)
    assert 'class="kix-lang-slot"' in html
    assert 'i18next-runtime.js' in html
    assert 'locale-switcher.js' in html


def test_generated_html_has_trust_footer():
    cfg = BrandConfig(brand_id="b1", brand_name="X", hero_tagline="T", hero_sub="S")
    html = generate_landing(cfg)
    assert "Mozat Pte Ltd" in html
    assert "ACRA" in html
    assert "Verify independently" in html


def test_generated_html_has_compliance_badges():
    cfg = BrandConfig(brand_id="b1", brand_name="X", hero_tagline="T", hero_sub="S")
    html = generate_landing(cfg)
    assert "PDPA-SG" in html
    assert "Halal-aware" in html


def test_generator_marker_in_meta():
    cfg = BrandConfig(brand_id="brand_xyz", brand_name="X", hero_tagline="T", hero_sub="S")
    html = generate_landing(cfg)
    assert 'name="generator"' in html
    assert "brand_xyz" in html
    assert "do not hand-edit" in html


def test_brand_id_in_cta_url():
    cfg = BrandConfig(brand_id="brand_xyz", brand_name="X", hero_tagline="T", hero_sub="S")
    html = generate_landing(cfg)
    assert "brand=brand_xyz" in html


def test_primary_color_applied_to_css_var():
    cfg = BrandConfig(brand_id="b1", brand_name="X", hero_tagline="T", hero_sub="S",
                      primary_color="#7C2D12")
    html = generate_landing(cfg)
    assert "#7C2D12" in html


def test_unsafe_color_falls_back_to_default():
    cfg = BrandConfig(brand_id="b1", brand_name="X", hero_tagline="T", hero_sub="S",
                      primary_color="javascript:alert(1)")
    html = generate_landing(cfg)
    assert "#00B341" in html      # default
    assert "javascript:" not in html


def test_brand_name_html_escaped():
    cfg = BrandConfig(brand_id="b1",
                      brand_name='<script>alert("xss")</script>',
                      hero_tagline="T", hero_sub="S")
    html = generate_landing(cfg)
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_what_you_get_renders_when_provided():
    cfg = BrandConfig(brand_id="b1", brand_name="X", hero_tagline="T", hero_sub="S",
                      what_you_get=[
                          WhatYouGetItem("79+", "Templates", "AI-generated."),
                          WhatYouGetItem("5 min", "Setup", "Live by lunch."),
                      ])
    html = generate_landing(cfg)
    assert "What you actually get" in html
    assert "79+" in html
    assert "AI-generated." in html
    assert "5 min" in html


def test_what_you_get_omitted_when_empty():
    cfg = BrandConfig(brand_id="b1", brand_name="X", hero_tagline="T", hero_sub="S",
                      what_you_get=[])
    html = generate_landing(cfg)
    assert "What you actually get" not in html


def test_case_studies_render_with_stats():
    cfg = BrandConfig(brand_id="b1", brand_name="X", hero_tagline="T", hero_sub="S",
                      case_studies=[
                          CaseStudy(brand_name="Heng Heng Kopi",
                                    location="Bedok 85, Singapore",
                                    vertical="Kopitiam",
                                    quote="Best decision we made.",
                                    quote_attribution="— Uncle Ng",
                                    stats=[("S$4.90", "D61-90 CPA"), ("28%", "14-day return")])
                      ])
    html = generate_landing(cfg)
    assert "Heng Heng Kopi" in html
    assert "Best decision we made." in html
    assert "S$4.90" in html
    assert "28%" in html
    assert "📍 Bedok 85" in html


def test_case_without_photo_shows_pending_consent():
    cfg = BrandConfig(brand_id="b1", brand_name="X", hero_tagline="T", hero_sub="S",
                      case_studies=[
                          CaseStudy(brand_name="Y", location="Bedok", vertical="V",
                                    quote="Q", quote_attribution="— a")
                      ])
    html = generate_landing(cfg)
    assert "photo pending consent" in html


def test_case_with_photo_url_includes_img():
    cfg = BrandConfig(brand_id="b1", brand_name="X", hero_tagline="T", hero_sub="S",
                      case_studies=[
                          CaseStudy(brand_name="Y", location="Bedok", vertical="V",
                                    quote="Q", quote_attribution="— a",
                                    photo_url="https://cdn.example.com/x.jpg")
                      ])
    html = generate_landing(cfg)
    assert 'src="https://cdn.example.com/x.jpg"' in html


def test_founding_block_shows_remaining_slots():
    cfg = BrandConfig(brand_id="b1", brand_name="X", hero_tagline="T", hero_sub="S",
                      city="KLCC", founding_slots_total=100, founding_slots_taken=23)
    html = generate_landing(cfg)
    assert "Founding-100 · KLCC" in html
    assert "77 of 100 founding slots remain" in html
    assert "Approved-only" in html


def test_founding_block_handles_full():
    cfg = BrandConfig(brand_id="b1", brand_name="X", hero_tagline="T", hero_sub="S",
                      city="Bedok", founding_slots_total=100, founding_slots_taken=100)
    html = generate_landing(cfg)
    assert "0 of 100 founding slots remain" in html


def test_missing_brand_id_raises():
    cfg = BrandConfig(brand_id="", brand_name="X", hero_tagline="T", hero_sub="S")
    with pytest.raises(ValueError):
        generate_landing(cfg)


def test_wrong_input_type_raises():
    with pytest.raises(TypeError):
        generate_landing({"brand_id": "x"})


def test_from_dict_roundtrip():
    d = {
        "brand_id": "b_test",
        "brand_name": "Test Brand",
        "hero_tagline": "Hello world",
        "hero_sub": "Sub",
        "city": "Bedok",
        "founding_slots_taken": 17,
        "what_you_get": [{"headline": "X", "title": "Y", "body": "Z"}],
        "case_studies": [{
            "brand_name": "B", "location": "L", "vertical": "V",
            "quote": "Q", "quote_attribution": "A",
            "stats": [("a", "b"), ("c", "d")],
        }],
    }
    cfg = from_dict(d)
    assert cfg.brand_id == "b_test"
    assert cfg.founding_slots_taken == 17
    assert len(cfg.what_you_get) == 1
    assert len(cfg.case_studies) == 1
    assert cfg.case_studies[0].stats == [("a", "b"), ("c", "d")]
    # Renders end-to-end
    html = generate_landing(cfg)
    assert "Test Brand" in html
    assert "83 of 100 founding slots remain" in html
