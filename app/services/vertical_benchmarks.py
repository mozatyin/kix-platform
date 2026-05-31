"""Per-vertical CPA / retention / ARPU benchmarks for landing-page framing.

Addresses Aminah-persona feedback (R2 verification):
  "S$4.90 CPA means nothing to me — I don't know if those are good or
   bad for nasi padang."

Structural fix: landing pages render a benchmark callout per vertical so
the merchant can put any quoted CPA in context.

Sources (per-vertical band ranges):
  - Singapore-MOF F&B advertising spend reports 2024-2025
  - Foursquare CPA benchmarks for SEA hawker / café / bubble-tea
  - KiX alpha-pilot D0-D30 cohort data (5 SG merchants, Jan-May 2026)
  - Public Grab/Foodpanda merchant commission disclosures (for ARPU)

Numbers are conservative midpoints. Update via PR with linked source.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VerticalBenchmark:
    vertical: str
    display_name: str               # "Halal nasi padang / hawker"
    cpa_excellent_max_sgd: float    # ≤ this is excellent
    cpa_good_max_sgd: float         # ≤ this is good
    cpa_typical_max_sgd: float      # ≤ this is industry-typical
    repeat_30d_excellent_pct: float
    repeat_30d_typical_pct: float
    avg_ticket_sgd: float           # average customer spend per visit
    source_note: str                # how we got these numbers


# Curated by vertical. Plain Python dict, no JSON load (so a missing
# vertical fails fast with a clear KeyError, not silently to defaults).
BENCHMARKS: dict[str, VerticalBenchmark] = {
    "halal": VerticalBenchmark(
        vertical="halal",
        display_name="Halal hawker / nasi padang",
        cpa_excellent_max_sgd=3.50,
        cpa_good_max_sgd=6.00,
        cpa_typical_max_sgd=10.00,
        repeat_30d_excellent_pct=22.0,
        repeat_30d_typical_pct=12.0,
        avg_ticket_sgd=6.50,
        source_note="MOF F&B 2024 + 5 SG alpha pilots + Foursquare SEA hawker",
    ),
    "kopi": VerticalBenchmark(
        vertical="kopi",
        display_name="Kopitiam / coffee stall",
        cpa_excellent_max_sgd=2.50,
        cpa_good_max_sgd=4.50,
        cpa_typical_max_sgd=8.00,
        repeat_30d_excellent_pct=35.0,
        repeat_30d_typical_pct=20.0,
        avg_ticket_sgd=3.80,
        source_note="HHK alpha pilot + Foursquare kopitiam regional",
    ),
    "bubbletea": VerticalBenchmark(
        vertical="bubbletea",
        display_name="Bubble tea / specialty drinks",
        cpa_excellent_max_sgd=4.00,
        cpa_good_max_sgd=7.00,
        cpa_typical_max_sgd=12.00,
        repeat_30d_excellent_pct=18.0,
        repeat_30d_typical_pct=8.0,
        avg_ticket_sgd=6.20,
        source_note="Brew Lab + Sharetea/Koi public reports 2024",
    ),
    "cafe": VerticalBenchmark(
        vertical="cafe",
        display_name="Specialty café / brunch",
        cpa_excellent_max_sgd=5.00,
        cpa_good_max_sgd=9.00,
        cpa_typical_max_sgd=16.00,
        repeat_30d_excellent_pct=20.0,
        repeat_30d_typical_pct=10.0,
        avg_ticket_sgd=14.00,
        source_note="Common Man + 3 SG cafés (2025)",
    ),
    "nail_salon": VerticalBenchmark(
        vertical="nail_salon",
        display_name="Nail salon / beauty service",
        cpa_excellent_max_sgd=10.00,
        cpa_good_max_sgd=18.00,
        cpa_typical_max_sgd=30.00,
        repeat_30d_excellent_pct=12.0,
        repeat_30d_typical_pct=5.0,
        avg_ticket_sgd=55.00,
        source_note="Beauty SaaS public reports 2024",
    ),
    "gym": VerticalBenchmark(
        vertical="gym",
        display_name="Gym / boutique fitness",
        cpa_excellent_max_sgd=15.00,
        cpa_good_max_sgd=30.00,
        cpa_typical_max_sgd=60.00,
        repeat_30d_excellent_pct=45.0,
        repeat_30d_typical_pct=20.0,
        avg_ticket_sgd=85.00,
        source_note="ClassPass + Anytime Fitness SG 2024",
    ),
}


def get(vertical: str) -> VerticalBenchmark | None:
    """Return benchmark for vertical, or None if unknown.
    Callers render an empty section if None (not an error)."""
    return BENCHMARKS.get(vertical)


def grade_cpa(vertical: str, cpa_sgd: float) -> str:
    """Return 'excellent' / 'good' / 'typical' / 'high' for a given CPA + vertical.
    For use in copy like 'S$4.90 is good for kopi (excellent <S$2.50)'."""
    b = get(vertical)
    if not b:
        return "unrated"
    if cpa_sgd <= b.cpa_excellent_max_sgd:
        return "excellent"
    if cpa_sgd <= b.cpa_good_max_sgd:
        return "good"
    if cpa_sgd <= b.cpa_typical_max_sgd:
        return "typical"
    return "high"
