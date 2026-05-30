"""Tests — Wave F mechanic registry (spec #13)."""

from __future__ import annotations

import pytest

from app.services import wavef_mechanics as svc


# ── Service ──────────────────────────────────────────────────────────────


def test_registry_seeded_with_at_least_ten():
    assert len(svc.REGISTRY) >= 10


def test_every_mechanic_passes_meta_validator():
    # Re-run validation to guard against future edits.
    for m in svc.REGISTRY:
        svc._validate_meta(m)


def test_no_duplicate_ids():
    ids = [m["id"] for m in svc.REGISTRY]
    assert len(ids) == len(set(ids))


def test_get_mechanic_known_and_unknown():
    assert svc.get_mechanic("spin_wheel") is not None
    assert svc.get_mechanic("does-not-exist") is None


def test_get_schema_returns_object_schema():
    schema = svc.get_schema("spin_wheel")
    assert schema is not None
    assert schema["type"] == "object"
    assert "slices" in schema["properties"]


def test_list_filters_by_category():
    chance = svc.list_mechanics(category="chance")
    assert len(chance) >= 2
    assert all(m["category"] == "chance" for m in chance)


def test_list_filters_by_region():
    th = svc.list_mechanics(region="th")
    assert len(th) >= 1
    assert all("th" in m["regions_supported"] for m in th)


def test_list_filters_by_status():
    beta = svc.list_mechanics(status="beta")
    assert all(m["status"] == "beta" for m in beta)


# ── Router ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_list(client):
    res = await client.get("/api/v1/wavef/mechanics")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] >= 10
    assert body["count"] == len(body["items"])


@pytest.mark.asyncio
async def test_router_get_404(client):
    res = await client.get("/api/v1/wavef/mechanics/does-not-exist")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_router_schema(client):
    res = await client.get("/api/v1/wavef/mechanics/spin_wheel/schema")
    assert res.status_code == 200
    schema = res.json()
    assert schema["type"] == "object"


@pytest.mark.asyncio
async def test_router_category_filter(client):
    res = await client.get("/api/v1/wavef/mechanics?category=social")
    assert res.status_code == 200
    body = res.json()
    assert body["count"] >= 1
    assert all(m["category"] == "social" for m in body["items"])
