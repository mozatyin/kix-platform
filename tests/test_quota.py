"""Brand subscription / quota tests."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_list_tiers(client, clean_redis):
    res = await client.get("/api/v1/brand-subscriptions/tiers")
    assert res.status_code == 200, res.text
    body = res.json()
    assert "tiers" in body
    assert "free" in body["tiers"]
    # Tier ordering must be stable for portal display.
    assert isinstance(body.get("order"), list)
    assert body["order"][0] == "free"


@pytest.mark.asyncio
async def test_quota_check_free_tier_allows_under_limit(client, clean_redis):
    res = await client.post(
        "/api/v1/brand-subscriptions/quota/check",
        json={"brand_id": "quota_brand_1", "resource": "games"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # New brand at FREE tier with zero games used → allowed.
    assert body.get("allowed") is True


@pytest.mark.asyncio
async def test_quota_check_unknown_resource_422(client, clean_redis):
    res = await client.post(
        "/api/v1/brand-subscriptions/quota/check",
        json={"brand_id": "quota_brand_x", "resource": "unobtanium"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_get_current_defaults_to_free(client, clean_redis):
    res = await client.post(
        "/api/v1/brand-subscriptions/new_brand_current/current",
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # Unconfigured brand should default to the FREE tier.
    assert body["subscription"]["tier"] == "free"
    assert body["config"]["monthly_cents"] == 0
