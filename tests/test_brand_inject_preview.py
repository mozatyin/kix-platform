"""Tests for app/services/brand_inject_preview.py — Gap F."""
import pytest

from app.services.brand_inject_preview import (
    BrandAssets, diff_summary, inject_brand,
    _sanitize_color, _sanitize_url,
)


def test_inject_brand_name_placeholder():
    html = "<h1>{{brand_name}}</h1>"
    out = inject_brand(html, BrandAssets(brand_id="b1", brand_name="Toast Box"))
    assert out == "<h1>Toast Box</h1>"


def test_inject_brand_name_multiple_placeholders():
    html = "<title>{{BRAND_NAME}}</title><meta name='author' content='__BRAND_NAME__'>"
    out = inject_brand(html, BrandAssets(brand_id="b1", brand_name="Heng Heng"))
    assert "Heng Heng" in out
    assert "{{" not in out
    assert "__BRAND_NAME__" not in out


def test_inject_voucher_copy_and_code():
    html = "<p>Win {{voucher_copy}} · code {{voucher_code}}</p>"
    out = inject_brand(html, BrandAssets(brand_id="b1", voucher_copy="Free Kopi-O",
                                          voucher_code="DEMO-2026"))
    assert "Free Kopi-O" in out
    assert "DEMO-2026" in out


def test_inject_css_vars_added_before_head():
    html = "<html><head><title>T</title></head><body></body></html>"
    out = inject_brand(html, BrandAssets(brand_id="b1", primary_color="#7C2D12",
                                          accent_color="#FBBF24"))
    assert "kix-brand-vars" in out
    assert "--kix-brand-primary: #7C2D12" in out
    assert "--kix-brand-accent: #FBBF24" in out
    assert out.index("kix-brand-vars") < out.index("</head>")


def test_inject_css_vars_no_head_prepends():
    html = "<div>game</div>"
    out = inject_brand(html, BrandAssets(brand_id="b1", primary_color="#FF0000"))
    assert out.startswith("\n<style id=\"kix-brand-vars\">")
    assert "<div>game</div>" in out


def test_no_assets_no_change():
    html = "<html><head></head><body><p>hi</p></body></html>"
    out = inject_brand(html, BrandAssets(brand_id="b1"))
    assert out == html


def test_inject_logo_url():
    html = "<html><head></head><body></body></html>"
    out = inject_brand(html, BrandAssets(brand_id="b1",
                                          logo_url="https://cdn.kix.app/b1/logo.png"))
    assert "--kix-brand-logo: url('https://cdn.kix.app/b1/logo.png')" in out


def test_inject_background_image():
    html = "<html><head></head><body></body></html>"
    out = inject_brand(html, BrandAssets(brand_id="b1",
                                          background_image_url="https://x.com/bg.jpg"))
    assert "--kix-brand-bg: url('https://x.com/bg.jpg')" in out


def test_inject_custom_slot():
    html = '<div data-kix-slot="hero">OLD</div>'
    out = inject_brand(html, BrandAssets(brand_id="b1",
                                          custom_slots={"hero": "<strong>NEW</strong>"}))
    assert "<strong>NEW</strong>" in out
    assert "OLD" not in out


def test_inject_multiple_custom_slots():
    html = '<div data-kix-slot="a">A</div><span data-kix-slot="b">B</span>'
    out = inject_brand(html, BrandAssets(brand_id="b1",
                                          custom_slots={"a": "AA", "b": "BB"}))
    assert "AA" in out and "BB" in out


def test_custom_slot_with_unsafe_id_ignored():
    html = '<div data-kix-slot="x">X</div>'
    out = inject_brand(html, BrandAssets(brand_id="b1", custom_slots={"": "Y"}))
    assert "X" in out


def test_sanitize_color_hex():
    assert _sanitize_color("#fff") == "#fff"
    assert _sanitize_color("#FFFFFF") == "#FFFFFF"
    assert _sanitize_color("#aabbcc88") == "#aabbcc88"


def test_sanitize_color_keyword():
    assert _sanitize_color("red") == "red"
    assert _sanitize_color("BLUE") == "blue"


def test_sanitize_color_rejects_unsafe():
    assert _sanitize_color("url(evil)") == "currentColor"
    assert _sanitize_color("rgb(255,0,0); /* comment */") == "currentColor"
    assert _sanitize_color("expression(alert(1))") == "currentColor"


def test_sanitize_url_https_ok():
    assert _sanitize_url("https://cdn.kix.app/x.png") == "https://cdn.kix.app/x.png"


def test_sanitize_url_data_ok():
    assert _sanitize_url("data:image/png;base64,abc==").startswith("data:image/")


def test_sanitize_url_relative_ok():
    assert _sanitize_url("/static/logo.png") == "/static/logo.png"


def test_sanitize_url_strips_quotes_parens():
    cleaned = _sanitize_url("https://x.com/a.png\"); /* evil */")
    assert "'" not in cleaned
    assert "(" not in cleaned and ")" not in cleaned
    assert '"' not in cleaned


def test_sanitize_url_rejects_javascript():
    assert _sanitize_url("javascript:alert(1)") == ""
    assert _sanitize_url("ftp://something") == ""


def test_inject_brand_wrong_html_type():
    with pytest.raises(TypeError, match="html must be a string"):
        inject_brand(None, BrandAssets(brand_id="b1"))


def test_inject_brand_wrong_assets_type():
    with pytest.raises(TypeError, match="assets must be BrandAssets"):
        inject_brand("<html></html>", {"brand_id": "b1"})


def test_diff_summary_detects_css_var_addition():
    html = "<html><head></head></html>"
    out = inject_brand(html, BrandAssets(brand_id="b1", primary_color="#fff"))
    d = diff_summary(html, out)
    assert d["css_vars_added"] == 1
    assert d["char_delta"] > 0


def test_diff_summary_no_change():
    html = "<html><head></head></html>"
    out = inject_brand(html, BrandAssets(brand_id="b1"))
    d = diff_summary(html, out)
    assert d["css_vars_added"] == 0
    assert d["char_delta"] == 0


def test_full_brand_injection_realistic():
    html = """<!DOCTYPE html>
<html>
<head><title>{{brand_name}} — Spin</title></head>
<body>
  <h1>Welcome to {{BRAND_NAME}}</h1>
  <div data-kix-slot="hero">Default hero</div>
  <p>Win {{voucher_copy}}!</p>
  <code>{{voucher_code}}</code>
</body>
</html>"""
    out = inject_brand(html, BrandAssets(
        brand_id="brand_xyz",
        brand_name="Heng Heng Kopi",
        voucher_copy="Free Kopi-O",
        voucher_code="HHK-2026-ABCD",
        primary_color="#7C2D12",
        accent_color="#FBBF24",
        logo_url="https://cdn.kix.app/hh/logo.png",
        custom_slots={"hero": "<img src='https://cdn.kix.app/hh/banner.jpg'>"},
    ))
    assert "Heng Heng Kopi" in out
    assert "Free Kopi-O" in out
    assert "HHK-2026-ABCD" in out
    assert "#7C2D12" in out
    assert "https://cdn.kix.app/hh/banner.jpg" in out
    assert "{{" not in out
    assert "kix-brand-vars" in out
