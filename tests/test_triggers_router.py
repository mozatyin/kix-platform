"""Triggers router — attribute check, rate-limit, limited-drop, FCFS."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_attribute_first_visit_fires_once(client, clean_redis):
    body = {"user_id": "u1", "brand_id": "b1", "trigger_type": "first_visit"}
    r1 = await client.post("/api/v1/triggers/attribute/check", json=body)
    r2 = await client.post("/api/v1/triggers/attribute/check", json=body)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["triggered"] is True
    assert r2.json()["triggered"] is False


@pytest.mark.asyncio
async def test_attribute_invalid_type_422(client, clean_redis):
    res = await client.post(
        "/api/v1/triggers/attribute/check",
        json={"user_id": "u", "brand_id": "b", "trigger_type": "BOGUS"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_ratelimit_check_allowed_initially(client, clean_redis):
    res = await client.post(
        "/api/v1/triggers/ratelimit/check",
        json={
            "user_id": "u1",
            "brand_id": "b1",
            "action_name": "claim",
            "limit_per": "day",
            "limit": 3,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["allowed"] is True
    assert body["remaining"] == 3


@pytest.mark.asyncio
async def test_ratelimit_invalid_period_422(client, clean_redis):
    res = await client.post(
        "/api/v1/triggers/ratelimit/check",
        json={
            "user_id": "u1",
            "brand_id": "b1",
            "action_name": "claim",
            "limit_per": "year",
        },
    )
    assert res.status_code == 422
