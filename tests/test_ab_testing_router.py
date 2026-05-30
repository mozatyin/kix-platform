"""A/B testing router — create, start, pause, assign, record-event, conclude."""

from __future__ import annotations

import pytest


async def _create(client) -> str:
    res = await client.post(
        "/api/v1/ab-testing/create",
        json={
            "subject_type": "voucher",
            "subject_id": "v_001",
            "variants": [
                {"id": "A", "weight": 0.5},
                {"id": "B", "weight": 0.5},
            ],
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


@pytest.mark.asyncio
async def test_create_experiment_happy(client, clean_redis):
    eid = await _create(client)
    assert eid.startswith("exp_")


@pytest.mark.asyncio
async def test_create_weights_must_sum_to_one(client, clean_redis):
    res = await client.post(
        "/api/v1/ab-testing/create",
        json={
            "subject_type": "voucher",
            "subject_id": "v_002",
            "variants": [
                {"id": "A", "weight": 0.3},
                {"id": "B", "weight": 0.4},
            ],
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_create_invalid_subject_type_422(client, clean_redis):
    res = await client.post(
        "/api/v1/ab-testing/create",
        json={
            "subject_type": "BOGUS",
            "subject_id": "v",
            "variants": [
                {"id": "A", "weight": 0.5},
                {"id": "B", "weight": 0.5},
            ],
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_get_experiment_404(client, clean_redis):
    res = await client.get("/api/v1/ab-testing/exp_doesnotexist")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_start_then_pause(client, clean_redis):
    eid = await _create(client)
    r1 = await client.post(f"/api/v1/ab-testing/{eid}/start")
    assert r1.status_code == 200
    assert r1.json()["status"] == "running"
    r2 = await client.post(f"/api/v1/ab-testing/{eid}/pause")
    assert r2.status_code == 200
    assert r2.json()["status"] == "paused"


@pytest.mark.asyncio
async def test_assign_requires_running(client, clean_redis):
    eid = await _create(client)
    res = await client.post(
        f"/api/v1/ab-testing/{eid}/assign",
        json={"kid": "u_1"},
    )
    # draft → 409
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_assign_returns_variant_when_running(client, clean_redis):
    eid = await _create(client)
    await client.post(f"/api/v1/ab-testing/{eid}/start")
    res = await client.post(
        f"/api/v1/ab-testing/{eid}/assign",
        json={"kid": "u_2"},
    )
    assert res.status_code == 200
    assert res.json()["variant_id"] in {"A", "B"}
