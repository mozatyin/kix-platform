"""Tests for app/services/customer_vocab.py — CLASS-D structural fix."""
import pytest
from app.services.customer_vocab import (
    FORBIDDEN, PREFERRED, VocabHit, VocabViolation,
    find_forbidden, is_clean, suggest, vocab_check,
)


def test_clean_text_passes():
    text = "Pay only for verified new customers. CPA from S$3."
    assert is_clean(text)
    vocab_check(text)  # no raise


def test_trinity_3t_triggers_violation():
    text = "We use Trinity 3T to build."
    assert not is_clean(text)
    with pytest.raises(VocabViolation) as exc:
        vocab_check(text)
    assert "trinity 3t" in str(exc.value).lower()


def test_pdca_triggers_violation():
    text = "Our PDCA cycle is fast."
    with pytest.raises(VocabViolation):
        vocab_check(text)


def test_word_boundary_avoids_false_positive():
    # "wafler" must NOT match "wafl"
    text = "She is a wafler in her sleep."
    assert is_clean(text)


def test_multiple_hits_all_reported():
    text = "Our Trinity 3T uses ELTM and WAFL together."
    hits = find_forbidden(text)
    assert len(hits) == 3
    words = sorted(h.word for h in hits)
    assert words == ["eltm", "trinity 3t", "wafl"]


def test_violation_includes_context():
    text = "Built with Trinity 3T technology since 2025."
    with pytest.raises(VocabViolation) as exc:
        vocab_check(text)
    msg = str(exc.value)
    assert "Built with" in msg or "technology" in msg


def test_violation_caps_displayed_hits():
    text = "PDCA PDCA PDCA PDCA PDCA PDCA PDCA PDCA PDCA"
    with pytest.raises(VocabViolation) as exc:
        vocab_check(text)
    # 9 hits, message shows first 5 + "+4 more"
    assert "+4 more" in str(exc.value)


def test_case_insensitive():
    for variant in ["trinity 3t", "Trinity 3T", "TRINITY 3T", "tRinITy 3T"]:
        assert not is_clean(variant), variant


def test_suggest_replaces_enterprise_phrases():
    assert "chain owner" in suggest("Our enterprise customer signs up")
    assert "for chains" in suggest("Solutions for enterprise teams")
    assert "chains tier" in suggest("enterprise tier pricing")


def test_suggest_replaces_internal_terms():
    out = suggest("Our Trinity 3T method uses brick library.")
    assert "how we build" in out
    assert "game template library" in out
    assert "Trinity 3T" not in out
    assert "brick library" not in out


def test_suggest_preserves_clean_text():
    text = "Pay only for verified new customers."
    assert suggest(text) == text


def test_forbidden_set_immutable():
    """Regression: someone tried to FORBIDDEN.add('foo') — must raise."""
    with pytest.raises(AttributeError):
        FORBIDDEN.add("new_word")   # type: ignore


def test_empty_text_clean():
    assert is_clean("")
    assert is_clean(None)  # type: ignore
    vocab_check("")
    assert find_forbidden("") == []


def test_preferred_dict_has_expected_keys():
    assert "trinity 3t" in PREFERRED
    assert "enterprise customer" in PREFERRED


def test_vocab_hit_dataclass():
    h = VocabHit(word="pdca", position=10, context="...PDCA...")
    assert h.word == "pdca"
    assert h.position == 10


def test_vocab_check_on_landing_gen_output():
    """Integration smoke: real landing_gen output passes vocab_check."""
    from app.services.landing_gen import BrandConfig, generate_landing
    cfg = BrandConfig(
        brand_id="b1", brand_name="Heng Heng Kopi",
        hero_tagline="Pay <em>only for verified new customers</em>",
        hero_sub="Free SaaS. CPA from S$3.",
    )
    html = generate_landing(cfg)
    # Must pass without raising
    vocab_check(html)


def test_landing_gen_rejects_jargon_in_hero_sub():
    """If hero_sub contains forbidden term, generate_landing must raise."""
    from app.services.landing_gen import BrandConfig, generate_landing
    cfg = BrandConfig(
        brand_id="b1", brand_name="X",
        hero_tagline="T",
        hero_sub="We use Trinity 3T methodology and PDCA cycles.",
    )
    with pytest.raises(VocabViolation):
        generate_landing(cfg)
