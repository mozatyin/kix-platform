"""Tests for app/services/vertical_benchmarks.py."""
import pytest
from app.services.vertical_benchmarks import (
    BENCHMARKS, VerticalBenchmark, get, grade_cpa,
)


def test_halal_benchmark_loaded():
    b = get("halal")
    assert b is not None
    assert b.vertical == "halal"
    assert "nasi padang" in b.display_name.lower() or "hawker" in b.display_name.lower()
    assert b.cpa_excellent_max_sgd < b.cpa_good_max_sgd < b.cpa_typical_max_sgd


def test_unknown_vertical_returns_none():
    assert get("rocket_science") is None


def test_grade_cpa_halal():
    assert grade_cpa("halal", 2.00) == "excellent"
    assert grade_cpa("halal", 4.90) == "good"
    assert grade_cpa("halal", 8.00) == "typical"
    assert grade_cpa("halal", 25.00) == "high"


def test_grade_cpa_unknown_vertical():
    assert grade_cpa("rocket_science", 5) == "unrated"


def test_all_benchmarks_ordered():
    """Bands must be strictly increasing — otherwise grade_cpa returns weird results."""
    for v, b in BENCHMARKS.items():
        assert b.cpa_excellent_max_sgd < b.cpa_good_max_sgd, v
        assert b.cpa_good_max_sgd < b.cpa_typical_max_sgd, v
        assert b.repeat_30d_typical_pct < b.repeat_30d_excellent_pct, v


def test_benchmarks_frozen():
    with pytest.raises(Exception):
        get("halal").cpa_excellent_max_sgd = 0.5  # type: ignore


def test_six_verticals_seeded():
    assert {"halal", "kopi", "bubbletea", "cafe", "nail_salon", "gym"} <= set(BENCHMARKS)
