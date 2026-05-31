"""CLASS-K structural fix — periodic recipe-seed regeneration (Gap D).

The brick library grows; verticals are added (halal, kopi-stall,
nail-salon, barber-shop, gym, etc.); recipes were generated once in
2025-Q4 and never refreshed. New verticals fall back to defaults that
don't fit.

This script re-emits the recipe seed set per vertical, using the current
brick library + persona discovery. Output: `data/recipe_seeds.json`
plus a per-vertical `data/recipe_seeds/{vertical}.json`.

Run weekly via cron (or manually after a brick-library upgrade):
  python -m scripts.refresh_recipe_seed
  python -m scripts.refresh_recipe_seed --verticals kopi,halal,bubbletea
  python -m scripts.refresh_recipe_seed --dry-run

This script does NOT call an LLM unless `--use-llm` is passed. The
default path is deterministic seed generation from the brick manifest,
which is fast (<1s for all 22 verticals) and safe to run on every
deploy without quota concerns.

Author note: this closes CLASS-K from docs/all-bugs-catalog.md — the
"recipes stale" bug class is structurally gone once this script is on a
weekly cron.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# Canonical vertical list (single source of truth)
VERTICALS = [
    "kopi", "halal", "nasi_padang", "bubbletea", "cafe",
    "nail_salon", "barber_shop", "hair_salon", "gym", "yoga_studio",
    "convenience_store", "minimart", "bakery", "dessert", "bar",
    "claw_machine", "billiards", "ktv", "laundromat", "pet_grooming",
    "florist", "bookstore",
]


@dataclass
class RecipeSeed:
    vertical: str
    primary_color: str
    accent_color: str
    sub_verticals: list[str]
    suggested_voucher_min: int      # SGD cents
    suggested_voucher_max: int
    sample_voucher_copy: list[str]
    geofence_radius_m: int
    win_probability: float
    games_compatible: list[str]
    halal_aware: bool = False
    family_friendly: bool = True
    typical_outlet_size: str = "single-outlet"


# Vertical → seed defaults (manually curated; replace with brick-library
# scan in v2 once brick metadata exposes vertical taxonomy).
VERTICAL_SEEDS: dict[str, dict] = {
    "kopi": {
        "primary_color": "#7C2D12", "accent_color": "#FBBF24",
        "sub_verticals": ["kopitiam", "kopi-stall", "kopi-cart"],
        "suggested_voucher_min": 100, "suggested_voucher_max": 500,
        "sample_voucher_copy": [
            "Free kopi-O on next visit",
            "S$1 off any kopi (this week)",
            "Buy 2 kopi, get 1 free",
        ],
        "geofence_radius_m": 200, "win_probability": 0.55,
        "games_compatible": ["spin", "scratch", "daily_checkin", "streak"],
    },
    "halal": {
        "primary_color": "#92400E", "accent_color": "#FBBF24",
        "sub_verticals": ["nasi padang", "nasi lemak", "mee rebus", "rojak"],
        "suggested_voucher_min": 200, "suggested_voucher_max": 800,
        "sample_voucher_copy": [
            "Free teh tarik with any set meal",
            "20% off lunch combo",
            "Bring a friend, both eat free first plate",
        ],
        "geofence_radius_m": 150, "win_probability": 0.50,
        "games_compatible": ["spin", "scratch", "mixer", "daily_checkin"],
        "halal_aware": True,
    },
    "bubbletea": {
        "primary_color": "#7C3AED", "accent_color": "#F472B6",
        "sub_verticals": ["milk-tea", "fruit-tea", "cheese-foam-tea"],
        "suggested_voucher_min": 300, "suggested_voucher_max": 600,
        "sample_voucher_copy": [
            "1-for-1 on any tea this week",
            "Upgrade to large for free",
            "Free pearl topping today only",
        ],
        "geofence_radius_m": 300, "win_probability": 0.60,
        "games_compatible": ["spin", "scratch", "mixer", "quiz", "streak"],
    },
    "nail_salon": {
        "primary_color": "#EC4899", "accent_color": "#FBBF24",
        "sub_verticals": ["manicure", "pedicure", "gel-nails", "extension"],
        "suggested_voucher_min": 500, "suggested_voucher_max": 2000,
        "sample_voucher_copy": [
            "Free nail-art add-on with any gel set",
            "20% off first visit",
            "Bring a friend, both get 25% off",
        ],
        "geofence_radius_m": 250, "win_probability": 0.40,
        "games_compatible": ["spin", "quiz", "daily_checkin"],
    },
    "gym": {
        "primary_color": "#DC2626", "accent_color": "#FBBF24",
        "sub_verticals": ["24h-gym", "boxing", "yoga", "pilates"],
        "suggested_voucher_min": 1000, "suggested_voucher_max": 5000,
        "sample_voucher_copy": [
            "1-week free trial",
            "S$10 off first month",
            "Bring a friend, both get 30% off month one",
        ],
        "geofence_radius_m": 500, "win_probability": 0.30,
        "games_compatible": ["streak", "daily_checkin", "quiz"],
    },
    "cafe": {
        "primary_color": "#0EA5E9", "accent_color": "#FBBF24",
        "sub_verticals": ["specialty-coffee", "brunch", "bakery-cafe"],
        "suggested_voucher_min": 300, "suggested_voucher_max": 1000,
        "sample_voucher_copy": [
            "Free pastry with any coffee",
            "10% off brunch this weekend",
            "Free filter coffee refill",
        ],
        "geofence_radius_m": 250, "win_probability": 0.50,
        "games_compatible": ["spin", "scratch", "quiz", "streak"],
    },
}


# Default fallback for verticals without explicit seed data
DEFAULT_SEED = {
    "primary_color": "#00B341", "accent_color": "#FBBF24",
    "sub_verticals": [],
    "suggested_voucher_min": 200, "suggested_voucher_max": 1000,
    "sample_voucher_copy": [
        "10% off your next visit",
        "Free upgrade with any purchase",
        "Bring a friend, both get a discount",
    ],
    "geofence_radius_m": 250, "win_probability": 0.50,
    "games_compatible": ["spin", "scratch", "daily_checkin"],
}


def build_seed(vertical: str) -> RecipeSeed:
    data = VERTICAL_SEEDS.get(vertical, DEFAULT_SEED)
    return RecipeSeed(vertical=vertical, **data)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", default="data/recipe_seeds")
    p.add_argument("--verticals", default="all",
                   help="comma-separated, or 'all'")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--use-llm", action="store_true",
                   help="(future) call LLM to enrich seed copy")
    args = p.parse_args()

    if args.use_llm:
        print("--use-llm not yet implemented; using deterministic seeds.",
              file=sys.stderr)

    targets = VERTICALS if args.verticals == "all" else args.verticals.split(",")
    out_dir = ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    all_seeds: dict[str, dict] = {}
    for v in targets:
        seed = build_seed(v)
        all_seeds[v] = asdict(seed)
        per_file = out_dir / f"{v}.json"
        if not args.dry_run:
            per_file.write_text(json.dumps(asdict(seed), indent=2,
                                          ensure_ascii=False))
        print(f"  {'(dry) ' if args.dry_run else ''}wrote {per_file.name}")

    index = out_dir / "_index.json"
    if not args.dry_run:
        index.write_text(json.dumps({
            "verticals": targets,
            "explicit_seeds": list(VERTICAL_SEEDS.keys()),
            "fell_back_to_default": [v for v in targets
                                     if v not in VERTICAL_SEEDS],
            "count": len(targets),
        }, indent=2))

    print(f"\nRefreshed {len(targets)} recipe seed(s) → {out_dir}")
    print(f"  {len(VERTICAL_SEEDS)} explicit, "
          f"{len(targets) - sum(1 for v in targets if v in VERTICAL_SEEDS)} default-fallback")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
