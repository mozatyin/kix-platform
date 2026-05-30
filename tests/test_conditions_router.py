"""Conditions router — check, reserve, commit, refund, campaign config."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_check_no_campaign_returns_not_found(client, clean_redis):
    res = await client.post(
        "/api/v1/conditions/check",
        json={
            "brand_id": "b1",
            "user_id": "u1",
            "campaign_id": "cmp_nonexistent",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["eligible"] is False
    assert "campaign_not_found" in body["blocked_by"]


@pytest.mark.asyncio
async def test_check_missing_fields_422(client, clean_redis):
    res = await client.post("/api/v1/conditions/check", json={})
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_check_with_inline_conditions(client, clean_redis):
    res = await client.post(
        "/api/v1/conditions/check",
        json={
            "brand_id": "b1",
            "user_id": "u1",
            "campaign_id": "cmp_inline",
            "conditions": {},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert "eligible" in body


@pytest.mark.asyncio
async def test_get_campaign_404(client, clean_redis):
    res = await client.get("/api/v1/conditions/campaigns/no_campaign")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_user_eligibility(client, clean_redis):
    res = await client.get(
        "/api/v1/conditions/user/u1/eligibility",
        params={"campaign_id": "cmp_x", "brand_id": "b1"},
    )
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_commit_invalid_reservation(client, clean_redis):
    res = await client.post(
        "/api/v1/conditions/commit",
        json={"reservation_id": "res_nope"},
    )
    assert res.status_code in (404, 400, 409)


@pytest.mark.asyncio
async def test_refund_invalid_reservation(client, clean_redis):
    res = await client.post(
        "/api/v1/conditions/refund",
        json={"reservation_id": "res_nope"},
    )
    assert res.status_code in (404, 400, 409)
