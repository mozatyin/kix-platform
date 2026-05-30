"""Game-mechanic registry — Wave F spec #13.

Single source of truth for every mechanic available on KiX: name,
configurable parameters (JSONSchema), payload types, supported regions,
default KPIs.

Storage divergence from spec
----------------------------
The spec calls for ``data/wavef_mechanics/*.yaml``. To honour the "NEW
files inside ``wavef_*`` namespace only" constraint of this slice, the
seed catalog is shipped inline here as a Python list of dicts and a
JSONSchema validator runs at import time. The shape matches the spec
1:1 so a future move-to-YAML is mechanical.

Each mechanic dict has:
* ``id`` — slug, unique
* ``category`` — chance | skill | social | reveal | collect
* ``status`` — stable | beta | experimental
* ``params_schema`` — full JSONSchema for the config
* ``payload_types`` — voucher | points | none
* ``regions_supported`` — list of ISO country codes
* ``default_kpis`` — dict (plays_per_session, completion_rate, …)
* ``description`` — short human-readable string
* ``docs_url`` — relative URL of dev docs

NEW file.
"""

from __future__ import annotations

from typing import Any


def _slice_param(items_type: str = "string") -> dict:
    return {"type": "array", "minItems": 2, "maxItems": 12, "items": {"type": items_type}}


REGISTRY: list[dict[str, Any]] = [
    {
        "id": "spin_wheel",
        "category": "chance",
        "status": "stable",
        "description": "Weighted wheel with N slices; one spin per session.",
        "docs_url": "/docs/mechanics/spin-wheel",
        "payload_types": ["voucher", "points", "none"],
        "regions_supported": ["sg", "my", "id", "ph", "th", "vn"],
        "default_kpis": {"plays_per_session": 1.4, "completion_rate": 0.92},
        "params_schema": {
            "type": "object",
            "required": ["slices"],
            "properties": {
                "slices": _slice_param(),
                "weights_normalize": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "id": "scratch_card",
        "category": "reveal",
        "status": "stable",
        "description": "Scratch-to-reveal card with prize underneath.",
        "docs_url": "/docs/mechanics/scratch-card",
        "payload_types": ["voucher", "points"],
        "regions_supported": ["sg", "my", "id", "ph"],
        "default_kpis": {"plays_per_session": 1.6, "completion_rate": 0.88},
        "params_schema": {
            "type": "object",
            "required": ["prizes"],
            "properties": {
                "prizes": {"type": "array", "minItems": 1, "maxItems": 10},
                "scratch_threshold": {"type": "number", "minimum": 0.1, "maximum": 1.0, "default": 0.5},
            },
        },
    },
    {
        "id": "memory_match",
        "category": "skill",
        "status": "stable",
        "description": "Flip pairs of brand-asset cards within a time limit.",
        "docs_url": "/docs/mechanics/memory-match",
        "payload_types": ["voucher", "points", "none"],
        "regions_supported": ["sg", "my", "id"],
        "default_kpis": {"plays_per_session": 2.1, "completion_rate": 0.74},
        "params_schema": {
            "type": "object",
            "required": ["pair_count", "time_limit_sec"],
            "properties": {
                "pair_count": {"type": "integer", "minimum": 2, "maximum": 18},
                "time_limit_sec": {"type": "integer", "minimum": 10, "maximum": 300},
            },
        },
    },
    {
        "id": "quick_poll",
        "category": "social",
        "status": "stable",
        "description": "One-question poll with public live results.",
        "docs_url": "/docs/mechanics/quick-poll",
        "payload_types": ["none"],
        "regions_supported": ["sg", "my", "id", "ph", "th", "vn"],
        "default_kpis": {"plays_per_session": 1.0, "completion_rate": 0.96},
        "params_schema": {
            "type": "object",
            "required": ["question", "options"],
            "properties": {
                "question": {"type": "string", "minLength": 1, "maxLength": 280},
                "options": _slice_param("string"),
            },
        },
    },
    {
        "id": "daily_checkin",
        "category": "collect",
        "status": "stable",
        "description": "Streak-based daily reveal with escalating reward.",
        "docs_url": "/docs/mechanics/daily-checkin",
        "payload_types": ["points", "voucher"],
        "regions_supported": ["sg", "my", "id", "ph"],
        "default_kpis": {"plays_per_session": 1.0, "completion_rate": 0.81},
        "params_schema": {
            "type": "object",
            "required": ["days"],
            "properties": {
                "days": {"type": "integer", "minimum": 3, "maximum": 30},
            },
        },
    },
    {
        "id": "collect_a_set",
        "category": "collect",
        "status": "beta",
        "description": "Collect N of M brand pieces; complete the set to redeem.",
        "docs_url": "/docs/mechanics/collect-a-set",
        "payload_types": ["voucher"],
        "regions_supported": ["sg", "my"],
        "default_kpis": {"plays_per_session": 2.8, "completion_rate": 0.45},
        "params_schema": {
            "type": "object",
            "required": ["pieces", "target"],
            "properties": {
                "pieces": {"type": "array", "minItems": 2, "maxItems": 64},
                "target": {"type": "integer", "minimum": 2, "maximum": 64},
            },
        },
    },
    {
        "id": "calendar_reveal",
        "category": "reveal",
        "status": "stable",
        "description": "Advent-style calendar: one tile per day.",
        "docs_url": "/docs/mechanics/calendar-reveal",
        "payload_types": ["voucher", "points", "none"],
        "regions_supported": ["sg", "my", "id"],
        "default_kpis": {"plays_per_session": 1.1, "completion_rate": 0.78},
        "params_schema": {
            "type": "object",
            "required": ["tiles"],
            "properties": {
                "tiles": {"type": "array", "minItems": 1, "maxItems": 31},
            },
        },
    },
    {
        "id": "sweepstakes",
        "category": "chance",
        "status": "stable",
        "description": "Time-bound prize draw; one entry per user.",
        "docs_url": "/docs/mechanics/sweepstakes",
        "payload_types": ["voucher", "points"],
        "regions_supported": ["sg", "my", "id", "ph", "th"],
        "default_kpis": {"plays_per_session": 1.0, "completion_rate": 0.99},
        "params_schema": {
            "type": "object",
            "required": ["entry_window_hours", "prizes"],
            "properties": {
                "entry_window_hours": {"type": "integer", "minimum": 1, "maximum": 720},
                "prizes": {"type": "array", "minItems": 1, "maxItems": 50},
            },
        },
    },
    {
        "id": "geofenced_voucher",
        "category": "reveal",
        "status": "stable",
        "description": "Voucher unlocked when user enters a defined region.",
        "docs_url": "/docs/mechanics/geofenced-voucher",
        "payload_types": ["voucher"],
        "regions_supported": ["sg", "my"],
        "default_kpis": {"plays_per_session": 1.0, "completion_rate": 0.34},
        "params_schema": {
            "type": "object",
            "required": ["radius_m", "center_lat", "center_lng"],
            "properties": {
                "radius_m": {"type": "number", "minimum": 25, "maximum": 5000},
                "center_lat": {"type": "number", "minimum": -90, "maximum": 90},
                "center_lng": {"type": "number", "minimum": -180, "maximum": 180},
            },
        },
    },
    {
        "id": "refer_a_friend",
        "category": "social",
        "status": "stable",
        "description": "Both-win referral with reciprocal voucher pool.",
        "docs_url": "/docs/mechanics/refer-a-friend",
        "payload_types": ["voucher", "points"],
        "regions_supported": ["sg", "my", "id", "ph"],
        "default_kpis": {"plays_per_session": 1.0, "completion_rate": 0.21},
        "params_schema": {
            "type": "object",
            "required": ["referrer_reward", "referred_reward"],
            "properties": {
                "referrer_reward": {"type": "object"},
                "referred_reward": {"type": "object"},
            },
        },
    },
    {
        "id": "flash_promo",
        "category": "reveal",
        "status": "beta",
        "description": "Time-limited countdown; voucher pool drains on claim.",
        "docs_url": "/docs/mechanics/flash-promo",
        "payload_types": ["voucher"],
        "regions_supported": ["sg", "my", "id"],
        "default_kpis": {"plays_per_session": 1.2, "completion_rate": 0.62},
        "params_schema": {
            "type": "object",
            "required": ["duration_minutes", "pool_size"],
            "properties": {
                "duration_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
                "pool_size": {"type": "integer", "minimum": 1, "maximum": 100_000},
            },
        },
    },
]


_META_REQUIRED = {
    "id",
    "category",
    "status",
    "description",
    "docs_url",
    "payload_types",
    "regions_supported",
    "default_kpis",
    "params_schema",
}


def _validate_meta(m: dict) -> None:
    missing = _META_REQUIRED - set(m)
    if missing:
        raise ValueError(f"{m.get('id')}: missing fields {missing}")
    schema = m["params_schema"]
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError(f"{m['id']}: params_schema must be an object schema")
    if "properties" not in schema:
        raise ValueError(f"{m['id']}: params_schema needs properties")


# Validate on import — guarantees seed integrity.
_seen_ids: set[str] = set()
for _m in REGISTRY:
    _validate_meta(_m)
    if _m["id"] in _seen_ids:
        raise ValueError(f"duplicate mechanic id: {_m['id']}")
    _seen_ids.add(_m["id"])


# ── Public API ──────────────────────────────────────────────────────────


def list_mechanics(
    *,
    category: str | None = None,
    status: str | None = None,
    region: str | None = None,
) -> list[dict]:
    out = []
    for m in REGISTRY:
        if category and m["category"] != category:
            continue
        if status and m["status"] != status:
            continue
        if region and region not in m["regions_supported"]:
            continue
        out.append(m)
    return out


def get_mechanic(mech_id: str) -> dict | None:
    for m in REGISTRY:
        if m["id"] == mech_id:
            return m
    return None


def get_schema(mech_id: str) -> dict | None:
    m = get_mechanic(mech_id)
    return m["params_schema"] if m else None
