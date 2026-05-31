"""Tests for app/services/persona_registry.py — C · single source of truth."""
import pytest
from app.services.persona_registry import (
    Persona, PersonaAxes, PERSONAS, for_page, for_page_ids, get, list_ids,
)


def test_six_personas_seeded():
    assert set(PERSONAS) == {
        "aminah_first_time_merchant", "skeptical_owner",
        "ahmad_kopi_chain", "enterprise_manager", "consumer", "steve_jobs",
    }


def test_get_known_persona():
    p = get("ahmad_kopi_chain")
    assert p.name == "Ahmad bin Hassan"
    assert p.axes.scale == "chain"


def test_get_unknown_raises():
    with pytest.raises(KeyError):
        get("nobody")


def test_persona_frozen():
    with pytest.raises(Exception):
        get("ahmad_kopi_chain").name = "Other"


def test_aminah_axes_single_merchant():
    p = get("aminah_first_time_merchant")
    assert p.axes.audience == "merchant"
    assert p.axes.scale == "single"


def test_sandeep_axes_enterprise():
    p = get("enterprise_manager")
    assert p.axes.scale == "enterprise"
    assert p.score_floor_override == 45  # stricter than default


def test_consumer_axes_both_scales():
    p = get("consumer")
    assert p.axes.audience == "consumer"
    assert p.axes.scale == "both"


def test_for_page_single_merchant():
    ids = for_page_ids("merchant", "single")
    assert "aminah_first_time_merchant" in ids
    assert "skeptical_owner" in ids
    assert "steve_jobs" not in ids   # for_gate=False excluded by default
    assert "ahmad_kopi_chain" not in ids
    assert "enterprise_manager" not in ids


def test_critic_included_when_opted_in():
    ids = for_page_ids("merchant", "single", include_critics=True)
    assert "steve_jobs" in ids


def test_for_page_chain_merchant():
    ids = for_page_ids("merchant", "chain")
    assert ids == ["ahmad_kopi_chain"]


def test_for_page_enterprise_merchant():
    ids = for_page_ids("merchant", "enterprise")
    assert "enterprise_manager" in ids
    assert "ahmad_kopi_chain" not in ids


def test_for_page_consumer():
    ids = for_page_ids("consumer", "both")
    assert ids == ["consumer"]


def test_for_page_no_match_returns_empty():
    """A bogus axis tuple returns empty — no defaults."""
    ids = for_page_ids("alien", "outer-space")
    assert ids == []


def test_list_ids_stable():
    ids = list_ids()
    assert len(ids) == 6
    assert isinstance(ids, list)
