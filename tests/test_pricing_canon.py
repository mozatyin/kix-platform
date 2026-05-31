"""Tests for app/services/pricing_canon.py — CLASS-J structural fix."""
import pytest

from app.services.pricing_canon import (
    BY_ID, CANONICAL_TIERS, TIER_FOUNDING_100, TIER_FREE,
    TIER_VERIFIED_BUSINESS,
    find_off_canon_pricing, get_tier, render_short_label,
)


# ── Canon integrity ──

def test_three_canonical_tiers():
    assert len(CANONICAL_TIERS) == 3
    ids = {t.tier_id for t in CANONICAL_TIERS}
    assert ids == {"free", "verified_business", "founding_100"}


def test_tier_free_requires_cc_as_anti_abuse():
    """Founder 2026-05-31: Free tier requires CC as joker/hacker filter.
    CC is NEVER charged on free tier — pure anti-abuse signal."""
    assert TIER_FREE.cc_required is True
    assert "NEVER" in TIER_FREE.no_charge_until
    assert TIER_FREE.take_rate_pct == 0.0


def test_tier_verified_requires_cc():
    assert TIER_VERIFIED_BUSINESS.cc_required is True
    # R10 copy fix: removed "first successful campaign" ambiguity; now trial-first
    assert "14-day free trial" in TIER_VERIFIED_BUSINESS.no_charge_until
    assert "no card needed" in TIER_VERIFIED_BUSINESS.no_charge_until


def test_tier_founding_zero_take_rate_per_city():
    assert TIER_FOUNDING_100.take_rate_pct == 0.0
    assert TIER_FOUNDING_100.scope == "per-city"
    assert TIER_FOUNDING_100.approval_required is True
    assert "0% take rate forever" in TIER_FOUNDING_100.included[2]


def test_tiers_are_frozen():
    """Cannot mutate canonical values — guards against accidental drift."""
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        TIER_FREE.cc_required = True  # type: ignore


def test_get_tier_by_id():
    assert get_tier("free") is TIER_FREE
    assert get_tier("verified_business") is TIER_VERIFIED_BUSINESS


def test_get_unknown_tier_raises():
    with pytest.raises(KeyError):
        get_tier("enterprise")     # we explicitly don't have this tier


def test_short_labels_distinct():
    labels = [render_short_label(t) for t in CANONICAL_TIERS]
    assert len(set(labels)) == 3   # all distinct


def test_short_label_free_emphasizes_no_card():
    assert "no card" in render_short_label(TIER_FREE).lower()


def test_short_label_founding_includes_city_and_approval():
    label = render_short_label(TIER_FOUNDING_100).lower()
    assert "founding" in label
    assert "per city" in label
    assert "approval" in label


# ── Drift detection ──

def test_clean_landing_text_passes():
    text = "Pay only for verified new customers. CPA from S$3."
    assert find_off_canon_pricing(text) == []


def test_detects_free_period_without_founding():
    text = "Get 3 months free when you sign up today."
    issues = find_off_canon_pricing(text)
    assert any("free-period" in i for i in issues)


def test_free_period_with_founding_context_ok():
    text = "Founding-100 merchants get 6 months Premium free."
    assert find_off_canon_pricing(text) == []


def test_detects_lifetime_discount():
    text = "Lifetime discount available for early adopters."
    issues = find_off_canon_pricing(text)
    assert any("lifetime" in i for i in issues)


def test_detects_commission_wording():
    text = "We take 5% commission on every sale."
    issues = find_off_canon_pricing(text)
    assert any("commission" in i for i in issues)


def test_canonical_take_rate_passes():
    text = "We take 10% take rate on Verified tier."
    assert find_off_canon_pricing(text) == []


def test_landing_gen_output_passes_drift_check():
    """End-to-end: real landing_gen output is canon-clean."""
    from app.services.landing_gen import BrandConfig, generate_landing
    cfg = BrandConfig(brand_id="b1", brand_name="X",
                      hero_tagline="Pay only for verified new customers",
                      hero_sub="Free SaaS.")
    html = generate_landing(cfg)
    assert find_off_canon_pricing(html) == []
