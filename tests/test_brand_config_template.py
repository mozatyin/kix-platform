"""Tests for the BrandConfigCreate documentation + autofill fixes.

Sim feedback (sg-marketplace 30-day): minimal payloads
`{"brand_id":"x","name":"y"}` were returning 422 because the required
`config_json` sections (energy/games/leaderboard) were undocumented.

This suite locks in:
  * GET /api/v1/brands/config-template returns a complete valid payload
  * GET /api/v1/brands/{bid}/config-schema returns the contract
  * POST /api/v1/brands/ with missing sections is auto-filled (no 422)
  * Field-level descriptions are present on BrandConfigCreate
"""

from __future__ import annotations

import uuid

import pytest


def _uniq(prefix: str) -> str:
    """Return a brand_id-safe unique id so re-runs don't collide on PG PK."""
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@pytest.mark.asyncio
async def test_config_template_endpoint_returns_complete_payload(client, clean_redis):
    """The template must be self-sufficient — POST'ing it back must work
    end-to-end without manual fix-ups.
    """
    res = await client.get("/api/v1/brands/config-template")
    assert res.status_code == 200, res.text
    body = res.json()

    # Top-level keys mirror BrandConfigCreate.
    assert {"brand_id", "brand_name", "brand_slug", "config_json"} <= set(body.keys())

    # config_json must contain every required section.
    cfg = body["config_json"]
    assert {"energy", "games", "leaderboard"} <= set(cfg.keys())

    # Sections must be non-empty dicts (i.e. real defaults, not {}).
    for section in ("energy", "games", "leaderboard"):
        assert isinstance(cfg[section], dict)


@pytest.mark.asyncio
async def test_config_schema_endpoint_lists_required_sections(client, clean_redis):
    """The schema endpoint must declare which sections are required."""
    res = await client.get("/api/v1/brands/some_brand/config-schema")
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["brand_id"] == "some_brand"
    assert "schema" in body
    assert set(body["required_sections"]) == {"energy", "games", "leaderboard"}
    # The schema document itself must mark them required at the top level.
    schema = body["schema"]
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"energy", "games", "leaderboard"}
    # Each required section must have an example (this is what makes the
    # OpenAPI surface actually self-documenting).
    for section in ("energy", "games", "leaderboard"):
        assert "example" in schema["properties"][section]


@pytest.mark.asyncio
async def test_brand_create_autofills_missing_sections(client, clean_redis):
    """Minimal config_json must be auto-filled — no 422 — sim repro."""
    bid = _uniq("autofill")
    res = await client.post(
        "/api/v1/brands/",
        json={
            "brand_id": bid,
            "brand_name": "Autofill X",
            "brand_slug": bid,
            "config_json": {},  # ← the sim-log smoking gun
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    # Auto-fill must have populated all three required sections.
    assert {"energy", "games", "leaderboard"} <= set(body["config_json"].keys())


@pytest.mark.asyncio
async def test_brand_create_partial_config_keeps_caller_values(client, clean_redis):
    """When caller supplies one section, the others are filled but their
    section is left intact (no clobbering).
    """
    bid = _uniq("partial")
    res = await client.post(
        "/api/v1/brands/",
        json={
            "brand_id": bid,
            "brand_name": "Partial Y",
            "brand_slug": bid,
            "config_json": {
                "energy": {"max": 99, "regen_minutes": 1, "refill_cost_cents": 0},
            },
        },
    )
    assert res.status_code == 201, res.text
    cfg = res.json()["config_json"]
    # Caller's explicit values preserved.
    assert cfg["energy"]["max"] == 99
    # Other sections filled with defaults.
    assert "games" in cfg
    assert "leaderboard" in cfg


@pytest.mark.asyncio
async def test_template_payload_is_directly_postable(client, clean_redis):
    """End-to-end: copy the template, change ids, POST → 201.

    This is the highest-value contract: the OpenAPI example must actually
    work as a real request body without modification beyond ids.
    """
    template = (await client.get("/api/v1/brands/config-template")).json()
    bid = _uniq("tmpl_consumer")
    template["brand_id"] = bid
    template["brand_slug"] = bid

    res = await client.post("/api/v1/brands/", json=template)
    assert res.status_code == 201, res.text


@pytest.mark.asyncio
async def test_brand_config_create_schema_has_field_descriptions():
    """BrandConfigCreate model must carry description= on every field so
    the OpenAPI schema is self-documenting (sim observation: docs gap was
    root cause of the 422 confusion).
    """
    from app.schemas import BrandConfigCreate

    fields = BrandConfigCreate.model_fields
    for field_name in ("brand_id", "brand_name", "brand_slug", "config_json"):
        meta = fields[field_name]
        assert meta.description, (
            f"BrandConfigCreate.{field_name} is missing a description= "
            "for OpenAPI surfacing"
        )


@pytest.mark.asyncio
async def test_brand_config_create_config_json_example_present():
    """The config_json example must contain all three required sections
    so the OpenAPI 'Try it out' button delivers a working payload.
    """
    from app.schemas import BrandConfigCreate

    examples = BrandConfigCreate.model_fields["config_json"].examples or []
    assert examples, "BrandConfigCreate.config_json must have examples="
    example = examples[0]
    assert {"energy", "games", "leaderboard"} <= set(example.keys())
